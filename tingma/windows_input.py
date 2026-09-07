"""Fail-closed focused text insertion for Windows UI Automation.

Only UI Automation's ValuePattern is used to write. Selection discovery uses
TextPattern when its document text agrees exactly with ValuePattern, with a
bounded native ``Edit`` fallback for controls that expose a real focused HWND.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import re
import sys
import threading
from typing import Optional, Tuple


_SAFE_TEXT = re.compile(r"[A-Za-z0-9_.-]+\Z")
_SUPPORTED_CONTROL_TYPES = frozenset(("EditControl", "DocumentControl"))


class InputError(RuntimeError):
    """Raised when the target cannot be identified or written safely."""


def is_safe_insert_text(text: object, max_length: int = 64) -> bool:
    """Return whether *text* is a bounded normalized code payload."""

    return (
        isinstance(text, str)
        and not isinstance(max_length, bool)
        and isinstance(max_length, int)
        and 1 <= len(text) <= max_length
        and _SAFE_TEXT.fullmatch(text) is not None
    )


def _utf16_bytes(text: str) -> bytes:
    if not isinstance(text, str):
        raise InputError("输入框内容不是字符串")
    return text.encode("utf-16-le")


def splice_utf16(value: str, start: int, end: int, inserted: str) -> str:
    """Splice using UTF-16 code-unit offsets, as used by Windows controls."""

    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
    ):
        raise InputError("输入框选区位置不是整数")
    if not isinstance(inserted, str):
        raise InputError("待写入内容不是字符串")
    encoded = _utf16_bytes(value)
    unit_count = len(encoded) // 2
    if start < 0 or end < start or end > unit_count:
        raise InputError("输入框选区超出可编辑内容范围")
    try:
        prefix = encoded[: start * 2].decode("utf-16-le")
        suffix = encoded[end * 2 :].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise InputError("输入框选区截断了一个 UTF-16 字符") from exc
    return prefix + inserted + suffix


def _utf16_slice(value: str, start: int, end: int) -> str:
    encoded = _utf16_bytes(value)
    unit_count = len(encoded) // 2
    if start < 0 or end < start or end > unit_count:
        raise InputError("输入框选区超出可编辑内容范围")
    try:
        return encoded[start * 2 : end * 2].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise InputError("输入框选区截断了一个 UTF-16 字符") from exc


@dataclass(frozen=True)
class _Snapshot:
    foreground_hwnd: int
    process_id: int
    runtime_id: Tuple[int, ...]
    value: str
    selection_start: Optional[int]
    selection_end: Optional[int]
    control_type: str
    description: str


def _validate_snapshot(snapshot: _Snapshot) -> None:
    if snapshot.foreground_hwnd <= 0 or snapshot.process_id <= 0:
        raise InputError("无法确认当前前台窗口")
    if not snapshot.runtime_id:
        raise InputError("当前输入框没有 UI Automation 运行时标识")
    if snapshot.control_type not in _SUPPORTED_CONTROL_TYPES:
        raise InputError("当前控件不是受支持的普通文本输入框")
    if not isinstance(snapshot.value, str):
        raise InputError("无法读取当前输入框内容")
    if snapshot.selection_start is None or snapshot.selection_end is None:
        raise InputError("无法确认当前输入框的选区或光标位置")
    # This also rejects out-of-range offsets and split surrogate pairs.
    splice_utf16(snapshot.value, snapshot.selection_start, snapshot.selection_end, "")


class FocusTarget:
    """A one-use snapshot of an exact foreground editable UIA control."""

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

    def insert(self, text: str) -> None:
        if not is_safe_insert_text(text):
            raise InputError("待写入内容不安全或超过 64 个字符")
        with self._lock:
            if self._used:
                raise InputError("该输入目标已使用，不能重复写入")
            current = self._validated_current()
            replacement = splice_utf16(
                current.value,
                current.selection_start,
                current.selection_end,
                text,
            )
            # Consume the target before crossing the mutation boundary. If UIA
            # reports an uncertain failure, retrying could duplicate text.
            self._used = True
            try:
                written = self._backend.write(current, replacement)
            except InputError:
                raise
            except Exception as exc:
                raise InputError("UI Automation 写入失败：{}".format(exc)) from exc
            if written is not True:
                raise InputError("UI Automation 未确认写入成功")

    def _validated_current(self) -> _Snapshot:
        try:
            current = self._backend.snapshot()
        except InputError:
            raise
        except Exception as exc:
            raise InputError("无法重新核对输入目标：{}".format(exc)) from exc
        _validate_snapshot(current)
        if current != self._original:
            raise InputError("焦点、输入框内容或选区已经变化，已取消写入")
        return current


def _selection_from_text_pattern(automation, text_pattern, value: str) -> Tuple[int, int]:
    try:
        document = text_pattern.DocumentRange
        document_text = document.GetText(-1)
        if document_text != value:
            raise InputError("TextPattern 文档内容与 ValuePattern 不一致")
        selections = text_pattern.GetSelection()
        if len(selections) != 1:
            raise InputError("当前输入框没有提供唯一且明确的选区")
        selection = selections[0]
        prefix_range = document.Clone()
        moved = prefix_range.MoveEndpointByRange(
            automation.TextPatternRangeEndpoint.End,
            selection,
            automation.TextPatternRangeEndpoint.Start,
            waitTime=0,
        )
        if moved is not True:
            raise InputError("无法确定当前选区的起点")
        prefix = prefix_range.GetText(-1)
        selected = selection.GetText(-1)
    except InputError:
        raise
    except Exception as exc:
        raise InputError("无法读取 UI Automation 选区：{}".format(exc)) from exc

    start = len(_utf16_bytes(prefix)) // 2
    end = start + len(_utf16_bytes(selected)) // 2
    try:
        prefix_matches = _utf16_slice(value, 0, start) == prefix
        selected_matches = _utf16_slice(value, start, end) == selected
    except InputError:
        raise InputError("UI Automation 选区超出输入框内容范围")
    if not prefix_matches or not selected_matches:
        raise InputError("UI Automation 选区内容与 ValuePattern 不一致")
    return start, end


def _native_edit_class(class_name: str) -> bool:
    lowered = class_name.lower()
    return lowered == "edit" or lowered.startswith("richedit")


class _WindowsBackend:
    def __init__(self):
        if sys.platform != "win32":
            raise InputError("自动写入输入框仅支持 Windows 10/11")
        try:
            import uiautomation as automation
        except ImportError as exc:  # pragma: no cover - Windows dependency check.
            raise InputError("Windows 上需要安装 uiautomation 2.0.29") from exc
        self._automation = automation
        self._winapi = _WinApi()
        self._thread_id = threading.get_ident()

    def snapshot(self) -> _Snapshot:
        snapshot, _pattern = self._read_current()
        return snapshot

    def write(self, expected: _Snapshot, value: str) -> bool:
        current, value_pattern = self._read_current()
        if current != expected:
            raise InputError("写入前目标再次发生变化，已取消写入")
        try:
            return value_pattern.SetValue(value, waitTime=0) is True
        except Exception as exc:
            raise InputError("ValuePattern.SetValue 写入失败：{}".format(exc)) from exc

    def _read_current(self):
        if threading.get_ident() != self._thread_id:
            raise InputError("输入目标必须在捕获它的线程中使用")
        foreground_hwnd, foreground_pid, native_focus_hwnd = self._winapi.focus_identity()
        try:
            control = self._automation.GetFocusedControl()
        except Exception as exc:
            raise InputError("UI Automation 无法读取当前焦点控件") from exc
        if control is None:
            raise InputError("UI Automation 没有返回焦点控件")

        try:
            process_id = int(control.ProcessId)
            runtime_id = tuple(int(part) for part in control.GetRuntimeId())
            control_type = str(control.ControlTypeName)
            native_hwnd = int(control.NativeWindowHandle or 0)
            name = str(control.Name or "")
            if not control.HasKeyboardFocus:
                raise InputError("UI Automation 焦点已经变化")
            if control.IsPassword:
                raise InputError("密码框不能作为自动写入目标")
            if not control.IsEnabled:
                raise InputError("当前输入框已禁用")
        except InputError:
            raise
        except Exception as exc:
            raise InputError("当前焦点控件的 UI Automation 标识不完整") from exc
        if process_id != foreground_pid:
            raise InputError("焦点控件不属于当前前台进程")
        if control_type not in _SUPPORTED_CONTROL_TYPES:
            raise InputError("当前控件不是受支持的普通文本输入框")

        try:
            value_pattern = control.GetPattern(self._automation.PatternId.ValuePattern)
        except Exception as exc:
            raise InputError("无法查询 UI Automation ValuePattern") from exc
        if value_pattern is None:
            raise InputError("当前输入框没有提供 UI Automation ValuePattern")
        try:
            if value_pattern.IsReadOnly:
                raise InputError("当前输入框为只读状态")
            value = value_pattern.Value
        except InputError:
            raise
        except Exception as exc:
            raise InputError("无法读取输入框的 ValuePattern") from exc
        if not isinstance(value, str):
            raise InputError("ValuePattern 没有返回文本内容")

        try:
            text_pattern = control.GetPattern(self._automation.PatternId.TextPattern)
        except Exception:
            text_pattern = None
        if text_pattern is not None:
            selection_start, selection_end = _selection_from_text_pattern(
                self._automation, text_pattern, value
            )
        else:
            if native_hwnd <= 0 or native_hwnd != native_focus_hwnd:
                raise InputError("没有精确聚焦的原生 Edit HWND，无法确认选区")
            class_name = self._winapi.class_name(native_hwnd)
            if not _native_edit_class(class_name):
                raise InputError("原生选区读取仅支持 Windows Edit 控件")
            selection_start, selection_end = self._winapi.edit_selection(native_hwnd)

        clean_name = " ".join(name.split())[:80]
        label = clean_name or "当前输入框"
        description = "{} ({}, PID {})".format(label, control_type, process_id)
        snapshot = _Snapshot(
            foreground_hwnd=foreground_hwnd,
            process_id=process_id,
            runtime_id=runtime_id,
            value=value,
            selection_start=selection_start,
            selection_end=selection_end,
            control_type=control_type,
            description=description,
        )
        _validate_snapshot(snapshot)
        return snapshot, value_pattern


class _WinApi:
    _EM_GETSEL = 0x00B0
    _SMTO_BLOCK = 0x0001
    _SMTO_ABORTIFHUNG = 0x0002
    _TIMEOUT_MS = 250

    class _GuiThreadInfo(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        )

    def __init__(self):
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetGUIThreadInfo.argtypes = (
            wintypes.DWORD,
            ctypes.POINTER(self._GuiThreadInfo),
        )
        self._user32.GetGUIThreadInfo.restype = wintypes.BOOL
        self._user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        self._user32.GetClassNameW.restype = ctypes.c_int
        self._user32.SendMessageTimeoutW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        )
        self._user32.SendMessageTimeoutW.restype = wintypes.LPARAM

    def focus_identity(self) -> Tuple[int, int, int]:
        foreground = int(self._user32.GetForegroundWindow() or 0)
        if foreground <= 0:
            raise InputError("Windows 没有返回前台窗口")
        process_id = wintypes.DWORD()
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
        return foreground, int(process_id.value), int(info.hwndFocus or 0)

    def class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        copied = int(self._user32.GetClassNameW(hwnd, buffer, len(buffer)))
        if copied <= 0:
            raise InputError("无法确认原生焦点控件类型")
        return buffer.value

    def edit_selection(self, hwnd: int) -> Tuple[int, int]:
        start = wintypes.DWORD()
        end = wintypes.DWORD()
        result = ctypes.c_size_t()
        completed = self._user32.SendMessageTimeoutW(
            hwnd,
            self._EM_GETSEL,
            ctypes.addressof(start),
            ctypes.addressof(end),
            self._SMTO_BLOCK | self._SMTO_ABORTIFHUNG,
            self._TIMEOUT_MS,
            ctypes.byref(result),
        )
        if not completed:
            raise InputError("读取原生 Edit 选区超时或失败")
        return int(start.value), int(end.value)
