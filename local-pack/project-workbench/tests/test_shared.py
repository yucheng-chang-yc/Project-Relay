"""Exact file/grant contracts; local fixtures, not live ChatGPT evidence."""
import base64, hashlib, json, os, subprocess, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from workbench.core import Runtime, WorkbenchError
from workbench.mcp import MCP, TOOLS, SHARED_URI
from workbench.shared import CHUNK, MAX_BYTES
sha=lambda raw:hashlib.sha256(raw).hexdigest()

class SharedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name).resolve()
        self.folder=self.base/'shared';self.folder.mkdir();private=self.base/'private';private.mkdir()
        self.config=private/'config.json';self.config.write_text(json.dumps({'data_dir':str(private/'state'),'projects':[]}))
        self.runtime=Runtime(self.config);self.store=self.runtime.shared;self.mcp=MCP(self.runtime)
    def tearDown(self):self.temp.cleanup()
    def grant(self,access='read_write',path=None,key='grant'):
        r=self.store.request(str(path or self.folder),access,key);t=self.store.prepare(r['request_id'])
        return self.store.approve(r['request_id'],t['approval_token'],'approve')['grant_id']
    def upload(self,grant,name,raw,old='',key='write'):
        w=self.store.begin_write(grant,name,old,sha(raw),len(raw),key)
        for offset in range(0,len(raw),CHUNK):
            part=raw[offset:offset+CHUNK];w=self.store.write_chunk(w['write_id'],offset,base64.b64encode(part).decode(),sha(part))
        return w
    def test_request_is_not_authority_and_model_flag_rejected(self):
        r=self.store.request(str(self.folder),'read_write','grant');self.assertEqual(r['status'],'awaiting_user_approval')
        self.assertNotIn('approval_token',json.dumps(r));self.assertEqual(self.store.list(),[])
        with self.assertRaises(WorkbenchError):self.store.approve(r['request_id'],'guess','approve')
        reply=self.mcp.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'request_shared_folder','arguments':{'path':str(self.folder),'access':'read_write','idempotency_key':'bad','authorized':True}}})
        self.assertTrue(reply['result']['isError'])
        for name in ('prepare_shared_folder_approval','approve_shared_folder'):
            self.assertEqual(next(t for t in TOOLS if t['name']==name)['_meta']['ui']['visibility'],['app'])
        self.assertIn('Allow access to this folder?',self.mcp.resource(SHARED_URI)['text'])
    def test_scope_token_rotation_denial_expiry(self):
        r=self.store.request(str(self.folder),'read_only','grant')
        self.assertEqual(self.store.request(str(self.folder),'read_only','grant')['request_id'],r['request_id'])
        with self.assertRaises(WorkbenchError):self.store.request(str(self.folder),'read_write','grant')
        old=self.store.prepare(r['request_id']);new=self.store.prepare(r['request_id'])
        with self.assertRaises(WorkbenchError):self.store.approve(r['request_id'],old['approval_token'],'approve')
        self.assertEqual(self.store.approve(r['request_id'],new['approval_token'],'deny')['status'],'denied')
        with self.runtime.connection() as c:self.assertIsNone(c.execute('SELECT approval_hash FROM shared_requests').fetchone()[0])
        other=self.store.request(str(self.folder),'read_write','expire')
        with self.runtime.connection(write=True) as c:c.execute('UPDATE shared_requests SET expires=0 WHERE id=?',(other['request_id'],))
        self.assertEqual(self.store.prepare(other['request_id'])['status'],'expired');self.assertEqual(self.store.list(),[])
    def test_binary_roundtrip_multiwindow_retry_without_tasks(self):
        grant=self.grant();raw=bytes(range(256))*4000;w=self.upload(grant,'result.zip',raw);part=raw[:CHUNK]
        self.assertEqual(self.store.write_chunk(w['write_id'],0,base64.b64encode(part).decode(),sha(part))['next_offset'],len(raw))
        self.assertFalse((self.folder/'result.zip').exists());receipt=self.store.commit_write(w['write_id'])
        self.assertEqual(receipt['status'],'committed');self.assertEqual(self.store.commit_write(w['write_id']),receipt)
        self.assertEqual((self.folder/'result.zip').read_bytes(),raw);received=[];offset=0
        while True:
            r=self.store.read(grant,'result.zip',sha(raw),offset,CHUNK);part=base64.b64decode(r['base64'])
            self.assertEqual(sha(part),r['chunk_sha256']);received.append(part);offset=r['next_offset']
            if r['eof']:break
        self.assertEqual(b''.join(received),raw);self.assertEqual(self.runtime.list_tasks(),[])
    def test_restart_future_descendants_and_mkdir(self):
        grant=self.grant();self.store.mkdir(grant,'results');self.store.mkdir(grant,'results');restarted=Runtime(self.config)
        (self.folder/'results'/'later.txt').write_bytes(b'future')
        self.assertEqual(restarted.shared.list()[0]['grant_id'],grant)
        self.assertEqual(base64.b64decode(restarted.shared.read(grant,'results/later.txt')['base64']),b'future')
        self.assertEqual(self.store.list_directory(grant)['entries'][0]['name'],'results')
    def test_readonly_blocks_write_and_mkdir(self):
        grant=self.grant('read_only');(self.folder/'data').write_bytes(b'read');self.assertEqual(self.store.read(grant,'data')['sha256'],sha(b'read'))
        with self.assertRaises(WorkbenchError):self.upload(grant,'new',b'write')
        with self.assertRaises(WorkbenchError):self.store.mkdir(grant,'new')
    def test_live_revoke_blocks_pending_commit_and_preserves_original(self):
        grant=self.grant();(self.folder/'old').write_bytes(b'original');w=self.upload(grant,'new',b'pending');self.store.revoke(grant)
        for fn in (lambda:self.store.read(grant,'old'),lambda:self.store.list_directory(grant),lambda:self.store.commit_write(w['write_id'])):
            with self.assertRaises(WorkbenchError):fn()
        self.assertEqual((self.folder/'old').read_bytes(),b'original');self.assertFalse((self.folder/'new').exists())
        self.assertEqual(self.store.list(),[]);self.assertEqual(Runtime(self.config).shared.list(),[])
    def test_stale_overwrite_and_concurrent_create_preserve_files(self):
        grant=self.grant();target=self.folder/'out';target.write_bytes(b'old');w=self.upload(grant,'out',b'new',sha(b'old'));target.write_bytes(b'external')
        with self.assertRaises(WorkbenchError):self.store.commit_write(w['write_id'])
        self.assertEqual(target.read_bytes(),b'external')
        w=self.upload(grant,'fresh',b'new',key='fresh');(self.folder/'fresh').write_bytes(b'external')
        with self.assertRaises(WorkbenchError):self.store.commit_write(w['write_id'])
        w=self.upload(grant,'out',b'replaced',sha(b'external'),key='valid');self.store.commit_write(w['write_id']);self.assertEqual(target.read_bytes(),b'replaced')
    def test_changed_read_cannot_mix_windows(self):
        grant=self.grant();target=self.folder/'data';target.write_bytes(b'abc');r=self.store.read(grant,'data',limit=1);target.write_bytes(b'xyz')
        with self.assertRaises(WorkbenchError):self.store.read(grant,'data',r['sha256'],1)
    def test_escape_ads_devices_and_git_metadata(self):
        grant=self.grant()
        for path in ('../x','a/../x','/tmp/x','C:/x','x:secret','a\\b','NUL','a/.git/config','x.','a//b'):
            with self.subTest(path=path),self.assertRaises(WorkbenchError):self.store.read(grant,path)
        (self.folder/'.git').mkdir();(self.folder/'.git'/'config').write_text('private');self.assertEqual(self.store.list_directory(grant)['entries'],[])
    def test_link_junction_descendant_and_root(self):
        grant=self.grant();outside=self.base/'outside';outside.mkdir();(outside/'secret').write_bytes(b'secret');alias=self.folder/'alias'
        try:alias.symlink_to(outside,target_is_directory=True)
        except OSError:
            if os.name!='nt':raise
            subprocess.run(['cmd','/c','mklink','/J',str(alias),str(outside)],check=True,capture_output=True)
        with self.assertRaises(WorkbenchError):self.store.read(grant,'alias/secret')
        self.assertEqual(self.store.list_directory(grant)['entries'],[])
        with self.assertRaises(WorkbenchError):self.grant(path=alias,key='alias')
    def test_hardlink_not_read_or_overwritten(self):
        grant=self.grant();outside=self.base/'outside-secret';outside.write_bytes(b'secret');os.link(outside,self.folder/'alias')
        with self.assertRaises(WorkbenchError):self.store.read(grant,'alias')
        w=self.upload(grant,'alias',b'new',sha(b'secret'))
        with self.assertRaises(WorkbenchError):self.store.commit_write(w['write_id'])
        self.assertEqual(outside.read_bytes(),b'secret')
    def test_private_registered_and_parent_roots_excluded(self):
        for path in (self.base,self.runtime.data,self.config.parent):
            with self.subTest(path=path),self.assertRaises(WorkbenchError):self.grant(path=path,key=path.name)
        cfg=json.loads(self.config.read_text());cfg['projects']=[{'id':'p','root':str(self.folder)}];self.config.write_text(json.dumps(cfg));self.store=Runtime(self.config).shared
        with self.assertRaises(WorkbenchError):self.grant(key='project')
    def test_replaced_root_requires_new_grant(self):
        grant=self.grant();self.folder.rename(self.base/'previous');self.folder.mkdir();(self.folder/'x').write_bytes(b'new')
        with self.assertRaises(WorkbenchError):self.store.read(grant,'x')
    def test_chunk_order_identity_full_sha_empty_and_limits(self):
        grant=self.grant();w=self.store.begin_write(grant,'x','',sha(b'abc'),3,'w')
        for offset,raw,h in ((1,b'abc',sha(b'abc')),(0,b'abc',sha(b'bad')),(0,b'long',sha(b'long'))):
            with self.assertRaises(WorkbenchError):self.store.write_chunk(w['write_id'],offset,base64.b64encode(raw).decode(),h)
        with self.assertRaises(WorkbenchError):self.store.commit_write(w['write_id'])
        self.store.write_chunk(w['write_id'],0,base64.b64encode(b'abc').decode(),sha(b'abc'))
        with self.assertRaises(WorkbenchError):self.store.write_chunk(w['write_id'],0,base64.b64encode(b'xyz').decode(),sha(b'xyz'))
        (self.store.uploads/w['write_id']).write_bytes(b'xyz')
        with self.assertRaises(WorkbenchError):self.store.commit_write(w['write_id'])
        w=self.upload(grant,'empty',b'',key='empty');self.store.commit_write(w['write_id']);self.assertEqual((self.folder/'empty').read_bytes(),b'')
        with self.assertRaises(WorkbenchError):self.store.begin_write(grant,'big','',sha(b''),MAX_BYTES+1,'big')
    def test_expired_and_uncertain_writes_block_replay(self):
        grant=self.grant();w=self.upload(grant,'out',b'new')
        with self.runtime.connection(write=True) as c:c.execute('INSERT INTO shared_publish_attempts VALUES(?,?)',(w['write_id'],time.time()))
        with self.assertRaisesRegex(WorkbenchError,'uncertain'):self.store.commit_write(w['write_id'])
        self.assertFalse((self.folder/'out').exists());other=self.upload(grant,'later',b'new',key='later')
        with self.runtime.connection(write=True) as c:c.execute('UPDATE shared_writes SET expires=0 WHERE id=?',(other['write_id'],))
        with self.assertRaises(WorkbenchError):self.store.commit_write(other['write_id'])
        self.assertFalse((self.store.uploads/other['write_id']).exists())
    def test_uncertain_publication_requires_local_recovery_even_after_expiry(self):
        grant=self.grant();w=self.upload(grant,'out',b'new')
        with self.runtime.connection(write=True) as c:
            c.execute('INSERT INTO shared_publish_attempts VALUES(?,?)',(w['write_id'],time.time()))
            c.execute('UPDATE shared_writes SET expires=0 WHERE id=?',(w['write_id'],))
        with self.assertRaisesRegex(WorkbenchError,'uncertain'):self.upload(grant,'out',b'new',key='bypass')
        with self.assertRaises(WorkbenchError):self.store.resolve_write_locally(w['write_id'],sha(b'wrong'),'Inspected')
        result=self.store.resolve_write_locally(w['write_id'],'','Inspected absent target; do not replay')
        self.assertEqual(result['status'],'resolved_not_replayed')
        self.assertFalse((self.folder/'out').exists())
        new=self.upload(grant,'out',b'new',key='after-recovery');self.store.commit_write(new['write_id'])
        self.assertEqual((self.folder/'out').read_bytes(),b'new')
    def test_revocation_retry_is_idempotent(self):
        grant=self.grant();first=self.store.revoke(grant);self.assertEqual(self.store.revoke(grant),first)
    @unittest.skipIf(os.name=='nt','POSIX fd swap injection; Windows handle path is tested natively')
    def test_file_open_swap_refused(self):
        grant=self.grant();target=self.folder/'data';target.write_bytes(b'old');outside=self.base/'outside';outside.write_bytes(b'secret')
        from workbench import shared
        original=shared.Directory.open_file
        def switch(d,name,flags,mode=0o600):
            if name=='data':target.unlink();os.link(outside,target)
            return original(d,name,flags,mode)
        with patch.object(shared.Directory,'open_file',switch),self.assertRaises(WorkbenchError):self.store.read(grant,'data')
        self.assertEqual(outside.read_bytes(),b'secret')
if __name__=='__main__':unittest.main()
