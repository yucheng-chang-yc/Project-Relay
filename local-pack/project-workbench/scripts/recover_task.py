"""Local operator recovery after a worker disappeared; never relaunch an uncertain run."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.core import Runtime, WorkbenchError

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--task", required=True)
parser.add_argument("--note", required=True, help="Record actual process/workspace inspection, including descendants")
args = parser.parse_args()
try:
    task = Runtime(args.config).resolve_interrupted_locally(args.task, args.note)
    print("Resolved as failed without replay: " + task["id"])
except WorkbenchError as e:
    raise SystemExit(str(e))
