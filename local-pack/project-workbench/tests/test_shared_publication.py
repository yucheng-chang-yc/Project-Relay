"""D-005 classification, durable barriers and nonreplayed recovery regression.

Synchronous failure injection proves source behavior; it is not Windows live evidence.
"""
import errno
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import test_shared as fixture
from workbench.core import Runtime, WorkbenchError
from workbench.shared import Directory, read_exact
sha = fixture.sha


class PublicationTests(unittest.TestCase):
    setUp = fixture.SharedTests.setUp
    tearDown = fixture.SharedTests.tearDown
    grant = fixture.SharedTests.grant
    upload = fixture.SharedTests.upload

    def attempt(self, create=False):
        grant = self.grant()
        target = self.folder / 'out'
        if not create:
            target.write_bytes(b'old')
        w = self.upload(grant, 'out', b'new', '' if create else sha(b'old'))
        return grant, target, w['write_id']

    def journal_count(self, write_id):
        with self.runtime.connection() as c:
            return c.execute('SELECT COUNT(*) FROM shared_publish_attempts WHERE write_id=?', (write_id,)).fetchone()[0]

    def last_failure(self, write_id):
        with self.runtime.connection() as c:
            return dict(c.execute('SELECT * FROM shared_publish_failures WHERE write_id=? ORDER BY id DESC', (write_id,)).fetchone())

    def assert_blocked(self, grant, write_id):
        with self.assertRaisesRegex(WorkbenchError, 'uncertain'):
            self.store.commit_write(write_id)
        with self.assertRaisesRegex(WorkbenchError, 'uncertain'):
            self.upload(grant, 'out', b'later', sha(b'old'), key='later')

    def test_permission_rejection_same_write_retry_and_restart(self):
        grant, target, wid = self.attempt()
        error = PermissionError(errno.EACCES, 'DO_NOT_LOG secret/file details')
        error.winerror = 32
        with patch('workbench.shared.os.replace', side_effect=error):
            with self.assertRaisesRegex(WorkbenchError, 'not published.*phase=publication.*winerror=32') as caught:
                self.store.commit_write(wid)
        self.assertNotIn('DO_NOT_LOG', str(caught.exception))
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(self.journal_count(wid), 0)
        failure = self.last_failure(wid)
        self.assertEqual((failure['phase'], failure['error_type'], failure['outcome']), ('publication', 'PermissionError', 'not_published'))
        self.assertNotIn('DO_NOT_LOG', json.dumps(failure))
        self.store = Runtime(self.config).shared
        self.assertEqual(self.store.commit_write(wid)['status'], 'committed')
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(self.store.commit_write(wid)['status'], 'committed')

    def test_create_rejection_keeps_absence_and_same_write_retry(self):
        _, target, wid = self.attempt(create=True)
        with patch('workbench.shared.os.link', side_effect=PermissionError(errno.EACCES, 'rejected')):
            with self.assertRaisesRegex(WorkbenchError, 'not published'):
                self.store.commit_write(wid)
        self.assertFalse(target.exists())
        self.assertEqual(self.journal_count(wid), 0)
        self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(target.stat().st_nlink, 1)

    def test_last_prepublication_read_failure_does_not_leave_barrier(self):
        _, target, wid = self.attempt()
        calls = 0
        def read(d, name):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise PermissionError(errno.EACCES, 'last precheck rejected')
            return read_exact(d, name)
        with patch('workbench.shared.read_exact', side_effect=read), patch('workbench.shared.os.replace') as replace:
            with self.assertRaisesRegex(WorkbenchError, 'not published.*phase=prepublication'):
                self.store.commit_write(wid)
            replace.assert_not_called()
        self.assertEqual(self.journal_count(wid), 0)
        self.assertEqual(target.read_bytes(), b'old')
        self.store.commit_write(wid)

    def test_rejection_with_unverifiable_target_stays_uncertain(self):
        grant, target, wid = self.attempt()
        calls = 0
        def read(d, name):
            nonlocal calls
            calls += 1
            if calls > 3:
                raise PermissionError(errno.EACCES, 'verification blocked')
            return read_exact(d, name)
        with patch('workbench.shared.read_exact', side_effect=read), patch('workbench.shared.os.replace', side_effect=PermissionError(errno.EACCES, 'rejected')):
            with self.assertRaisesRegex(WorkbenchError, 'uncertain'):
                self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(self.journal_count(wid), 1)
        self.assert_blocked(grant, wid)

    def test_rejection_with_changed_target_stays_uncertain(self):
        grant, target, wid = self.attempt()
        def reject(*args, **kw):
            target.write_bytes(b'external')
            raise PermissionError(errno.EACCES, 'rejected')
        with patch('workbench.shared.os.replace', side_effect=reject):
            with self.assertRaisesRegex(WorkbenchError, 'uncertain'):
                self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'external')
        self.assertEqual(self.journal_count(wid), 1)
        self.assert_blocked(grant, wid)

    def test_unknown_io_error_preserves_attempt_across_grant_and_restart(self):
        grant, target, wid = self.attempt()
        with patch('workbench.shared.os.replace', side_effect=OSError(errno.EIO, 'unknown outcome')):
            with self.assertRaisesRegex(WorkbenchError, 'uncertain'):
                self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'old')
        self.store.revoke(grant)
        self.store = Runtime(self.config).shared
        new = self.grant(key='renewed')
        with self.assertRaisesRegex(WorkbenchError, 'uncertain'):
            self.upload(new, 'out', b'later', sha(b'old'), key='later')
        self.assertEqual(self.journal_count(wid), 1)
        result = self.store.resolve_write_locally(wid, sha(b'old'), 'Fixture inspected; never replay')
        self.assertEqual(result['status'], 'resolved_not_replayed')
        self.assertEqual(target.read_bytes(), b'old')

    def test_cleanup_after_publication_preserves_barrier_and_first_phase(self):
        grant, target, wid = self.attempt()
        with patch.object(Directory, 'unlink', side_effect=PermissionError(errno.EACCES, 'cleanup failed')):
            with self.assertRaisesRegex(WorkbenchError, 'uncertain.*phase=temporary_cleanup'):
                self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(self.last_failure(wid)['phase'], 'temporary_cleanup')
        self.assert_blocked(grant, wid)
        with self.runtime.connection() as c:
            first = c.execute('SELECT phase FROM shared_publish_failures WHERE write_id=? ORDER BY id', (wid,)).fetchone()[0]
        self.assertEqual(first, 'temporary_cleanup')
        self.assertEqual(self.journal_count(wid), 1)

    def test_receipt_failure_after_publication_preserves_barrier(self):
        grant, target, wid = self.attempt()
        with patch.object(self.store, 'write_receipt', side_effect=sqlite3.OperationalError('receipt failure')):
            with self.assertRaisesRegex(WorkbenchError, 'uncertain.*phase=database_receipt'):
                self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'new')
        self.assert_blocked(grant, wid)
        self.assertEqual(self.journal_count(wid), 1)

    def test_staging_cleanup_failure_reports_actual_committed_result(self):
        _, target, wid = self.attempt()
        original = Path.unlink
        def unlink(path, *args, **kw):
            if path == self.store.uploads / wid:
                raise PermissionError(errno.EACCES, 'private cleanup failed')
            return original(path, *args, **kw)
        with patch.object(Path, 'unlink', unlink):
            receipt = self.store.commit_write(wid)
        self.assertEqual(receipt['status'], 'committed')
        self.assertIn('staging_cleanup', receipt['warning'])
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(self.last_failure(wid)['outcome'], 'committed')
        self.assertEqual(self.store.commit_write(wid)['status'], 'committed')

    def test_process_interrupt_does_not_autoclear_attempt(self):
        grant, target, wid = self.attempt()
        with patch('workbench.shared.os.replace', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'old')
        self.store = Runtime(self.config).shared
        self.assertEqual(self.journal_count(wid), 1)
        self.assert_blocked(grant, wid)

    def test_error_cleanup_does_not_mask_publication_rejection(self):
        _, target, wid = self.attempt()
        with patch('workbench.shared.os.replace', side_effect=PermissionError(errno.EACCES, 'first rejection')), patch.object(Directory, 'unlink', side_effect=OSError(errno.EIO, 'later cleanup error')):
            with self.assertRaisesRegex(WorkbenchError, 'not published.*phase=publication.*PermissionError'):
                self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(self.journal_count(wid), 0)

    def test_existing_journal_cannot_be_cleared_by_validation_retry(self):
        grant, target, wid = self.attempt()
        with self.runtime.connection(write=True) as c:
            c.execute('INSERT INTO shared_publish_attempts VALUES(?,0)', (wid,))
        self.assert_blocked(grant, wid)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(self.journal_count(wid), 1)

    def test_database_commit_failure_after_publication_stays_uncertain(self):
        grant, target, wid = self.attempt()
        original_connect = sqlite3.connect
        fired = False
        class CommitFailure(sqlite3.Connection):
            def commit(connection):
                nonlocal fired
                row = connection.execute('SELECT state FROM shared_writes WHERE id=?', (wid,)).fetchone()
                if row and row[0] == 'committed' and not fired:
                    fired = True
                    raise sqlite3.OperationalError('injected receipt commit failure')
                super().commit()
        def connect(*args, **kw):
            kw['factory'] = CommitFailure
            return original_connect(*args, **kw)
        with patch('workbench.core.sqlite3.connect', side_effect=connect):
            with self.assertRaisesRegex(WorkbenchError, 'uncertain.*phase=database_receipt'):
                self.store.commit_write(wid)
        self.assertTrue(fired)
        self.assertEqual(target.read_bytes(), b'new')
        self.store = Runtime(self.config).shared
        self.assert_blocked(grant, wid)

    def test_publish_then_unknown_ack_error_stays_uncertain(self):
        grant, target, wid = self.attempt()
        import os
        original = os.replace
        def publish(*args, **kw):
            original(*args, **kw)
            raise OSError(errno.EIO, 'injected unknown acknowledgement')
        with patch('workbench.shared.os.replace', side_effect=publish):
            with self.assertRaisesRegex(WorkbenchError, 'uncertain.*phase=publication'):
                self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'new')
        self.assert_blocked(grant, wid)

    def test_revoke_between_journal_and_publish_never_invokes_filesystem(self):
        grant, target, wid = self.attempt()
        original_connect = sqlite3.connect
        fired = False
        class RevokeAfterJournal(sqlite3.Connection):
            def commit(connection):
                nonlocal fired
                attempted = connection.execute('SELECT 1 FROM shared_publish_attempts WHERE write_id=?', (wid,)).fetchone()
                super().commit()
                if attempted and not fired:
                    fired = True
                    self.store.revoke(grant)
        def connect(*args, **kw):
            kw['factory'] = RevokeAfterJournal
            return original_connect(*args, **kw)
        with patch('workbench.core.sqlite3.connect', side_effect=connect), patch('workbench.shared.os.replace') as replace:
            with self.assertRaisesRegex(WorkbenchError, 'not published.*phase=prepublication'):
                self.store.commit_write(wid)
            replace.assert_not_called()
        self.assertTrue(fired)
        self.assertEqual(self.journal_count(wid), 0)
        self.assertEqual(target.read_bytes(), b'old')
        with self.runtime.connection() as c:
            self.assertEqual(c.execute('SELECT state FROM shared_writes WHERE id=?', (wid,)).fetchone()[0], 'revoked')

    def test_upgrade_adds_diagnostics_without_clearing_old_barrier(self):
        grant, target, wid = self.attempt()
        with self.runtime.connection(write=True) as c:
            c.execute('DROP TABLE shared_publish_failures')
            c.execute('INSERT INTO shared_publish_attempts VALUES(?,0)', (wid,))
        self.store = Runtime(self.config).shared
        self.assert_blocked(grant, wid)
        self.assertEqual(self.journal_count(wid), 1)
        self.assertEqual(target.read_bytes(), b'old')

    def test_revoked_committed_write_still_refuses_commit_receipt(self):
        grant, target, wid = self.attempt()
        self.store.commit_write(wid)
        self.store.revoke(grant)
        with self.assertRaisesRegex(WorkbenchError, 'revoked'):
            self.store.commit_write(wid)
        self.assertEqual(target.read_bytes(), b'new')


if __name__ == '__main__':
    unittest.main()
