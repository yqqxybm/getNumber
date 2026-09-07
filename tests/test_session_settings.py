from array import array
from pathlib import Path
import tempfile
import unittest
from tingma.session import SessionGate, Endpoint
from tingma.settings import Settings, load_settings, save_settings


class SessionTests(unittest.TestCase):
    def test_old_cancelled_result_cannot_write_new_session(self):
        gate = SessionGate(); first = gate.begin()
        self.assertIsNone(gate.begin())
        gate.cancel(); second = gate.begin()
        self.assertFalse(gate.complete(first))
        self.assertTrue(gate.is_current(second))
        self.assertTrue(gate.complete(second))
        self.assertFalse(gate.complete(second))

    def test_pcm_silence_and_voice_endpoint(self):
        endpoint = Endpoint()
        endpoint.feed(bytes(32000)); self.assertFalse(endpoint.usable)
        endpoint.feed(array('h', [2500] * 4000).tobytes())
        self.assertTrue(endpoint.usable); self.assertFalse(endpoint.ended)
        endpoint.feed(bytes(20800)); self.assertTrue(endpoint.ended)

    def test_settings_roundtrip_excludes_unknown_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            save_settings(Settings(model='chosen-model', preview_only=True), path)
            self.assertEqual('chosen-model', load_settings(path).model)
            self.assertNotIn('api_key', path.read_text())
            path.write_text('{"api_key":"test-only-placeholder","max_length":12}')
            settings = load_settings(path)
            self.assertFalse(hasattr(settings, 'api_key'))
            save_settings(settings, path)
            self.assertNotIn('test-only-placeholder', path.read_text())

    def test_invalid_config_returns_safe_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            for value in ['[]', '{', '{"preview_only":"false"}', '{"max_length":true}', '{"min_length":20,"max_length":2}']:
                path.write_text(value)
                self.assertEqual(Settings(), load_settings(path))

    def test_legacy_modes_cannot_restore_letters_or_discard_api_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            for old_mode in ('alphanumeric', 'numeric', 'numbers'):
                path.write_text('{"mode":"' + old_mode + '","model":"my-model","endpoint":"wss://example.test/api-ws/v1/inference","min_length":6,"max_length":6}')
                settings = load_settings(path)
                self.assertFalse(hasattr(settings, 'mode'))
                self.assertEqual('my-model', settings.model)
                self.assertEqual(6, settings.min_length)
                self.assertEqual(6, settings.max_length)
                save_settings(settings, path)
                self.assertNotIn('"mode"', path.read_text())
