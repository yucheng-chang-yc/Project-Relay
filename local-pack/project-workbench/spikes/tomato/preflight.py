"""Actual environment preflight. Never turns a fixture or exit 0 into E2E PASS."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    paths={name:shutil.which(name) for name in ('Rscript','claude','docker','podman','bwrap')}
    containment={'status':'UNAVAILABLE','detail':'No container engine'}
    if paths['bwrap']:
        p=subprocess.run([paths['bwrap'],'--unshare-all','--die-with-parent','--ro-bind','/usr','/usr','--proc','/proc','--dev','/dev','/usr/bin/true'],capture_output=True,timeout=8)
        containment={'status':'AVAILABLE_PROBE_ONLY' if p.returncode==0 else 'FAILED', 'exit_code':p.returncode,
                     'stderr':p.stderr.decode('utf-8','replace')[:2000]}
    stages=['host_fileParams_exact_staging','real_Claude_restricted_runtime','R_intentional_error_stderr',
            'Claude_edit_R_rerun_exit0','CSV_RDS_PNG_ZIP_capture','artifact_to_ChatGPT_exact_bytes',
            'independent_host_SHA_and_model_review','followup_prior_artifact_without_human_shuttle']
    result={'schema':'wb.tomato_e2e.v1','platform':platform.platform(),'status':'BLOCKED','scope':'actual_current_environment',
            'paths':paths,'containment_probe':containment,'host_route':'NOT_DEPLOYED_OR_VERIFIED',
            'stages':[{'name':name,'status':'NOT_RUN','reason':'P0 foundation/environment blockers'} for name in stages],
            'fixture_input_sha256':hashlib.sha256(Path(__file__).with_name('input.csv').read_bytes()).hexdigest(),
            'product_acceptance':False,'scoped_trial_ready':False}
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':result['status'],'actual_R_execution':False,'product_acceptance':False}))
    return 2

if __name__=='__main__':
    raise SystemExit(main())
