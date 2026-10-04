"""Offline, bounded runtime upgrade. Run from the newly extracted package.

Stop the Workbench facade/tunnel before --apply. This script never stops processes,
launches the runtime/models, edits configuration, or changes Python registration.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

ACTIVE = ("queued", "running", "cancelling", "interrupted")
OWNED_DIRECTORIES = {"workbench", "ui", "scripts", "tests", "docs"}
OWNED_FILES = {"README.md", "plugin.json"}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized(path):
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def plain_path(root, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or relative.startswith("/") or any(p in ("..", ".git") for p in Path(relative).parts)):
        raise ValueError("Invalid package inventory path")
    path = root / relative
    if normalized(path) != os.path.normcase(str(path.absolute())):
        raise ValueError("Package-owned paths cannot pass through aliases")
    return path


def version(root):
    text = (root / "workbench" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*[\'\"]([0-9.]+)[\'\"]', text)
    if not match:
        raise ValueError("Target runtime version cannot be identified")
    return match[1]


def inventory(source):
    manifest_path = source / "docs" / "PACKAGE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != "0.1.2" or version(source) != "0.1.2":
        raise ValueError("Upgrade source must be runtime 0.1.2")
    entries, seen = [], set()
    for item in manifest["files"]:
        relative = item["path"]
        path = plain_path(source, relative)
        if relative in seen or not path.is_file() or path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]:
            raise ValueError("Source package inventory does not match its files")
        seen.add(relative)
        if relative in OWNED_FILES or Path(relative).parts[0] in OWNED_DIRECTORIES:
            entries.append(item)
    # The inventory excludes itself by definition; install its exact reviewed bytes too.
    entries.append({"path": "docs/PACKAGE_MANIFEST.json", "sha256": sha(manifest_path),
                    "bytes": manifest_path.stat().st_size})
    return entries


def settings(config):
    data = json.loads(config.read_text(encoding="utf-8-sig"))
    state = Path(data["data_dir"]).expanduser().resolve()
    roots = [Path(p["root"]).expanduser().resolve() for p in data.get("projects", [])]
    return state, roots


def make_plan(source, target, config):
    source, target, config = (Path(p).expanduser().resolve() for p in (source, target, config))
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise ValueError("Extract the update into a separate directory from the installation")
    old_version = version(target)
    if old_version not in ("0.1.1", "0.1.2"):
        raise ValueError("This updater supports runtime 0.1.1 or 0.1.2 only")
    entries = inventory(source)
    state, roots = settings(config)
    protected = [config, state, *roots]
    for entry in entries:
        destination = plain_path(target, entry["path"])
        if destination.exists() and not destination.is_file():
            raise ValueError("An owned file is occupied by a directory or special file")
        if any(destination == p or destination.is_relative_to(p) or p.is_relative_to(destination) for p in protected):
            raise ValueError("An update file overlaps configuration, runtime data or a registered project")
    return {"source": source, "target": target, "config": config, "state": state,
            "from_version": old_version, "entries": entries, "config_sha256": sha(config)}


@contextlib.contextmanager
def idle_database(plan):
    db = plan["state"] / "workbench.sqlite3"
    if not db.is_file():
        raise ValueError("Existing runtime database is required; no new state will be created")
    # Hold the existing writer lock during replacement. The facade must still be stopped.
    c = sqlite3.connect(db.as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        c.execute("BEGIN IMMEDIATE")
        count = c.execute(f"SELECT COUNT(*) FROM tasks WHERE status IN ({','.join('?' for _ in ACTIVE)})", ACTIVE).fetchone()[0]
        if count:
            raise ValueError("Active or uncertain tasks exist; finish/cancel or recover them locally before upgrading")
        yield db
    finally:
        c.rollback()
        c.close()


def atomic_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".workbench-upgrade-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        shutil.copymode(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def apply_plan(plan):
    with idle_database(plan) as db:
        if sha(plan["config"]) != plan["config_sha256"]:
            raise ValueError("Configuration changed after planning")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = plan["target"].parent / (plan["target"].name + "-backup-" + stamp)
        backup.mkdir()
        saved = []
        for entry in plan["entries"]:
            destination = plain_path(plan["target"], entry["path"])
            exists = destination.is_file()
            saved.append({"path": entry["path"], "existed": exists,
                          "previous_sha256": sha(destination) if exists else None,
                          "installed_sha256": entry["sha256"]})
            if exists:
                old = backup / "files" / entry["path"]
                old.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, old)
        with contextlib.closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as original:
            with contextlib.closing(sqlite3.connect(backup / "workbench.sqlite3")) as snapshot:
                original.backup(snapshot)
        record = {"from_version": plan["from_version"], "to_version": "0.1.2", "files": saved}
        (backup / "upgrade-record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        installed = []
        try:
            for entry in plan["entries"]:
                source = plain_path(plan["source"], entry["path"])
                if sha(source) != entry["sha256"]:
                    raise ValueError("Update source changed after planning")
                destination = plain_path(plan["target"], entry["path"])
                atomic_copy(source, destination)
                installed.append(entry["path"])
                if sha(destination) != entry["sha256"]:
                    raise ValueError("Installed file verification failed")
            if sha(plan["config"]) != plan["config_sha256"]:
                raise ValueError("Configuration changed during update")
        except BaseException:
            for relative in reversed(installed):
                destination = plain_path(plan["target"], relative)
                previous = backup / "files" / relative
                if previous.is_file():
                    atomic_copy(previous, destination)
                else:
                    destination.unlink(missing_ok=True)
            raise
        return {"updated": True, "runtime_version": "0.1.2", "backup": str(backup),
                "files_updated": len(installed), "configuration_preserved": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--target", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--apply", action="store_true", help="Replace package-owned files after stopping the facade/tunnel")
    args = parser.parse_args()
    try:
        plan = make_plan(args.source, args.target, args.config)
        if args.apply:
            result = apply_plan(plan)
        else:
            with idle_database(plan):
                result = {"updated": False, "from_version": plan["from_version"], "to_version": "0.1.2",
                          "files_to_update": len(plan["entries"]), "target": str(plan["target"]),
                          "preserved": ["config", "data_dir", "registered projects", "mcp.json", "skills", "tunnel/Bridge/Python settings"],
                          "next_step": "Stop the Workbench facade/tunnel, then repeat with --apply"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        # Do not print settings, commands, credentials or task specifications.
        print("Upgrade stopped: " + (str(e) if isinstance(e, ValueError) else type(e).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
