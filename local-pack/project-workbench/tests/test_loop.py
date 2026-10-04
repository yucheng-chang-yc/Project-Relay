import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from workbench.core import Runtime, WorkbenchError, digest, run_git
from workbench.loop import MAX_INPUT, pinned_download
from workbench.mcp import MCP, TOOLS, validate
from workbench.restricted import container_argv, validate_policy
from workbench.adapters import agent_command

class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'project'
        self.root.mkdir()
        run_git(self.root, ['init', '-b', 'main'])
        run_git(self.root, ['config', 'user.name', 'Loop Fixture'])
        run_git(self.root, ['config', 'user.email', 'fixture@example.invalid'])
        (self.root / 'baseline.txt').write_text('original', encoding='utf-8')
        run_git(self.root, ['add', 'baseline.txt'])
        run_git(self.root, ['commit', '-m', 'baseline'])
        self.config = self.base / 'cfg.json'
        self.config.write_text(json.dumps({'data_dir': str(self.base / 'state'), 'auth_token': 'x'*48,
            'projects': [{'id': 'test', 'root': str(self.root), 'writable': True,
                'profiles': {}, 'claude_command': [sys.executable, str(Path(__file__).with_name('fake_loop_agent.py'))]}]}))
        self.runtime = Runtime(self.config)

    def tearDown(self):
        self.temp.cleanup()

    def input(self, size=1024):
        data = (bytes(range(256)) * (size//256+1))[:size]
        path = self.base / ('data-'+str(size)+'.bin')
        path.write_bytes(data)
        return self.runtime.loop.stage_local('test', path, digest(data), 'stage-'+str(size)), data, path

    def task(self, item, key='run-1', **extra):
        spec = {'goal': 'exact binary fixture', 'acceptance': ['Fixture adapter preserves SHA; not product E2E'],
                'inputs': [{'input_id': item['input_id'], 'destination': 'wb_inputs/data.bin'}],
                'artifacts': ['result.bin'], 'timeout_seconds': 20, **extra}
        task = self.runtime.start_task('test', 'claude', spec, key)
        deadline = time.monotonic()+25
        while time.monotonic()<deadline:
            row = self.runtime.get_task(task['id'])
            if row['status'] not in ('queued', 'running', 'cancelling'):
                self.assertEqual(row['status'], 'completed', row)
                return self.runtime.get_task_result(task['id'])
            time.sleep(.05)
        self.fail('Fixture task did not complete')

    def test_exact_1mib_20mib_binary_staging_resources_and_hidden_windows(self):
        for size in (1024*1024, 20*1024*1024):
            with self.subTest(size=size):
                item, data, path = self.input(size)
                result = self.task(item, key='run-'+str(size))
                task_id = result['task']['id']
                artifact = result['result']['artifacts'][0]
                self.assertEqual(artifact['sha256'], digest(data))
                self.assertEqual(artifact['size_bytes'], size)
                meta = self.runtime.loop.artifact_resource(task_id, 0, digest(data))
                wire = MCP(self.runtime).handle({'jsonrpc':'2.0','id':1,'method':'resources/read','params':{'uri':meta['uri']}})
                self.assertEqual(base64.b64decode(wire['result']['contents'][0]['blob']), data)
                assembled = bytearray()
                while len(assembled)<size:
                    response = MCP(self.runtime).handle({'jsonrpc':'2.0','id':2,'method':'tools/call',
                        'params':{'name':'read_artifact_chunk','arguments':{'task_id':task_id,'artifact_id':0,
                        'expected_sha256':digest(data),'offset':len(assembled),'limit':262144}}})['result']
                    self.assertNotIn('base64', response['structuredContent']['data'])
                    chunk = base64.b64decode(response['_meta']['bytes_base64'])
                    self.assertEqual(digest(chunk),response['structuredContent']['data']['chunk_sha256'])
                    assembled.extend(chunk)
                self.assertEqual(digest(assembled), digest(data))
                self.assertNotIn('wb_inputs', '\n'.join(result['result']['changed_files']))
                self.assertEqual(run_git(self.root,['status','--porcelain']), '')

    def test_prior_artifact_followup_retains_bytes_and_unit_without_upload(self):
        item, data, _ = self.input()
        first = self.task(item)
        previous = first['task']['id']
        staged = self.runtime.loop.stage_prior_artifact('test', previous, 0, first['result_sha256'], digest(data), 'prior-1')
        self.assertEqual(staged['source']['task_id'], previous)
        second = self.task(staged, key='follow-up', parent_task_id=previous)
        a, b = (self.runtime.loop.snapshot(t['task']['id']) for t in (first,second))
        self.assertEqual(a['work_unit_id'], b['work_unit_id'])
        self.assertEqual(b['parents'], [{'task_id':previous,'result_sha256':first['result_sha256']}])
        self.assertEqual(second['result']['artifacts'][0]['sha256'], digest(data))
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.stage_prior_artifact('test', previous, 0, '0'*64, digest(data), 'wrong-parent')

    def test_input_idempotency_concurrency_mismatch_and_tamper_fail_closed(self):
        item, data, path = self.input()
        with ThreadPoolExecutor(max_workers=2) as pool:
            again = list(pool.map(lambda _:self.runtime.loop.stage_local('test',path,digest(data),'stage-1024'),range(2)))
        self.assertTrue(all(x['input_id']==item['input_id'] for x in again))
        path.write_bytes(data+b'changed')
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.stage_local('test',path,digest(data+b'changed'),'stage-1024')
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.stage_local('test',path,digest(data),'new-wrong-sha')
        _, stored = self.runtime.loop.input('test',item['input_id'])
        stored.write_bytes(b'tampered')
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.prepare('test','claude',{'inputs':[{'input_id':item['input_id'],'destination':'wb_inputs/data.bin'}]})
        with self.runtime.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],0)

    def test_portable_destinations_metadata_aliases_and_prefix_conflicts(self):
        item, _, _ = self.input()
        paths = ['../escape','wb_inputs/../escape','wb_inputs/.git/config','wb_inputs/AUX.txt',
                 'wb_inputs/a./b','wb_inputs/a\\b','wb_inputs/CON','wb_inputs//data','wb_inputs/a:stream']
        for path in paths:
            with self.subTest(path=path), self.assertRaises(WorkbenchError):
                self.runtime.loop.prepare('test','claude',{'inputs':[{'input_id':item['input_id'],'destination':path}]})
        for paths in [('wb_inputs/A','wb_inputs/a'),('wb_inputs/a','wb_inputs/a/b')]:
            with self.assertRaises(WorkbenchError):
                self.runtime.loop.prepare('test','claude',{'inputs':[{'input_id':item['input_id'],'destination':p} for p in paths]})

    def test_explicit_uncommitted_primary_input_exact_destination_and_no_dirty_leak(self):
        (self.root/'data').mkdir()
        data=b'plant,yield\nT01,1.2\n'
        (self.root/'data'/'input.csv').write_bytes(data)
        (self.root/'not-selected-secret.txt').write_text('must not enter worktree')
        item=self.runtime.loop.stage_project_input('test','data/input.csv',digest(data),'primary-input')
        result=self.task(item,inputs=[{'input_id':item['input_id'],'destination':'data/input.csv'}])
        workspace=Path(result['task']['worktree'])
        self.assertEqual((workspace/'data'/'input.csv').read_bytes(),data)
        self.assertFalse((workspace/'not-selected-secret.txt').exists())
        self.assertNotIn('data/input.csv',result['result']['changed_files'])
        unit=self.runtime.loop.get_work_unit(self.runtime.loop.snapshot(result['task']['id'])['work_unit_id'])
        self.assertEqual(unit['authority'],'navigation_only')
        self.assertEqual(unit['artifacts'][0]['sha256'],digest(data))
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.stage_project_input('test','data/input.csv','0'*64,'stale-primary')

    def test_review_invalidates_on_workspace_head_move_and_apply_excludes_staged_input(self):
        item,data,_=self.input()
        result=self.task(item)
        ident=result['task']['id']
        snapshot=self.runtime.loop.snapshot(ident)
        self.runtime.loop.record_review_object(ident,result['result_sha256'],snapshot['snapshot_sha256'],'PASS','fixture','review')
        # PASS alone has no integration authority.
        with self.assertRaises(WorkbenchError):
            self.runtime.apply_task_changes(ident,result['result']['diff_sha256'])
        self.runtime.mark_task_reviewed(ident,result['result_sha256'],'accept_changes','Scoped fixture integration authority')
        self.runtime.apply_task_changes(ident,result['result']['diff_sha256'])
        self.assertEqual((self.root/'result.bin').read_bytes(),data)
        self.assertFalse((self.root/'wb_inputs').exists())
        workspace=Path(result['task']['worktree'])
        run_git(workspace,['add','result.bin'])
        run_git(workspace,['commit','-m','unauthorized head move'])
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.latest_review(ident)

    def test_input_size_limit_precedes_writer_and_native_bash_is_rejected(self):
        with patch('shutil.copyfile') as copy,self.assertRaises(WorkbenchError):
            self.runtime.loop._stage('test',{'kind':'fixture'},MAX_INPUT+1,'0'*64,'oversize',copy)
        copy.assert_not_called()
        from workbench.adapters import claude_policy
        with self.assertRaises(WorkbenchError):
            claude_policy({'claude_allowed_tools':['Read','Bash']})

    def test_append_only_reviews_exact_evidence_and_terminal_seal(self):
        item, data, _ = self.input()
        result = self.task(item)
        ident = result['task']['id']
        snapshot = self.runtime.loop.snapshot(ident)
        args = (ident,result['result_sha256'],snapshot['snapshot_sha256'],'PASS','Fixture evidence only','review-1')
        review = self.runtime.loop.record_review_object(*args)
        self.assertFalse(review['human_acceptance'])
        self.assertEqual(review,self.runtime.loop.record_review_object(*args))
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.record_review_object(ident,result['result_sha256'],'0'*64,'PASS','stale','other')
        with self.assertRaises(WorkbenchError):
            self.runtime.finish(ident,'completed',0,result={'forged':True})
        with self.runtime.connection(write=True) as c:
            for table, field in [('loop_inputs','manifest'),('loop_snapshots','body'),('loop_reviews','body')]:
                with self.assertRaises(sqlite3.IntegrityError):
                    c.execute(f'UPDATE {table} SET {field}=?', ('{}',))
        log = self.runtime.task_dir(ident)/'executor.log'
        log.write_bytes(log.read_bytes()+b'changed')
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.latest_review(ident)
        historical=self.runtime.loop.get_review_object(review['review_id'])
        self.assertEqual(historical['object'],review)
        self.assertFalse(historical['current_evidence_valid'])

    def test_artifact_tamper_and_binding_change_stop_prior_staging(self):
        item,data,_=self.input()
        result=self.task(item)
        ident=result['task']['id']
        path,_=self.runtime.artifact_path(ident,0)
        path.write_bytes(b'tampered')
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.stage_prior_artifact('test',ident,0,result['result_sha256'],digest(data),'prior')
        config=json.loads(self.config.read_text())
        config['projects'][0]['compute_runtime']={'backend':'raw-powershell'}
        self.config.write_text(json.dumps(config))
        with self.assertRaises(WorkbenchError):
            self.runtime.loop.input('test',item['input_id'])

    def test_host_file_schema_and_no_native_command_fallback(self):
        tool=next(x for x in TOOLS if x['name']=='stage_binary_input')
        self.assertEqual(tool['_meta']['openai/fileParams'], ['file'])
        file=tool['inputSchema']['properties']['file']
        self.assertEqual(set(file['properties']),{'download_url','file_id','mime_type','file_name'})
        self.assertEqual(set(file['required']),{'download_url','file_id'})
        for policy in (None, {'backend':'powershell'}, {'backend':'docker','executable':sys.executable,'image':'sha256:'+'a'*64,'user':'1000:1000'}):
            with patch('subprocess.Popen') as launch, self.assertRaises(WorkbenchError):
                validate_policy(policy)
            launch.assert_not_called()
        item,_,_=self.input()
        with self.assertRaises(WorkbenchError):
            self.runtime.start_task('test','claude',{'goal':'compute','acceptance':['real R'], 'compute_runtime':True},'unsafe')
        with self.runtime.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM tasks').fetchone()[0],0)

    def test_container_argv_has_mature_enforcement_and_no_shell(self):
        policy={'backend':'docker','executable':sys.executable,'image':'sha256:'+'a'*64,'user':'1000:1000','enforcement_verified':True}
        (self.root/'analysis.R').write_text('print(1)')
        argv=container_argv(policy,self.root,'wb-test','Rscript','analysis.R',5)
        for flag in ('--network=none','--cap-drop=ALL','--read-only','--security-opt=no-new-privileges:true','--pull=never'):
            self.assertIn(flag,argv)
        self.assertNotIn('bash',argv)
        self.assertNotIn('powershell',argv)
        with self.assertRaises(WorkbenchError):
            container_argv(policy,self.root,'wb-test','Rscript','../escape.R',5)
        directory=self.base/'adapter'
        directory.mkdir()
        (directory/'restricted-mcp.json').write_text('{}')
        argv=agent_command('claude',self.runtime.projects['test'],directory)
        self.assertIn('--restricted',argv)
        self.assertNotIn('Bash',argv[argv.index('--tools')+1])
        self.assertIn('mcp__wb_runtime__run',argv)
        self.assertNotIn('mcp__*',argv)

    def test_ssrf_rejections_never_open_sockets_or_leak_signed_urls(self):
        urls=['http://public.example/signed?token=secret','https://user:secret@public.example/file',
              'https://public.example:444/file','https://127.0.0.1/file','https://public.example:bad/file']
        for index,url in enumerate(urls):
            with patch('socket.create_connection') as connect, self.assertRaises(WorkbenchError) as failure:
                pinned_download(url,['public.example'],self.base/str(index),1,'0'*64)
            connect.assert_not_called()
            self.assertNotIn('secret',str(failure.exception))
        for address in ('127.0.0.1','10.0.0.1','169.254.169.254','::1','224.0.0.1'):
            with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))]), patch('socket.create_connection') as connect, self.assertRaises(WorkbenchError):
                pinned_download('https://public.example/file',['public.example'],self.base/'never',1,'0'*64)
            connect.assert_not_called()

    def test_stream_size_hash_redirect_and_expired_url_reuse(self):
        # Controlled HTTPS primitive fixture. Does not connect to ChatGPT.
        class Response(io.BytesIO):
            status=200
            def getheader(self,name,default=None):return default
        class Context:
            def wrap_socket(self,raw,server_hostname):return raw
        class Connection:
            def __init__(self,*a,**k):self._context=Context()
            def request(self,*a,**k):pass
            def getresponse(self):return Response(b'\0\xfffixture\n')
            def close(self):pass
        data=b'\0\xfffixture\n'
        config=json.loads(self.config.read_text());config['binary_transfer']={'allowed_download_hosts':['public.example']}
        self.config.write_text(json.dumps(config));self.runtime=Runtime(self.config)
        with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]), patch('socket.create_connection'),patch('http.client.HTTPSConnection',Connection):
            item=self.runtime.loop.stage_binary_input('test',{'file_id':'fixture-file','download_url':'https://public.example/file?secret=token'},len(data),digest(data),'host-1')
            with self.assertRaises(WorkbenchError):
                pinned_download('https://public.example/file',['public.example'],self.base/'wrong',len(data)-1,digest(data))
            with self.assertRaises(WorkbenchError):
                pinned_download('https://public.example/file',['public.example'],self.base/'wrong-hash',len(data),'0'*64)
        with patch('workbench.loop.pinned_download') as again:
            old=self.runtime.loop.stage_binary_input('test',{'file_id':'fixture-file','download_url':'expired URL'},len(data),digest(data),'host-1')
            again.assert_not_called()
        self.assertEqual(item,old)
        self.assertNotIn('download_url',json.dumps(item))
        self.assertNotIn('token',self.runtime.db.read_bytes().decode('utf-8','ignore'))
        class Redirect(Connection):
            def getresponse(self):
                result=Response(b'');result.status=302;return result
        with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]),patch('socket.create_connection'),patch('http.client.HTTPSConnection',Redirect),self.assertRaises(WorkbenchError):
            pinned_download('https://public.example/file',['public.example'],self.base/'redirect',0,digest(b''))

if __name__=='__main__':
    unittest.main()
