from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

from .core import Runtime, WorkbenchError, canonical, digest, hash_file, run_git, safe_path
from .adapters import AGENT_KINDS, agent_command, agent_result


class UncertainExecution(WorkbenchError):
    pass


def stop_process_tree(proc):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=10)


def execute(runtime, task_id):
    row = runtime.get_row(task_id)
    spec = json.loads(row["spec"])
    runtime.validate_task_binding(row)
    project, root = runtime.project(row["project"], writable=True)
    directory = runtime.task_dir(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    with runtime.connection(write=True) as c:
        current = c.execute("SELECT status,cancel_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
        if current["cancel_requested"]:
            cancelled = True
        elif current["status"] != "queued":
            raise WorkbenchError("Worker cannot claim a non-queued task")
        else:
            cancelled = False
            c.execute("UPDATE tasks SET status='running',started=?,heartbeat=?,worker_pid=? WHERE id=?",
                      (time.time(), time.time(), os.getpid(), task_id))
            runtime.event(c, task_id, "task.running")
    if cancelled:
        runtime.finish(task_id, "cancelled")
        return
    runtime.checkpoint_task(task_id, "preparation", "running")
    runtime.validate_task_binding(runtime.get_row(task_id))
    cwd = root
    input_data = None
    if row["kind"] in AGENT_KINDS:
        cwd = directory / "worktree"
        run_git(root, ["worktree", "add", "--detach", str(cwd), row["base_head"]])
        with runtime.connection(write=True) as c:
            c.execute("UPDATE tasks SET worktree=? WHERE id=?", (str(cwd), task_id))
        snapshot = runtime.loop.materialize(task_id, cwd)
        if spec.get("compute_runtime"):
            from .restricted import prepare_adapter
            prepare_adapter(runtime, task_id, cwd)
        command = agent_command(row["kind"], project, directory)
        packet = {"goal": spec["goal"], "constraints": spec.get("constraints", []),
                  "acceptance": spec["acceptance"], "workspace": str(cwd),
                  "baseline": row["base_head"], "expected_artifacts": spec.get("artifacts", []),
                  "input_snapshot": snapshot, "restricted_commands": bool(spec.get("compute_runtime")),
                  "command_runtime_contract": ("Use wb_runtime run(profile Rscript, project-relative .R script, stable idempotency_key); poll get_run until a terminal status for exit/stdout/stderr. Only Rscript is available; runs execute natively on the local account without OS containment, in this worktree. Do not write outside the worktree or modify staged inputs. Inspect failure before editing; edited reruns need a new key. Terminate uncertain active runs; never use a shell."
                      if (project.get("compute_runtime") or {}).get("backend") == "native_trusted" else
                      "Use wb_runtime run(profile Rscript/Python, project-relative script, stable idempotency_key); poll get_run for exit/stdout/stderr. Inspect failure before editing; edited reruns need a new key. Terminate uncertain active runs; never use native shell."),
                  "execution_rules": ["Work only in the supplied isolated worktree.",
                    "Do not commit, push, change branches, or alter repository configuration.",
                    "Do not modify files outside this task workspace.",
                    "Return actual findings and checks; report unresolved issues without claiming acceptance.",
                    "Return the required JSON result schema."]}
        (directory / "packet.json").write_text(canonical(packet), encoding="utf-8")
        input_data = ("Execute the following bounded task packet:\n" + canonical(packet)).encode("utf-8")
    else:
        command = project["profiles"][spec["profile"]]
    if not isinstance(command, list) or not all(isinstance(a, str) for a in command):
        raise WorkbenchError("Configured executor must be an argv array")
    log_path = directory / "executor.log"
    env = os.environ.copy()
    env.update({"PYTHONIOENCODING": "utf-8", "GIT_TERMINAL_PROMPT": "0"})
    flags = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    timeout = spec.get("timeout_seconds", 300)
    started = time.monotonic()
    reason = None
    output_path = directory / "claude-output.json"
    runtime.validate_task_binding(runtime.get_row(task_id))
    runtime.checkpoint_task(task_id, "preparation", "completed")
    runtime.checkpoint_task(task_id, "execution", "running")
    with log_path.open("wb") as log, output_path.open("wb") as structured:
        if row["kind"] == "claude":
            log.write(b"Claude Code started; structured stdout is retained separately until completion.\n")
            log.flush()
        process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE if input_data else subprocess.DEVNULL,
                                   stdout=structured if row["kind"] == "claude" else log,
                                   stderr=log, env=env, shell=False, **flags)
        try:
            with runtime.connection(write=True) as c:
                c.execute("UPDATE tasks SET child_pid=? WHERE id=?", (process.pid, task_id))
            if input_data:
                def feed_packet():
                    try:
                        process.stdin.write(input_data)
                    except (BrokenPipeError, OSError):
                        pass  # Process exit/result determine failure; supervision must keep running.
                    finally:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
                threading.Thread(target=feed_packet, daemon=True).start()
            while process.poll() is None:
                if runtime.heartbeat(task_id):
                    reason = "cancelled"
                elif time.monotonic() - started > timeout:
                    reason = "timed_out"
                elif log_path.stat().st_size + output_path.stat().st_size > 8 * 1024 * 1024:
                    reason = "failed"
                if reason:
                    stop_process_tree(process)
                    break
                time.sleep(0.2)
            code = process.wait()
        except BaseException as failure:
            try:
                stop_process_tree(process)
            except BaseException as stopping:
                raise UncertainExecution("Cannot confirm executor termination; project remains locked. Inspect processes locally.") from stopping
            raise
    if row["kind"] == "claude":
        # Keep stdout machine-parseable even if CLI writes diagnostics to stderr.
        with log_path.open("ab") as log, output_path.open("rb") as structured:
            log.write(b"\nClaude Code structured stdout (bounded):\n")
            log.write(structured.read(1024 * 1024))
    runtime.checkpoint_task(task_id, "execution", reason or ("completed" if code == 0 else "failed"),
        result={"summary": "Executor completed" if code == 0 and not reason else "Executor did not complete successfully",
                "findings": [], "tests": [], "changed_files": [], "artifacts": [], "base_head": row["base_head"],
                "kind": row["kind"], "executor_log_sha256": hash_file(log_path)}, exit_code=code)
    if reason:
        if spec.get("compute_runtime"):
            from .restricted import stop_owned
            try:
                stop_owned(runtime, task_id)
            except Exception as e:
                raise UncertainExecution("Cannot confirm command container termination; inspect owned leases locally") from e
        runtime.finish(task_id, reason, code, error="Executor output limit exceeded" if reason == "failed" else reason)
        return
    if code != 0:
        if spec.get("compute_runtime"):
            from .restricted import stop_owned
            try:
                stop_owned(runtime, task_id)
            except Exception as e:
                raise UncertainExecution("Failed executor left an uncertain command lease") from e
        runtime.finish(task_id, "failed", code, error=f"Executor exited with code {code}; inspect bounded log")
        return
    runtime.checkpoint_task(task_id, "command_shutdown", "running")
    if spec.get("compute_runtime"):
        from .restricted import stop_owned, capture_runs
        try:
            stop_owned(runtime, task_id)
            capture_runs(runtime, task_id)
        except Exception as e:
            raise UncertainExecution("Command lease did not terminate before artifact/diff capture") from e
    runtime.checkpoint_task(task_id, "command_shutdown", "completed")
    result = {"summary": "Executor exited with code 0; result verification pending" if row["kind"] in AGENT_KINDS else "Configured command finished successfully", "findings": [], "tests": [],
              "changed_files": [], "artifacts": [], "base_head": row["base_head"], "kind": row["kind"]}
    if row["kind"] in AGENT_KINDS:
        runtime.checkpoint_task(task_id, "result_parse", "running", result=result)
        report, metadata = agent_result(row["kind"], directory)
        result.update(report)
        result.update(metadata)
        result["reported_changed_files"] = report["changed_files"]
        result["changed_files"] = []  # Agent claims are not captured diff evidence.
        runtime.checkpoint_task(task_id, "result_parse", "completed", result=result)
    runtime.checkpoint_task(task_id, "artifact_capture", "running", result=result)
    artifact_bytes = 0
    for i, relative in enumerate(spec.get("artifacts", [])):
        source = safe_path(cwd, relative)
        if not source.is_file():
            raise WorkbenchError(f"Required artifact missing: {relative}")
        if source.stat().st_size > 500 * 1024 * 1024:
            raise WorkbenchError("Artifact exceeds the 500 MiB PoC transfer limit")
        artifact_bytes += source.stat().st_size
        if artifact_bytes > 1024 * 1024 * 1024:
            raise WorkbenchError("Artifacts exceed the 1 GiB task capture limit")
        stored = "artifacts/" + str(i).zfill(3) + "-" + source.name
        target = directory / stored
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(source, target)
        result["artifacts"].append({"id": i, "name": relative, "stored_name": stored,
                                    "size_bytes": target.stat().st_size, "sha256": hash_file(target),
                                    "artifact_identity": f"{task_id}:{i}", "task_id": task_id,
                                    "work_unit_id": runtime.loop.snapshot(task_id)["work_unit_id"],
                                    "mime_type": mimetypes.guess_type(relative)[0] or "application/octet-stream",
                                    "role": "declared_output", "durability": "copied_task_store", "untrusted": True})
        result["artifacts"][-1]["captured_at"] = time.time()
        runtime.checkpoint_task(task_id, "artifact_capture", "running", result=result)
    runtime.checkpoint_task(task_id, "artifact_capture", "completed", result=result)
    if row["kind"] in AGENT_KINDS:
        runtime.checkpoint_task(task_id, "change_capture", "running", result=result)
        if run_git(cwd, ["rev-parse", "HEAD"]) != row["base_head"]:
            raise WorkbenchError("Executor changed worktree HEAD; automatic integration is unavailable")
        diff, actual_names = runtime.snapshot_diff(cwd, directory, input_paths=[e["destination"] for e in spec.get("inputs", [])])
        for name in actual_names:
            safe_path(cwd, name)
        (directory / "changes.patch").write_bytes(diff)
        result.update({"changed_files": actual_names, "reported_changed_files": report["changed_files"],
                       "diff_sha256": digest(diff), "diff_bytes": len(diff)})
        runtime.checkpoint_task(task_id, "change_capture", "completed", result=result)
    if row["kind"] in AGENT_KINDS:
        p = directory / "changes.patch"
        result["artifacts"].append({"id": len(result["artifacts"]), "name": "changes.patch",
             "stored_name": "changes.patch", "size_bytes": p.stat().st_size, "sha256": hash_file(p),
             "artifact_identity": f"{task_id}:{len(result['artifacts'])}", "task_id": task_id,
             "work_unit_id": runtime.loop.snapshot(task_id)["work_unit_id"], "mime_type": "text/x-diff",
             "role": "diff", "durability": "copied_task_store", "untrusted": True})
        result["artifacts"][-1]["captured_at"] = time.time()
    runtime.checkpoint_task(task_id, "evidence_seal", "running", result=result)
    result["input_snapshot_sha256"] = runtime.loop.snapshot(task_id)["snapshot_sha256"]
    result["artifact_manifest_sha256"] = digest(canonical(result["artifacts"]).encode())
    result["executor_log_sha256"] = hash_file(log_path)
    if spec.get("compute_runtime"):
        from .restricted import stop_owned, capture_runs
        try:
            stop_owned(runtime, task_id)
            result["command_runs"] = capture_runs(runtime, task_id)
        except Exception as e:
            raise UncertainExecution("Command lease/evidence remains uncertain; retain task lock") from e
    runtime.checkpoint_task(task_id, "evidence_seal", "completed", result=result)
    runtime.finish(task_id, "completed", code, result=result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    runtime = Runtime(args.config)
    try:
        execute(runtime, args.task)
    except BaseException as e:
        try:
            row = runtime.get_row(args.task)
            if json.loads(row["spec"]).get("compute_runtime"):
                from .restricted import stop_owned
                stop_owned(runtime, args.task)
        except Exception:
            e = UncertainExecution("Cannot confirm command-container shutdown; local recovery required")
        runtime.finish(args.task, "interrupted" if isinstance(e, UncertainExecution) else "failed",
                       error=f"{type(e).__name__}: {str(e)[:2000]}")
        print(f"Task worker failed: {type(e).__name__}: {str(e)[:2000]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
