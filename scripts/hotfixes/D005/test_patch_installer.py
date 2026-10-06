"""Offline overlay tests against the original unpacked preview.3 Local package."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PACKAGE = None
HERE = Path(__file__).resolve().parent

class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='relay-d005-overlay-')
        self.base=Path(self.temp.name)
        self.root=self.base/'install unicode 安裝'
        installed=self.run_command(PACKAGE/'install.py','--root',self.root)
        self.assertEqual(installed.returncode,0,installed.stderr)
        self.backup=self.base/'backup'

    def tearDown(self):
        self.temp.cleanup()

    def run_command(self, script, *args):
        return subprocess.run([sys.executable,str(script),*map(str,args)],capture_output=True,text=True,encoding='utf-8',timeout=60)

    def patch(self,*args):
        return self.run_command(HERE/'apply_d005.py','--root',self.root,*args)

    def test_plan_apply_idempotency_check_and_code_only_rollback(self):
        before=(self.root/'INSTALLATION.json').read_bytes()
        cfg=(self.root/'config/config.json').read_bytes()
        plan=self.patch();self.assertEqual(plan.returncode,0,plan.stderr)
        self.assertEqual(json.loads(plan.stdout)['status'],'plan')
        self.assertEqual((self.root/'INSTALLATION.json').read_bytes(),before)
        applied=self.patch('--apply','--backup-dir',self.backup);self.assertEqual(applied.returncode,0,applied.stderr)
        self.assertEqual(json.loads(applied.stdout)['status'],'applied')
        repeated=self.patch('--apply','--backup-dir',self.backup);self.assertEqual(repeated.returncode,0,repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)['status'],'unchanged')
        checked=self.run_command(self.root/'maintenance/check_install.py','--root',self.root,'--self-test')
        self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)
        self.assertEqual(json.loads(checked.stdout)['status'],'PASS')
        tests=self.run_command('-c', 'import sys,unittest;sys.path.insert(0,sys.argv[1]);sys.path.insert(0,sys.argv[1]+"/tests");r=unittest.TextTestRunner().run(unittest.defaultTestLoader.discover(sys.argv[1]+"/tests",pattern="test_shared_publication.py"));sys.exit(not r.wasSuccessful())',self.root/'app/0.2.0-preview.3/project-workbench')
        self.assertEqual(tests.returncode,0,tests.stderr)
        self.assertIn('Ran 17 tests',tests.stderr)
        # A rollback keeps newly created diagnostic history/state, including DB.
        db=self.root/'state/workbench.sqlite3';db_before=hashlib.sha256(db.read_bytes()).hexdigest()
        restored=self.patch('--rollback','--backup-dir',self.backup);self.assertEqual(restored.returncode,0,restored.stderr)
        self.assertEqual(json.loads(restored.stdout)['status'],'rolled_back')
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(),db_before)
        self.assertEqual((self.root/'INSTALLATION.json').read_bytes(),before)
        self.assertEqual((self.root/'config/config.json').read_bytes(),cfg)

    def test_service_lock_refuses_apply_without_changes(self):
        before=(self.root/'INSTALLATION.json').read_bytes()
        (self.root/'state/facade.lock').write_text('fixture active owner')
        rejected=self.patch('--apply','--backup-dir',self.backup)
        self.assertNotEqual(rejected.returncode,0)
        self.assertFalse(self.backup.exists())
        self.assertEqual((self.root/'INSTALLATION.json').read_bytes(),before)

    def test_modified_baseline_refuses_plan_without_overwrite(self):
        source=self.root/'app/0.2.0-preview.3/project-workbench/workbench/shared.py'
        source.write_bytes(source.read_bytes()+b'\n# unrelated change\n')
        before=source.read_bytes()
        rejected=self.patch('--apply','--backup-dir',self.backup)
        self.assertNotEqual(rejected.returncode,0)
        self.assertFalse(self.backup.exists())
        self.assertEqual(source.read_bytes(),before)

    def test_rollback_refuses_later_source_changes(self):
        applied=self.patch('--apply','--backup-dir',self.backup)
        self.assertEqual(applied.returncode,0,applied.stderr)
        source=self.root/'app/0.2.0-preview.3/project-workbench/workbench/shared.py'
        source.write_bytes(source.read_bytes()+b'\n# later source change\n')
        before=source.read_bytes()
        rejected=self.patch('--rollback','--backup-dir',self.backup)
        self.assertNotEqual(rejected.returncode,0)
        self.assertEqual(source.read_bytes(),before)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local-pack',type=Path,required=True)
    args=parser.parse_args();PACKAGE=args.local_pack.resolve()
    unittest.main(argv=[sys.argv[0],'-v'])
