"""Qt controller integration with fake audio, cloud and focused targets.

These tests do not claim Windows device/input or real cloud verification.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from array import array
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog
from tingma.app import MainWindow, ApiDialog, STYLE
from tingma.settings import Settings


class FakeCloud:
    instances = []

    def __init__(self, config, partial, final, error):
        self.config, self.partial, self.final, self.error = config, partial, final, error
        self.audio = []; self.cancelled = False; self.finished = False
        self.instances.append(self)

    def start(self):
        pass

    def feed(self, pcm):
        self.audio.append(pcm)

    def finish(self):
        self.finished = True

    def cancel(self):
        self.cancelled = True


class FakeTarget:
    description = '测试输入框'

    def __init__(self):
        self.changed = False; self.writes = []

    def validate(self):
        if self.changed:
            raise RuntimeError('输入框已变化')

    def insert(self, text):
        self.validate(); self.writes.append(text)


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setStyleSheet(STYLE)

    def setUp(self):
        self.window = MainWindow(demo=True, settings=Settings(protocol='qwen_realtime', endpoint='wss://example.test/realtime', model='test-model'))
        self.window.api_key = 'test-only-placeholder'
        self.window.audio = object()
        self.target = FakeTarget()
        self.cloud_patch = patch('tingma.app.CloudSession', FakeCloud); self.cloud_patch.start()
        self.focus_patch = patch('tingma.app.FocusTarget.capture', return_value=self.target); self.focus_patch.start()

    def tearDown(self):
        self.window.audio = None
        with patch('tingma.app.save_settings'):
            self.window.close()
        self.window.deleteLater(); self.qt.processEvents()
        self.cloud_patch.stop(); self.focus_patch.stop()

    def test_partial_never_writes_and_final_is_normalized_once(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        cloud.partial('两个 m'); self.qt.processEvents()
        self.assertEqual([], self.target.writes)
        cloud.final('两个 m 零零八'); self.qt.processEvents()
        cloud.final('两个 m 零零八'); self.qt.processEvents()
        self.assertEqual(['mm008'], self.target.writes)
        self.assertTrue(cloud.cancelled)

    def test_cancelled_callbacks_and_audio_cannot_enter_next_round(self):
        self.window.arm(); first = FakeCloud.instances[-1]; token = self.window.gate.active_id
        self.window.audio_queue.put((token, b'\x01\x00' * 320))
        self.window.cancel(); self.window.arm(); second = FakeCloud.instances[-1]
        first.final('123'); self.qt.processEvents(); self.window._drain_audio()
        self.assertEqual([], self.target.writes)
        self.assertEqual([], second.audio)
        second.final('456'); self.qt.processEvents()
        self.assertEqual(['456'], self.target.writes)

    def test_changed_target_and_ambiguous_phrase_never_write(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        self.target.changed = True
        cloud.final('123'); self.qt.processEvents()
        self.assertEqual([], self.target.writes)
        self.assertIn('未自动填入', self.window.status.text())
        self.target.changed = False
        self.window.arm(); FakeCloud.instances[-1].final('一百二'); self.qt.processEvents()
        self.assertEqual([], self.target.writes)
        self.assertFalse(self.window.copy.isEnabled())

    def test_preview_round_does_not_capture_or_write_target(self):
        self.window.preview.setChecked(True)
        with patch('tingma.app.FocusTarget.capture', side_effect=AssertionError('must not capture')):
            self.window.arm()
        FakeCloud.instances[-1].final('两个 m'); self.qt.processEvents()
        self.assertEqual('mm', self.window.result.text())
        self.assertEqual([], self.target.writes)

    def test_silence_finishes_streaming_clip_after_voice(self):
        self.window.arm(); token = self.window.gate.active_id; cloud = FakeCloud.instances[-1]
        voice = array('h', [2500] * 4000).tobytes()
        self.window.audio_queue.put((token, voice))
        self.window.audio_queue.put((token, bytes(20800)))
        self.window._drain_audio()
        self.assertTrue(cloud.finished)
        self.assertFalse(self.window.collecting)
        self.assertEqual([], self.target.writes)

    def test_missing_key_does_not_capture_focus_or_start_request(self):
        self.window.api_key = ''
        before = len(FakeCloud.instances)
        with patch('tingma.app.FocusTarget.capture', side_effect=AssertionError('must not capture')):
            self.window.arm()
        self.assertIsNone(self.window.gate.active_id)
        self.assertEqual(before, len(FakeCloud.instances))
        self.assertIn('Key', self.window.status.text())

    def test_api_dialog_accepts_custom_endpoint_model_key_without_disk_write(self):
        dialog = ApiDialog(self.window.settings, '', self.window)
        dialog.endpoint.setText('wss://workspace.example.test/realtime')
        dialog.model.setText('my-official-model'); dialog.key.setText('test-only-custom-key')
        with patch('tingma.app.save_settings') as save:
            dialog._save()
        self.assertEqual(QDialog.DialogCode.Accepted, dialog.result())
        self.assertEqual('my-official-model', dialog.settings.model)
        self.assertEqual('test-only-custom-key', dialog.api_key)
        self.assertFalse(hasattr(dialog.settings, 'api_key'))
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
