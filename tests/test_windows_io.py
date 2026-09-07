import sys
import importlib.util
import struct
import unittest

from tingma.windows_audio import AudioError, LoopbackAudio, _PcmChunker, _float32_to_pcm16
from tingma.windows_input import (
    FocusTarget,
    InputError,
    _Snapshot,
    _native_edit_class,
    _selection_from_text_pattern,
    is_safe_insert_text,
    splice_utf16,
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
    def test_utf16_splice_uses_windows_offsets(self):
        emoji = chr(0x1F600)
        self.assertEqual("A-XB", splice_utf16("A" + emoji + "B", 1, 3, "-X"))
        self.assertEqual(emoji + "x", splice_utf16(emoji, 2, 2, "x"))

    def test_utf16_splice_rejects_split_surrogate_and_bad_ranges(self):
        for start, end in ((2, 2), (-1, 0), (0, 4), (3, 1)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(InputError):
                    splice_utf16("A" + chr(0x1F600), start, end, "x")

    def test_insert_allowlist(self):
        for value in ("abc", "A09-_.", "x" * 64):
            with self.subTest(value=value):
                self.assertTrue(is_safe_insert_text(value))
        for value in ("", "a b", "中文", "a/b", "x" * 65, None):
            with self.subTest(value=value):
                self.assertFalse(is_safe_insert_text(value))

    def test_native_selection_fallback_class_whitelist(self):
        for class_name in ("Edit", "RichEdit20W", "RICHEDIT50W"):
            self.assertTrue(_native_edit_class(class_name))
        for class_name in ("Chrome_RenderWidgetHostHWND", "ComboBox", "Static", ""):
            self.assertFalse(_native_edit_class(class_name))

    def test_text_pattern_selection_must_match_value_pattern_document(self):
        value = "A" + chr(0x1F600) + "BC"
        text_pattern = _FakeTextPattern(value, start=1, end=3)
        self.assertEqual((1, 3), _selection_from_text_pattern(_FakeAutomation, text_pattern, value))

        mismatched = _FakeTextPattern("different", start=0, end=0)
        with self.assertRaisesRegex(InputError, "不一致"):
            _selection_from_text_pattern(_FakeAutomation, mismatched, value)


class _FakeAutomation:
    class TextPatternRangeEndpoint:
        Start = 0
        End = 1


class _FakeTextRange:
    def __init__(self, text, start, end):
        self.text = text
        self.start = start
        self.end = end

    def Clone(self):
        return _FakeTextRange(self.text, self.start, self.end)

    def MoveEndpointByRange(self, endpoint, other, other_endpoint, waitTime=0):
        self.assert_compatible(other)
        if endpoint != _FakeAutomation.TextPatternRangeEndpoint.End:
            return False
        if other_endpoint != _FakeAutomation.TextPatternRangeEndpoint.Start:
            return False
        self.end = other.start
        return True

    def GetText(self, _max_length):
        encoded = self.text.encode("utf-16-le")
        return encoded[self.start * 2 : self.end * 2].decode("utf-16-le")

    def assert_compatible(self, other):
        if self.text != other.text:
            raise AssertionError("ranges belong to different documents")


class _FakeTextPattern:
    def __init__(self, text, start, end):
        units = len(text.encode("utf-16-le")) // 2
        self.DocumentRange = _FakeTextRange(text, 0, units)
        self._selection = _FakeTextRange(text, start, end)

    def GetSelection(self):
        return [self._selection]


class _FakeBackend:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.writes = []

    def snapshot(self):
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        else:
            return self.snapshots[0]

    def write(self, expected, value):
        current = self.snapshot()
        if current != expected:
            raise InputError("写入前目标发生变化")
        self.writes.append(value)
        return True


def _snapshot(**changes):
    values = {
        "foreground_hwnd": 101,
        "process_id": 202,
        "runtime_id": (42, 7),
        "value": "ab" + chr(0x1F600) + "cd",
        "selection_start": 2,
        "selection_end": 4,
        "control_type": "EditControl",
        "description": "Code (EditControl, PID 202)",
    }
    values.update(changes)
    return _Snapshot(**values)


class FocusTargetTests(unittest.TestCase):
    def test_insert_splices_verified_utf16_selection_once(self):
        backend = _FakeBackend([_snapshot(), _snapshot(), _snapshot()])
        target = FocusTarget._capture_with_backend(backend)
        self.assertEqual("Code (EditControl, PID 202)", target.description)
        self.assertIsNone(target.validate())
        target.insert("Z9")
        self.assertEqual(["abZ9cd"], backend.writes)
        with self.assertRaisesRegex(InputError, "不能重复写入"):
            target.insert("x")

    def test_value_focus_and_selection_changes_fail_closed(self):
        changes = (
            {"foreground_hwnd": 999},
            {"runtime_id": (42, 8)},
            {"value": "changed"},
            {"selection_start": 1, "selection_end": 1},
        )
        for change in changes:
            with self.subTest(change=change):
                backend = _FakeBackend([_snapshot(), _snapshot(**change)])
                target = FocusTarget._capture_with_backend(backend)
                with self.assertRaises(InputError):
                    target.insert("x")
                self.assertEqual([], backend.writes)

    def test_change_at_final_write_boundary_fails_closed(self):
        backend = _FakeBackend([_snapshot(), _snapshot(), _snapshot(value="late")])
        target = FocusTarget._capture_with_backend(backend)
        with self.assertRaisesRegex(InputError, "写入前目标发生变化"):
            target.insert("x")
        self.assertEqual([], backend.writes)

    def test_unknown_range_and_unsafe_text_are_rejected(self):
        backend = _FakeBackend([_snapshot(selection_start=None, selection_end=None)])
        with self.assertRaisesRegex(InputError, "选区"):
            FocusTarget._capture_with_backend(backend)

        backend = _FakeBackend([_snapshot()])
        target = FocusTarget._capture_with_backend(backend)
        with self.assertRaisesRegex(InputError, "不安全"):
            target.insert("hello world")
        self.assertEqual([], backend.writes)


if __name__ == "__main__":
    unittest.main()
