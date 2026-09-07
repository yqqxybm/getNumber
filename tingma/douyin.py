"""One explicitly armed numeric comment in an app-owned Edge live-room tab.

All Playwright objects stay on one worker thread. No private Douyin endpoints,
system-wide Enter key, credentials, cookies or transcript logging are used.
"""
from __future__ import annotations

from dataclasses import dataclass
import queue
import re
import threading
import time
from urllib.parse import urlsplit

from .settings import config_path
from .windows_input import is_safe_insert_text


# Observed in the logged-in public web UI on 2026-09-07. Fail closed if changed.
EDITOR = '#chatInput [data-e2e="live-chatting"] [contenteditable="true"][data-slate-editor="true"]'
SEND = '#chatInput .webcast-chatroom___send-btn'
READ_TEXT = "e => (e.textContent || '').replace(/[\\u200b\\ufeff]/g, '').trim()"


class DouyinError(ValueError):
    pass


def room_url(value):
    """Accept only canonical public live-room URLs, not share/redirect URLs."""
    if not isinstance(value, str) or len(value) > 2048:
        raise DouyinError('请填写抖音网页版直播间的完整链接。')
    try:
        parsed = urlsplit(value.strip())
        valid = (parsed.scheme == 'https' and parsed.netloc == 'live.douyin.com'
                 and re.fullmatch(r'/[0-9]{1,30}/?', parsed.path))
    except ValueError:
        valid = False
    if not valid:
        raise DouyinError('链接格式应为 https://live.douyin.com/直播间数字编号。')
    return 'https://live.douyin.com/' + parsed.path.strip('/')


@dataclass
class BoundRound:
    token: str
    room: str
    page: object
    editor: object
    button: object
    cancelled: threading.Event
    generation: int


class RoomSender:
    """Worker-thread state machine, separately testable without a live browser."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.page = None
        self.room = ''
        self.bound = None
        self.last_attempt = float('-inf')
        self.last_value = ''
        self.recent = {}
        self.uncertain = False
        self.generation = 0
        self.observed_page = None

    def select(self, page, url):
        self.bound = None
        self.page, self.room = page, room_url(url)
        self.generation += 1
        if page is not self.observed_page:
            self.observed_page = page
            def navigated(frame):
                if page is self.page and frame is page.main_frame:
                    self.generation += 1
            page.on('framenavigated', navigated)

    def _page_valid(self):
        if self.page is None or self.page.is_closed():
            raise DouyinError('请先打开专用 Edge 直播间并登录。')
        if room_url(self.page.url) != self.room:
            raise DouyinError('直播间已切换，请重新打开并绑定要发送的直播间。')
        if self.page.evaluate('document.visibilityState') != 'visible':
            raise DouyinError('请切回已绑定的直播间标签页。')

    @staticmethod
    def _usable(element):
        return element is not None and element.is_visible() and element.is_enabled()

    def prepare(self, token, cancelled, expected_room=None):
        self.bound = None
        if cancelled.is_set():
            raise DouyinError('本轮已取消。')
        if self.uncertain:
            raise DouyinError('上次发送状态不明，请检查直播间后点击“我已检查”。')
        if self.clock() - self.last_attempt < 3:
            raise DouyinError('发送间隔至少 3 秒，请稍后再听一轮。')
        self._page_valid()
        if expected_room is not None and room_url(expected_room) != self.room:
            raise DouyinError('填写的直播间尚未打开，请先点击“打开 Edge”。')
        editors, buttons = self.page.query_selector_all(EDITOR), self.page.query_selector_all(SEND)
        if len(editors) != 1 or len(buttons) != 1:
            raise DouyinError('没有找到唯一的弹幕框和发送按钮，请登录或检查页面。')
        editor, button = editors[0], buttons[0]
        if not self._usable(editor) or not editor.is_editable() or not self._usable(button):
            raise DouyinError('弹幕区暂不可用，请处理登录、禁言或页面提示。')
        if editor.evaluate(READ_TEXT):
            raise DouyinError('弹幕框已有内容，请自行发送或清空后再开始。')
        self.bound = BoundRound(token, self.room, self.page, editor, button, cancelled, self.generation)
        return '已绑定直播间 ' + self.room.rsplit('/', 1)[-1]

    def send(self, token, value):
        target = self.bound
        if target is None or target.token != token:
            raise DouyinError('本轮已失效，未发送。')
        self.bound = None  # Consume before any side effect.
        if not is_safe_insert_text(value):
            raise DouyinError('结果不是 1–32 位纯数字，未发送。')
        if self.uncertain:
            raise DouyinError('上次状态尚未检查，未发送。')
        if self.clock() - self.last_attempt < 3:
            raise DouyinError('发送过快，未发送；不会排队补发。')
        if self.clock() - self.recent.get(value, float('-inf')) < 30:
            raise DouyinError('30 秒内相同数字已提交过，已拦截重复弹幕。')
        self._validate(target)
        if target.editor.evaluate(READ_TEXT):
            raise DouyinError('弹幕框已被编辑，未覆盖、未发送。')
        # Pin the actual elements. Re-render/navigation cannot redirect a locator
        # retry to a new composer that happens to have the same selector.
        try:
            target.editor.fill(value, timeout=1500)
            self._validate(target)
            if target.editor.evaluate(READ_TEXT) != value:
                raise DouyinError('填入内容与数字结果不一致。')
        except Exception as error:
            raise DouyinError('未点击发送；请检查弹幕框中是否有草稿。') from error
        if target.cancelled.is_set():
            raise DouyinError('已取消，未点击发送；请检查弹幕草稿。')
        # Once click may have happened, no automatic retry is ever safe.
        self.last_attempt, self.last_value = self.clock(), value
        self.recent = {code: when for code, when in self.recent.items() if self.clock() - when < 30}
        self.recent[value] = self.last_attempt
        self.uncertain = True
        try:
            target.button.click(timeout=1500)
            for _ in range(15):
                target.page.wait_for_timeout(100)
                self._page_valid()
                if not target.editor.evaluate(READ_TEXT):
                    self.uncertain = False
                    return '已提交一次，输入框已清空；请在直播间核对是否显示。'
        except Exception:
            pass
        raise DouyinError('发送状态不明，已暂停；请检查直播间，软件不会自动重发。')

    def _validate(self, target):
        if target.cancelled.is_set():
            raise DouyinError('本轮已取消，未发送。')
        if self.page is not target.page or self.room != target.room:
            raise DouyinError('目标已变化，未发送。')
        self._page_valid()
        if target.generation != self.generation:
            raise DouyinError('直播页面已跳转或刷新，请重新听一轮。')
        if not self._usable(target.editor) or not target.editor.is_editable() or not self._usable(target.button):
            raise DouyinError('弹幕区已变化，未发送。')
        # Element identity and connectivity also protect same-URL reloads.
        if not target.editor.evaluate('e => e.isConnected') or not target.button.evaluate('e => e.isConnected'):
            raise DouyinError('直播页面已刷新，未发送。')

    def acknowledge(self):
        self.uncertain = False


class DouyinBrowser:
    """Lazy dedicated Edge process with its own profile and bounded work queue."""
    def __init__(self, callback):
        self.callback = callback  # (kind, token, ok, message), Qt signal emitter
        self.commands = queue.Queue(maxsize=3)
        self.stopped = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._round_cancelled = None
        self.sender = RoomSender()
        self.context = None
        self.runtime = None
        self.thread = threading.Thread(target=self._run, name='douyin-browser', daemon=True)
        self.thread.start()

    def request(self, kind, token='', payload=None):
        with self._lifecycle_lock:
            if self.stopped.is_set():
                error = '浏览器已关闭，本轮操作未执行。'
            else:
                try:
                    self.commands.put_nowait((kind, token, payload))
                except queue.Full:
                    error = '浏览器正在处理上一项操作，请稍后重试。'
                else:
                    if kind == 'prepare':
                        self._round_cancelled = payload[0]
                    return True
        self._emit(kind, token, False, error)
        return False

    def _emit(self, kind, token, ok, message):
        try:
            self.callback(kind, token, ok, message)
        except RuntimeError:
            pass  # Main window was destroyed.

    def _open(self, url):
        url = room_url(url)
        if self.context is None:
            from playwright.sync_api import sync_playwright
            self.runtime = sync_playwright().start()
            try:
                self.context = self.runtime.chromium.launch_persistent_context(
                    str(config_path().parent / 'DouyinEdge'), channel='msedge',
                    headless=False, no_viewport=True, timeout=20000,
                )
            except Exception:
                self.runtime.stop(); self.runtime = None
                raise DouyinError('无法打开 Edge，请确认 Windows 已安装 Microsoft Edge，且专用窗口已关闭。')
        page = self.sender.page
        if page is None or page.is_closed():
            page = self.context.new_page()
        page.set_default_timeout(2000)
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        page.bring_to_front()
        self.sender.select(page, url)
        return '直播间已打开。请在 Edge 登录并播放直播，再按 F8。'

    def _run(self):
        try:
            while not self.stopped.is_set():
                try:
                    kind, token, payload = self.commands.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self.stopped.is_set():
                    break
                try:
                    if kind == 'open':
                        message = self._open(payload)
                    elif kind == 'prepare':
                        cancelled, url = payload
                        message = self.sender.prepare(token, cancelled, url)
                    elif kind == 'send':
                        message = self.sender.send(token, payload)
                    elif kind == 'acknowledge':
                        self.sender.acknowledge(); message = '已解除暂停；请按 F8 开始新一轮。'
                    else:
                        raise DouyinError('不支持的浏览器操作。')
                    self._emit(kind, token, True, message)
                except DouyinError as error:
                    self._emit(kind, token, False, str(error))
                except Exception:
                    if kind == 'open':
                        # A user-closed context must not poison every future open.
                        if self.context:
                            try:
                                self.context.close()
                            except Exception:
                                pass
                        self.context = None
                        if self.runtime:
                            self.runtime.stop(); self.runtime = None
                        self.sender.page = None
                    self._emit(kind, token, False, '浏览器操作失败，请关闭专用 Edge 窗口并重新打开；本轮不重试。')
        finally:
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
            if self.runtime:
                self.runtime.stop()

    def close(self):
        # The worker consumes sender.bound before clicking. Retain the signal
        # independently so closing never races a read of that mutable target.
        with self._lifecycle_lock:
            self.stopped.set()
            cancelled = self._round_cancelled
            if cancelled is not None:
                cancelled.set()
