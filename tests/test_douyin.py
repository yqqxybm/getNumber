"""Browser state-machine tests. These do not launch Edge or send public chat."""
import threading
import unittest
from unittest.mock import patch

from tingma.douyin import EDITOR, READ_TEXT, RoomSender, DouyinBrowser, DouyinError, room_url


class Element:
    def __init__(self, page, button=False):
        self.page = page; self.button = button; self.text = ''
        self.visible = True; self.enabled = True; self.connected = True
        self.fills = []; self.clicks = 0; self.after_fill = None

    def is_visible(self): return self.visible
    def is_enabled(self): return self.enabled
    def is_editable(self): return self.enabled

    def evaluate(self, expression):
        return self.text if expression == READ_TEXT else self.connected

    def fill(self, value, **kwargs):
        self.fills.append(value); self.text = value
        if self.after_fill: self.after_fill()

    def click(self, **kwargs):
        self.clicks += 1
        if self.page.click_error: raise TimeoutError('unknown')
        if self.page.clears: self.page.editor.text = ''


class Page:
    def __init__(self):
        self.url = 'https://live.douyin.com/12345'
        self.closed = False; self.visibility = 'visible'; self.clears = True
        self.click_error = False; self.editor_count = 1
        self.editor = Element(self); self.button = Element(self, True)
        self.main_frame = object(); self.handler = None

    def on(self, event, handler): self.handler = handler
    def is_closed(self): return self.closed
    def evaluate(self, expression): return self.visibility
    def wait_for_timeout(self, ms): pass
    def query_selector_all(self, selector):
        return [self.editor] * self.editor_count if selector == EDITOR else [self.button]


class DouyinTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.sender = RoomSender(lambda: self.now); self.page = Page()
        self.sender.select(self.page, self.page.url)
        self.stop = threading.Event()

    def prepare(self, token='round'):
        return self.sender.prepare(token, self.stop, self.page.url)

    def test_room_url_rejects_redirects_credentials_and_other_origins(self):
        self.assertEqual('https://live.douyin.com/123', room_url(' https://live.douyin.com/123/?from=web '))
        for url in ('http://live.douyin.com/123', 'https://evil.test/123', 'https://live.douyin.com.evil.test/123', 'https://me@live.douyin.com/123', 'https://live.douyin.com:443/123', 'https://live.douyin.com/', 'https://v.douyin.com/code', 'https://live.douyin.com/１２３', 'https://live.douyin.com/123/../456', None):
            with self.subTest(url=url), self.assertRaises(DouyinError): room_url(url)

    def test_digits_leading_zeros_submit_once_and_do_not_claim_delivery(self):
        self.prepare(); message = self.sender.send('round', '008')
        self.assertEqual(['008'], self.page.editor.fills)
        self.assertEqual(1, self.page.button.clicks); self.assertIn('已提交', message)
        self.assertNotIn('发送成功', message)
        with self.assertRaises(DouyinError): self.sender.send('round', '008')
        self.assertEqual(1, self.page.button.clicks)

    def test_no_login_or_ambiguous_composer_blocks_before_asr(self):
        for count in (0, 2):
            self.page.editor_count = count
            with self.assertRaises(DouyinError): self.prepare()
        self.assertEqual(0, self.page.button.clicks)

    def test_draft_preserved_at_prepare_and_after_recognition(self):
        self.page.editor.text = '用户草稿'
        with self.assertRaises(DouyinError): self.prepare()
        self.page.editor.text = ''; self.prepare(); self.page.editor.text = '新草稿'
        with self.assertRaises(DouyinError): self.sender.send('round', '008')
        self.assertEqual('新草稿', self.page.editor.text); self.assertEqual([], self.page.editor.fills)

    def test_cancel_before_or_after_fill_never_clicks(self):
        self.prepare(); self.stop.set()
        with self.assertRaises(DouyinError): self.sender.send('round', '008')
        self.assertEqual([], self.page.editor.fills)
        self.stop.clear(); self.prepare(); self.page.editor.after_fill = self.stop.set
        with self.assertRaises(DouyinError): self.sender.send('round', '008')
        self.assertEqual(0, self.page.button.clicks)

    def test_room_switch_reload_hidden_tab_closed_or_detached_blocks(self):
        mutations = (
            lambda: setattr(self.page, 'url', 'https://live.douyin.com/999'),
            lambda: self.page.handler(self.page.main_frame),
            lambda: setattr(self.page, 'visibility', 'hidden'),
            lambda: setattr(self.page, 'closed', True),
            lambda: setattr(self.page.editor, 'connected', False),
            lambda: setattr(self.page.button, 'enabled', False),
        )
        for mutate in mutations:
            self.setUp(); self.prepare(); mutate()
            with self.assertRaises(DouyinError): self.sender.send('round', '008')
            self.assertEqual(0, self.page.button.clicks)

    def test_edited_url_field_cannot_send_to_previous_room(self):
        with self.assertRaises(DouyinError): self.sender.prepare('round', self.stop, 'https://live.douyin.com/999')

    def test_non_digits_do_not_reach_composer(self):
        for value in ('12m3', '１２', '١٢', '', '-1', '1.2', '0' * 33, None):
            self.prepare()
            with self.subTest(value=value), self.assertRaises(DouyinError): self.sender.send('round', value)
        self.assertEqual([], self.page.editor.fills)

    def test_tampered_fill_is_not_submitted(self):
        self.prepare(); self.page.editor.after_fill = lambda: setattr(self.page.editor, 'text', '0089')
        with self.assertRaises(DouyinError): self.sender.send('round', '008')
        self.assertEqual(0, self.page.button.clicks)

    def test_click_error_or_no_clear_pauses_without_retry(self):
        for error in (False, True):
            self.setUp(); self.prepare(); self.page.click_error = error; self.page.clears = False
            with self.assertRaisesRegex(DouyinError, '状态不明'): self.sender.send('round', '008')
            self.now += 40
            with self.assertRaises(DouyinError): self.prepare('next')
            self.assertEqual(1, self.page.button.clicks)
            self.sender.acknowledge(); self.page.editor.text = ''; self.prepare('next')
            self.assertEqual(1, self.page.button.clicks)  # Acknowledge never sends.

    def test_rate_limit_and_intervening_code_do_not_defeat_deduplication(self):
        self.prepare(); self.sender.send('round', '008')
        with self.assertRaises(DouyinError): self.prepare('next')
        self.now += 4; self.prepare('next'); self.sender.send('next', '009')
        self.now += 4; self.prepare('third')
        with self.assertRaisesRegex(DouyinError, '重复'): self.sender.send('third', '008')
        self.assertEqual(2, self.page.button.clicks)
        self.now += 30; self.prepare('fourth'); self.sender.send('fourth', '008')
        self.assertEqual(3, self.page.button.clicks)

    def test_stale_token_cannot_consume_new_round(self):
        self.prepare('new')
        with self.assertRaises(DouyinError): self.sender.send('old', '008')
        self.sender.send('new', '008'); self.assertEqual(1, self.page.button.clicks)


class BrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        with patch('tingma.douyin.threading.Thread'):
            self.browser = DouyinBrowser(lambda *event: self.events.append(event))

    def test_close_cancels_round_after_worker_consumed_target(self):
        cancelled = threading.Event()
        self.browser.request('prepare', 'round', (cancelled, 'https://live.douyin.com/12345'))
        self.browser.sender.bound = None  # send() has already consumed the target.
        self.browser.close()
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.browser.stopped.is_set())

    def test_close_never_reads_worker_owned_target(self):
        class UnreadableSender:
            @property
            def bound(self): raise AssertionError('worker owns bound target')
        self.browser.sender = UnreadableSender()
        self.browser.close(); self.browser.close()

    def test_command_dequeued_during_close_is_not_executed(self):
        def closing_get(**kwargs):
            self.browser.close()
            return 'open', '', 'https://live.douyin.com/12345'
        with patch.object(self.browser.commands, 'get', side_effect=closing_get), patch.object(self.browser, '_open') as opening:
            self.browser._run()
        opening.assert_not_called()

    def test_request_after_close_receives_failure_instead_of_waiting_forever(self):
        self.browser.close()
        self.assertFalse(self.browser.request('send', 'round', '008'))
        self.assertEqual(('send', 'round', False), self.events[-1][:3])
        self.assertTrue(self.browser.commands.empty())

    def test_full_queue_keeps_current_round_cancellation(self):
        first, rejected = threading.Event(), threading.Event()
        self.browser.request('prepare', 'first', (first, 'https://live.douyin.com/12345'))
        self.browser.request('acknowledge'); self.browser.request('acknowledge')
        self.assertFalse(self.browser.request('prepare', 'rejected', (rejected, 'https://live.douyin.com/12345')))
        self.browser.close()
        self.assertTrue(first.is_set()); self.assertFalse(rejected.is_set())


if __name__ == '__main__':
    unittest.main()
