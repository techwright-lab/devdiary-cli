import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from devdiary import observer
from devdiary import observer_upload as upload


class UploadTest(unittest.TestCase):
    def install(self):
        observer.apply(observer.plan(self.settings, self.state, self.repo, sys.executable), consent=True)

    def fire(self, event='SessionStart', **extra):
        m = observer.manifest(self.state)
        assert m is not None
        data = dict(session_id='session-1', cwd=str(self.repo), hook_event_name=event, **extra)
        result = subprocess.run([sys.executable, '-I', observer.HOOK_SCRIPT, '--state-dir', str(self.state), '--installation-id', m['installation_id']], input=json.dumps(data).encode(), capture_output=True, timeout=2, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'', b''))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.settings = self.root / 'settings.json'
        self.state = self.root / 'private'
        self.requests = []
        self.status = 201
        self.bad = False
        self.poison = None
        self.poison_status = 409
        self.poison_bad = False
        self.slow = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers['Content-Length']))
                owner.requests.append(raw)
                data = json.loads(raw)
                receipt = {k: data[k] for k in ('observation_id', 'installation_id')}
                receipt.update(collector_ref='collector:test', record_id=1)
                poisoned = owner.poison is not None and data.get('prompt_id') == owner.poison
                if owner.bad or (poisoned and owner.poison_bad):
                    receipt['record_id'] = True
                self.send_response(owner.poison_status if poisoned else owner.status)
                self.send_header('Location', '/redirect')
                self.end_headers()
                raw = json.dumps(receipt).encode()
                try:
                    if owner.slow:
                        for byte in raw:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.05)
                    else:
                        self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}/ingest/v1/observations'
        self.key = self.root / 'key'
        self.key.write_text('dc_live_fixture')
        self.key.chmod(0o600)

    def configure(self, **extra):
        return upload.configure(self.state, endpoint=self.url, collector_ref='collector:test',
                                repository_ref='https://github.com/owner/repo',
                                key_file=self.key, consent=True, **extra)

    def test_upload_unknown_private_exact_retry_and_health(self):
        self.install()
        self.configure()
        self.fire('Stop', prompt_id='p1', prompt='SECRET')
        self.status = 401
        self.assertEqual(upload.sync(self.state)['pending'], 1)
        self.assertEqual(upload.health(self.state)['last_failure'], 'http_401')
        self.status = 200
        self.assertEqual(upload.sync(self.state)['delivered'], 1)
        self.assertEqual(self.requests[0], self.requests[1])
        data = json.loads(self.requests[0])
        self.assertEqual(data['attribution_basis'], 'unknown')
        self.assertTrue(data['observed_at'].endswith('Z'))
        for forbidden in ('repository', 'delivery', 'deduplication', 'source_tag', 'prompt'):
            self.assertNotIn(forbidden, data)
        self.assertNotIn(b'SECRET', self.requests[0])
        self.assertNotIn(str(self.key), (self.state / 'manifest.json').read_text())
        upload.sync(self.state)
        self.assertEqual(len(self.requests), 2)

    def test_wrong_receipt_redirect_conflict_stay_pending(self):
        self.install()
        self.configure()
        self.fire()
        for status, bad, failure in [(201, True, 'invalid_receipt'), (302, False, 'http_302'), (409, False, 'http_409')]:
            self.status, self.bad = status, bad
            self.assertEqual(upload.sync(self.state)['pending'], 1)
            self.assertEqual(upload.health(self.state)['last_failure'], failure)
        self.assertEqual(len(self.requests), 3)

    def test_crash_after_server_acceptance_restart_and_config_conflict(self):
        self.install()
        self.configure()
        self.fire()
        with patch.object(upload, 'acknowledge', side_effect=RuntimeError('crash')), self.assertRaises(RuntimeError):
            upload.sync(self.state)
        self.assertEqual(upload.health(self.state)['pending'], 1)
        with self.assertRaises(ValueError):
            upload.configure(self.state, endpoint=self.url, collector_ref='collector:changed', repository_ref='https://github.com/owner/repo', key_file=self.key, consent=True)
        self.key.write_text('dc_live_rotated')
        self.configure()
        self.status = 200
        result = subprocess.run([sys.executable, '-m', 'devdiary', 'observer', 'sync',
            '--state-dir', str(self.state)], capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['delivered'], 1)
        self.assertEqual(self.requests[0], self.requests[1])

    def test_connection_consent_permissions_and_tls(self):
        self.install()
        with self.assertRaises(ValueError):
            upload.configure(self.state, endpoint=self.url, collector_ref='collector:test', repository_ref='https://github.com/owner/repo', key_file=self.key)
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            self.configure()
        self.key.chmod(0o600)
        self.url = 'http://example.com/ingest/v1/observations'
        with self.assertRaises(ValueError):
            self.configure()
        self.assertEqual(self.requests, [])

    def test_sql_cursor_bounded_batch_and_reset_preserves_receipts(self):
        import sqlite3
        self.install()
        self.configure()
        for i in range(5):
            self.fire('Stop', prompt_id=f'p{i}')
        with sqlite3.connect(self.state / 'observations.sqlite3') as db:
            db.execute("UPDATE observations SET metadata=json_set(metadata, '$.private', ?)", ('SECRET' * 10000,))
        self.assertEqual(upload.sync(self.state, limit=2)['delivered'], 2)
        self.assertEqual(upload.sync(self.state, limit=2)['delivered'], 4)
        (self.state / upload.CONNECTION).unlink()
        with self.assertRaises(ValueError):
            upload.sync(self.state)
        self.configure()
        self.assertEqual(upload.sync(self.state, limit=2)['delivered'], 5)
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(all(b'SECRET' not in raw for raw in self.requests))
        with sqlite3.connect(self.state / 'observations.sqlite3') as db:
            self.assertEqual(db.execute('SELECT seq FROM upload_cursor').fetchone()[0], 5)
        self.assertEqual(observer.health(self.state)['upload']['pending'], 0)

    def test_receipt_contract_rejects_every_identity_and_shape_mismatch(self):
        payload = b'{"observation_id":"o","installation_id":"i"}'
        good = {'observation_id': 'o', 'installation_id': 'i', 'collector_ref': 'c', 'record_id': 1}
        for changes in ({'observation_id': 'other'}, {'installation_id': 'other'},
                        {'collector_ref': 'other'}, {'record_id': 0}, {'record_id': '1'},
                        {'record_id': True}, {'extra': 'SECRET'}):
            with self.assertRaises(ValueError):
                upload.receipt(json.dumps(dict(good, **changes)).encode(), payload, 'c')
        with self.assertRaises(ValueError):
            upload.receipt(b'{"record_id":1,"record_id":2}', payload, 'c')

    def test_cli_configuration_sync_and_safe_failure(self):
        import contextlib
        import io

        from devdiary.cli import main
        self.install()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['observer', 'connect', '--state-dir', str(self.state),
                '--endpoint', self.url, '--collector-ref', 'collector:test',
                '--repository-ref', 'https://github.com/owner/repo', '--key-file', str(self.key), '--consent']), 0)
            self.fire()
            self.status = 401
            self.assertEqual(main(['observer', 'sync', '--state-dir', str(self.state)]), 1)
            self.key.unlink()
            self.assertEqual(main(['observer', 'sync', '--state-dir', str(self.state)]), 2)
        self.assertNotIn(str(self.key), out.getvalue())
        self.assertNotIn('dc_live_', out.getvalue())
        self.assertEqual(upload.health(self.state)['pending'], 1)

    def test_wall_deadline_for_trickling_response(self):
        self.install()
        self.configure()
        self.fire()
        self.slow = True
        started = time.monotonic()
        with patch.object(upload, 'DEADLINE', 0.2):
            result = upload.sync(self.state)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['last_failure'], 'network_failure')

    def test_deterministic_microseconds_and_payload_ceiling(self):
        config = {'repository': '/local', 'installation_id': 'i', 'repository_ref': 'https://github.com/a/b'}
        row = {'repository': '/local', 'installation_id': 'i', 'observed_at': 1700000000.123456}
        self.assertEqual(json.loads(upload.envelope(dict(row), config))['observed_at'], '2023-11-14T22:13:20.123456Z')
        with self.assertRaises(ValueError):
            upload.envelope(dict(row, model='x' * 16384), config)

    def sync_process(self, limit=1):
        result = subprocess.run([sys.executable, '-m', 'devdiary', 'observer', 'sync',
            '--state-dir', str(self.state), '--limit', str(limit)],
            capture_output=True, timeout=10, check=False)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return json.loads(result.stdout)

    def assert_poison_fairness(self):
        import sqlite3
        self.install()
        self.configure()
        for i in range(3):
            self.fire('Stop', prompt_id=f'p{i}')
        self.poison = 'p0'
        for delivered in (0, 1, 2, 2):
            self.assertEqual(self.sync_process()['delivered'], delivered)
        self.assertEqual([json.loads(raw)['prompt_id'] for raw in self.requests],
                         ['p0', 'p1', 'p2', 'p0'])
        self.assertEqual(self.requests[0], self.requests[3])
        with sqlite3.connect(self.state / 'observations.sqlite3') as db:
            rows = db.execute('SELECT payload,delivered,failure FROM upload_outbox ORDER BY seq').fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][0], self.requests[0])
        self.assertEqual(rows[0][1], 0)
        self.assertIsNotNone(rows[0][2])
        self.poison = None
        self.assertEqual(self.sync_process()['delivered'], 3)
        self.assertEqual(self.sync_process()['pending'], 0)
        self.assertEqual(len(self.requests), 5)
        self.fire('Stop', prompt_id='new')
        self.assertEqual(self.sync_process()['delivered'], 4)
        self.assertEqual(json.loads(self.requests[-1])['prompt_id'], 'new')

    def test_poison_conflict_rotates_across_cli_restarts(self):
        self.assert_poison_fairness()

    def test_poison_receipt_rotates_across_cli_restarts(self):
        self.poison_status, self.poison_bad = 201, True
        self.assert_poison_fairness()

    def test_poison_does_not_abort_bounded_batch_or_repeat_within_it(self):
        self.install()
        self.configure()
        for i in range(3):
            self.fire('Stop', prompt_id=f'p{i}')
        self.poison = 'p0'
        self.assertEqual(upload.sync(self.state, limit=100)['delivered'], 2)
        self.assertEqual(len(self.requests), 3)
        upload.sync(self.state, limit=100)
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(self.requests[0], self.requests[-1])

    def test_batch_wraps_without_replaying_delivered_rows(self):
        self.install()
        self.configure()
        for i in range(3):
            self.fire('Stop', prompt_id=f'p{i}')
        self.status = 401
        upload.sync(self.state)
        self.status, self.poison = 201, 'p1'
        self.assertEqual(upload.sync(self.state, limit=1)['delivered'], 0)
        self.assertEqual(upload.sync(self.state, limit=2)['delivered'], 2)
        self.assertEqual([json.loads(raw)['prompt_id'] for raw in self.requests],
                         ['p0', 'p1', 'p2', 'p0'])
        self.assertEqual(upload.sync(self.state, limit=100)['pending'], 1)
        self.assertEqual(json.loads(self.requests[-1])['prompt_id'], 'p1')
        self.assertEqual(len(self.requests), 5)

    def test_global_failure_stops_batch_but_advances_on_restart(self):
        self.install()
        self.configure()
        for i in range(3):
            self.fire('Stop', prompt_id=f'p{i}')
        self.status = 401
        for i in range(4):
            self.assertEqual(self.sync_process(limit=100)['pending'], 3)
            self.assertEqual(len(self.requests), i + 1)
        self.assertEqual([json.loads(raw)['prompt_id'] for raw in self.requests],
                         ['p0', 'p1', 'p2', 'p0'])
        with patch.object(upload, 'post', return_value=(None, 'network_failure')) as post:
            self.assertEqual(upload.sync(self.state)['pending'], 3)
            post.assert_called_once()
            self.assertEqual(json.loads(post.call_args.args[2])['prompt_id'], 'p1')
        self.status = 201
        self.assertEqual(self.sync_process()['delivered'], 1)
        self.assertEqual(json.loads(self.requests[-1])['prompt_id'], 'p2')

    def test_migrates_existing_outbox_and_advances_before_crash(self):
        import sqlite3
        self.install()
        self.configure()
        for i in range(3):
            self.fire('Stop', prompt_id=f'p{i}')
        with sqlite3.connect(self.state / 'observations.sqlite3') as db:
            upload.freeze(db, upload.connection(self.state), 100)
            db.execute('DROP TABLE IF EXISTS upload_attempt_cursor')
            before = db.execute('SELECT * FROM upload_outbox ORDER BY seq').fetchall()
        with patch.object(upload, 'post', side_effect=RuntimeError('crash')), self.assertRaises(RuntimeError):
            upload.sync(self.state, limit=1)
        with sqlite3.connect(self.state / 'observations.sqlite3') as db:
            self.assertEqual(db.execute('SELECT * FROM upload_outbox ORDER BY seq').fetchall(), before)
        self.assertEqual(self.sync_process()['delivered'], 1)
        self.assertEqual(json.loads(self.requests[0])['prompt_id'], 'p1')

    def test_concurrent_uploader_cannot_advance_cursor(self):
        import sqlite3
        self.install()
        self.configure()
        self.fire('Stop', prompt_id='p0')
        with observer.locked(self.state / 'uploader'):
            result = subprocess.run([sys.executable, '-m', 'devdiary', 'observer', 'sync',
                '--state-dir', str(self.state)], capture_output=True, timeout=10, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(self.requests, [])
            with sqlite3.connect(self.state / 'observations.sqlite3') as db:
                self.assertEqual(db.execute('SELECT seq FROM upload_attempt_cursor').fetchone()[0], 0)
        self.assertEqual(self.sync_process()['delivered'], 1)

    def test_codex_turn_id_preserved(self):
        observer.apply(observer.plan(self.settings, self.state, self.repo, __import__('sys').executable, vendor='codex'), consent=True)
        self.configure()
        self.fire('Stop', turn_id='turn-one')
        upload.sync(self.state)
        self.assertEqual(json.loads(self.requests[0])['turn_id'], 'turn-one')
