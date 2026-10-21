import importlib.util
import ssl
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(path, name, stubs):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


class PipelineSecurityTests(unittest.TestCase):
    def test_wrapper_classification_uses_hostname_not_path_or_query(self):
        parser = types.ModuleType('mrf_parse')
        parser.ingest_one = lambda *args: None
        parser.normalize_source_url = lambda url: url
        batch = load('scripts/batch_ingest.py', 'security_batch', {'mrf_parse': parser})
        for url in ['https://urldefense.com/v3/__url__', 'https://mail.urldefense.com/v3/__url__']:
            self.assertTrue(batch.is_protected_wrapper_url(url))
        for url in ['https://example.test/urldefense.com/file',
                    'https://example.test/?next=https://urldefense.com/file',
                    'https://urldefense.com@other.test/file', 'not-a-url', 'https://[invalid']:
            self.assertFalse(batch.is_protected_wrapper_url(url), url)

    def test_direct_and_proxy_tls_pin_modern_floor_with_verification(self):
        r2 = load('pipeline/vm_r2client.py', 'security_r2', {'r2_put': types.ModuleType('r2_put')})
        for proxy in [None, 'http://proxy.example.test:8080']:
            with self.subTest(proxy=proxy):
                context = ssl.create_default_context()
                # Emulate a platform whose default context permits legacy TLS.
                context.minimum_version = ssl.TLSVersion.TLSv1
                sock = mock.Mock()
                sock.recv.return_value = b'HTTP/1.1 200 Connection established\r\n\r\n'
                with (mock.patch.object(r2, '_proxy', return_value=proxy),
                      mock.patch.object(r2.socket, 'create_connection', return_value=sock),
                      mock.patch.object(r2.ssl, 'create_default_context', return_value=context),
                      mock.patch.object(context, 'wrap_socket', return_value='isolated-tls') as wrap):
                    self.assertEqual(r2._tunnel_socket_inner('r2.example.test'), 'isolated-tls')
                    self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
                    self.assertTrue(context.check_hostname)
                    self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                    self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'r2.example.test')


if __name__ == '__main__':
    unittest.main()
