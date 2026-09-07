"""Windows WASAPI loopback capture with 16 kHz PCM callbacks.

The module deliberately has no Windows-only imports at module load time so its
pure buffering behavior can be tested on other platforms.
"""

from __future__ import annotations

from fractions import Fraction
import sys
import threading
from typing import Callable, List, Optional


_CALLBACK_BYTES = 16_000 // 50 * 2  # 20 ms of mono signed 16-bit PCM.
_np = None
_resample_poly = None


class AudioError(RuntimeError):
    """Raised when safe system-loopback capture cannot be provided."""


class _PcmChunker:
    """Turn a byte stream into fixed callbacks without allowing backlog growth."""

    def __init__(self, chunk_bytes: int = _CALLBACK_BYTES, max_buffer_bytes: int = 12_800):
        if chunk_bytes <= 0 or max_buffer_bytes < chunk_bytes:
            raise ValueError("PCM 缓冲区参数无效")
        self._chunk_bytes = chunk_bytes
        self._max_buffer_bytes = max_buffer_bytes
        self._buffer = bytearray()

    def feed(self, data: bytes) -> List[bytes]:
        if not isinstance(data, bytes):
            raise TypeError("PCM 数据必须是 bytes")
        if len(self._buffer) + len(data) > self._max_buffer_bytes:
            self._buffer.clear()
            raise AudioError("音频转换缓冲区溢出")
        self._buffer.extend(data)
        chunks = []
        while len(self._buffer) >= self._chunk_bytes:
            chunks.append(bytes(self._buffer[: self._chunk_bytes]))
            del self._buffer[: self._chunk_bytes]
        return chunks


def _load_audio_math() -> None:
    global _np, _resample_poly
    if _np is not None and _resample_poly is not None:
        return
    try:
        import numpy as np
        from scipy.signal import resample_poly
    except ImportError as exc:  # pragma: no cover - exercised on a Windows install.
        raise AudioError("Windows 音频需要安装 numpy 和 scipy") from exc
    _np = np
    _resample_poly = resample_poly


def _float32_to_pcm16(raw: bytes, channels: int, sample_rate: int) -> bytes:
    """Downmix interleaved float32 and resample it to mono 16 kHz PCM16."""

    _load_audio_math()
    np = _np

    if channels <= 0 or sample_rate <= 0:
        raise AudioError("WASAPI 返回了无效的音频格式")
    samples = np.frombuffer(raw, dtype="<f4")
    if samples.size == 0:
        return b""
    if samples.size % channels:
        raise AudioError("WASAPI 返回了不完整的交错音频帧")

    frames = samples.reshape((-1, channels))
    mono = frames.mean(axis=1, dtype=np.float32)
    if sample_rate != 16_000:
        ratio = Fraction(16_000, sample_rate)
        mono = _resample_poly(mono, ratio.numerator, ratio.denominator)
    mono = np.nan_to_num(mono, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = np.rint(np.clip(mono, -1.0, 1.0) * 32767.0).astype("<i2")
    return pcm.tobytes()


class LoopbackAudio:
    """Capture the default Windows output device through WASAPI loopback."""

    def __init__(self, on_audio: Callable[[bytes], None], on_error: Callable[[str], None]):
        if not callable(on_audio) or not callable(on_error):
            raise TypeError("on_audio 和 on_error 必须是可调用对象")
        self._on_audio = on_audio
        self._on_error = on_error
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stream = None
        self._audio = None
        self._pyaudio = None
        self._error_reported = False

    def start(self) -> str:
        """Start capture and return the selected loopback device name."""

        if sys.platform != "win32":
            raise AudioError("WASAPI 系统声音采集仅支持 Windows 10/11")
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:  # pragma: no cover - Windows dependency check.
            raise AudioError("Windows 上需要安装 PyAudioWPatch 0.2.12.8") from exc
        # scipy's first import can be slow. Finish it before opening PortAudio,
        # otherwise the device buffer can overflow before the first read.
        _load_audio_math()

        with self._lock:
            if self._thread is not None:
                raise AudioError("系统声音采集已经在运行")
            self._stop_event.clear()
            self._error_reported = False

        audio = None
        stream = None
        try:
            audio = pyaudio.PyAudio()
            device = audio.get_default_wasapi_loopback()
            if not isinstance(device, dict):
                raise AudioError("WASAPI 没有提供默认系统回放设备")
            device_index = int(device["index"])
            channels = int(device["maxInputChannels"])
            sample_rate_value = float(device["defaultSampleRate"])
            sample_rate = int(round(sample_rate_value))
            if channels <= 0 or sample_rate <= 0:
                raise AudioError("默认 WASAPI 系统回放设备没有可用的输入格式")
            if abs(sample_rate - sample_rate_value) > 0.001:
                raise AudioError("WASAPI 系统回放设备返回了不支持的采样率")
            frames_per_buffer = max(1, int(round(sample_rate / 50.0)))
            stream = audio.open(
                format=pyaudio.paFloat32,
                channels=channels,
                rate=sample_rate,
                input=True,
                input_device_index=device_index,
                frames_per_buffer=frames_per_buffer,
            )
            device_name = str(device.get("name") or "默认 WASAPI 系统回放设备")
        except Exception as exc:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            try:
                if audio is not None:
                    audio.terminate()
            except Exception:
                pass
            if isinstance(exc, AudioError):
                raise
            raise AudioError("无法启动 WASAPI 系统声音采集：{}".format(exc)) from exc

        thread = threading.Thread(
            target=self._capture_loop,
            args=(channels, sample_rate, frames_per_buffer),
            name="tingma-wasapi-loopback",
            daemon=True,
        )
        with self._lock:
            self._pyaudio = pyaudio
            self._audio = audio
            self._stream = stream
            self._thread = thread
        try:
            thread.start()
        except Exception as exc:
            with self._lock:
                self._thread = None
                self._stream = None
                self._audio = None
                self._pyaudio = None
            self._close_stream(stream)
            try:
                audio.terminate()
            except Exception:
                pass
            raise AudioError("无法启动声音采集线程：{}".format(exc)) from exc
        return device_name

    def stop(self) -> None:
        """Stop capture, closing PortAudio resources with bounded waits."""

        with self._lock:
            thread = self._thread
            stream = self._stream
            self._stop_event.set()
        if thread is None:
            return
        if thread is threading.current_thread():
            return

        thread.join(timeout=1.0)
        if thread.is_alive() and stream is not None:
            # A backend read should last only 20 ms. Closing is the bounded
            # escape hatch for a driver that ignores that contract.
            closer = threading.Thread(
                target=self._close_stream,
                args=(stream,),
                name="tingma-wasapi-close",
                daemon=True,
            )
            closer.start()
            closer.join(timeout=0.5)
            thread.join(timeout=0.5)
        if thread.is_alive():
            self._report_error("WASAPI 采集线程未能在 2 秒内停止")

    def _capture_loop(self, channels: int, sample_rate: int, frames_per_buffer: int) -> None:
        chunker = _PcmChunker()
        try:
            while not self._stop_event.is_set():
                try:
                    raw = self._stream.read(frames_per_buffer, exception_on_overflow=True)
                except OSError as exc:
                    overflow_code = getattr(self._pyaudio, "paInputOverflowed", None)
                    if overflow_code in exc.args or "overflow" in str(exc).lower():
                        raise AudioError("WASAPI 输入缓冲区溢出，已停止声音采集") from exc
                    raise AudioError("WASAPI 读取失败：{}".format(exc)) from exc
                for chunk in chunker.feed(
                    _float32_to_pcm16(raw, channels=channels, sample_rate=sample_rate)
                ):
                    if self._stop_event.is_set():
                        break
                    try:
                        self._on_audio(chunk)
                    except Exception as exc:
                        raise AudioError("音频回调处理失败：{}".format(exc)) from exc
        except Exception as exc:
            if not self._stop_event.is_set():
                self._report_error(str(exc))
        finally:
            self._stop_event.set()
            with self._lock:
                stream = self._stream
                audio = self._audio
            if stream is not None:
                try:
                    if not stream.is_stopped():
                        stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if audio is not None:
                try:
                    audio.terminate()
                except Exception:
                    pass
            with self._lock:
                self._stream = None
                self._audio = None
                self._pyaudio = None
                self._thread = None

    def _report_error(self, message: str) -> None:
        with self._lock:
            if self._error_reported:
                return
            self._error_reported = True
        try:
            self._on_error(message)
        except Exception:
            pass

    @staticmethod
    def _close_stream(stream) -> None:
        try:
            stream.close()
        except Exception:
            pass
