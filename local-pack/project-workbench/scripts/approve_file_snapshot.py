"""Local operator decision for one pending file snapshot request, when the ChatGPT card cannot approve it.

Without --approve/--deny it only shows the request. The operator's local access to the runtime is the authority.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.core import Runtime, WorkbenchError

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--request", required=True, help="fsr_... request ID shown in ChatGPT")
decision = parser.add_mutually_exclusive_group()
decision.add_argument("--approve", action="store_true", help="Take the snapshot of the exact path shown")
decision.add_argument("--deny", action="store_true")
args = parser.parse_args()
try:
    files = Runtime(args.config).files
    current = files.get(request_id=args.request)
    print(json.dumps({k: current.get(k) for k in ("request_id", "status", "path", "approval_expires_at")}, ensure_ascii=False, indent=2))
    if args.approve or args.deny:
        result = files.approve(args.request, decision="approve" if args.approve else "deny", via="local_operator")
        print(json.dumps({k: v for k, v in result.items() if k != "next_step"}, ensure_ascii=False, indent=2))
except WorkbenchError as e:
    raise SystemExit(str(e))
