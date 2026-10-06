"""Generate D-005 overlay payload from canonical reviewed source, never user state."""
import hashlib
import json
from pathlib import Path

here=Path(__file__).resolve().parent
repo=here.parents[2]
source=repo/'local-pack/project-workbench'
candidate=here/'candidate';candidate.mkdir(exist_ok=True)
for name,rel in [('shared.py','workbench/shared.py'),('test_shared_publication.py','tests/test_shared_publication.py')]:
    (candidate/name).write_bytes((source/rel).read_bytes())
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
manifest={'schema':'project-relay.hotfix.v1','hotfix':'D005-v1','runtime_version':'0.2.0-preview.3',
          'before_sha256':'71d1eb22f2469d7802a3fc29fff15651d14e5076858d43e69a58bc420d43cc60',
          'after_sha256':sha(candidate/'shared.py'),'test_sha256':sha(candidate/'test_shared_publication.py'),
          'baseline_local_zip_sha256':'d5b445d6aaf7f8c2b42565bf73f0a70690b376132bee9e1b347bea9df68f5f7b'}
(here/'PATCH_MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n')
print(json.dumps(manifest,indent=2))
