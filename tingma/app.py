"""Windows desktop workflow; cloud callbacks never write directly to a control."""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QFont, QCloseEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QLineEdit, QComboBox, QSpinBox, QCheckBox, QDialog,
    QFormLayout, QDialogButtonBox, QFrame, QScrollArea,
)

from backend.normalizer import normalize_cued, has_cued_payload_boundary
from .cloud_api import ApiConfig, ApiError, CloudSession, test_connection
from .session import SessionGate, Endpoint
from .settings import PRESETS, Settings, load_settings, save_settings
from .windows_audio import LoopbackAudio
from .windows_input import FocusTarget, is_safe_insert_text
from .douyin import DouyinBrowser, room_url


STYLE = """
QWidget { color: #172e35; font-family: 'Microsoft YaHei UI', 'PingFang SC'; font-size: 13px; }
QMainWindow, QDialog { background: #f4f7f5; }
QLabel#eyebrow { color: #647a7a; font-size: 11px; }
QLabel#title { font-size: 28px; font-weight: 700; }
QLabel#result { font-size: 44px; font-weight: 600; color: #087e79; }
QLabel#muted { color: #637577; }
QFrame#card { background: white; border: 1px solid #dce6e2; border-radius: 14px; }
QPushButton { background: white; border: 1px solid #cddbd6; border-radius: 7px; padding: 9px 13px; }
QPushButton:hover { background: #e5f2ec; }
QPushButton:disabled { color: #9ca8a6; background: #edf1ee; }
QPushButton#primary { background: #087e79; color: white; border: 0; font-weight: 600; }
QPushButton#primary:disabled { background: #89b7af; }
QPushButton#float { background: #087e79; color: white; border: 1px solid #8ac5bb; border-radius: 18px; padding: 14px 24px; font-size: 17px; font-weight: 600; }
QLineEdit, QComboBox, QSpinBox { background: white; border: 1px solid #cad9d3; border-radius: 6px; padding: 7px; }
QCheckBox { spacing: 7px; }
"""


class Events(QObject):
    partial = Signal(str, str)
    segment = Signal(str, str)
    final = Signal(str, str)
    error = Signal(str, str)
    audio_ready = Signal(object, str)
    audio_error = Signal(str)
    connection = Signal(str, bool)
    web = Signal(str, str, bool, str)


class ApiDialog(QDialog):
    def __init__(self, settings, api_key, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.api_key = api_key
        self.setWindowTitle('API 设置 · 听码')
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        title = QLabel('连接你的语音 API'); title.setObjectName('title')
        layout.addWidget(title)
        note = QLabel('点击“我想要”后，本轮系统播放的音频会发送到下方地址。\n请使用服务商控制台中与 Key 相同地域、工作空间的接口地址。')
        note.setWordWrap(True); note.setObjectName('muted'); layout.addWidget(note)
        form = QFormLayout(); form.setSpacing(12); layout.addLayout(form)
        self.protocol = QComboBox()
        for code, preset in PRESETS.items():
            self.protocol.addItem(preset[0], code)
        self.protocol.setCurrentIndex(self.protocol.findData(settings.protocol))
        self.endpoint = QLineEdit(settings.endpoint)
        self.model = QLineEdit(settings.model)
        self.key = QLineEdit(api_key); self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('粘贴你申请的 API Key，仅本次运行保留')
        self.key.setMaxLength(4096)
        form.addRow('接口协议', self.protocol)
        form.addRow('完整 API 地址', self.endpoint)
        form.addRow('模型名称', self.model)
        form.addRow('API Key', self.key)
        self.protocol.currentIndexChanged.connect(self._preset)
        self.reveal = QCheckBox('显示 Key（默认不保存到磁盘）')
        self.reveal.toggled.connect(lambda checked: self.key.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        layout.addWidget(self.reveal)
        self.test = QPushButton('测试连接'); self.test.clicked.connect(self._test)
        layout.addWidget(self.test, alignment=Qt.AlignmentFlag.AlignLeft)
        self.message = QLabel('连接测试不上传音频；HTTP 测试只检查认证和模型列表。')
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True); self.message.setObjectName('muted'); layout.addWidget(self.message)
        self.events = Events(self); self.events.connection.connect(self._tested)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('保存设置')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self._save); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _preset(self):
        _, endpoint, model = PRESETS[self.protocol.currentData()]
        self.endpoint.setText(endpoint); self.model.setText(model)
        self.message.setText('已填入预设。你可以修改地址和模型，以服务商控制台为准。')

    def config(self):
        return ApiConfig(self.protocol.currentData(), self.endpoint.text().strip(), self.model.text().strip(), self.key.text().strip()).validate()

    def _test(self):
        try:
            config = self.config()
        except ApiError as error:
            self.message.setText(str(error)); return
        self.test.setEnabled(False); self.message.setText('正在检查连接…')
        # Freeze fields so a successful check can only refer to the visible config.
        for field in (self.protocol, self.endpoint, self.model, self.key):
            field.setEnabled(False)
        events = self.events
        def run():
            try:
                message, ok = test_connection(config), True
            except ApiError as error:
                message, ok = str(error), False
            except Exception:
                message, ok = '连接测试失败，请检查网络和接口配置。', False
            try:
                events.connection.emit(message, ok)
            except RuntimeError:
                pass  # Dialog/application closed while network was pending.
        threading.Thread(target=run, daemon=True).start()

    def _tested(self, message, ok):
        self.test.setEnabled(True)
        for field in (self.protocol, self.endpoint, self.model, self.key):
            field.setEnabled(True)
        self.message.setText(message)

    def _save(self):
        try:
            config = self.config()
        except ApiError as error:
            self.message.setText(str(error)); return
        self.settings = replace(self.settings, protocol=config.protocol, endpoint=config.endpoint, model=config.model)
        self.api_key = config.api_key
        self.accept()


class FloatingButton(QPushButton):
    """A separate top-level Windows tool window that does not take input focus."""
    hotkey = Signal()
    escape = Signal()

    def __init__(self, *, native=True):
        super().__init__('我想要  ·  F8')
        self.native_enabled = native and sys.platform == 'win32'
        self.setObjectName('float')
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('点击或按 F8 听一轮；右键拖动可以移动按钮。')
        self.resize(208, 58)
        self._registered = False
        self._escape_registered = False
        self._drag = None

    def showEvent(self, event):
        super().showEvent(event)
        if self.native_enabled and not self._registered:
            user = ctypes.windll.user32
            get_style = user.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.GetWindowLongW
            set_style = user.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user.SetWindowLongW
            get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]; get_style.restype = ctypes.c_ssize_t
            set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]; set_style.restype = ctypes.c_ssize_t
            hwnd = ctypes.c_void_p(int(self.winId()))
            set_style(hwnd, -20, get_style(hwnd, -20) | 0x08000000)
            self._registered = bool(user.RegisterHotKey(hwnd, 1, 0x4000, 0x77))
            if not self._registered:
                self.setText('我想要'); self.setToolTip('F8 已被其他软件占用，请点击按钮。右键拖动可移动。')

    def set_active(self, active):
        if self.native_enabled:
            user = ctypes.windll.user32; hwnd = ctypes.c_void_p(int(self.winId()))
            if active and not self._escape_registered:
                self._escape_registered = bool(user.RegisterHotKey(hwnd, 2, 0x4000, 0x1B))
            elif not active and self._escape_registered:
                user.UnregisterHotKey(hwnd, 2); self._escape_registered = False

    def nativeEvent(self, event_type, message):
        if self.native_enabled:
            from ctypes.wintypes import MSG
            msg = MSG.from_address(int(message))
            if msg.message == 0x21:  # WM_MOUSEACTIVATE / MA_NOACTIVATE
                return True, 3
            if msg.message == 0x0312:
                (self.hotkey if msg.wParam == 1 else self.escape).emit()
                return True, 0
        return super().nativeEvent(event_type, message)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._drag = event.globalPosition().toPoint() - self.pos(); event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            self.move(event.globalPosition().toPoint() - self._drag); event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._drag = None; event.accept()
        else:
            super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        if self.native_enabled:
            self.set_active(False)
            ctypes.windll.user32.UnregisterHotKey(ctypes.c_void_p(int(self.winId())), 1)
        super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, *, demo=False, settings=None):
        super().__init__()
        self.demo = demo
        self.settings = settings or load_settings()
        self.api_key = ''
        self.gate = SessionGate(); self.cloud = None; self.audio = None
        self.target = None; self.active_settings = None; self.endpoint = Endpoint()
        self.collecting = False; self.preparing = False; self.closing = False
        self.browser = None; self.web_cancel = None; self.pending_send = None
        self.active_web = False; self.pending_config = None
        self.audio_queue = queue.Queue(maxsize=100)
        self.events = Events(self)
        self.events.partial.connect(self._partial); self.events.final.connect(self._final)
        self.events.segment.connect(self._segment)
        self.events.error.connect(self._error); self.events.audio_ready.connect(self._audio_ready)
        self.events.audio_error.connect(self._audio_error)
        self.events.web.connect(self._web_event)
        self.setWindowTitle('听码 · 直播口令输入'); self.resize(610, 800); self.setMinimumWidth(560)
        central = QWidget(); scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame); scroll.setWidget(central); self.setCentralWidget(scroll)
        layout = QVBoxLayout(central); layout.setContentsMargins(28, 26, 28, 24); layout.setSpacing(16)
        eyebrow = QLabel('系统声音 → 纯数字口令 → 输入 / 弹幕'); eyebrow.setObjectName('eyebrow'); layout.addWidget(eyebrow)
        header = QHBoxLayout(); title = QLabel('听码'); title.setObjectName('title'); header.addWidget(title); header.addStretch()
        self.api_button = QPushButton('API 设置'); self.api_button.clicked.connect(self._settings); header.addWidget(self.api_button); layout.addLayout(header)
        self.status = QLabel('先填写 API Key，再准备系统声音。'); self.status.setWordWrap(True); layout.addWidget(self.status)
        card = QFrame(); card.setObjectName('card'); body = QVBoxLayout(card); body.setContentsMargins(22, 18, 22, 18)
        self.result = QLabel('等待口令'); self.result.setObjectName('result'); self.result.setWordWrap(True); body.addWidget(self.result)
        self.original = QLabel('原话会显示在这里'); self.original.setObjectName('muted'); self.original.setWordWrap(True); body.addWidget(self.original)
        self.reason = QLabel('“这件29，扣一个00” → 00'); self.reason.setWordWrap(True); body.addWidget(self.reason)
        self.copy = QPushButton('复制结果'); self.copy.setEnabled(False); self.copy.clicked.connect(lambda: QApplication.clipboard().setText(self.result.text()))
        body.addWidget(self.copy, alignment=Qt.AlignmentFlag.AlignRight); layout.addWidget(card)
        options = QHBoxLayout()
        output_type = QLabel('纯数字口令 · 0–9'); options.addWidget(output_type)
        options.addStretch(); options.addWidget(QLabel('位数'))
        self.minimum = QSpinBox(); self.minimum.setRange(1, 32); self.minimum.setValue(self.settings.min_length)
        self.maximum = QSpinBox(); self.maximum.setRange(1, 32); self.maximum.setValue(self.settings.max_length)
        self.minimum.setAccessibleName('口令最少位数'); self.maximum.setAccessibleName('口令最多位数')
        self.minimum.setToolTip('已知固定长度时，两端设为相同位数。前导零也占一位。')
        self.maximum.setToolTip('长度不符会拒绝填写，不截断、不补零；前导零保留。')
        options.addWidget(self.minimum); options.addWidget(QLabel('至')); options.addWidget(self.maximum); layout.addLayout(options)
        rules = QLabel('只取“扣 / 飘”后的数字；保留前导零，加减乘除会计算。遇其他文字就结束，不再收集后文数字。')
        rules.setObjectName('muted'); rules.setWordWrap(True); layout.addWidget(rules)
        self.preview = QCheckBox('仅预览，不自动填入（建议第一次使用时开启）'); self.preview.setChecked(self.settings.preview_only); layout.addWidget(self.preview)
        destination = QHBoxLayout(); destination.addWidget(QLabel('输出到'))
        self.destination = QComboBox(); self.destination.addItem('当前输入框 · Ctrl+V 粘贴', 'input'); self.destination.addItem('抖音网页版弹幕', 'douyin')
        destination.addWidget(self.destination); layout.addLayout(destination)
        self.input_panel = QWidget(); input_layout = QVBoxLayout(self.input_panel); input_layout.setContentsMargins(0, 0, 0, 0); input_layout.setSpacing(6)
        self.input_target = QLabel('等待开始 · 先点中要粘贴的位置'); self.input_target.setWordWrap(True)
        self.input_target.setAccessibleName('当前粘贴目标'); self.input_target.setStyleSheet('font-weight: 600; color: #637577;')
        input_layout.addWidget(self.input_target)
        input_note = QLabel('光标在微信，就粘贴到微信；在编辑器，就粘贴到编辑器。\n按 F8 或点悬浮按钮开始，保持目标不变。只粘贴一次，不按回车。')
        input_note.setObjectName('muted'); input_note.setWordWrap(True); input_layout.addWidget(input_note)
        layout.addWidget(self.input_panel)
        self.web_panel = QWidget(); web_layout = QVBoxLayout(self.web_panel); web_layout.setContentsMargins(0, 0, 0, 0)
        room_row = QHBoxLayout(); self.room = QLineEdit(); self.room.setPlaceholderText('https://live.douyin.com/直播间编号'); self.room.setAccessibleName('抖音直播间链接')
        room_row.addWidget(self.room); self.open_room = QPushButton('打开 Edge'); self.open_room.clicked.connect(self._open_room); room_row.addWidget(self.open_room); web_layout.addLayout(room_row)
        self.auto_send = QCheckBox('识别后自动发送一条数字弹幕（每次启动需开启）'); web_layout.addWidget(self.auto_send)
        self.web_note = QLabel('先打开专用 Edge 窗口并登录，保持目标直播间可见、弹幕框为空。'); self.web_note.setWordWrap(True); self.web_note.setObjectName('muted'); web_layout.addWidget(self.web_note)
        self.ack_send = QPushButton('我已检查直播间，解除发送暂停'); self.ack_send.setEnabled(False); self.ack_send.clicked.connect(self._ack_send); web_layout.addWidget(self.ack_send)
        layout.addWidget(self.web_panel); self.web_panel.hide()
        self.destination.currentIndexChanged.connect(lambda: self.web_panel.setVisible(self.destination.currentData() == 'douyin'))
        self.destination.currentIndexChanged.connect(lambda: self.input_panel.setVisible(self.destination.currentData() == 'input'))
        controls = QHBoxLayout()
        self.prepare = QPushButton('准备系统声音'); self.prepare.setObjectName('primary'); self.prepare.clicked.connect(self._prepare_audio); controls.addWidget(self.prepare)
        self.show_float = QPushButton('显示悬浮按钮'); self.show_float.clicked.connect(self._show_float); controls.addWidget(self.show_float)
        self.cancel_button = QPushButton('取消'); self.cancel_button.setEnabled(False); self.cancel_button.clicked.connect(self.cancel); controls.addWidget(self.cancel_button); layout.addLayout(controls)
        self.finish_button = QPushButton('这句说完了，立即识别'); self.finish_button.setEnabled(False); self.finish_button.clicked.connect(self._finish_audio); layout.addWidget(self.finish_button)
        self.finish_button.hide()  # Returning to this window changes the target focus.
        self.device = QLabel('Windows 10 / 11 · 采集电脑播放的声音'); self.device.setObjectName('muted'); self.device.setWordWrap(True); layout.addWidget(self.device)
        usage = QLabel('打开直播并播放 → 点击悬浮“我想要”或按 F8 听一轮。\n当前输入框模式需先点中输入框；抖音模式需开启自动发送。Esc 取消。')
        usage.setWordWrap(True); usage.setObjectName('muted'); layout.addWidget(usage)
        trial = QHBoxLayout(); self.trial = QLineEdit(); self.trial.setPlaceholderText('试试：这件29，扣一个00 → 00'); self.trial.setAccessibleName('数字口令规则试算'); trial.addWidget(self.trial)
        try_button = QPushButton('试算'); try_button.clicked.connect(self._trial); trial.addWidget(try_button); layout.addLayout(trial)
        footer = QLabel('Key 仅保留到退出。只上传本轮短音频；识别服务按其规则计费。'); footer.setObjectName('eyebrow'); footer.setWordWrap(True); layout.addWidget(footer)
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.floating = FloatingButton(native=not demo); self.floating.clicked.connect(self.arm); self.floating.hotkey.connect(self.arm); self.floating.escape.connect(self.cancel)
        self.focus_timer = QTimer(self); self.focus_timer.setInterval(200); self.focus_timer.timeout.connect(self._check_focus)
        self.capture_timer = QTimer(self); self.capture_timer.setSingleShot(True); self.capture_timer.timeout.connect(self._finish_audio)
        self.deadline = QTimer(self); self.deadline.setSingleShot(True); self.deadline.timeout.connect(lambda: self.cancel('本轮等待超时，请重试。'))
        self.drain = QTimer(self); self.drain.setInterval(20); self.drain.timeout.connect(self._drain_audio); self.drain.start()
        if demo or sys.platform != 'win32':
            self.prepare.setEnabled(False); self.status.setText('界面演示 · 录制系统声音和自动填写需要 Windows。')

    def _read_options(self):
        if self.minimum.value() > self.maximum.value():
            raise ValueError('最短长度不能大于最长长度。')
        return replace(self.settings, min_length=self.minimum.value(), max_length=self.maximum.value(), preview_only=self.preview.isChecked())

    def _normalize_code(self, text, options):
        parsed = normalize_cued(text, min_length=options.min_length, max_length=options.max_length)
        value = parsed.get('value')
        if parsed.get('accepted') and (not is_safe_insert_text(value, options.max_length) or len(value) < options.min_length):
            return {'accepted': False, 'value': '', 'reason': '结果未通过纯数字和位数校验，请重新识别。', 'changes': []}
        return parsed

    def _set_result(self, text):
        # Keep long codes legible without adding display spaces that could be
        # mistaken for part of the clipboard/input payload.
        size = 44 if len(text) <= 14 else 32 if len(text) <= 22 else 23
        self.result.setStyleSheet(f'font-size: {size}px;')
        self.result.setText(text)

    def _set_input_target(self, text, *, state='active'):
        color = {'active': '#087e79', 'idle': '#637577', 'error': '#a43732'}[state]
        self.input_target.setStyleSheet(f'font-weight: 600; color: {color};')
        self.input_target.setText(text)

    def _settings(self):
        self.cancel('本轮已取消。') if self.gate.active_id else None
        dialog = ApiDialog(self.settings, self.api_key, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings, self.api_key = dialog.settings, dialog.api_key
            try:
                save_settings(self._read_options())
                self.status.setText('API 已配置。准备系统声音后，点中目标输入框，再点击悬浮按钮。')
            except (OSError, ValueError):
                self.status.setText('API 已在本次运行中生效；偏好设置未能保存。')

    def _show_float(self):
        if not self.floating.isVisible():
            area = QApplication.primaryScreen().availableGeometry()
            self.floating.move(area.right() - 245, area.bottom() - 115)
        self.floating.show()

    def _prepare_audio(self):
        if self.audio or self.preparing:
            return
        self.preparing = True; self.prepare.setEnabled(False); self.status.setText('正在连接系统播放设备…')
        def audio_callback(pcm):
            # Snapshot the round in the capture thread: queued pre-click/old audio
            # cannot migrate into a later round.
            token = self.gate.active_id
            if not token or not self.collecting:
                return
            try:
                self.audio_queue.put_nowait((token, pcm))
            except queue.Full:
                self.events.error.emit(token, '音频处理跟不上播放速度，本轮已停止。')
        def run():
            source = None
            try:
                source = LoopbackAudio(audio_callback, self.events.audio_error.emit)
                name = source.start()
                if self.closing:
                    source.stop()
                else:
                    self.events.audio_ready.emit(source, name)
            except Exception as error:
                if source:
                    source.stop()
                self.events.audio_error.emit(str(error))
        threading.Thread(target=run, daemon=True).start()

    def _audio_ready(self, source, name):
        self.preparing = False
        if self.closing:
            source.stop(); return
        self.audio = source; self.prepare.setText('系统声音已就绪')
        self.device.setText('播放设备：' + name)
        self.status.setText('就绪。点中目标输入框，再点击悬浮“我想要”。')
        self._show_float()

    def _audio_error(self, message):
        self.preparing = False
        self.cancel(message)
        source, self.audio = self.audio, None
        if source:
            threading.Thread(target=source.stop, daemon=True).start()
        self.prepare.setText('重新准备系统声音'); self.prepare.setEnabled(sys.platform == 'win32' and not self.demo)

    def arm(self):
        if self.pending_send:
            self.cancel('已请求停止；若发送已提交，取消无法撤回弹幕。'); return
        if self.gate.active_id:
            self.cancel('本轮已取消。'); return
        try:
            options = self._read_options()
            config = ApiConfig(options.protocol, options.endpoint, options.model, self.api_key).validate()
            if not self.audio:
                raise ValueError('请先在主窗口“准备系统声音”。')
            web = self.destination.currentData() == 'douyin' and not options.preview_only
            if web and (not self.auto_send.isChecked() or self.browser is None):
                raise ValueError('请先打开 Edge 直播间并开启“识别后自动发送”，或选择仅预览。')
            url = room_url(self.room.text()) if web else None
            target = None
            if options.preview_only:
                self._set_input_target('仅预览 · 本轮不会粘贴到其他软件', state='idle')
            elif not web:
                try:
                    target = FocusTarget.capture()
                except Exception as error:
                    self._set_input_target('未定位到粘贴目标：' + str(error), state='error')
                    raise
                self._set_input_target('本轮粘贴到：' + target.description)
        except Exception as error:
            self.status.setText(str(error)); return
        token = self.gate.begin(); self.active_settings = options; self.target = target
        self.active_web = web; self._active_controls(True)
        if web:
            self.web_cancel = threading.Event(); self.pending_config = config
            self.status.setText('正在检查直播间和弹幕框…'); self.deadline.start(5000)
            self.browser.request('prepare', token, (self.web_cancel, url))
            return
        self._listen(token, config, '仅预览' if target is None else '目标：' + target.description)

    def _listen(self, token, config, description):
        self.endpoint = Endpoint(); self.collecting = True
        self._set_result('正在听…'); self.original.setText('等待服务返回识别结果'); self.reason.setText('等待“扣 / 飘”后的完整数字或四则算式，最长收音 12 秒。'); self.copy.setEnabled(False)
        self.status.setText('正在听直播… ' + description)
        self._active_controls(True)
        self.cloud = CloudSession(config, lambda value: self.events.partial.emit(token, value), lambda value: self.events.final.emit(token, value), lambda value: self.events.error.emit(token, value), on_segment=lambda value: self.events.segment.emit(token, value))
        self.capture_timer.start(12000); self.deadline.start(35000); self.focus_timer.start()
        try:
            self.cloud.start()
        except ApiError as error:
            self.cancel(str(error))
        except Exception:
            self.cancel('无法启动云端识别，请检查 API 配置。')

    def _active_controls(self, active):
        self.cancel_button.setEnabled(active); self.finish_button.setEnabled(active)
        self.floating.set_active(active)
        self.floating.setText('取消本轮' if active else ('我想要  ·  F8' if sys.platform != 'win32' or self.floating._registered else '我想要'))
        for field in (self.minimum, self.maximum, self.preview, self.destination, self.room, self.open_room, self.auto_send, self.api_button):
            field.setEnabled(not active)

    def _open_room(self):
        if self.demo or sys.platform != 'win32':
            self.status.setText('请在 Windows 上打开专用 Edge 直播间。'); return
        try:
            url = room_url(self.room.text())
        except ValueError as error:
            self.status.setText(str(error)); return
        if self.browser is None:
            self.browser = DouyinBrowser(self.events.web.emit)
        self.open_room.setEnabled(False); self.web_note.setText('正在打开 Edge…')
        self.browser.request('open', payload=url)

    def _ack_send(self):
        if self.browser and not self.gate.active_id and not self.pending_send:
            self.browser.request('acknowledge')

    def _web_event(self, kind, token, ok, message):
        if self.closing:
            return
        if kind == 'prepare':
            if not self.gate.is_current(token):
                return
            if ok:
                self._listen(token, self.pending_config, message)
            else:
                self.cancel(message)
        elif kind == 'send':
            if token != self.pending_send:
                return
            self.pending_send = None; self.web_cancel = None
            self._active_controls(False); self.status.setText(message); self.web_note.setText(message)
            self.ack_send.setEnabled(not ok and '状态不明' in message)
        elif kind == 'open':
            self.open_room.setEnabled(True); self.web_note.setText(message)
        elif kind == 'acknowledge':
            self.ack_send.setEnabled(False); self.status.setText(message); self.web_note.setText(message)

    def _drain_audio(self):
        # Bounded work preserves UI responsiveness even after a short UIA delay.
        for _ in range(100):
            try:
                token, pcm = self.audio_queue.get_nowait()
            except queue.Empty:
                break
            if self.gate.is_current(token) and self.collecting and self.cloud:
                self.endpoint.feed(pcm)
                try:
                    self.cloud.feed(pcm)
                except ApiError as error:
                    self.cancel(str(error)); return
                # Streaming ASR owns sentence boundaries. A sales-price pause
                # must not cut off the later 扣/飘 command; HTTP needs a clip.
                if self.endpoint.ended and self.active_settings.protocol not in ('qwen_realtime', 'dashscope_streaming'):
                    self._finish_audio()

    def _finish_audio(self):
        if not self.gate.active_id or not self.collecting:
            return
        self.collecting = False; self.capture_timer.stop(); self.finish_button.setEnabled(False)
        if not self.endpoint.usable:
            self.cancel('本轮没有检测到足够声音，请确认直播正在播放。'); return
        self.status.setText('这句收音完成，等待最终识别…')
        self.cloud.finish()

    def _partial(self, token, text):
        if not self.gate.is_current(token):
            return
        self.original.setText('识别中：' + text[:256])
        options = self.active_settings
        if self.collecting and options.protocol in ('qwen_realtime', 'dashscope_streaming') and has_cued_payload_boundary(
            text, min_length=options.min_length, max_length=options.max_length
        ):
            # A following description establishes the payload's end even when
            # the speaker never pauses. Stop capture, but let ASR finalize or
            # correct the text before _segment/_final can perform any output.
            self._finish_audio()

    def _segment(self, token, text):
        """Complete on a provider-final cue sentence, not an interim guess."""
        if not self.gate.is_current(token):
            return
        options = self.active_settings
        parsed = self._normalize_code(text, options)
        if parsed['accepted']:
            # _final retires the token and stops audio/network before the write.
            # Waiting for session.finished would keep recording later chatter.
            self._final(token, text)

    def _check_focus(self):
        if self.gate.active_id and self.target:
            try:
                self.target.validate()
            except Exception as error:
                self.cancel('已取消：' + str(error))

    def _final(self, token, text):
        if not self.gate.is_current(token):
            return
        target, options = self.target, self.active_settings
        web = self.active_web
        # Retire ownership BEFORE a write or any UI changes. No second callback
        # can write, even if the adapter or platform re-enters the event loop.
        self.gate.complete(token); self._stop_round()
        parsed = self._normalize_code(text, options)
        self.original.setText('原话：' + text[:256])
        if not parsed['accepted']:
            if target is not None:
                self._set_input_target('未粘贴 · 本轮口令未通过检查', state='error')
            self._set_result('未填入'); self.reason.setText(parsed['reason']); self.status.setText('没有得到明确的“扣 / 飘 + 数字”口令，请重新听一轮。'); return
        self._set_result(parsed['value']); self.reason.setText(f"{len(parsed['value'])} 位数字 · " + ('；'.join(parsed['changes']) or '格式检查通过'))
        self.copy.setEnabled(True)
        if web:
            self.pending_send = token; self._active_controls(True)
            self.status.setText('正在向已绑定直播间提交一条数字弹幕…')
            self.browser.request('send', token, parsed['value'])
            return
        if target is None:
            self.status.setText('预览完成。结果没有自动填入。'); return
        try:
            target.insert(parsed['value'])
            self._set_input_target('已执行一次 Ctrl+V：' + target.description)
            self.status.setText('已执行一次粘贴，请在目标软件检查。没有按回车；数字保留在剪贴板。')
        except Exception as error:
            self._set_input_target('未确认粘贴 · ' + str(error), state='error')
            self.status.setText('未自动填入：' + str(error))

    def _error(self, token, message):
        if self.gate.is_current(token):
            self.cancel(message)

    def _stop_round(self):
        self.collecting = False
        for timer in (self.capture_timer, self.deadline, self.focus_timer):
            timer.stop()
        cloud, self.cloud = self.cloud, None
        if cloud:
            cloud.cancel()
        self.target = None; self._active_controls(False)

    def cancel(self, message='本轮已取消。'):
        if not isinstance(message, str):  # QPushButton.clicked(bool)
            message = '本轮已取消。'
        if self.web_cancel:
            self.web_cancel.set()
        if self.pending_send:
            self.status.setText('已请求停止；若发送已提交，取消无法撤回弹幕。')
            return
        was_active = self.gate.active_id is not None
        if was_active and self.target is not None:
            self._set_input_target('本轮粘贴已取消：' + message, state='idle')
        self.gate.cancel(); self._stop_round(); self.status.setText(message)
        if was_active:
            self._set_result('未填入'); self.copy.setEnabled(False)

    def _trial(self):
        if self.pending_send:
            self.status.setText('请等待本轮发送返回，再试算。'); return
        if self.gate.active_id:
            self.cancel('本轮已取消，正在试算纠错规则。')
        try:
            options = self._read_options()
        except ValueError as error:
            self.status.setText(str(error)); return
        parsed = self._normalize_code(self.trial.text(), options)
        self.original.setText('试算原话：' + self.trial.text()[:256]); self._set_result(parsed['value'] if parsed['accepted'] else '无法确认')
        self.reason.setText((f"{len(parsed['value'])} 位数字 · " + ('；'.join(parsed['changes']) or '格式检查通过')) if parsed['accepted'] else parsed['reason'])
        self.status.setText('规则试算，不调用 API，不自动填入。'); self.copy.setEnabled(parsed['accepted'])

    def closeEvent(self, event: QCloseEvent):
        self.closing = True; self.cancel(); self.floating.close(); self.drain.stop()
        if self.audio:
            self.audio.stop()
        if self.browser:
            self.browser.close()
        self.api_key = ''
        try:
            save_settings(self._read_options())
        except (OSError, ValueError):
            pass
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('听码'); app.setStyle('Fusion'); app.setStyleSheet(STYLE)
    app.setFont(QFont('Microsoft YaHei UI' if sys.platform == 'win32' else 'PingFang SC', 10))
    window = MainWindow(demo='--demo' in sys.argv); window.show()
    sys.exit(app.exec())
