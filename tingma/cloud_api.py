"""Configurable cloud speech-recognition transports.

Audio accepted by :class:`CloudSession` is mono, 16 kHz, little-endian PCM16.
The module deliberately keeps API credentials in memory and never logs them.
"""

from __future__ import annotations

import base64
import io
import json
import socket
import threading
import time
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlencode, urlsplit, urlunsplit


_WEBSOCKET_PROTOCOLS = frozenset({"qwen_realtime", "dashscope_streaming"})
_PROTOCOLS = _WEBSOCKET_PROTOCOLS | frozenset({"qwen_http", "openai_audio"})
_NETWORK_TIMEOUT_SECONDS = 30.0
_POLL_TIMEOUT_SECONDS = 0.005
_MAX_AUDIO_BYTES = 20 * 16_000 * 2
_CONNECT_SLOT = threading.BoundedSemaphore(1)
_HTTP_SLOT = threading.BoundedSemaphore(1)
_TEST_SLOT = threading.BoundedSemaphore(1)
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "bearer_token",
        "credential",
        "credentials",
        "key",
        "secret",
        "sig",
        "signature",
        "token",
        "x_api_key",
        "x_amz_credential",
        "x_amz_signature",
    }
)


class ApiError(RuntimeError):
    """A safe, user-displayable cloud API failure."""


@dataclass(frozen=True)
class ApiConfig:
    protocol: str = "qwen_realtime"
    endpoint: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)

    def validate(self) -> "ApiConfig":
        if not isinstance(self.protocol, str) or self.protocol not in _PROTOCOLS:
            raise ApiError("不支持的云端协议")
        if not isinstance(self.endpoint, str) or not self.endpoint or _has_control_character(self.endpoint):
            raise ApiError("请填写有效的 API 地址")
        if not isinstance(self.model, str) or not self.model.strip() or _has_control_character(self.model):
            raise ApiError("请填写有效的模型名称")
        if not isinstance(self.api_key, str) or not self.api_key.strip() or _has_control_character(self.api_key):
            raise ApiError("请填写有效的 API Key")
        # These fields are persisted by the UI. Validate the complete values,
        # including encoded URL paths, before allowing that persistence.
        for value in (self.endpoint, self.model):
            while True:
                if self.api_key in value or self.api_key in unquote_plus(value):
                    raise ApiError("请把 API Key 只填写在密钥栏，不要放入地址或模型名称")
                decoded = unquote(value)
                if decoded == value:
                    break
                value = decoded
        if self.model == self.api_key:
            raise ApiError("模型名称不能使用 API Key")

        try:
            parsed = urlsplit(self.endpoint)
            hostname = parsed.hostname
            parsed.port  # Force validation of a malformed port.
        except ValueError as exc:
            raise ApiError("API 地址格式无效") from exc

        required_scheme = "wss" if self.protocol in _WEBSOCKET_PROTOCOLS else "https"
        if parsed.scheme.lower() != required_scheme or not hostname:
            raise ApiError(f"{self.protocol} 必须使用完整的 {required_scheme.upper()} 地址")
        if parsed.username is not None or parsed.password is not None:
            raise ApiError("API 地址不能包含用户名、密码或 API Key")
        if parsed.fragment:
            raise ApiError("API 地址不能包含片段标识")
        for name, value in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = name.casefold().replace("-", "_")
            sensitive_suffix = normalized.endswith(
                ("_credential", "_key", "_secret", "_signature", "_token")
            )
            if (
                normalized in _SENSITIVE_QUERY_NAMES
                or sensitive_suffix
                or value == self.api_key
                or value.casefold().startswith("bearer ")
            ):
                raise ApiError("API 地址的查询参数不能包含认证信息")

        normalized_path = parsed.path.rstrip("/")
        if self.protocol == "qwen_http" and not normalized_path.endswith("/chat/completions"):
            raise ApiError("qwen_http 需要完整的 /chat/completions 地址")
        if self.protocol == "openai_audio" and not normalized_path.endswith("/audio/transcriptions"):
            raise ApiError("openai_audio 需要完整的 /audio/transcriptions 地址")
        if self.protocol == "dashscope_streaming" and not normalized_path.endswith(
            "/api-ws/v1/inference"
        ):
            raise ApiError("dashscope_streaming 需要完整的 /api-ws/v1/inference 地址")
        return self


class CloudSession:
    """One cancellable cloud ASR session.

    Callbacks are dispatched from daemon background threads. ``cancel`` is
    idempotent and suppresses every callback that has not started yet.
    """

    def __init__(
        self,
        config: ApiConfig,
        on_partial: Callable[[str], Any],
        on_final: Callable[[str], Any],
        on_error: Callable[[str], Any],
        *,
        on_segment: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_error = on_error
        # Only provider-committed sentences reach this callback, never a stash
        # or interim hypothesis. The consumer may finish its one-shot workflow.
        self.on_segment = on_segment

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._audio_queue: deque[bytes] = deque()
        self._queued_bytes = 0
        self._audio_buffer = bytearray()
        self._started = False
        self._finishing = False
        self._cancelled = False
        self._failed = False
        self._thread: threading.Thread | None = None
        self._websocket: Any = None
        self._http_client: Any = None

    def start(self) -> None:
        self.config.validate()
        with self._condition:
            if self._started:
                raise ApiError("云端识别会话已经启动")
            self._started = True
            if self.config.protocol in _WEBSOCKET_PROTOCOLS:
                self._thread = threading.Thread(
                    target=(
                        self._run_realtime
                        if self.config.protocol == "qwen_realtime"
                        else self._run_dashscope_streaming
                    ),
                    name="tingma-cloud-asr-realtime",
                    daemon=True,
                )
                self._thread.start()

    def feed(self, pcm16: bytes) -> None:
        if not isinstance(pcm16, (bytes, bytearray, memoryview)):
            raise ApiError("音频数据必须是 PCM16 字节")
        chunk = bytes(pcm16)
        if len(chunk) % 2:
            raise ApiError("PCM16 音频数据必须按完整采样点传入")
        if not chunk:
            return

        overflow = False
        with self._condition:
            self._require_feedable()
            if self.config.protocol in _WEBSOCKET_PROTOCOLS:
                if self._queued_bytes + len(chunk) > _MAX_AUDIO_BYTES:
                    overflow = True
                else:
                    self._audio_queue.append(chunk)
                    self._queued_bytes += len(chunk)
                    self._condition.notify_all()
            elif len(self._audio_buffer) + len(chunk) > _MAX_AUDIO_BYTES:
                overflow = True
            else:
                self._audio_buffer.extend(chunk)

        if overflow:
            self._fail_from_caller("录音缓存已达到 20 秒上限，请缩短本次录音")

    def finish(self) -> None:
        with self._condition:
            if not self._started:
                raise ApiError("云端识别会话尚未启动")
            if self._cancelled or self._failed or self._finishing:
                return
            self._finishing = True
            if self.config.protocol in _WEBSOCKET_PROTOCOLS:
                self._condition.notify_all()
                return
            self._thread = threading.Thread(
                target=self._run_http,
                name="tingma-cloud-asr-http",
                daemon=True,
            )
            self._thread.start()

    def cancel(self) -> None:
        with self._condition:
            if self._cancelled:
                return
            self._cancelled = True
            self._stop_event.set()
            self._audio_queue.clear()
            self._queued_bytes = 0
            self._audio_buffer.clear()
            websocket = self._websocket
            http_client = self._http_client
            self._condition.notify_all()
        _close_quietly(websocket)
        _close_quietly(http_client)

    def _require_feedable(self) -> None:
        if not self._started:
            raise ApiError("云端识别会话尚未启动")
        if self._cancelled:
            raise ApiError("云端识别会话已取消")
        if self._failed:
            raise ApiError("云端识别会话已经失败")
        if self._finishing:
            raise ApiError("云端识别会话已经结束接收音频")

    def _fail_from_caller(self, message: str) -> None:
        with self._condition:
            if self._failed or self._cancelled:
                return
            self._failed = True
            self._stop_event.set()
            websocket = self._websocket
            self._condition.notify_all()
        _close_quietly(websocket)
        threading.Thread(
            target=self._invoke_callback,
            args=(self.on_error, message),
            name="tingma-cloud-asr-error",
            daemon=True,
        ).start()

    def _run_realtime(self) -> None:
        websocket = None
        try:
            endpoint = _realtime_endpoint(self.config)
            websocket = _create_websocket(
                endpoint,
                header=[f"Authorization: Bearer {self.config.api_key}"],
                timeout=_NETWORK_TIMEOUT_SECONDS,
                redirect_limit=0,
            )
            with self._condition:
                if self._cancelled or self._failed:
                    _close_quietly(websocket)
                    return
                self._websocket = websocket

            first_event = _decode_event(websocket.recv())
            _raise_for_provider_event(first_event)
            if first_event.get("type") != "session.created":
                raise ApiError("云端实时接口未返回 session.created")
            _send_event(
                websocket,
                "session.update",
                session={
                    "input_audio_format": "pcm",
                    "sample_rate": 16_000,
                    "input_audio_transcription": {"language": "zh"},
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": 650,
                    },
                },
            )
            if hasattr(websocket, "settimeout"):
                websocket.settimeout(_POLL_TIMEOUT_SECONDS)

            finish_sent = False
            completed: list[str] = []
            last_network_progress = time.monotonic()
            while not self._stop_event.is_set():
                chunks, should_finish = self._take_audio_batch(finish_sent)
                for chunk in chunks:
                    _send_event(
                        websocket,
                        "input_audio_buffer.append",
                        audio=base64.b64encode(chunk).decode("ascii"),
                    )
                    last_network_progress = time.monotonic()
                if should_finish:
                    _send_event(websocket, "session.finish")
                    finish_sent = True
                    last_network_progress = time.monotonic()

                try:
                    raw = websocket.recv()
                except Exception as exc:
                    if _is_timeout(exc):
                        if time.monotonic() - last_network_progress >= _NETWORK_TIMEOUT_SECONDS:
                            raise ApiError("云端实时接口 30 秒内没有响应") from exc
                        continue
                    raise

                if raw in (None, "", b""):
                    raise ApiError("云端实时连接意外关闭")
                last_network_progress = time.monotonic()
                event = _decode_event(raw)
                _raise_for_provider_event(event)
                event_type = event.get("type")
                if event_type == "conversation.item.input_audio_transcription.text":
                    preview = "".join(completed) + str(event.get("text", "")) + str(
                        event.get("stash", "")
                    )
                    self._invoke_callback(self.on_partial, preview)
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = event.get("transcript")
                    if isinstance(transcript, str):
                        completed.append(transcript)
                        if self.on_segment is not None:
                            self._invoke_callback(self.on_segment, "".join(completed))
                elif event_type == "session.finished":
                    if not finish_sent:
                        raise ApiError("云端实时接口提前结束了会话")
                    self._invoke_callback(self.on_final, "".join(completed))
                    return
        except Exception as exc:
            self._worker_error(exc)
        finally:
            with self._condition:
                if self._websocket is websocket:
                    self._websocket = None
            _close_quietly(websocket)

    def _run_dashscope_streaming(self) -> None:
        websocket = None
        task_id = str(uuid.uuid4())
        try:
            websocket = _create_websocket(
                self.config.endpoint,
                header=[f"Authorization: Bearer {self.config.api_key}"],
                timeout=_NETWORK_TIMEOUT_SECONDS,
                redirect_limit=0,
            )
            with self._condition:
                if self._cancelled or self._failed:
                    _close_quietly(websocket)
                    return
                self._websocket = websocket

            _send_dashscope_control(
                websocket,
                "run-task",
                task_id,
                payload={
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": self.config.model,
                    "parameters": {"format": "pcm", "sample_rate": 16_000},
                    "input": {},
                },
            )
            started = _decode_event(websocket.recv())
            _raise_for_dashscope_event(started)
            _require_dashscope_task(started, task_id)
            if _dashscope_event_type(started) != "task-started":
                raise ApiError("DashScope 流式接口未返回 task-started")
            if hasattr(websocket, "settimeout"):
                websocket.settimeout(_POLL_TIMEOUT_SECONDS)

            finish_sent = False
            completed: list[str] = []
            last_network_progress = time.monotonic()
            while not self._stop_event.is_set():
                chunks, should_finish = self._take_audio_batch(finish_sent)
                for chunk in chunks:
                    websocket.send_binary(chunk)
                    last_network_progress = time.monotonic()
                if should_finish:
                    _send_dashscope_control(
                        websocket,
                        "finish-task",
                        task_id,
                        payload={"input": {}},
                    )
                    finish_sent = True
                    last_network_progress = time.monotonic()

                try:
                    raw = websocket.recv()
                except Exception as exc:
                    if _is_timeout(exc):
                        if time.monotonic() - last_network_progress >= _NETWORK_TIMEOUT_SECONDS:
                            raise ApiError("DashScope 流式接口 30 秒内没有响应") from exc
                        continue
                    raise
                if raw in (None, "", b""):
                    raise ApiError("DashScope 流式连接意外关闭")
                last_network_progress = time.monotonic()
                event = _decode_event(raw)
                _raise_for_dashscope_event(event)
                _require_dashscope_task(event, task_id)
                event_type = _dashscope_event_type(event)
                if event_type == "result-generated":
                    sentence = _dashscope_sentence(event)
                    if not sentence or sentence.get("heartbeat") is True:
                        continue
                    text = sentence.get("text")
                    if not isinstance(text, str):
                        raise ApiError("DashScope 流式结果缺少转写文本")
                    if sentence.get("sentence_end") is True:
                        completed.append(text)
                        if self.on_segment is not None:
                            self._invoke_callback(self.on_segment, "".join(completed))
                    else:
                        self._invoke_callback(self.on_partial, "".join(completed) + text)
                elif event_type == "task-finished":
                    if not finish_sent:
                        raise ApiError("DashScope 流式接口提前结束了任务")
                    self._invoke_callback(self.on_final, "".join(completed))
                    return
        except Exception as exc:
            self._worker_error(exc)
        finally:
            with self._condition:
                if self._websocket is websocket:
                    self._websocket = None
            _close_quietly(websocket)

    def _take_audio_batch(self, finish_sent: bool) -> tuple[list[bytes], bool]:
        chunks: list[bytes] = []
        with self._condition:
            while self._audio_queue:
                chunk = self._audio_queue.popleft()
                self._queued_bytes -= len(chunk)
                chunks.append(chunk)
            should_finish = not chunks and self._finishing and not finish_sent
        return chunks, should_finish

    def _run_http(self) -> None:
        if not _HTTP_SLOT.acquire(blocking=False):
            self._worker_error(ApiError("上一轮网络请求仍在结束，请稍后重试。"))
            return
        try:
            with self._lock:
                pcm16 = bytes(self._audio_buffer)
            wav_data = _pcm16_to_wav(pcm16)
            client = _new_http_client()
            with self._condition:
                if self._cancelled or self._failed:
                    _close_quietly(client)
                    return
                self._http_client = client
            with client:
                headers = {"Authorization": f"Bearer {self.config.api_key}"}
                if self.config.protocol == "qwen_http":
                    payload = {
                        "model": self.config.model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_audio",
                                        "input_audio": {
                                            "data": "data:audio/wav;base64,"
                                            + base64.b64encode(wav_data).decode("ascii")
                                        },
                                    }
                                ],
                            }
                        ],
                        "stream": False,
                        "asr_options": {"enable_itn": False},
                    }
                    response = client.post(self.config.endpoint, headers=headers, json=payload)
                    _check_http_response(response, self.config.api_key)
                    result = _qwen_result(response)
                else:
                    response = client.post(
                        self.config.endpoint,
                        headers=headers,
                        data={"model": self.config.model},
                        files={"file": ("audio.wav", wav_data, "audio/wav")},
                    )
                    _check_http_response(response, self.config.api_key)
                    result = _openai_audio_result(response)
            self._invoke_callback(self.on_final, result)
        except Exception as exc:
            self._worker_error(exc)
        finally:
            with self._condition:
                self._http_client = None
            _HTTP_SLOT.release()

    def _worker_error(self, exc: Exception) -> None:
        with self._condition:
            if self._cancelled or self._failed:
                return
            self._failed = True
        safe_message = _redact(str(exc) or exc.__class__.__name__, self.config.api_key)
        self._invoke_callback(self.on_error, safe_message)

    def _invoke_callback(self, callback: Callable[[str], Any], value: str) -> None:
        with self._lock:
            if self._cancelled:
                return
            try:
                callback(value)
            except Exception:
                # Application callbacks must not corrupt the transport worker.
                pass


def test_connection(config: ApiConfig) -> str:
    """Verify credentials/model availability without uploading audio."""
    if not _TEST_SLOT.acquire(blocking=False):
        raise ApiError("上一次连接测试尚未结束，请稍后重试。")
    try:
        return _test_connection(config)
    finally:
        _TEST_SLOT.release()


def _test_connection(config: ApiConfig) -> str:

    config.validate()
    if config.protocol in _WEBSOCKET_PROTOCOLS:
        websocket = None
        try:
            websocket = _create_websocket(
                (
                    _realtime_endpoint(config)
                    if config.protocol == "qwen_realtime"
                    else config.endpoint
                ),
                header=[f"Authorization: Bearer {config.api_key}"],
                timeout=_NETWORK_TIMEOUT_SECONDS,
                redirect_limit=0,
            )
            if config.protocol == "qwen_realtime":
                event = _decode_event(websocket.recv())
                _raise_for_provider_event(event)
                if event.get("type") != "session.created":
                    raise ApiError("云端实时接口未返回 session.created")
                session = event.get("session")
                if not isinstance(session, dict) or session.get("model") != config.model:
                    raise ApiError("身份认证成功，但实时接口返回的模型与配置不一致")
        except Exception as exc:
            if isinstance(exc, ApiError):
                raise ApiError(_redact(str(exc), config.api_key)) from None
            raise ApiError(_redact(str(exc) or exc.__class__.__name__, config.api_key)) from None
        finally:
            _close_quietly(websocket)
        if config.protocol == "dashscope_streaming":
            return "连接成功：身份认证有效；未发送任务，未测试模型可用性或语音识别。"
        return "连接成功：身份认证有效且模型可用；未上传音频，未测试语音识别。"

    client = _new_http_client()
    try:
        with client:
            response = client.get(
                _models_endpoint(config),
                headers={"Authorization": f"Bearer {config.api_key}"},
            )
            _check_http_response(response, config.api_key)
            payload = response.json()
            models = payload.get("data") if isinstance(payload, dict) else None
            model_ids = {
                item.get("id")
                for item in models or []
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            if config.model not in model_ids:
                raise ApiError(f"身份认证成功，但模型 {config.model} 不在可用模型列表中")
    except Exception as exc:
        if isinstance(exc, ApiError):
            raise ApiError(_redact(str(exc), config.api_key)) from None
        raise ApiError(_redact(str(exc) or exc.__class__.__name__, config.api_key)) from None
    return "连接成功：身份认证有效且模型可用；未上传音频，未测试语音识别。"


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _redact(message: str, api_key: str) -> str:
    redacted = str(message)
    if not api_key:
        return redacted
    variants = {api_key, quote(api_key, safe=""), quote_plus(api_key)}
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            redacted = redacted.replace(variant, "[REDACTED]")
    return redacted


def _realtime_endpoint(config: ApiConfig) -> str:
    parsed = urlsplit(config.endpoint)
    query = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if name.casefold() != "model"
    ]
    query.append(("model", config.model))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _models_endpoint(config: ApiConfig) -> str:
    parsed = urlsplit(config.endpoint)
    path = parsed.path.rstrip("/")
    suffix = "/chat/completions" if config.protocol == "qwen_http" else "/audio/transcriptions"
    path = path[: -len(suffix)] + "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def _send_event(websocket: Any, event_type: str, **values: Any) -> None:
    event = {"event_id": _event_id(), "type": event_type}
    event.update(values)
    websocket.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))


def _send_dashscope_control(
    websocket: Any,
    action: str,
    task_id: str,
    *,
    payload: dict[str, Any],
) -> None:
    event = {
        "header": {"action": action, "task_id": task_id, "streaming": "duplex"},
        "payload": payload,
    }
    websocket.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))


def _decode_event(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        event = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError("云端接口返回了无法解析的消息") from exc
    if not isinstance(event, dict):
        raise ApiError("云端接口返回了无效的消息结构")
    return event


def _raise_for_provider_event(event: dict[str, Any]) -> None:
    if event.get("type") not in {"error", "conversation.item.input_audio_transcription.failed"}:
        return
    error = event.get("error")
    if not isinstance(error, dict):
        raise ApiError("云端语音识别失败")
    code = error.get("code")
    message = error.get("message")
    detail = ": ".join(str(value) for value in (code, message) if value)
    raise ApiError(detail or "云端语音识别失败")


def _dashscope_event_type(event: dict[str, Any]) -> Any:
    header = event.get("header")
    return header.get("event") if isinstance(header, dict) else None


def _require_dashscope_task(event: dict[str, Any], task_id: str) -> None:
    header = event.get("header")
    if not isinstance(header, dict) or header.get("task_id") != task_id:
        raise ApiError("DashScope 流式接口返回了不匹配的任务事件")


def _raise_for_dashscope_event(event: dict[str, Any]) -> None:
    if _dashscope_event_type(event) != "task-failed":
        return
    header = event.get("header")
    if not isinstance(header, dict):
        raise ApiError("DashScope 流式语音识别失败")
    code = header.get("error_code")
    message = header.get("error_message")
    detail = ": ".join(str(value) for value in (code, message) if value)
    raise ApiError(detail or "DashScope 流式语音识别失败")


def _dashscope_sentence(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload")
    output = payload.get("output") if isinstance(payload, dict) else None
    sentence = output.get("sentence") if isinstance(output, dict) else None
    return sentence if isinstance(sentence, dict) else None


def _pcm16_to_wav(pcm16: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16_000)
        wav_file.writeframes(pcm16)
    return output.getvalue()


def _check_http_response(response: Any, api_key: str) -> None:
    status = int(response.status_code)
    if 200 <= status < 300:
        return
    # Redact before truncation: otherwise a key crossing the display limit
    # could leave its prefix visible in the error message.
    body = _redact(str(getattr(response, "text", "")), api_key)[:2_000].strip()
    if 300 <= status < 400:
        raise ApiError(f"云端接口返回重定向 HTTP {status}，为保护凭据已拒绝跟随")
    detail = f"：{body}" if body else ""
    raise ApiError(f"云端接口请求失败（HTTP {status}）{detail}")


def _qwen_result(response: Any) -> str:
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise ApiError("Qwen ASR 返回结果缺少转写文本") from exc
    if not isinstance(content, str):
        raise ApiError("Qwen ASR 返回结果缺少转写文本")
    return content


def _openai_audio_result(response: Any) -> str:
    try:
        text = response.json()["text"]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ApiError("音频转写接口返回结果缺少 text 字段") from exc
    if not isinstance(text, str):
        raise ApiError("音频转写接口返回结果缺少 text 字段")
    return text


def _is_timeout(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout)) or exc.__class__.__name__ in {
        "WebSocketTimeoutException",
    }


def _close_quietly(resource: Any) -> None:
    if resource is None:
        return
    try:
        if callable(getattr(resource, "shutdown", None)):
            # websocket-client.close() waits up to three seconds for a peer
            # close handshake; cancellation must not freeze the Qt thread.
            resource.shutdown()
        else:
            resource.close()
    except Exception:
        pass


def _create_websocket(url: str, **kwargs: Any) -> Any:
    try:
        import websocket
    except ImportError as exc:
        raise ApiError("缺少 websocket-client 依赖") from exc
    # A socket under DNS/TLS/proxy setup has no handle cancel() can close.
    # Permit only one such setup and fail subsequent attempts immediately;
    # retries never queue more blocked handshakes holding credentials.
    if not _CONNECT_SLOT.acquire(blocking=False):
        raise ApiError("上一轮连接仍在结束，请稍后重试。")
    try:
        return websocket.create_connection(url, **kwargs)
    finally:
        _CONNECT_SLOT.release()


def _new_http_client() -> Any:
    try:
        import httpx
    except ImportError as exc:
        raise ApiError("缺少 httpx 依赖") from exc
    return httpx.Client(
        timeout=httpx.Timeout(_NETWORK_TIMEOUT_SECONDS),
        follow_redirects=False,
    )


__all__ = ["ApiConfig", "ApiError", "CloudSession", "test_connection"]
