"""Protocol fixture, never R/Claude/model/host acceptance evidence."""
import json
from pathlib import Path
import sys
packet = json.loads(sys.stdin.read().split('\n', 1)[1])
data = b''.join(Path(x['destination']).read_bytes() for x in packet['input_snapshot']['inputs'])
Path('result.bin').write_bytes(data)
report = {'summary': 'Binary input/output adapter fixture', 'findings': [],
          'tests': ['Fixture only: no R, real Claude, sandbox or ChatGPT file route'], 'changed_files': ['result.bin']}
print(json.dumps({'subtype': 'success', 'is_error': False, 'structured_output': report}))
