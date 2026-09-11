import ctypes
import sys
import importlib.util
import struct
import unittest

from tingma.windows_audio import AudioError, LoopbackAudio, _PcmChunker, _float32_to_pcm16
from tingma.windows_input import (
    FocusTarget,
    InputError,
    _Snapshot,
    _WinApi,
    _WindowsBackend,
    is_safe_insert_text,
)


class WindowsImportGuardTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "non-Windows guard only")
    def test_audio_start_requires_windows(self):
        audio = LoopbackAudio(lambda _chunk: None, lambda _message: None)
        with self.assertRaisesRegex(AudioError, "Windows"):
            audio.start()

    @unittest.skipIf(sys.platform == "win32", "non-Windows guard only")
    def test_input_capture_requires_windows(self):
        with self.assertRaisesRegex(InputError, "Windows"):
            FocusTarget.capture()


class PcmChunkerTests(unittest.TestCase):
    def test_emits_only_stable_twenty_millisecond_chunks(self):
        chunker = _PcmChunker(chunk_bytes=640, max_buffer_bytes=2560)
        self.assertEqual([], chunker.feed(b"a" * 100))
        self.assertEqual([b"a" * 100 + b"b" * 540], chunker.feed(b"b" * 540))
        self.assertEqual([b"c" * 640, b"c" * 640], chunker.feed(b"c" * 1280))

    def test_rejects_unbounded_producer_output(self):
        chunker = _PcmChunker(chunk_bytes=640, max_buffer_bytes=1280)
        with self.assertRaisesRegex(AudioError, "溢出"):
            chunker.feed(b"x" * 1281)

    @unittest.skipUnless(
        importlib.util.find_spec("numpy") and importlib.util.find_spec("scipy"),
        "numpy/scipy are installed in the Windows application environment",
    )
    def test_float32_downmix_and_resample_produce_one_pcm_callback(self):
        stereo = [sample for _ in range(960) for sample in (0.5, 0.5)]
        raw = struct.pack("<{}f".format(len(stereo)), *stereo)
        pcm = _float32_to_pcm16(raw, channels=2, sample_rate=48_000)
        self.assertEqual(640, len(pcm))
        samples = struct.unpack("<320h", pcm)
        self.assertGreater(min(samples[16:-16]), 15_000)
        self.assertLess(max(samples[16:-16]), 17_000)


class InputHelperTests(unittest.TestCase):
    def test_insert_allowlist(self):
        for value in ("0", "008", "1234567890", "0" * 32):
            with self.subTest(value=value):
                self.assertTrue(is_safe_insert_text(value))
        for value in (
            "",
            "abc",
            "12m3",
            "O08",
            "a b",
            "中文",
            "a/b",
            "1.2",
            "-12",
            "+12",
            "１２",
            "١٢",
            "1" * 33,
            None,
        ):
            with self.subTest(value=value):
                self.assertFalse(is_safe_insert_text(value))

    def test_input_abi_contains_full_union_with_native_pointer_size(self):
        self.assertEqual(
            {"mi", "ki", "hi"},
            {name for name, _type in _WinApi._InputUnion._fields_},
        )
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        self.assertEqual(expected_size, ctypes.sizeof(_WinApi._Input))


class _FakeBackend:
    def __init__(self, snapshots, paste_result=True):
        self.snapshots = list(snapshots)
        self.clipboard_values = []
        self.paste_expectations = []
        self.paste_result = paste_result

    def snapshot(self):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        else:
            return self.snapshots[0]

    def set_clipboard(self, value):
        self.clipboard_values.append(value)

    def paste(self, expected, text):
        current = self.snapshot()
        if current != expected:
            raise InputError("粘贴前目标发生变化")
        self.paste_expectations.append((expected, text))
        return self.paste_result


def _snapshot(**changes):
    values = {
        "foreground_hwnd": 101,
        "process_id": 202,
        "native_focus_hwnd": 303,
        "runtime_id": (42, 7),
        "description": "WeChat · Chat composer",
    }
    values.update(changes)
    return _Snapshot(**values)


class FocusTargetTests(unittest.TestCase):
    def test_insert_sets_raw_digits_and_attempts_one_paste_once(self):
        backend = _FakeBackend([_snapshot()])
        target = FocusTarget._capture_with_backend(backend)
        self.assertEqual("WeChat · Chat composer", target.description)
        self.assertIsNone(target.validate())
        self.assertIs(target.insert("09"), True)
        self.assertEqual(["09"], backend.clipboard_values)
        self.assertEqual(1, len(backend.paste_expectations))
        with self.assertRaisesRegex(InputError, "不能重复写入"):
            target.insert("1")

    def test_native_or_uia_identity_changes_fail_closed(self):
        changes = (
            {"foreground_hwnd": 999},
            {"process_id": 999},
            {"native_focus_hwnd": 999},
            {"runtime_id": (42, 8)},
        )
        for change in changes:
            with self.subTest(change=change):
                backend = _FakeBackend([_snapshot(), _snapshot(**change)])
                target = FocusTarget._capture_with_backend(backend)
                with self.assertRaises(InputError):
                    target.insert("1")
                self.assertEqual([], backend.clipboard_values)

    def test_initially_missing_uia_identity_does_not_block_native_focus(self):
        backend = _FakeBackend(
            [
                _snapshot(runtime_id=None),
                _snapshot(runtime_id=(42, 7)),
                _snapshot(runtime_id=(42, 7)),
            ]
        )
        target = FocusTarget._capture_with_backend(backend)
        self.assertIs(target.insert("6"), True)
        self.assertEqual(["6"], backend.clipboard_values)

    def test_captured_uia_identity_cannot_disappear(self):
        backend = _FakeBackend([_snapshot(), _snapshot(runtime_id=None)])
        target = FocusTarget._capture_with_backend(backend)
        with self.assertRaisesRegex(InputError, "焦点目标"):
            target.insert("6")
        self.assertEqual([], backend.clipboard_values)

    def test_change_after_clipboard_at_final_paste_boundary_fails_closed(self):
        backend = _FakeBackend(
            [_snapshot(), _snapshot(), _snapshot(), _snapshot(native_focus_hwnd=999)]
        )
        target = FocusTarget._capture_with_backend(backend)
        with self.assertRaisesRegex(InputError, "粘贴前目标发生变化"):
            target.insert("1")
        self.assertEqual(["1"], backend.clipboard_values)
        self.assertEqual([], backend.paste_expectations)
        with self.assertRaisesRegex(InputError, "不能重复写入"):
            target.insert("1")

    def test_missing_native_focus_and_unsafe_text_are_rejected(self):
        backend = _FakeBackend([_snapshot(native_focus_hwnd=0)])
        with self.assertRaisesRegex(InputError, "焦点"):
            FocusTarget._capture_with_backend(backend)

        backend = _FakeBackend([_snapshot()])
        target = FocusTarget._capture_with_backend(backend)
        with self.assertRaises(InputError):
            target.insert("hello world")
        self.assertEqual([], backend.clipboard_values)


class _FakeClipboard:
    def __init__(self, confirmations=None):
        self.values = []
        self.confirmations = list(confirmations or [])
        self.confirmed = []

    def set_text(self, value):
        self.values.append(value)

    def confirm_text(self, value):
        self.confirmed.append(value)
        if self.confirmations and not self.confirmations.pop(0):
            raise InputError("剪贴板内容已变化")


class _FakeValuePattern:
    def __init__(self, read_only=False):
        self.IsReadOnly = read_only


class _FakeControl:
    def __init__(self, **changes):
        self.ProcessId = 202
        self.NativeWindowHandle = 303
        self.HasKeyboardFocus = True
        self.IsPassword = False
        self.IsEnabled = True
        self.Name = "Chat composer"
        self.ControlTypeName = "CustomControl"
        self._runtime_id = (42, 7)
        self._value_pattern = None
        for name, value in changes.items():
            setattr(self, name, value)

    def GetRuntimeId(self):
        return self._runtime_id

    def GetPattern(self, _pattern_id):
        return self._value_pattern


class _FakeAutomation:
    class PatternId:
        ValuePattern = 10002

    def __init__(self, control):
        self.control = control

    def GetFocusedControl(self):
        return self.control


class _FakeNative:
    def __init__(self, identities=None, title="WeChat"):
        self.identities = list(identities or [(101, 202, 303)])
        self.title = title
        self.send_calls = []

    def focus_identity(self):
        if len(self.identities) > 1:
            return self.identities.pop(0)
        return self.identities[0]

    def class_name(self, _hwnd):
        return "Chrome_RenderWidgetHostHWND"

    def window_title(self, _hwnd):
        return self.title

    def send_ctrl_v(self):
        self.send_calls.append(True)
        return True


class WindowsBackendTests(unittest.TestCase):
    def test_custom_editor_without_value_pattern_can_paste(self):
        native = _FakeNative()
        clipboard = _FakeClipboard()
        backend = _WindowsBackend(
            winapi=native,
            clipboard=clipboard,
            automation=_FakeAutomation(_FakeControl()),
        )
        target = FocusTarget._capture_with_backend(backend)
        self.assertEqual("WeChat · Chat composer", target.description)
        self.assertIs(target.insert("007"), True)
        self.assertEqual(["007"], clipboard.values)
        self.assertEqual(["007", "007"], clipboard.confirmed)
        self.assertEqual([True], native.send_calls)

    def test_missing_uia_provider_does_not_block_native_focus(self):
        native = _FakeNative()
        clipboard = _FakeClipboard()
        backend = _WindowsBackend(winapi=native, clipboard=clipboard, automation=None)
        target = FocusTarget._capture_with_backend(backend)
        self.assertIs(target.insert("8"), True)

    def test_failed_clipboard_ownership_after_write_does_not_inject(self):
        native = _FakeNative()
        clipboard = _FakeClipboard(confirmations=[False])
        backend = _WindowsBackend(winapi=native, clipboard=clipboard, automation=None)
        target = FocusTarget._capture_with_backend(backend)
        with self.assertRaisesRegex(InputError, "剪贴板"):
            target.insert("8")
        self.assertEqual([], native.send_calls)

    def test_clipboard_replacement_before_paste_does_not_inject(self):
        native = _FakeNative()
        clipboard = _FakeClipboard(confirmations=[True, False])
        backend = _WindowsBackend(winapi=native, clipboard=clipboard, automation=None)
        target = FocusTarget._capture_with_backend(backend)
        with self.assertRaisesRegex(InputError, "剪贴板"):
            target.insert("8")
        self.assertEqual([], native.send_calls)

    def test_fast_native_recheck_catches_switch_after_uia_work(self):
        unchanged = (101, 202, 303)
        native = _FakeNative(identities=[unchanged] * 4 + [(999, 404, 505)])
        clipboard = _FakeClipboard()
        backend = _WindowsBackend(
            winapi=native,
            clipboard=clipboard,
            automation=_FakeAutomation(_FakeControl()),
        )
        target = FocusTarget._capture_with_backend(backend)
        with self.assertRaisesRegex(InputError, "原生焦点"):
            target.insert("8")
        self.assertEqual([], native.send_calls)

    def test_description_falls_back_when_window_title_is_unavailable(self):
        backend = _WindowsBackend(
            winapi=_FakeNative(title=""),
            clipboard=_FakeClipboard(),
            automation=None,
        )
        target = FocusTarget._capture_with_backend(backend)
        self.assertEqual("当前输入位置", target.description)

    def test_known_uia_focus_mismatch_and_restrictions_block(self):
        controls = (
            _FakeControl(ProcessId=999),
            _FakeControl(HasKeyboardFocus=False),
            _FakeControl(IsPassword=True),
            _FakeControl(IsEnabled=False),
            _FakeControl(_value_pattern=_FakeValuePattern(read_only=True)),
        )
        for control in controls:
            with self.subTest(control=control.__dict__):
                backend = _WindowsBackend(
                    winapi=_FakeNative(),
                    clipboard=_FakeClipboard(),
                    automation=_FakeAutomation(control),
                )
                with self.assertRaises(InputError):
                    FocusTarget._capture_with_backend(backend)


class _FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeUser32:
    def __init__(self, send_results, held_keys=()):
        self.send_results = list(send_results)
        self.held_keys = set(held_keys)
        self.batches = []
        self.GetAsyncKeyState = _FakeFunction(self._get_async_key_state)
        self.SendInput = _FakeFunction(self._send_input)

    def _get_async_key_state(self, key):
        return 0x8000 if key in self.held_keys else 0

    def _send_input(self, count, inputs, size):
        self.batches.append(
            (
                count,
                [
                    (inputs[index].ki.wVk, inputs[index].ki.dwFlags)
                    for index in range(count)
                ],
                size,
            )
        )
        return self.send_results.pop(0)


class SendInputTests(unittest.TestCase):
    def _api(self, send_results, held_keys=()):
        api = object.__new__(_WinApi)
        api._user32 = _FakeUser32(send_results, held_keys)
        return api

    def test_ctrl_v_is_one_atomic_input_array(self):
        api = self._api([4])
        self.assertIs(api.send_ctrl_v(), True)
        self.assertEqual(
            [(0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2)],
            api._user32.batches[0][1],
        )
        self.assertEqual(1, len(api._user32.batches))
        self.assertEqual(ctypes.sizeof(_WinApi._Input), api._user32.batches[0][2])

    def test_conflicting_held_modifier_blocks_before_send(self):
        api = self._api([4], held_keys=(0x10,))
        with self.assertRaisesRegex(InputError, "修饰键"):
            api.send_ctrl_v()
        self.assertEqual([], api._user32.batches)

    def test_partial_send_releases_only_injected_held_keys_without_replay(self):
        api = self._api([2, 2])
        with self.assertRaisesRegex(InputError, "只接受了 2/4"):
            api.send_ctrl_v()
        self.assertEqual(2, len(api._user32.batches))
        self.assertEqual([(0x56, 2), (0x11, 2)], api._user32.batches[1][1])


if __name__ == "__main__":
    unittest.main()
