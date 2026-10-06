"""Local-only recovery of an uncertain shared-file publication. Never replays a write."""
import argparse
import json
from pathlib import Path
import sys
from support import installation

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--write',required=True)
    parser.add_argument('--observed-sha256',required=True,help='Locally observed current file SHA; empty string means absent')
    parser.add_argument('--note',required=True)
    args=parser.parse_args()
    try:
        _,_,app,config,_=installation(args.root)
        sys.path.insert(0,str(app))
        from workbench.core import Runtime
        result=Runtime(config).shared.resolve_write_locally(args.write,args.observed_sha256,args.note)
        print(json.dumps(result,indent=2));return 0
    except Exception as e:
        print('Recovery failed: '+type(e).__name__,file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
