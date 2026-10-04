from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
import unittest

from workbench.server import Server


class HTTPFramingTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stderr(self.output)
        self.redirect.__enter__()
        self.server = Server(('127.0.0.1', 0), SimpleNamespace(config={'auth_token': 't' * 48}))
        self.server.mcp = SimpleNamespace(handle=lambda body: {'jsonrpc': '2.0', 'id': body.get('id'), 'result': {}})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)

    def tearDown(self):
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.redirect.__exit__(None, None, None)

    def request(self, body=b'{"id":1}', **headers):
        base = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + 't' * 48}
        base.update(headers)
        self.client.request('POST', '/mcp', body, base)
        response = self.client.getresponse()
        result = response.status, response.getheader('Connection'), response.read()
        return result

    def raw(self, first_line, headers=(), body=b'', half_close=False):
        with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=3) as connection:
            message = (first_line + '\r\nHost: 127.0.0.1:' + str(self.server.server_port) + '\r\n'
                       + '\r\n'.join(headers) + '\r\n\r\n').encode() + body
            connection.sendall(message)
            if half_close:
                connection.shutdown(socket.SHUT_WR)
            result = b''
            while True:
                block = connection.recv(65536)
                if not block:
                    return result
                result += block

    def test_authorized_posts_keep_connection(self):
        self.assertEqual(self.request()[0], 200)
        original = self.client.sock
        self.assertIsNotNone(original)
        status, connection, body = self.request(b'{"id":2}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['id'], 2)
        self.assertNotEqual(connection, 'close')
        self.assertIs(self.client.sock, original)

    def test_early_rejection_closes_and_next_request_succeeds(self):
        cases = [({'Authorization': 'Bearer bad'}, 401),
                 ({'Origin': 'https://invalid.example'}, 403),
                 ({'Content-Type': 'text/plain'}, 400),
                 ({'Content-Length': '1048577'}, 400)]
        for headers, expected in cases:
            with self.subTest(headers=tuple(headers)):
                status, connection, _ = self.request(**headers)
                self.assertEqual(status, expected)
                self.assertEqual(connection, 'close')
                self.assertEqual(self.request()[0], 200)

    def test_consumed_invalid_json_does_not_poison_connection(self):
        self.assertEqual(self.request(b'not-json')[0], 400)
        original = self.client.sock
        self.assertEqual(self.request()[0], 200)
        self.assertIs(self.client.sock, original)

    def test_ambiguous_framing_is_rejected_and_connection_closed(self):
        base = ('Authorization: Bearer ' + 't' * 48, 'Content-Type: application/json')
        for framing in [('Content-Length: 8', 'Transfer-Encoding: chunked'),
                        ('Content-Length: 8', 'Content-Length: 8')]:
            with self.subTest(framing=framing):
                response = self.raw('POST /mcp HTTP/1.1', base + framing, b'{"id":1}')
                self.assertTrue(response.startswith(b'HTTP/1.1 400'))
                self.assertIn(b'Connection: close', response)

    def test_truncated_body_is_rejected(self):
        response = self.raw('POST /mcp HTTP/1.1',
                            ('Authorization: Bearer ' + 't' * 48,
                             'Content-Type: application/json', 'Content-Length: 120'),
                            b'{"id":1}', half_close=True)
        self.assertTrue(response.startswith(b'HTTP/1.1 400'))
        self.assertIn(b'Connection: close', response)

    def test_malformed_request_returns_error_without_logging_payload(self):
        response = self.raw('bad request line too long SECRET_BODY')
        self.assertIn(b'400', response)
        response = self.raw('SECRET_BODY /signed-secret?token=SECRET_TOKEN HTTP/1.1', ('Connection: close',))
        self.assertTrue(response.startswith(b'HTTP/1.1 501'))
        response = self.raw('GET /signed-secret?token=SECRET_TOKEN HTTP/1.1', ('Connection: close',))
        self.assertTrue(response.startswith(b'HTTP/1.1 401'))
        logs = self.output.getvalue()
        self.assertNotIn('SECRET_BODY', logs)
        self.assertNotIn('SECRET_TOKEN', logs)
        self.assertNotIn('signed-secret', logs)
        self.assertNotIn('Traceback', logs)


class DoctorDecodingTests(unittest.TestCase):
    def test_localized_bomless_wsl_output_and_utf8(self):
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'compute_doctor.py'
        spec = importlib.util.spec_from_file_location('compute_doctor_under_test', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = '未安裝Windows 子系統 Linux 版。請先完成系統必要元件安裝並重新啟動。詳細設定方式請參閱安裝說明與系統需求文件。\r\n'
        raw = text.encode('utf-16-le')
        self.assertLessEqual(raw[:512].count(b'\0'), len(raw[:512]) // 5)
        self.assertEqual(module.decode(raw), text)
        self.assertEqual(module.decode('Python 3.14.3：版本'.encode()), 'Python 3.14.3：版本')
        self.assertEqual(module.decode(text.encode('utf-16')), text)


if __name__ == '__main__':
    unittest.main()
