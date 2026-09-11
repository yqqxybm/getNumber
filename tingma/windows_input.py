"""One-use, focus-bound clipboard paste for Windows.

The target is anchored to the foreground window, its process, and the native
focused HWND. UI Automation strengthens that identity and supplies known safety
restrictions when a provider is available, but edit patterns are not required:
the actual insertion mechanism is one native Ctrl+V chord.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import re
import sys
import threading
from typing import Optional, Tuple


_SAFE_TEXT = re.compile(r"[0-9]+\Z")
_AUTOMATION_DEFAULT = object()


class InputError(RuntimeError):
    """Raised when a focused paste cannot be attempted safely."""


def is_safe_insert_text(text: object, max_length: int = 32) -> bool:
    """Only bounded ASCII digits may cross the native input boundary."""

    return (
        isinstance(text, str)
        and not isinstance(max_length, bool)
        and isinstance(max_length, int)
        and 1 <= max_length <= 32
        and 1 <= len(text) <= max_length
        and _SAFE_TEXT.fullmatch(text) is not None
    )


@dataclass(frozen=True)
class _Snapshot:
    foreground_hwnd: int
    process_id: int
    native_focus_hwnd: int
    runtime_id: Optional[Tuple[int, ...]]
    description: str = field(compare=False)


def _validate_snapshot(snapshot: _Snapshot) -> None:
    if snapshot.foreground_hwnd <= 0 or snapshot.process_id <= 0:
        raise InputError("无法确认当前前台窗口")
    if snapshot.native_focus_hwnd <= 0:
        raise InputError("无法确认当前原生焦点控件")
    if snapshot.runtime_id is not None:
        if not snapshot.runtime_id or not all(
            isinstance(part, int) and not isinstance(part, bool)
            for part in snapshot.runtime_id
        ):
            raise InputError("UI Automation 运行时标识无效")
    if not isinstance(snapshot.description, str) or not snapshot.description:
        raise InputError("输入目标描述无效")


def _same_target(expected: _Snapshot, current: _Snapshot) -> bool:
    if (
        current.foreground_hwnd != expected.foreground_hwnd
        or current.process_id != expected.process_id
        or current.native_focus_hwnd != expected.native_focus_hwnd
    ):
        return False
    # A target captured without UIA retains the native compatibility path. If
    # UIA identified the original element, losing or changing that identity is
    # no longer enough evidence that a shared native HWND still means it.
    return expected.runtime_id is None or current.runtime_id == expected.runtime_id


class FocusTarget:
    """A one-use snapshot of an exact foreground native focus target."""

    def __init__(self, backend, snapshot: _Snapshot):
        self._backend = backend
        self._original = snapshot
        self._used = False
        self._lock = threading.Lock()
        self.description = snapshot.description

    @classmethod
    def capture(cls) -> "FocusTarget":
        if sys.platform != "win32":
            raise InputError("自动写入输入框仅支持 Windows 10/11")
        return cls._capture_with_backend(_WindowsBackend())

    @classmethod
    def _capture_with_backend(cls, backend) -> "FocusTarget":
        snapshot = backend.snapshot()
        _validate_snapshot(snapshot)
        return cls(backend, snapshot)

    def validate(self) -> None:
        with self._lock:
            if self._used:
                raise InputError("该输入目标已使用，不能重复写入")
            self._validated_current()

    def insert(self, text: str) -> bool:
        """Attempt exactly one Ctrl+V; ``True`` does not assert visible delivery."""

        if not is_safe_insert_text(text):
            raise InputError("待写入内容必须是 1–32 位纯数字")
        with self._lock:
            if self._used:
                raise InputError("该输入目标已使用，不能重复写入")
            self._validated_current()

            # Clipboard mutation crosses the one-use boundary. Leave the new
            # public clipboard value in place and never retry an uncertain paste.
            self._used = True
            try:
                self._backend.set_clipboard(text)
            except InputError:
                raise
            except Exception as exc:
                raise InputError("无法把数字写入剪贴板：{}".format(exc)) from exc

            # Recheck after clipboard work, then let the backend perform one
            # final check immediately adjacent to its native SendInput call.
            current = self._validated_current()
            try:
                attempted = self._backend.paste(current, text)
            except InputError:
                raise
            except Exception as exc:
                raise InputError("无法执行 Ctrl+V：{}".format(exc)) from exc
            if attempted is not True:
                raise InputError("Windows 未接受完整的 Ctrl+V 输入序列")
            return True

    def _validated_current(self) -> _Snapshot:
        try:
            current = self._backend.snapshot()
        except InputError:
            raise
        except Exception as exc:
            raise InputError("无法重新核对输入目标：{}".format(exc)) from exc
        _validate_snapshot(current)
        if not _same_target(self._original, current):
            raise InputError("前台窗口或焦点目标已经变化，已取消写入")
        return current


def _optional_attribute(value, name):
    try:
        return True, getattr(value, name)
    except Exception:
        return False, None


class _QtClipboard:
    def __init__(self):
        if threading.current_thread() is not threading.main_thread():
            raise InputError("剪贴板只能在应用主线程中使用")
        try:
            from PySide6.QtCore import QThread
            from PySide6.QtWidgets import QApplication
        except ImportError as exc:  # pragma: no cover - packaged dependency.
            raise InputError("Windows 应用缺少 PySide6 剪贴板支持") from exc
        application = QApplication.instance()
        if application is None:
            raise InputError("Qt 应用尚未初始化，无法使用剪贴板")
        if QThread.currentThread() != application.thread():
            raise InputError("剪贴板只能在 Qt 主线程中使用")
        self._clipboard = application.clipboard()

    def set_text(self, text: str) -> None:
        if not is_safe_insert_text(text):
            raise InputError("拒绝把非纯数字内容写入剪贴板")
        self._clipboard.setText(text)

    def confirm_text(self, text: str) -> None:
        if not is_safe_insert_text(text):
            raise InputError("拒绝核对非纯数字剪贴板内容")
        try:
            if not self._clipboard.ownsClipboard():
                raise InputError("剪贴板已被其他应用接管，已取消粘贴")
            matches = self._clipboard.text() == text
        except InputError:
            raise
        except Exception as exc:
            raise InputError("无法核对剪贴板内容") from exc
        if not matches:
            raise InputError("剪贴板不再由本应用持有或数字内容已变化")


class _WindowsBackend:
    def __init__(
        self,
        winapi=None,
        clipboard=None,
        automation=_AUTOMATION_DEFAULT,
    ):
        if sys.platform != "win32" and (winapi is None or clipboard is None):
            raise InputError("自动写入输入框仅支持 Windows 10/11")
        self._winapi = winapi if winapi is not None else _WinApi()
        self._clipboard = clipboard if clipboard is not None else _QtClipboard()
        if automation is _AUTOMATION_DEFAULT:
            try:
                import uiautomation as automation_module
            except ImportError:
                automation_module = None
            self._automation = automation_module
        else:
            self._automation = automation
        self._thread_id = threading.get_ident()

    def snapshot(self) -> _Snapshot:
        self._ensure_thread()
        foreground_hwnd, foreground_pid, native_focus_hwnd = (
            self._winapi.focus_identity()
        )
        if native_focus_hwnd <= 0:
            raise InputError("Windows 没有返回当前原生焦点控件")

        runtime_id = None
        name = ""
        control = self._focused_uia_control()
        if control is not None:
            runtime_id, name = self._uia_identity(
                control, foreground_pid, native_focus_hwnd
            )

        try:
            title = self._winapi.window_title(foreground_hwnd)
        except Exception:
            title = ""
        clean_title = " ".join(title.split())[:80]
        clean_name = " ".join(name.split())[:60]
        if clean_title and clean_name and clean_name != clean_title:
            description = "{} · {}".format(clean_title, clean_name)
        else:
            description = clean_title or "当前输入位置"
        snapshot = _Snapshot(
            foreground_hwnd=foreground_hwnd,
            process_id=foreground_pid,
            native_focus_hwnd=native_focus_hwnd,
            runtime_id=runtime_id,
            description=description,
        )
        _validate_snapshot(snapshot)
        return snapshot

    def set_clipboard(self, text: str) -> None:
        self._ensure_thread()
        if not is_safe_insert_text(text):
            raise InputError("拒绝把非纯数字内容写入剪贴板")
        try:
            self._clipboard.set_text(text)
            self._clipboard.confirm_text(text)
        except InputError:
            raise
        except Exception as exc:
            raise InputError("Qt 剪贴板写入失败：{}".format(exc)) from exc

    def paste(self, expected: _Snapshot, text: str) -> bool:
        self._ensure_thread()
        current = self.snapshot()
        if not _same_target(expected, current):
            raise InputError("执行 Ctrl+V 前焦点目标再次变化，已取消写入")
        try:
            self._clipboard.confirm_text(text)
        except InputError:
            raise
        except Exception as exc:
            raise InputError("无法在粘贴前核对剪贴板") from exc
        native_identity = self._winapi.focus_identity()
        expected_identity = (
            expected.foreground_hwnd,
            expected.process_id,
            expected.native_focus_hwnd,
        )
        if native_identity != expected_identity:
            raise InputError("执行 Ctrl+V 前原生焦点再次变化，已取消写入")
        return self._winapi.send_ctrl_v()

    def _ensure_thread(self) -> None:
        if (
            threading.get_ident() != self._thread_id
            or threading.current_thread() is not threading.main_thread()
        ):
            raise InputError("输入目标必须在捕获它的应用主线程中使用")

    def _focused_uia_control(self):
        if self._automation is None:
            return None
        try:
            return self._automation.GetFocusedControl()
        except Exception:
            # Provider absence cannot weaken the native focus tuple, so it is
            # not by itself a rejection condition.
            return None

    def _uia_identity(self, control, foreground_pid: int, native_focus_hwnd: int):
        has_process, process_id = _optional_attribute(control, "ProcessId")
        if has_process:
            try:
                process_id = int(process_id)
            except (TypeError, ValueError):
                process_id = 0
            if process_id > 0 and process_id != foreground_pid:
                raise InputError("UI Automation 焦点不属于当前前台进程")

        has_focus, has_keyboard_focus = _optional_attribute(
            control, "HasKeyboardFocus"
        )
        if has_focus and has_keyboard_focus is False:
            raise InputError("UI Automation 报告焦点已经变化")

        has_native, uia_hwnd = _optional_attribute(control, "NativeWindowHandle")
        if has_native:
            try:
                uia_hwnd = int(uia_hwnd or 0)
            except (TypeError, ValueError):
                uia_hwnd = 0
            if uia_hwnd > 0 and uia_hwnd != native_focus_hwnd:
                raise InputError("UI Automation 与原生焦点控件不一致")

        has_password, is_password = _optional_attribute(control, "IsPassword")
        if has_password and is_password is True:
            raise InputError("密码框不能作为自动写入目标")
        has_enabled, is_enabled = _optional_attribute(control, "IsEnabled")
        if has_enabled and is_enabled is False:
            raise InputError("当前输入控件已禁用")

        value_pattern = None
        try:
            pattern_id = self._automation.PatternId.ValuePattern
            value_pattern = control.GetPattern(pattern_id)
        except Exception:
            pass
        if value_pattern is not None:
            has_read_only, is_read_only = _optional_attribute(
                value_pattern, "IsReadOnly"
            )
            if has_read_only and is_read_only is True:
                raise InputError("当前输入控件为只读状态")

        try:
            parts = tuple(int(part) for part in control.GetRuntimeId())
            runtime_id = parts or None
        except Exception:
            runtime_id = None
        has_name, name = _optional_attribute(control, "Name")
        return runtime_id, str(name or "") if has_name else ""


class _WinApi:
    """Small user32 boundary with an ABI-correct Win32 INPUT declaration."""

    _DWORD = ctypes.c_uint32
    _HANDLE = ctypes.c_void_p

    class _MouseInput(ctypes.Structure):
        _fields_ = (
            ("dx", ctypes.c_int32),
            ("dy", ctypes.c_int32),
            ("mouseData", ctypes.c_uint32),
            ("dwFlags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            (
                "dwExtraInfo",
                ctypes.c_uint64
                if ctypes.sizeof(ctypes.c_void_p) == 8
                else ctypes.c_uint32,
            ),
        )

    class _KeyboardInput(ctypes.Structure):
        _fields_ = (
            ("wVk", ctypes.c_uint16),
            ("wScan", ctypes.c_uint16),
            ("dwFlags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            (
                "dwExtraInfo",
                ctypes.c_uint64
                if ctypes.sizeof(ctypes.c_void_p) == 8
                else ctypes.c_uint32,
            ),
        )

    class _HardwareInput(ctypes.Structure):
        _fields_ = (
            ("uMsg", ctypes.c_uint32),
            ("wParamL", ctypes.c_uint16),
            ("wParamH", ctypes.c_uint16),
        )

    class _InputUnion(ctypes.Union):
        pass

    _InputUnion._fields_ = (
        ("mi", _MouseInput),
        ("ki", _KeyboardInput),
        ("hi", _HardwareInput),
    )

    class _Input(ctypes.Structure):
        pass

    _Input._anonymous_ = ("payload",)
    _Input._fields_ = (("type", ctypes.c_uint32), ("payload", _InputUnion))

    class _Rect(ctypes.Structure):
        _fields_ = (
            ("left", ctypes.c_int32),
            ("top", ctypes.c_int32),
            ("right", ctypes.c_int32),
            ("bottom", ctypes.c_int32),
        )

    class _GuiThreadInfo(ctypes.Structure):
        pass

    _GuiThreadInfo._fields_ = (
        ("cbSize", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("hwndActive", ctypes.c_void_p),
        ("hwndFocus", ctypes.c_void_p),
        ("hwndCapture", ctypes.c_void_p),
        ("hwndMenuOwner", ctypes.c_void_p),
        ("hwndMoveSize", ctypes.c_void_p),
        ("hwndCaret", ctypes.c_void_p),
        ("rcCaret", _Rect),
    )

    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002
    _VK_SHIFT = 0x10
    _VK_CONTROL = 0x11
    _VK_MENU = 0x12
    _VK_V = 0x56
    _VK_LWIN = 0x5B
    _VK_RWIN = 0x5C
    _MODIFIERS = (_VK_SHIFT, _VK_CONTROL, _VK_MENU, _VK_LWIN, _VK_RWIN)

    def __init__(self):
        if sys.platform != "win32":
            raise InputError("原生输入仅支持 Windows 10/11")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._bind_functions()

    def _bind_functions(self) -> None:
        self._user32.GetForegroundWindow.argtypes = ()
        self._user32.GetForegroundWindow.restype = self._HANDLE
        self._user32.GetWindowThreadProcessId.argtypes = (
            self._HANDLE,
            ctypes.POINTER(self._DWORD),
        )
        self._user32.GetWindowThreadProcessId.restype = self._DWORD
        self._user32.GetGUIThreadInfo.argtypes = (
            self._DWORD,
            ctypes.POINTER(self._GuiThreadInfo),
        )
        self._user32.GetGUIThreadInfo.restype = ctypes.c_int32
        self._user32.GetClassNameW.argtypes = (
            self._HANDLE,
            ctypes.c_wchar_p,
            ctypes.c_int,
        )
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = (
            self._HANDLE,
            ctypes.c_wchar_p,
            ctypes.c_int,
        )
        self._user32.GetWindowTextW.restype = ctypes.c_int
        self._user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        self._user32.GetAsyncKeyState.restype = ctypes.c_int16
        self._user32.SendInput.argtypes = (
            self._DWORD,
            ctypes.POINTER(self._Input),
            ctypes.c_int,
        )
        self._user32.SendInput.restype = self._DWORD

    def focus_identity(self) -> Tuple[int, int, int]:
        foreground = int(self._user32.GetForegroundWindow() or 0)
        if foreground <= 0:
            raise InputError("Windows 没有返回前台窗口")
        process_id = self._DWORD()
        thread_id = int(
            self._user32.GetWindowThreadProcessId(foreground, ctypes.byref(process_id)) or 0
        )
        if thread_id <= 0 or process_id.value <= 0:
            raise InputError("无法确认前台窗口所属进程")
        info = self._GuiThreadInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            raise InputError("无法确认当前焦点控件的原生 HWND")
        if int(info.hwndActive or 0) != foreground:
            raise InputError("捕获焦点时前台窗口发生了变化")
        native_focus = int(info.hwndFocus or 0)
        if native_focus <= 0:
            raise InputError("Windows 没有返回当前原生焦点控件")
        return foreground, int(process_id.value), native_focus

    def class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        copied = int(self._user32.GetClassNameW(hwnd, buffer, len(buffer)))
        if copied <= 0:
            raise InputError("无法确认原生焦点控件类型")
        return buffer.value

    def window_title(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(161)
        copied = int(self._user32.GetWindowTextW(hwnd, buffer, len(buffer)))
        if copied <= 0:
            return ""
        return buffer.value

    @classmethod
    def _key_input(cls, virtual_key: int, key_up: bool = False):
        flags = cls._KEYEVENTF_KEYUP if key_up else 0
        return cls._Input(
            type=cls._INPUT_KEYBOARD,
            ki=cls._KeyboardInput(
                wVk=virtual_key,
                wScan=0,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            ),
        )

    def send_ctrl_v(self) -> bool:
        held = [
            key
            for key in self._MODIFIERS
            if int(self._user32.GetAsyncKeyState(key)) & 0x8000
        ]
        if held:
            raise InputError("检测到仍按下的修饰键，已取消 Ctrl+V")

        inputs = (self._Input * 4)(
            self._key_input(self._VK_CONTROL),
            self._key_input(self._VK_V),
            self._key_input(self._VK_V, key_up=True),
            self._key_input(self._VK_CONTROL, key_up=True),
        )
        accepted = int(
            self._user32.SendInput(4, inputs, ctypes.sizeof(self._Input)) or 0
        )
        if accepted == 4:
            # SendInput acceptance cannot prove that the target displayed text.
            return True

        cleanup = []
        if accepted >= 2 and accepted < 3:
            cleanup.append(self._key_input(self._VK_V, key_up=True))
        if accepted >= 1 and accepted < 4:
            cleanup.append(self._key_input(self._VK_CONTROL, key_up=True))
        cleanup_accepted = 0
        if cleanup:
            releases = (self._Input * len(cleanup))(*cleanup)
            cleanup_accepted = int(
                self._user32.SendInput(
                    len(cleanup), releases, ctypes.sizeof(self._Input)
                )
                or 0
            )
        cleanup_note = ""
        if cleanup_accepted != len(cleanup):
            cleanup_note = "；按键释放也未被完整接受"
        raise InputError(
            "Windows 只接受了 {}/4 个 Ctrl+V 输入事件{}；不会重试".format(
                accepted, cleanup_note
            )
        )
