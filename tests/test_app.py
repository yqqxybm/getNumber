"""Qt controller integration with fake audio, cloud and focused targets.

These tests do not claim Windows device/input or real cloud verification.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from array import array
from dataclasses import replace
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog
from tingma.app import MainWindow, ApiDialog, STYLE
from tingma.settings import Settings


class FakeCloud:
    instances = []

    def __init__(self, config, partial, final, error, *, on_segment=None):
        self.config, self.partial, self.final, self.error = config, partial, final, error
        self.segment = on_segment
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


class FakeBrowser:
    def __init__(self): self.requests = []
    def request(self, kind, token='', payload=None):
        self.requests.append((kind, token, payload)); return True
    def close(self): pass


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
        cloud.partial('扣两个零'); self.qt.processEvents()
        self.assertEqual([], self.target.writes)
        cloud.final('扣两个零，八'); self.qt.processEvents()
        cloud.final('扣两个零，八'); self.qt.processEvents()
        self.assertEqual(['008'], self.target.writes)
        self.assertTrue(cloud.cancelled)

    def test_cancelled_callbacks_and_audio_cannot_enter_next_round(self):
        self.window.arm(); first = FakeCloud.instances[-1]; token = self.window.gate.active_id
        self.window.audio_queue.put((token, b'\x01\x00' * 320))
        self.window.cancel(); self.window.arm(); second = FakeCloud.instances[-1]
        first.final('扣123'); self.qt.processEvents(); self.window._drain_audio()
        self.assertEqual([], self.target.writes)
        self.assertEqual([], second.audio)
        second.final('扣456'); self.qt.processEvents()
        self.assertEqual(['456'], self.target.writes)

    def test_live_prompt_final_fills_only_the_digits_once(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        cloud.partial('飘一个数字9'); self.qt.processEvents()
        self.assertEqual([], self.target.writes)
        for _ in range(2):
            cloud.final('飘一个数字9'); self.qt.processEvents()
        self.assertEqual(['9'], self.target.writes)

    def test_live_prompt_final_uses_the_same_digits_for_web_submission(self):
        self.web_mode(); self.window.arm(); token = self.window.gate.active_id
        self.window._web_event('prepare', token, True, '已绑定')
        cloud = FakeCloud.instances[-1]
        cloud.partial('扣一个数字零零八'); self.qt.processEvents()
        self.assertEqual(1, len(self.window.browser.requests))
        for _ in range(2):
            cloud.final('扣一个数字零零八'); self.qt.processEvents()
        self.assertEqual([('send', token, '008')], self.window.browser.requests[1:])
        self.assertEqual([], self.target.writes)

    def test_every_final_path_requires_kou_or_piao_even_after_capture_limit(self):
        for protocol in ('qwen_realtime', 'dashscope_streaming', 'openai_audio'):
            self.window.settings = replace(self.window.settings, protocol=protocol,
                endpoint='https://example.test/audio/transcriptions' if protocol == 'openai_audio'
                else 'wss://example.test/api-ws/v1/inference' if protocol == 'dashscope_streaming'
                else 'wss://example.test/realtime')
            for text in ('29', '00', '数字是00', '打个数字00', '发00', '这件29'):
                with self.subTest(protocol=protocol, text=text):
                    self.window.arm(); cloud = FakeCloud.instances[-1]
                    self.window.endpoint.feed(array('h', [2500] * 8000).tobytes())
                    self.window._finish_audio()
                    cloud.final(text); self.qt.processEvents()
                    self.assertEqual([], self.target.writes)
                    self.assertFalse(self.window.copy.isEnabled())

    def test_uncued_web_final_and_trial_cannot_offer_digits(self):
        self.web_mode(); self.window.arm(); token = self.window.gate.active_id
        self.window._web_event('prepare', token, True, '已绑定')
        FakeCloud.instances[-1].final('00'); self.qt.processEvents()
        self.assertEqual(1, len(self.window.browser.requests))
        self.assertEqual('prepare', self.window.browser.requests[0][0])
        self.window.trial.setText('00'); self.window._trial()
        self.assertFalse(self.window.copy.isEnabled())
        self.assertEqual('无法确认', self.window.result.text())

    def test_committed_cue_ends_round_and_pastes_without_waiting_for_session_end(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        self.assertIn(self.target.description, self.window.input_target.text())
        cloud.partial('这件29，扣一个0'); self.qt.processEvents()
        self.assertTrue(self.window.collecting)
        self.assertEqual([], self.target.writes)
        cloud.segment('这件29，扣一个00'); self.qt.processEvents()
        self.assertEqual(['00'], self.target.writes)
        self.assertFalse(self.window.collecting)
        self.assertIsNone(self.window.gate.active_id)
        self.assertTrue(cloud.cancelled)
        self.assertFalse(cloud.finished)
        self.assertIn('Ctrl+V', self.window.input_target.text())
        cloud.final('这件29，扣一个00'); self.qt.processEvents()
        self.assertEqual(['00'], self.target.writes)

    def test_multiplication_waits_for_complete_final_then_pastes_product_once(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        for text in ('这件30扣一个2', '这件30扣一个2乘', '这件30扣一个2乘3'):
            cloud.partial(text); self.qt.processEvents()
            self.assertTrue(self.window.collecting)
            self.assertEqual([], self.target.writes)
        cloud.segment('这件30扣一个2乘'); self.qt.processEvents()
        self.assertTrue(self.window.collecting)
        cloud.segment('这件30扣一个2乘3'); self.qt.processEvents()
        self.assertEqual(['6'], self.target.writes)
        self.assertFalse(self.window.collecting)
        self.assertTrue(cloud.cancelled)
        self.assertFalse(cloud.finished)
        cloud.final('这件30扣一个2乘3'); self.qt.processEvents()
        self.assertEqual(['6'], self.target.writes)

    def test_narrative_boundary_stops_audio_then_uses_final_recognition(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        self.window.endpoint.feed(array('h', [2500] * 8000).tobytes())
        cloud.partial('29一件飘一个9'); self.qt.processEvents()
        self.assertTrue(self.window.collecting)
        cloud.partial('29一件飘一个9全'); self.qt.processEvents()
        self.assertFalse(self.window.collecting)
        self.assertTrue(cloud.finished)
        self.assertFalse(cloud.cancelled)
        self.assertEqual([], self.target.writes)
        # Even after stopping capture, the provider may correct its interim 9.
        cloud.final('29一件飘一个8全羊毛'); self.qt.processEvents()
        self.assertEqual(['8'], self.target.writes)
        cloud.final('29一件飘一个8全羊毛'); self.qt.processEvents()
        self.assertEqual(['8'], self.target.writes)

    def test_arithmetic_continues_through_operators_then_stops_at_description(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        self.window.endpoint.feed(array('h', [2500] * 8000).tobytes())
        for text in ('扣2', '扣2加', '扣2加3', '扣2加3乘', '扣2加3乘4'):
            cloud.partial(text); self.qt.processEvents()
            self.assertTrue(self.window.collecting)
            self.assertEqual([], self.target.writes)
        cloud.partial('扣2加3乘4全羊毛'); self.qt.processEvents()
        self.assertFalse(self.window.collecting)
        self.assertTrue(cloud.finished)
        self.assertEqual([], self.target.writes)
        cloud.segment('扣2加3乘4全羊毛'); self.qt.processEvents()
        self.assertEqual(['14'], self.target.writes)

    def test_narrative_final_pastes_only_payload_and_invalid_math_never_writes(self):
        for text, value in (('29一件飘一个9全羊毛', '9'), ('扣00全羊毛', '00'),
                            ('扣2加3乘4全羊毛', '14'), ('扣9除3减1全羊毛', '2'),
                            ('飘9或者8', '9'), ('飘一个数字9元', '9')):
            self.window.arm(); cloud = FakeCloud.instances[-1]
            cloud.segment(text); self.qt.processEvents()
            self.assertTrue(self.target.writes, text)
            self.assertEqual(value, self.target.writes[-1])
            self.assertFalse(self.window.collecting)
        count = len(self.target.writes)
        for text in ('扣2乘全羊毛', '扣2加全羊毛', '扣1除0全羊毛', '扣1除2全羊毛',
                     '扣2减3全羊毛', '扣2.5加3全羊毛'):
            self.window.arm(); FakeCloud.instances[-1].final(text); self.qt.processEvents()
            self.assertEqual(count, len(self.target.writes))
            self.assertFalse(self.window.copy.isEnabled())

    def test_noncue_or_invalid_committed_sentences_do_not_end_round(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        for text in ('这件29。', '29', '扣一个', '不要扣00', '扣2乘'):
            cloud.segment(text); self.qt.processEvents()
            self.assertTrue(self.window.collecting)
            self.assertEqual([], self.target.writes)

    def test_committed_cue_web_submission_and_cancelled_segment_isolation(self):
        self.web_mode(); self.window.arm(); token = self.window.gate.active_id
        self.window._web_event('prepare', token, True, '已绑定')
        cloud = FakeCloud.instances[-1]
        cloud.segment('这件29，飘一个00'); self.qt.processEvents()
        cloud.final('这件29，飘一个00'); self.qt.processEvents()
        self.assertEqual([('send', token, '00')], self.window.browser.requests[1:])
        self.window._web_event('send', token, True, '已提交一次')
        self.window.arm(); next_token = self.window.gate.active_id
        self.window._web_event('prepare', next_token, True, '已绑定')
        self.window.cancel()
        FakeCloud.instances[-1].segment('扣11'); self.qt.processEvents()
        self.assertEqual(1, sum(kind == 'send' for kind, _, _ in self.window.browser.requests))

    def test_preview_committed_cue_finishes_without_paste(self):
        self.window.preview.setChecked(True)
        self.window.arm(); cloud = FakeCloud.instances[-1]
        cloud.segment('这件29，扣一个00'); self.qt.processEvents()
        self.assertEqual('00', self.window.result.text())
        self.assertEqual([], self.target.writes)
        self.assertFalse(self.window.collecting)

    def test_changed_target_and_ambiguous_phrase_never_write(self):
        self.window.arm(); cloud = FakeCloud.instances[-1]
        self.target.changed = True
        cloud.final('扣123'); self.qt.processEvents()
        self.assertEqual([], self.target.writes)
        self.assertIn('未自动填入', self.window.status.text())
        self.target.changed = False
        self.window.arm(); FakeCloud.instances[-1].final('扣一百二'); self.qt.processEvents()
        self.assertEqual([], self.target.writes)
        self.assertFalse(self.window.copy.isEnabled())

    def test_preview_round_does_not_capture_or_write_target(self):
        self.window.preview.setChecked(True)
        with patch('tingma.app.FocusTarget.capture', side_effect=AssertionError('must not capture')):
            self.window.arm()
        FakeCloud.instances[-1].final('扣三个八'); self.qt.processEvents()
        self.assertEqual('888', self.window.result.text())
        self.assertEqual([], self.target.writes)

    def test_contaminated_or_non_integer_transcript_never_fills(self):
        for text in ('12m3', '两个m', 'O08', '1.2', '负十二', '12或34', '不是123，是456',
                     '不要飘一个数字9'):
            with self.subTest(text=text):
                self.window.arm(); FakeCloud.instances[-1].final(text); self.qt.processEvents()
                self.assertEqual([], self.target.writes)
                self.assertFalse(self.window.copy.isEnabled())

    def test_digit_gate_also_rejects_a_bad_normalizer_result(self):
        for value in ('12a3', '１２', '١٢', '-12', '1.2', '', '1' * 17):
            with self.subTest(value=value):
                self.window.arm()
                with patch('tingma.app.normalize_cued', return_value={'accepted': True, 'value': value, 'reason': 'ok', 'changes': []}):
                    FakeCloud.instances[-1].final('扣123'); self.qt.processEvents()
                self.assertEqual([], self.target.writes)
                self.assertFalse(self.window.copy.isEnabled())

    def test_length_failure_does_not_pad_and_zeros_survive_valid_round(self):
        self.window.minimum.setValue(6); self.window.maximum.setValue(6)
        self.window.arm(); FakeCloud.instances[-1].final('扣0012'); self.qt.processEvents()
        self.assertEqual([], self.target.writes)
        self.window.arm(); FakeCloud.instances[-1].final('扣000012'); self.qt.processEvents()
        self.assertEqual(['000012'], self.target.writes)

    def test_identical_codes_in_distinct_user_rounds_are_not_deduplicated(self):
        for _ in range(2):
            self.window.arm(); FakeCloud.instances[-1].final('扣0008'); self.qt.processEvents()
        self.assertEqual(['0008', '0008'], self.target.writes)

    def web_mode(self):
        self.window.destination.setCurrentIndex(1)
        self.window.room.setText('https://live.douyin.com/12345')
        self.window.auto_send.setChecked(True)
        self.window.browser = FakeBrowser()

    def test_web_checks_target_before_asr_then_sends_only_final_once(self):
        self.web_mode(); before = len(FakeCloud.instances)
        with patch('tingma.app.FocusTarget.capture', side_effect=AssertionError('web uses bound DOM')):
            self.window.arm()
        token = self.window.gate.active_id
        self.assertEqual(before, len(FakeCloud.instances))
        self.assertEqual('prepare', self.window.browser.requests[0][0])
        self.window._web_event('prepare', token, True, '已绑定直播间 12345')
        cloud = FakeCloud.instances[-1]; cloud.partial('扣两个零'); self.qt.processEvents()
        self.assertEqual(1, len(self.window.browser.requests))
        cloud.final('扣两个零，八'); self.qt.processEvents()
        cloud.final('扣008'); self.qt.processEvents()
        self.assertEqual(('send', token, '008'), self.window.browser.requests[1])
        self.assertEqual(2, len(self.window.browser.requests))
        self.assertEqual([], self.target.writes)
        self.assertFalse(self.window.destination.isEnabled())
        self.window._web_event('send', token, True, '已提交一次')
        self.assertIsNone(self.window.pending_send)
        self.assertTrue(self.window.destination.isEnabled())

    def test_web_cancelled_preflight_and_cloud_callbacks_do_not_send(self):
        self.web_mode(); before = len(FakeCloud.instances); self.window.arm()
        token = self.window.gate.active_id; event = self.window.web_cancel
        self.window.cancel(); self.assertTrue(event.is_set())
        self.window._web_event('prepare', token, True, '已绑定')
        self.assertEqual(before, len(FakeCloud.instances))
        self.window.arm(); token2 = self.window.gate.active_id
        self.window._web_event('prepare', token2, True, '已绑定')
        cloud = FakeCloud.instances[-1]; self.window.cancel(); cloud.final('扣008'); self.qt.processEvents()
        self.assertTrue(all(kind != 'send' for kind, _, _ in self.window.browser.requests))

    def test_web_preview_and_off_switch_cannot_send(self):
        self.web_mode(); self.window.auto_send.setChecked(False)
        before = len(FakeCloud.instances); self.window.arm()
        self.assertIsNone(self.window.gate.active_id); self.assertEqual(before, len(FakeCloud.instances))
        self.window.preview.setChecked(True); self.window.arm()
        FakeCloud.instances[-1].final('扣008'); self.qt.processEvents()
        self.assertEqual([], self.window.browser.requests); self.assertEqual([], self.target.writes)

    def test_cancel_during_web_send_blocks_new_round_until_reply(self):
        self.web_mode(); self.window.arm(); token = self.window.gate.active_id
        self.window._web_event('prepare', token, True, '已绑定')
        FakeCloud.instances[-1].final('扣008'); self.qt.processEvents()
        event = self.window.web_cancel; self.window.arm()
        self.assertTrue(event.is_set()); self.assertEqual(token, self.window.pending_send)
        self.assertEqual(2, len(self.window.browser.requests))
        self.window._web_event('send', token, False, '发送状态不明，已暂停')
        self.assertTrue(self.window.ack_send.isEnabled()); self.assertIsNone(self.window.pending_send)
        self.window._ack_send()
        self.assertEqual('acknowledge', self.window.browser.requests[-1][0])

    def test_web_invalid_recognition_and_failed_preflight_never_send(self):
        self.web_mode(); self.window.arm(); token = self.window.gate.active_id
        self.window._web_event('prepare', token, False, '请登录')
        self.assertIsNone(self.window.gate.active_id)
        self.window.arm(); token = self.window.gate.active_id
        self.window._web_event('prepare', token, True, '已绑定')
        FakeCloud.instances[-1].final('12m3'); self.qt.processEvents()
        self.assertTrue(all(kind != 'send' for kind, _, _ in self.window.browser.requests))
        self.assertTrue(self.window.auto_send.isEnabled())

    def test_streaming_pause_after_price_keeps_listening_for_cue(self):
        self.window.arm(); token = self.window.gate.active_id; cloud = FakeCloud.instances[-1]
        voice = array('h', [2500] * 4000).tobytes()
        self.window.audio_queue.put((token, voice))
        self.window.audio_queue.put((token, bytes(20800)))
        self.window._drain_audio()
        self.assertFalse(cloud.finished)
        self.assertTrue(self.window.collecting)
        cloud.segment('这件29。'); self.qt.processEvents()
        self.assertTrue(self.window.collecting)
        cloud.segment('这件29。扣一个00。'); self.qt.processEvents()
        self.assertEqual(['00'], self.target.writes)
        self.assertFalse(self.window.collecting)

    def test_http_clip_still_finishes_after_silence(self):
        self.window.settings = replace(self.window.settings, protocol='openai_audio', endpoint='https://example.test/audio/transcriptions')
        self.window.arm(); token = self.window.gate.active_id; cloud = FakeCloud.instances[-1]
        self.window.audio_queue.put((token, array('h', [2500] * 4000).tobytes()))
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
