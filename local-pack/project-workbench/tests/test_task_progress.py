"""Exercise real fixture executor + injected postprocessing failure, never real model calls."""
import json, os, sys, unittest, subprocess
from unittest.mock import Mock, patch
import test_runtime as fixtures
from workbench import worker
from workbench.core import Runtime, WorkbenchError

class TaskProgressTests(unittest.TestCase):
    setUp=fixtures.RuntimeTests.setUp
    start=fixtures.RuntimeTests.start
    wait=fixtures.RuntimeTests.wait
    def tearDown(self):
        with self.runtime.connection(write=True) as c:
            c.execute('UPDATE tasks SET worker_pid=NULL WHERE worker_pid=?',(os.getpid(),))
        fixtures.RuntimeTests.tearDown(self)
    def prepared(self,kind='codex',artifacts=None):
        stub=Mock(pid=None);stub.wait.return_value=0
        original = subprocess.Popen
        def launch(argv, *args, **kwargs):
            return stub if "workbench.worker" in argv else original(argv, *args, **kwargs)
        with patch('workbench.core.subprocess.Popen',side_effect=launch):
            return self.runtime.start_task('test',kind,{'goal':'fixture work','acceptance':['valid output'],
                'timeout_seconds':5,'artifacts':artifacts or []},'fixture')
    def main(self,task):
        with patch.object(sys,'argv',['worker','--config',str(self.config),'--task',task['id']]),patch.object(worker,'Runtime',return_value=self.runtime):
            return worker.main()
    def test_git_capture_failure_retains_exit_report_verified_artifact_and_phase(self):
        task=self.prepared(artifacts=['analysis.txt'])
        with patch.object(self.runtime,'snapshot_diff',side_effect=WorkbenchError('open("hello_relay.py"): Permission denied')):
            self.assertEqual(self.main(task),1)
        data=self.runtime.get_task_result(task['id']);result=data['result']
        self.assertEqual(data['task']['status'],'failed');self.assertEqual(data['task']['exit_code'],0)
        self.assertTrue(result['execution_completed']);self.assertFalse(result['complete'])
        self.assertEqual(result['error_phase'],'change_capture')
        self.assertEqual(result['stages']['execution']['status'],'completed')
        self.assertEqual(result['stages']['artifact_capture']['status'],'completed')
        self.assertEqual(result['stages']['change_capture']['status'],'failed')
        self.assertEqual(result['reported_changed_files'],['analysis.txt']);self.assertEqual(result['changed_files'],[])
        self.assertNotIn('diff_sha256',result);self.assertEqual(len(result['artifacts']),1)
        self.assertIn('分析完成',self.runtime.read_task_artifact(task['id'],0)['text'])
        with self.assertRaises(WorkbenchError):self.runtime.mark_task_reviewed(task['id'],data['result_sha256'],'accept_changes','partial')
    def test_later_artifact_failure_keeps_earlier_verified_output(self):
        task=self.prepared(artifacts=['analysis.txt','missing.txt']);self.assertEqual(self.main(task),1)
        result=self.runtime.get_task_result(task['id'])['result']
        self.assertEqual(result['error_phase'],'artifact_capture');self.assertEqual(len(result['artifacts']),1)
        self.assertFalse(result['complete']);self.assertTrue(self.runtime.artifact_path(task['id'],0)[0].is_file())
    def test_complete_stages_and_write_once_terminal_hash(self):
        task=self.prepared();self.assertEqual(self.main(task),0)
        data=self.runtime.get_task_result(task['id']);result=data['result']
        self.assertTrue(result['complete']);self.assertIsNone(result['error_phase'])
        self.assertEqual(set(result['stages']),{'preparation','execution','command_shutdown','result_parse','artifact_capture','change_capture','evidence_seal'})
        self.assertTrue(all(r['status']=='completed' for r in result['stages'].values()))
        with self.assertRaises(WorkbenchError):self.runtime.checkpoint_task(task['id'],'execution','failed')
        self.assertEqual(self.runtime.get_task_result(task['id'])['result_sha256'],data['result_sha256'])
    def test_executor_nonzero_remains_failure_with_exit_and_log(self):
        task=self.start('fail');done=self.wait(task['id']);result=self.runtime.get_task_result(task['id'])['result']
        self.assertEqual(done['exit_code'],7);self.assertFalse(result['execution_completed']);self.assertFalse(result['complete'])
        self.assertEqual(result['error_phase'],'execution');self.assertIn('executor_log_sha256',result)
    def test_archive_restore_survives_restart_without_review_or_evidence_change(self):
        task=self.prepared(artifacts=['analysis.txt']);self.main(task);before=self.runtime.get_task_result(task['id'])
        self.runtime.archive_task(task['id']);self.assertEqual(self.runtime.list_tasks(),[])
        restarted=Runtime(self.config);self.assertEqual(restarted.list_tasks(archived=True)[0]['id'],task['id'])
        after=restarted.get_task_result(task['id']);self.assertEqual(after['result_sha256'],before['result_sha256'])
        self.assertEqual(after['task']['review_status'],'pending');self.assertTrue(restarted.artifact_path(task['id'],0)[0].is_file())
        restarted.archive_task(task['id'],False);self.assertEqual(restarted.list_tasks()[0]['id'],task['id'])
    def test_active_and_uncertain_cannot_archive_bulk_excludes_them(self):
        task=self.prepared()
        with self.assertRaises(WorkbenchError):self.runtime.archive_task(task['id'])
        self.assertEqual(self.runtime.archive_finished_tasks()['archived_count'],0)
        with self.runtime.connection(write=True) as c:c.execute("UPDATE tasks SET status='interrupted' WHERE id=?",(task['id'],))
        with self.assertRaises(WorkbenchError):self.runtime.archive_task(task['id'])
        self.assertEqual(self.runtime.archive_finished_tasks()['archived_count'],0)
    def test_bulk_archive_failed_and_completed_leaves_review_pending(self):
        task=self.prepared();self.main(task);other=self.start('fail',key='fail');self.wait(other['id'])
        self.assertEqual(self.runtime.archive_finished_tasks('test')['archived_count'],2)
        self.assertEqual(self.runtime.list_tasks(),[]);self.assertEqual(len(self.runtime.list_tasks(archived=True)),2)
        self.assertEqual(self.runtime.archive_finished_tasks('test')['archived_count'],0)
        self.assertTrue(all(t['review_status']=='pending' for t in self.runtime.list_tasks(archived=True)))
    def test_migration_adds_archive_default_without_changing_results(self):
        task=self.prepared();self.main(task);before=self.runtime.get_task_result(task['id'])['result_sha256']
        with self.runtime.connection(write=True) as c:c.execute('ALTER TABLE tasks DROP COLUMN archived')
        restarted=Runtime(self.config);self.assertEqual(restarted.list_tasks()[0]['archived'],0)
        self.assertEqual(restarted.get_task_result(task['id'])['result_sha256'],before)
    def test_archive_cursor_can_reach_more_than_100_retained_tasks(self):
        task=self.prepared();self.main(task)
        with self.runtime.connection(write=True) as c:
            row=dict(c.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone())
            names=list(row)
            for i in range(120):
                copied={**row,'id':'task_'+format(i,'032x'),'idempotency_key':'page-'+str(i),'created':float(i),'archived':1}
                c.execute('INSERT INTO tasks ('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',[copied[n] for n in names])
        first=self.runtime.list_tasks(archived=True,limit=100)
        second=self.runtime.list_tasks(archived=True,limit=100,before_task_id=first[-1]['id'])
        self.assertEqual(len(first),100);self.assertEqual(len(second),20)
        self.assertEqual(len({t['id'] for t in first+second}),120)
        self.runtime.archive_task(second[-1]['id'],False)
        self.assertEqual(self.runtime.get_task(second[-1]['id'])['archived'],0)
if __name__=='__main__':unittest.main()
