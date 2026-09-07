import unittest
import threading
from unittest.mock import patch
from urllib.parse import quote, quote_plus
from types import SimpleNamespace
from tingma.cloud_api import ApiConfig, ApiError, CloudSession, _check_http_response, _close_quietly


class CloudCleanupTests(unittest.TestCase):
    def test_redaction_precedes_error_display_truncation(self):
        secret = 'test-only-key-long-enough-to-cross-boundary'
        response = SimpleNamespace(status_code=401, text='x' * 1990 + secret)
        with self.assertRaises(ApiError) as failure:
            _check_http_response(response, secret)
        self.assertNotIn('test-only', str(failure.exception))

    def test_socket_shutdown_does_not_wait_for_close_handshake(self):
        calls = []
        socket = SimpleNamespace(shutdown=lambda: calls.append('shutdown'), close=lambda: calls.append('close'))
        _close_quietly(socket)
        self.assertEqual(['shutdown'], calls)

    def test_key_cannot_be_saved_inside_endpoint_or_model(self):
        secret = 'test-key/with space'
        deep = secret
        for _ in range(5):
            deep = quote(deep, safe='')
        for encoded in (secret, quote(secret, safe=''), quote_plus(secret), deep):
            with self.subTest(encoded=encoded):
                with self.assertRaises(ApiError):
                    ApiConfig('openai_audio', 'https://api.example/v1/' + encoded + '/audio/transcriptions', 'my-model', secret).validate()
                with self.assertRaises(ApiError):
                    ApiConfig('openai_audio', 'https://api.example/v1/audio/transcriptions', 'Bearer ' + encoded, secret).validate()

    def test_cancel_during_connect_cannot_accumulate_pending_handshakes(self):
        entered, release, errored = threading.Event(), threading.Event(), threading.Event()
        calls = []; unexpected = []; errors = []
        def blocked_connect(*args, **kwargs):
            calls.append(1); entered.set(); release.wait(2)
            return SimpleNamespace(close=lambda: None)
        config = ApiConfig('dashscope_streaming', 'wss://api.example/api-ws/v1/inference', 'test-model', 'test-placeholder-key')
        first = CloudSession(config, unexpected.append, unexpected.append, unexpected.append)
        second = CloudSession(config, unexpected.append, unexpected.append, lambda value: (errors.append(value), errored.set()))
        with patch('websocket.create_connection', side_effect=blocked_connect):
            try:
                first.start(); self.assertTrue(entered.wait(1)); first.cancel()
                second.start(); self.assertTrue(errored.wait(1))
                self.assertEqual(1, len(calls))
                self.assertIn('上一轮连接', errors[0])
            finally:
                release.set(); first.cancel(); second.cancel()
                if first._thread: first._thread.join(1)
                if second._thread: second._thread.join(1)
        self.assertEqual([], unexpected)
