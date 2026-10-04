"""Task-bound command adapter: container backend or operator-acknowledged native trusted Rscript.

The container backend never falls back to a native interpreter/shell. The native_trusted backend
is an explicit operator choice: it runs only the configured Rscript executable, without a shell,
in the task worktree, as the local account. It has NO OS containment; it owns and stops the
process tree and records evidence, nothing more.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from . import __version__
from .core import Runtime, WorkbenchError, canonical, digest, hash_file, process_alive, safe_path
from .proctree import ProcessTree

MAX_OUTPUT = 8 * 1024 * 1024
MAX_RUNS = 30
MAX_SECONDS = 120
NATIVE_BACKEND = "native_trusted"
# Exact operator acknowledgement (user consent recorded 2026-10-03): the R program runs with the
# local account's rights and no OS containment.
NATIVE_ACK = "local-account-r-execution-without-os-containment"
# Environment passed to native R. Not a security boundary (R can read the account's files);
# it keeps tokens/keys/agent variables of the facade, CLI and tunnel out of the R process.
NATIVE_ENV = ("SystemRoot", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
              "APPDATA", "LOCALAPPDATA", "PATHEXT", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
              "OS", "R_LIBS_USER", "R_LIBS", "RTOOLS45_HOME", "LANG", "TZ", "HOME")


def validate_native_policy(policy):
    if set(policy) - {"backend", "rscript", "trust_acknowledgement"}:
        raise WorkbenchError("Native trusted policy accepts only backend, rscript and trust_acknowledgement")
    if policy.get("trust_acknowledgement") != NATIVE_ACK:
        raise WorkbenchError("Native trusted R requires the exact operator trust_acknowledgement")
    rscript = policy.get("rscript")
    if not isinstance(rscript, str) or not Path(rscript).is_absolute() or not Path(rscript).is_file() or \
            Path(rscript).name.lower() not in ("rscript.exe", "rscript"):
        raise WorkbenchError("Native trusted R requires an existing absolute Rscript executable")
    return policy


def native_argv(policy, workspace, profile, script, seconds, frozen=None):
    validate_native_policy(policy)
    if profile != "Rscript":
        raise WorkbenchError("Native trusted mode authorizes only the configured Rscript entry")
    path = safe_path(workspace, script)
    if not path.is_file() or path.suffix.lower() != ".r":
        raise WorkbenchError("Command requires an existing project-relative .R script")
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise WorkbenchError("Command timeout must be 1–120 seconds")
    return [policy["rscript"], "--vanilla", str(frozen or path)]


def native_env(policy):
    env = {k: os.environ[k] for k in NATIVE_ENV if k in os.environ}
    system = os.environ.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([str(Path(policy["rscript"]).parent), str(Path(system) / "System32"), system]) \
        if os.name == "nt" else os.pathsep.join([str(Path(policy["rscript"]).parent), "/usr/bin", "/bin"])
    return env


def validate_policy(policy):
    if isinstance(policy, dict) and policy.get("backend") == NATIVE_BACKEND:
        return validate_native_policy(policy)
    if not isinstance(policy, dict) or policy.get("backend") not in ("docker", "podman"):
        raise WorkbenchError("No verified container command route configured; native execution is refused")
    executable = policy.get("executable", "")
    if not Path(executable).is_absolute() or not Path(executable).is_file():
        raise WorkbenchError("Container engine must be an operator-configured absolute executable")
    if not re.fullmatch(r"(?:[a-z0-9][a-z0-9./:_-]*@)?sha256:[0-9a-f]{64}", policy.get("image", "")):
        raise WorkbenchError("A preloaded digest-pinned computational image is required")
    if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", policy.get("user", "")):
        raise WorkbenchError("Container user/group must be explicit non-root IDs")
    if policy.get("enforcement_verified") is not True:
        raise WorkbenchError("Container containment canaries have not passed; fail closed")
    return policy


def engine_env(directory):
    # Host credentials/proxy/env do not propagate. A separate empty Docker config
    # avoids reading the operator's registry credentials and automatic context.
    directory.mkdir(exist_ok=True)
    env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
    env.update({"DOCKER_CONFIG": str(directory), "HOME": str(directory), "USERPROFILE": str(directory)})
    return env


def container_argv(policy, workspace, name, profile, script, seconds, script_snapshot=None, input_snapshot=None):
    validate_policy(policy)
    if profile not in ("Rscript", "Python"):
        raise WorkbenchError("Only Rscript/Python script profiles are authorized")
    path = safe_path(workspace, script)
    if not path.is_file() or path.suffix.lower() not in ((".r",) if profile == "Rscript" else (".py",)):
        raise WorkbenchError("Command requires an existing project-relative R/Python script")
    if any(c in str(workspace) for c in (",", "\n", "\r")):
        raise WorkbenchError("Workspace cannot be represented safely as a container bind mount")
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS:
        raise WorkbenchError("Command timeout must be 1–120 seconds")
    mounts = []
    if script_snapshot:
        mounts += ["--mount", f"type=bind,src={script_snapshot},dst=/wb-script,readonly"]
    for item in input_snapshot or []:
        if any(c in str(item["source"]) for c in (",", "\n", "\r")):
            raise WorkbenchError("Input snapshot cannot be represented safely as a mount")
        mounts += ["--mount", f"type=bind,src={item['source']},dst=/workspace/{item['destination']},readonly"]
    return [policy["executable"], "run", "--name", name, "--rm", "--pull=never", "--network=none",
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--user", policy["user"], "--pids-limit=128", "--memory=512m", "--cpus=1",
            "--ulimit", "nofile=256:256", "--stop-timeout=1", "--workdir=/workspace",
            "--mount", f"type=bind,src={workspace},dst=/workspace",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--env", "HOME=/tmp", "--env", "LC_ALL=C.UTF-8",
            *mounts, "--entrypoint", "/usr/bin/timeout", policy["image"], "--signal=KILL", str(seconds),
            "Rscript" if profile == "Rscript" else "python3", "--vanilla" if profile == "Rscript" else "-I",
            "/wb-script/script" + path.suffix.lower() if script_snapshot else "/workspace/" + script]


def prepare_adapter(runtime, task_id, workspace):
    row = runtime.get_row(task_id)
    project, _ = runtime.project(row["project"], writable=True)
    validate_policy(project.get("compute_runtime"))
    from .adapters import configured_command
    version = subprocess.run([*configured_command("claude", project), "--version"], capture_output=True,
                             timeout=10, shell=False)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version.stdout.decode("utf-8", "replace")[:4096])
    if version.returncode or not match or tuple(map(int, match.groups())) < (2, 1, 248):
        raise WorkbenchError("Computational Claude requires verified CLI >=2.1.248 with --restricted")
    directory = runtime.task_dir(task_id)
    descriptor = {"mcpServers": {"wb_runtime": {"command": sys.executable,
        "args": ["-I", str(Path(__file__).resolve().parents[1] / "scripts" / "restricted_runtime.py"),
                 "--config", str(runtime.config_path), "--task", task_id]}}}
    (directory / "restricted-mcp.json").write_text(canonical(descriptor), encoding="utf-8")


def stop_native(runs):
    """Native leases without a confirmed receipt: stop the recorded root PID tree and verify.

    Normally the run's Job Object (KILL_ON_JOB_CLOSE) has already removed every descendant when
    the adapter exited; this is the worker/operator safety net and never targets unrecorded PIDs.
    """
    for directory in runs.iterdir():
        if not re.fullmatch(r"run_[0-9a-f]{32}", directory.name):
            raise WorkbenchError("Unknown command lease; local inspection required")
        receipt = directory / "receipt.json"
        if receipt.exists() and json.loads(receipt.read_text(encoding="utf-8")).get("termination_confirmed") is True:
            continue
        native = directory / "native.json"
        if not native.exists():
            continue  # Lease written but the process was never started.
        pid = json.loads(native.read_text(encoding="utf-8")).get("pid")
        was_alive = bool(pid and process_alive(pid))
        if was_alive:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                try:
                    os.killpg(pid, 9)
                except ProcessLookupError:
                    pass
            end = time.monotonic() + 5
            while process_alive(pid) and time.monotonic() < end:
                time.sleep(.05)
            if process_alive(pid):
                raise WorkbenchError("Cannot confirm native command termination; retain task lock")
        # The adapter died before writing a receipt (e.g. task cancel stopped the CLI tree).
        # Record the worker-side confirmation separately; receipt.json stays absent because the
        # run did not finish on its own.
        record = directory / "termination.json"
        if not record.exists():
            record.write_text(canonical({"schema": "wb.command_termination.v1", "run_id": directory.name,
                                         "confirmed_by": "worker_stop_native", "pid": pid,
                                         "pid_alive_when_checked": was_alive, "pid_alive_after": False,
                                         "checked_at": time.time()}), encoding="utf-8")


def stop_owned(runtime, task_id):
    """Local worker/operator cleanup of exact persisted names, never arbitrary IDs."""
    project, _ = runtime.project(runtime.get_row(task_id)["project"], writable=True)
    policy = project.get("compute_runtime")
    runs = runtime.task_dir(task_id) / "command-runs"
    if not runs.exists():
        return
    validate_policy(policy)
    if policy.get("backend") == NATIVE_BACKEND:
        stop_native(runs)
        return
    env = engine_env(runtime.task_dir(task_id) / "engine-config")
    for directory in runs.iterdir():
        if not re.fullmatch(r"run_[0-9a-f]{32}", directory.name):
            raise WorkbenchError("Unknown command lease; local inspection required")
        if (directory / "receipt.json").exists():
            receipt = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))
            if receipt.get("termination_confirmed") is True:
                continue
        name = "wb-" + task_id[5:] + "-" + directory.name[4:]
        p = subprocess.run([policy["executable"], "rm", "--force", name], capture_output=True,
                           env=env, timeout=10, shell=False)
        if p.returncode:
            # A failed rm alone cannot prove absence; engine must confirm it.
            check = subprocess.run([policy["executable"], "ps", "-a", "--format", "{{.Names}}"],
                                   capture_output=True, env=env, timeout=10, shell=False)
            if check.returncode or name in check.stdout.decode("utf-8", "replace").splitlines():
                raise WorkbenchError("Cannot confirm owned container termination; retain task lock")


class CommandRuntime:
    def __init__(self, runtime, task_id):
        self.runtime, self.task_id = runtime, task_id
        self.lock = threading.Lock()
        self.threads = {}

    def context(self):
        row = self.runtime.get_row(self.task_id)
        self.runtime.validate_task_binding(row)
        if row["status"] != "running" or not json.loads(row["spec"]).get("compute_runtime"):
            raise WorkbenchError("Restricted command capability is inactive")
        project, _ = self.runtime.project(row["project"], writable=True)
        policy = validate_policy(project.get("compute_runtime"))
        workspace = Path(row["worktree"]).resolve()
        expected = (self.runtime.task_dir(self.task_id) / "worktree").resolve()
        if workspace != expected or not workspace.is_dir():
            raise WorkbenchError("Command workspace binding differs")
        return policy, workspace

    def run(self, profile, script, idempotency_key, timeout_seconds=60):
        with self.lock:
            policy, workspace = self.context()
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
                raise WorkbenchError("Command runs require a stable idempotency key")
            script_bytes = safe_path(workspace, script).read_bytes()
            if len(script_bytes) > 1024 * 1024:
                raise WorkbenchError("Command script exceeds 1 MiB")
            snapshot = self.runtime.loop.snapshot(self.task_id)
            semantic = {"profile": profile, "script": script, "script_sha256": digest(script_bytes),
                        "input_snapshot_sha256": snapshot["snapshot_sha256"], "timeout_seconds": timeout_seconds,
                        "policy_sha256": digest(canonical(policy).encode())}
            fingerprint = digest(canonical(semantic).encode())
            base = self.runtime.task_dir(self.task_id) / "command-runs"
            base.mkdir(exist_ok=True)
            previous = list(base.iterdir())
            for directory in previous:
                lease = directory / "lease.json"
                if lease.exists():
                    old = json.loads(lease.read_text(encoding="utf-8"))
                    if old.get("idempotency_key") == idempotency_key:
                        if old.get("fingerprint") != fingerprint:
                            raise WorkbenchError("Command key reused for a different script/input/profile")
                        return self.get_run(directory.name)
            if len(previous) >= MAX_RUNS:
                raise WorkbenchError("Task command retry limit reached")
            if any(not (p / "receipt.json").exists() for p in previous):
                raise WorkbenchError("One command lease is active/uncertain; poll or terminate it")
            run_id = "run_" + uuid.uuid4().hex
            name = "wb-" + self.task_id[5:] + "-" + run_id[4:]
            native = policy.get("backend") == NATIVE_BACKEND
            if native:
                native_argv(policy, workspace, profile, script, timeout_seconds)
            else:
                argv = container_argv(policy, workspace, name, profile, script, timeout_seconds)
            directory = base / run_id
            directory.mkdir()
            # Execute the frozen primary script, not a host file editable during the run.
            frozen = directory / "script"
            frozen.mkdir()
            (frozen / ("script" + Path(script).suffix.lower())).write_bytes(script_bytes)
            if native:
                # Inputs stay at their workspace destinations; no OS read-only mount exists, so
                # verify them before the run and record any change after it.
                for entry in snapshot["inputs"]:
                    if hash_file(safe_path(workspace, entry["destination"])) != entry["sha256"]:
                        raise WorkbenchError("Workspace input changed after explicit snapshot")
                argv = native_argv(policy, workspace, profile, script, timeout_seconds,
                                   frozen / ("script" + Path(script).suffix.lower()))
                descriptor = {"run_id": run_id, "task_id": self.task_id, "profile": profile, "script": script,
                              "script_sha256": digest(script_bytes), "input_snapshot_sha256": snapshot["snapshot_sha256"],
                              "timeout_seconds": timeout_seconds, "backend": NATIVE_BACKEND, "rscript": policy["rscript"],
                              "os_containment": False, "created_at": time.time(),
                              "idempotency_key": idempotency_key, "fingerprint": fingerprint}
                (directory / "lease.json").write_text(canonical(descriptor), encoding="utf-8")
                thread = threading.Thread(target=self._execute_native,
                                          args=(directory, argv, policy, workspace, timeout_seconds, snapshot["inputs"]),
                                          daemon=True)
                self.threads[run_id] = thread
                thread.start()
                return {"run_id": run_id, "status": "starting", "task_id": self.task_id,
                        "backend": NATIVE_BACKEND, "os_containment": False}
            input_directory = directory / "inputs"
            input_mounts = []
            if snapshot["inputs"]:
                input_directory.mkdir()
                for entry in snapshot["inputs"]:
                    if hash_file(safe_path(workspace, entry["destination"])) != entry["sha256"]:
                        raise WorkbenchError("Workspace input changed after explicit snapshot")
                    item, source = self.runtime.loop.input(self.runtime.get_row(self.task_id)["project"], entry["input_id"])
                    target = safe_path(input_directory, entry["destination"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(source.read_bytes())
                    input_mounts.append({"source": target, "destination": entry["destination"]})
            argv = container_argv(policy, workspace, name, profile, script, timeout_seconds, frozen,
                                  input_mounts)
            descriptor = {"run_id": run_id, "task_id": self.task_id, "profile": profile, "script": script,
                          "script_sha256": digest(script_bytes), "input_snapshot_sha256": snapshot["snapshot_sha256"], "timeout_seconds": timeout_seconds,
                          "container_name": name, "image": policy["image"], "created_at": time.time(),
                          "idempotency_key": idempotency_key, "fingerprint": fingerprint}
            (directory / "lease.json").write_text(canonical(descriptor), encoding="utf-8")
            thread = threading.Thread(target=self._execute, args=(directory, argv, policy, timeout_seconds), daemon=True)
            self.threads[run_id] = thread
            thread.start()
            return {"run_id": run_id, "status": "starting", "task_id": self.task_id}

    def _execute(self, directory, argv, policy, seconds):
        env = engine_env(self.runtime.task_dir(self.task_id) / "engine-config")
        process, reason, code, confirmed = None, None, None, False
        stdout, stderr = directory / "stdout.bin", directory / "stderr.bin"
        try:
            with stdout.open("xb") as out, stderr.open("xb") as err:
                process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                           env=env, shell=False)
                started = time.monotonic()
                while process.poll() is None:
                    if (directory / "cancel").exists():
                        reason = "cancelled"
                    elif time.monotonic() - started > seconds + 10:
                        reason = "timed_out"
                    elif stdout.stat().st_size + stderr.stat().st_size > MAX_OUTPUT:
                        reason = "output_limit"
                    if reason:
                        stop_owned(self.runtime, self.task_id)
                        process.kill()
                        break
                    time.sleep(.05)
                code = process.wait(timeout=10)
            # Even after normal engine-client exit, verify/clean the lease by exact name.
            stop_owned(self.runtime, self.task_id)
            confirmed = True
        except Exception as error:
            reason = "runner_error:" + type(error).__name__
            try:
                stop_owned(self.runtime, self.task_id)
                if process and process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                confirmed = True
            except Exception:
                reason = "termination_uncertain"
        receipt = {"schema": "wb.command_run.v1", "run_id": directory.name, "exit_code": code,
                   "status": reason or ("completed" if code == 0 else "failed"), "termination_confirmed": confirmed,
                   "finished_at": time.time(), "lease_sha256": hash_file(directory / "lease.json"),
                   "streams": {p.name: {"sha256": hash_file(p), "size_bytes": p.stat().st_size} for p in (stdout, stderr) if p.exists()}}
        (directory / "receipt.json").write_text(canonical(receipt), encoding="utf-8")

    def _execute_native(self, directory, argv, policy, workspace, seconds, inputs):
        process, reason, code, confirmed, leftover, tree = None, None, None, False, None, None
        stdout, stderr = directory / "stdout.bin", directory / "stderr.bin"
        try:
            with stdout.open("xb") as out, stderr.open("xb") as err:
                tree = ProcessTree()
                process = tree.start(argv, cwd=str(workspace), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                     env=native_env(policy), shell=False)
                (directory / "native.json").write_text(canonical({"pid": process.pid, "started_at": time.time()}),
                                                       encoding="utf-8")
                started = time.monotonic()
                while process.poll() is None:
                    if (directory / "cancel").exists():
                        reason = "cancelled"
                    elif time.monotonic() - started > seconds:
                        reason = "timed_out"
                    elif stdout.stat().st_size + stderr.stat().st_size > MAX_OUTPUT:
                        reason = "output_limit"
                    if reason:
                        break
                    time.sleep(.05)
                # Descendants still alive after the root exits are stopped too; nothing outlives a run.
                leftover = tree.active_processes()
                confirmed = tree.stop()
                code = process.wait(timeout=10)
        except Exception as error:
            reason = "runner_error:" + type(error).__name__
            try:
                confirmed = tree.stop() if tree else process is None
            except Exception:
                confirmed = False
            if not confirmed:
                reason = "termination_uncertain"
        finally:
            if tree:
                tree.close()
        changed = []
        for entry in inputs:
            path = safe_path(workspace, entry["destination"])
            if not path.is_file() or hash_file(path) != entry["sha256"]:
                changed.append(entry["destination"])
        status = reason or ("completed" if code == 0 else "failed")
        if changed and status == "completed":
            status = "input_modified"
        receipt = {"schema": "wb.command_run.v1", "run_id": directory.name, "exit_code": code, "status": status,
                   "termination_confirmed": confirmed, "finished_at": time.time(),
                   "lease_sha256": hash_file(directory / "lease.json"), "backend": NATIVE_BACKEND,
                   "os_containment": False,
                   "process_tree": "windows_job_object" if os.name == "nt" else "posix_process_group",
                   "processes_alive_at_stop": leftover, "inputs_unchanged": not changed, "modified_inputs": changed[:20],
                   "streams": {p.name: {"sha256": hash_file(p), "size_bytes": p.stat().st_size} for p in (stdout, stderr) if p.exists()}}
        (directory / "receipt.json").write_text(canonical(receipt), encoding="utf-8")

    def get_run(self, run_id):
        if not re.fullmatch(r"run_[0-9a-f]{32}", run_id):
            raise WorkbenchError("Invalid command run ID")
        directory = self.runtime.task_dir(self.task_id) / "command-runs" / run_id
        if not directory.is_dir():
            raise WorkbenchError("Unknown task command run")
        receipt = directory / "receipt.json"
        data = json.loads(receipt.read_text(encoding="utf-8")) if receipt.exists() else {"run_id": run_id, "status": "running_or_uncertain"}
        for name in ("stdout", "stderr"):
            path = directory / (name + ".bin")
            if path.exists():
                with path.open("rb") as stream:
                    data[name + "_preview"] = stream.read(65536).decode("utf-8", "replace")
            else:
                data[name + "_preview"] = ""
        return data

    def terminate_run(self, run_id):
        self.get_run(run_id)
        self.context()
        directory = self.runtime.task_dir(self.task_id) / "command-runs" / run_id
        (directory / "cancel").touch()
        return {"run_id": run_id, "cancellation_requested": True}


def capture_runs(runtime, task_id):
    base = runtime.task_dir(task_id) / "command-runs"
    result = []
    for directory in sorted(base.iterdir()) if base.exists() else []:
        receipt = directory / "receipt.json"
        if not receipt.is_file():
            raise WorkbenchError("Command lease did not reach durable completion")
        body = json.loads(receipt.read_text(encoding="utf-8"))
        if not body.get("termination_confirmed"):
            raise WorkbenchError("Command termination uncertain; local recovery required")
        if hash_file(directory / "lease.json") != body["lease_sha256"]:
            raise WorkbenchError("Command lease evidence changed")
        for filename, meta in body["streams"].items():
            if filename not in ("stdout.bin", "stderr.bin") or hash_file(directory / filename) != meta["sha256"]:
                raise WorkbenchError("Command stream evidence changed")
        result.append({"run_id": directory.name, "receipt_sha256": hash_file(receipt), "receipt": body})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    # MCP stdio is UTF-8. The adapter runs under `python -I`, which ignores PYTHONIOENCODING, so a
    # Windows legacy code page would fail on non-ASCII R output (e.g. localized error messages).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict", newline="\n")
    runtime = CommandRuntime(Runtime(args.config), args.task)
    schemas = {
        "run": ({"profile": {"type": "string", "enum": ["Rscript", "Python"]}, "script": {"type": "string", "maxLength": 1000},
                 "idempotency_key": {"type": "string", "maxLength": 128},
                 "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_SECONDS}}, ["profile", "script", "idempotency_key"]),
        "get_run": ({"run_id": {"type": "string", "maxLength": 64}}, ["run_id"]),
        "terminate_run": ({"run_id": {"type": "string", "maxLength": 64}}, ["run_id"])}
    from .mcp import validate
    for line in iter(lambda: sys.stdin.buffer.readline(1024 * 1024 + 1), b""):
        message = None
        if len(line) > 1024 * 1024:
            return 1
        try:
            message = json.loads(line)
            if "id" not in message:
                continue
            method = message.get("method")
            if method == "initialize":
                data = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "wb_runtime", "version": __version__}}
            elif method == "tools/list":
                kind = ("native trusted Rscript (local account, no OS containment)" if
                        runtime.runtime.project(runtime.runtime.get_row(runtime.task_id)["project"])[0]
                        .get("compute_runtime", {}).get("backend") == NATIVE_BACKEND else "container")
                data = {"tools": [{"name": name, "description": f"Task-bound {kind} command " + name,
                         "inputSchema": {"type": "object", "properties": props, "required": required, "additionalProperties": False}}
                         for name, (props, required) in schemas.items()]}
            elif method == "tools/call":
                params = message["params"]
                name = params["name"]
                if name not in schemas:
                    raise WorkbenchError("Unknown restricted runtime operation")
                props, required = schemas[name]
                arguments = params.get("arguments", {})
                validate(arguments, {"type": "object", "properties": props, "required": required, "additionalProperties": False})
                value = getattr(runtime, name)(**arguments)
                data = {"content": [{"type": "text", "text": canonical(value)}]}
            elif method == "ping":
                data = {}
            else:
                raise WorkbenchError("Unsupported restricted runtime method")
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": data}
        except Exception as e:
            reply = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                     "error": {"code": -32602, "message": str(e)[:1000]}}
        print(canonical(reply), flush=True)
    # Client closed stdin: let active runs finish (bounded by their own timeout) so every lease
    # gets a durable receipt; the process tree is stopped by the run itself.
    for thread in list(runtime.threads.values()):
        thread.join(timeout=MAX_SECONDS + 20)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
