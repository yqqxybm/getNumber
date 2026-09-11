from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tingma.cloud_api import ApiConfig, ApiError, CloudSession, test_connection


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _HttpClient:
    def __init__(self, *, post_response=None, get_response=None) -> None:
        self.post_response = post_response
        self.get_response = get_response
        self.posts = []
        self.gets = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.closed = True

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_response

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.get_response


class _RealtimeWebSocket:
    def __init__(self) -> None:
        self.sent = []
        self.closed = False
        self._messages = [
            json.dumps({"type": "session.created", "session": {"model": "asr-live"}})
        ]

    def settimeout(self, value):
        self.timeout = value

    def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.update":
            self._messages.append(json.dumps({"type": "session.updated"}))
        elif event["type"] == "input_audio_buffer.append":
            self._messages.append(
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.text",
                        "text": "你",
                        "stash": "好",
                    }
                )
            )
        elif event["type"] == "session.finish":
            self._messages.extend(
                [
                    json.dumps(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": "你好",
                        }
                    ),
                    json.dumps({"type": "session.finished"}),
                ]
            )

    def recv(self):
        if self._messages:
            return self._messages.pop(0)
        raise socket.timeout()

    def close(self):
        self.closed = True


class _IdleWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self._created = False

    def settimeout(self, value):
        pass

    def send(self, raw):
        pass

    def recv(self):
        if not self._created:
            self._created = True
            return json.dumps({"type": "session.created", "session": {"model": "m"}})
        time.sleep(0.005)
        raise socket.timeout()

    def close(self):
        self.closed = True


class _DashscopeWebSocket:
    def __init__(self) -> None:
        self.controls = []
        self.binary = []
        self.closed = False
        self.started_delivered = False
        self._messages = []

    def settimeout(self, value):
        self.timeout = value

    def send(self, raw):
        event = json.loads(raw)
        self.controls.append(event)
        action = event["header"]["action"]
        task_id = event["header"]["task_id"]
        if action == "run-task":
            self._messages.append(
                json.dumps(
                    {
                        "header": {"event": "task-started", "task_id": task_id},
                        "payload": {},
                    }
                )
            )
        elif action == "finish-task":
            self._messages.append(
                json.dumps(
                    {
                        "header": {"event": "task-finished", "task_id": task_id},
                        "payload": {},
                    }
                )
            )

    def send_binary(self, data):
        if not self.started_delivered:
            raise AssertionError("binary audio sent before task-started")
        self.binary.append(data)
        task_id = self.controls[0]["header"]["task_id"]
        self._messages.extend(
            [
                json.dumps(
                    {
                        "header": {"event": "result-generated", "task_id": task_id},
                        "payload": {
                            "output": {
                                "sentence": {
                                    "text": "听码",
                                    "sentence_end": False,
                                    "heartbeat": False,
                                }
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "header": {"event": "result-generated", "task_id": task_id},
                        "payload": {
                            "output": {
                                "sentence": {
                                    "text": "听码",
                                    "sentence_end": True,
                                    "heartbeat": False,
                                }
                            }
                        },
                    }
                ),
            ]
        )

    def recv(self):
        if self._messages:
            raw = self._messages.pop(0)
            if json.loads(raw)["header"]["event"] == "task-started":
                self.started_delivered = True
            return raw
        raise socket.timeout()

    def close(self):
        self.closed = True


class _BlockingWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self._closed = threading.Event()

    def send(self, raw):
        pass

    def recv(self):
        self._closed.wait(1)
        return ""

    def close(self):
        self.closed = True
        self._closed.set()


class _BurstRealtimeWebSocket:
    def __init__(self, release_created) -> None:
        self.release_created = release_created
        self.append_count = 0
        self.append_count_at_first_poll = None
        self._first = True
        self._messages = []

    def settimeout(self, value):
        pass

    def send(self, raw):
        event = json.loads(raw)
        if event["type"] == "session.update":
            self._messages.append(json.dumps({"type": "session.updated"}))
        elif event["type"] == "input_audio_buffer.append":
            self.append_count += 1
        elif event["type"] == "session.finish":
            self._messages.extend(
                [
                    json.dumps({"type": "session.finished"}),
                ]
            )

    def recv(self):
        if self._first:
            self._first = False
            self.release_created.wait(1)
            return json.dumps({"type": "session.created"})
        if self.append_count_at_first_poll is None:
            self.append_count_at_first_poll = self.append_count
        if self._messages:
            return self._messages.pop(0)
        raise socket.timeout()

    def close(self):
        pass


class CloudApiTests(unittest.TestCase):
    def test_completed_segments_arrive_before_session_finish_and_can_cancel(self):
        class RealtimeSegments(_RealtimeWebSocket):
            def send(self, raw):
                event = json.loads(raw); self.sent.append(event)
                if event['type'] == 'session.update':
                    self._messages.extend(json.dumps({
                        'type': 'conversation.item.input_audio_transcription.completed',
                        'transcript': text,
                    }) for text in ('这件29。', '扣一个00。'))

        class DashscopeSegments(_DashscopeWebSocket):
            def send_binary(self, data):
                self.binary.append(data)
                task_id = self.controls[0]['header']['task_id']
                self._messages.extend(json.dumps({
                    'header': {'event': 'result-generated', 'task_id': task_id},
                    'payload': {'output': {'sentence': {'text': text, 'sentence_end': True}}},
                }) for text in ('这件29。', '扣一个00。'))

        for protocol, ws in (('qwen_realtime', RealtimeSegments()), ('dashscope_streaming', DashscopeSegments())):
            with self.subTest(protocol=protocol):
                segments, unexpected = [], []
                done = threading.Event()
                def segment(text):
                    segments.append(text)
                    if len(segments) == 2:
                        session.cancel(); done.set()
                path = 'realtime' if protocol == 'qwen_realtime' else 'api-ws/v1/inference'
                config = ApiConfig(protocol, 'wss://example.test/' + path, 'm', 'test-key')
                with mock.patch('tingma.cloud_api._create_websocket', return_value=ws):
                    session = CloudSession(config, unexpected.append, unexpected.append, unexpected.append, on_segment=segment)
                    try:
                        session.start()
                        if protocol == 'dashscope_streaming':
                            session.feed(b'\0' * 640)
                        self.assertTrue(done.wait(1))
                    finally:
                        session.cancel(); session._thread.join(1)
                self.assertEqual(['这件29。', '这件29。扣一个00。'], segments)
                self.assertEqual([], unexpected)
                self.assertTrue(ws.closed)
                self.assertFalse(session._finishing)

    def test_config_is_frozen_validates_tls_and_hides_key(self):
        config = ApiConfig(
            endpoint="wss://workspace.example/api-ws/v1/realtime",
            model="asr-live",
            api_key="top-secret",
        )
        self.assertIs(config.validate(), config)
        self.assertNotIn("top-secret", repr(config))
        with self.assertRaises(Exception):
            config.model = "changed"

        bad_endpoints = (
            "ws://workspace.example/realtime",
            "wss://user:pass@workspace.example/realtime",
            "wss://workspace.example/realtime?access_token=secret",
            "wss://workspace.example/realtime?session=Bearer%20secret",
            "wss://workspace.example/realtime#fragment",
        )
        for endpoint in bad_endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(ApiError):
                ApiConfig(endpoint=endpoint, model="m", api_key="k").validate()

    def test_config_requires_protocol_specific_full_endpoint(self):
        valid = (
            ApiConfig("qwen_http", "https://example.test/v1/chat/completions", "m", "k"),
            ApiConfig("openai_audio", "https://example.test/v1/audio/transcriptions", "m", "k"),
            ApiConfig(
                "dashscope_streaming",
                "wss://example.test/api-ws/v1/inference",
                "m",
                "k",
            ),
        )
        for config in valid:
            self.assertIs(config.validate(), config)

        with self.assertRaises(ApiError):
            ApiConfig("unknown", "https://example.test/v1", "m", "k").validate()
        with self.assertRaises(ApiError):
            ApiConfig("qwen_http", "https://example.test/v1", "m", "k").validate()
        with self.assertRaises(ApiError):
            ApiConfig([], "https://example.test/v1", "m", "k").validate()
        key = "must-not-leak"
        with self.assertRaises(ApiError) as raised:
            ApiConfig(key, "https://example.test/v1", "m", key).validate()
        self.assertNotIn(key, str(raised.exception))
        with self.assertRaises(ApiError):
            ApiConfig(
                "dashscope_streaming", "wss://example.test/realtime", "m", "k"
            ).validate()

    def test_realtime_sends_official_schema_and_delivers_final_after_finish(self):
        ws = _RealtimeWebSocket()
        connected = {}

        def connect(url, **kwargs):
            connected.update(url=url, kwargs=kwargs)
            return ws

        partials = []
        finals = []
        errors = []
        done = threading.Event()
        config = ApiConfig(
            endpoint="wss://workspace.example/api-ws/v1/realtime?region=cn",
            model="qwen/asr live",
            api_key="secret",
        )
        with mock.patch("tingma.cloud_api._create_websocket", side_effect=connect):
            session = CloudSession(config, partials.append, lambda text: (finals.append(text), done.set()), errors.append)
            session.start()
            session.feed(b"\x01\x00" * 160)
            session.finish()
            self.assertTrue(done.wait(1), "realtime worker did not finish")

        query = parse_qs(urlsplit(connected["url"]).query)
        self.assertEqual(["qwen/asr live"], query["model"])
        self.assertEqual(["cn"], query["region"])
        self.assertEqual(0, connected["kwargs"]["redirect_limit"])
        self.assertIn("Authorization: Bearer secret", connected["kwargs"]["header"])
        update = next(item for item in ws.sent if item["type"] == "session.update")
        self.assertEqual("pcm", update["session"]["input_audio_format"])
        self.assertEqual(16000, update["session"]["sample_rate"])
        self.assertEqual("zh", update["session"]["input_audio_transcription"]["language"])
        self.assertEqual(
            {"type": "server_vad", "silence_duration_ms": 650},
            update["session"]["turn_detection"],
        )
        self.assertEqual(["你好"], partials)
        self.assertEqual(["你好"], finals)
        self.assertEqual([], errors)
        self.assertEqual("session.finish", ws.sent[-1]["type"])

    def test_qwen_realtime_drains_audio_queue_before_polling(self):
        release_created = threading.Event()
        ws = _BurstRealtimeWebSocket(release_created)
        done = threading.Event()
        config = ApiConfig(endpoint="wss://example.test/realtime", model="m", api_key="k")
        with mock.patch("tingma.cloud_api._create_websocket", return_value=ws):
            session = CloudSession(config, lambda value: None, lambda value: done.set(), self.fail)
            session.start()
            for _ in range(100):
                session.feed(b"\0" * 640)
            session.finish()
            release_created.set()
            self.assertTrue(done.wait(1))

        self.assertEqual(100, ws.append_count_at_first_poll)

    def test_dashscope_streaming_orders_task_audio_and_final(self):
        ws = _DashscopeWebSocket()
        partials = []
        finals = []
        errors = []
        done = threading.Event()
        config = ApiConfig(
            "dashscope_streaming",
            "wss://workspace.example/api-ws/v1/inference",
            "qwen-audio-3.0-asr-flash-streaming",
            "secret",
        )
        with mock.patch("tingma.cloud_api._create_websocket", return_value=ws):
            session = CloudSession(
                config,
                partials.append,
                lambda value: (finals.append(value), done.set()),
                errors.append,
            )
            session.start()
            session.feed(b"\x01\x00" * 160)
            session.finish()
            self.assertTrue(done.wait(1))

        run_task, finish_task = ws.controls
        self.assertEqual("run-task", run_task["header"]["action"])
        self.assertEqual("duplex", run_task["header"]["streaming"])
        self.assertEqual("audio", run_task["payload"]["task_group"])
        self.assertEqual("asr", run_task["payload"]["task"])
        self.assertEqual("recognition", run_task["payload"]["function"])
        self.assertEqual(config.model, run_task["payload"]["model"])
        self.assertEqual(
            {"format": "pcm", "sample_rate": 16000},
            run_task["payload"]["parameters"],
        )
        self.assertEqual(run_task["header"]["task_id"], finish_task["header"]["task_id"])
        self.assertEqual("finish-task", finish_task["header"]["action"])
        self.assertEqual([b"\x01\x00" * 160], ws.binary)
        self.assertEqual(["听码"], partials)
        self.assertEqual(["听码"], finals)
        self.assertEqual([], errors)

    def test_dashscope_cancel_closes_socket_without_callback(self):
        ws = _BlockingWebSocket()
        callbacks = []
        config = ApiConfig(
            "dashscope_streaming",
            "wss://example.test/api-ws/v1/inference",
            "m",
            "k",
        )
        with mock.patch("tingma.cloud_api._create_websocket", return_value=ws):
            session = CloudSession(
                config,
                lambda value: callbacks.append(value),
                lambda value: callbacks.append(value),
                lambda value: callbacks.append(value),
            )
            session.start()
            deadline = time.monotonic() + 1
            while session._websocket is None and time.monotonic() < deadline:
                time.sleep(0.005)
            session.cancel()
            session._thread.join(1)

        self.assertTrue(ws.closed)
        self.assertEqual([], callbacks)

    def test_cancel_closes_socket_and_suppresses_callbacks(self):
        ws = _IdleWebSocket()
        callbacks = []
        config = ApiConfig(endpoint="wss://example.test/realtime", model="m", api_key="k")
        with mock.patch("tingma.cloud_api._create_websocket", return_value=ws):
            session = CloudSession(
                config,
                lambda value: callbacks.append(("partial", value)),
                lambda value: callbacks.append(("final", value)),
                lambda value: callbacks.append(("error", value)),
            )
            session.start()
            deadline = time.monotonic() + 1
            while session._websocket is None and time.monotonic() < deadline:
                time.sleep(0.005)
            session.cancel()
            session._thread.join(1)

        self.assertTrue(ws.closed)
        self.assertEqual([], callbacks)

    def test_http_buffer_is_bounded_to_twenty_seconds(self):
        errors = []
        errored = threading.Event()
        config = ApiConfig(
            "qwen_http", "https://example.test/v1/chat/completions", "m", "k"
        )
        session = CloudSession(config, lambda value: None, lambda value: None, lambda value: (errors.append(value), errored.set()))
        session.start()
        session.feed(b"\0" * (20 * 16000 * 2))
        session.feed(b"\0\0")
        self.assertTrue(errored.wait(1))
        self.assertEqual(20 * 16000 * 2, len(session._audio_buffer))
        self.assertIn("20", errors[0])

    def test_realtime_queue_is_bounded_to_twenty_seconds(self):
        release_connect = threading.Event()
        errors = []
        errored = threading.Event()

        def delayed_connect(url, **kwargs):
            release_connect.wait(1)
            return _IdleWebSocket()

        config = ApiConfig(endpoint="wss://example.test/realtime", model="m", api_key="k")
        with mock.patch("tingma.cloud_api._create_websocket", side_effect=delayed_connect):
            session = CloudSession(
                config,
                lambda value: None,
                lambda value: None,
                lambda value: (errors.append(value), errored.set()),
            )
            session.start()
            session.feed(b"\0" * (20 * 16000 * 2))
            session.feed(b"\0\0")
            self.assertTrue(errored.wait(1))
            self.assertEqual(20 * 16000 * 2, session._queued_bytes)
            release_connect.set()
            session._thread.join(1)

        self.assertIn("20", errors[0])

    def test_qwen_http_posts_base64_wav_schema(self):
        client = _HttpClient(
            post_response=_Response(
                200, {"choices": [{"message": {"content": "识别完成"}}]}
            )
        )
        final = []
        done = threading.Event()
        config = ApiConfig(
            "qwen_http", "https://example.test/v1/chat/completions", "asr", "secret"
        )
        with mock.patch("tingma.cloud_api._new_http_client", return_value=client):
            session = CloudSession(config, lambda value: None, lambda value: (final.append(value), done.set()), self.fail)
            session.start()
            session.feed(b"\x01\x00" * 16)
            session.finish()
            self.assertTrue(done.wait(1))

        url, request = client.posts[0]
        self.assertEqual(config.endpoint, url)
        self.assertEqual("Bearer secret", request["headers"]["Authorization"])
        body = request["json"]
        self.assertFalse(body["stream"])
        self.assertFalse(body["asr_options"]["enable_itn"])
        data_uri = body["messages"][0]["content"][0]["input_audio"]["data"]
        self.assertTrue(data_uri.startswith("data:audio/wav;base64,UklGR"))
        self.assertEqual(["识别完成"], final)

    def test_openai_audio_posts_multipart_wav(self):
        client = _HttpClient(post_response=_Response(200, {"text": "hello"}))
        final = []
        done = threading.Event()
        config = ApiConfig(
            "openai_audio", "https://example.test/v1/audio/transcriptions", "whisper", "secret"
        )
        with mock.patch("tingma.cloud_api._new_http_client", return_value=client):
            session = CloudSession(config, lambda value: None, lambda value: (final.append(value), done.set()), self.fail)
            session.start()
            session.feed(b"\0\0" * 10)
            session.finish()
            self.assertTrue(done.wait(1))

        _, request = client.posts[0]
        self.assertEqual({"model": "whisper"}, request["data"])
        name, wav, media_type = request["files"]["file"]
        self.assertEqual("audio.wav", name)
        self.assertEqual("audio/wav", media_type)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(["hello"], final)

    def test_provider_error_redacts_key_from_callback(self):
        key = "sk-key/with special"
        client = _HttpClient(
            post_response=_Response(401, text=f"invalid key {key} Bearer {key}")
        )
        errors = []
        done = threading.Event()
        config = ApiConfig(
            "qwen_http", "https://example.test/v1/chat/completions", "asr", key
        )
        with mock.patch("tingma.cloud_api._new_http_client", return_value=client):
            session = CloudSession(config, lambda value: None, self.fail, lambda value: (errors.append(value), done.set()))
            session.start()
            session.feed(b"\0\0")
            session.finish()
            self.assertTrue(done.wait(1))

        self.assertNotIn(key, errors[0])
        self.assertIn("[REDACTED]", errors[0])

    def test_http_connection_probe_uses_models_sibling_without_audio(self):
        client = _HttpClient(get_response=_Response(200, {"data": [{"id": "asr"}]}))
        config = ApiConfig(
            "qwen_http", "https://example.test/compatible-mode/v1/chat/completions", "asr", "secret"
        )
        with mock.patch("tingma.cloud_api._new_http_client", return_value=client):
            message = test_connection(config)

        self.assertEqual(0, len(client.posts))
        self.assertEqual("https://example.test/compatible-mode/v1/models", client.gets[0][0])
        self.assertIn("身份认证", message)
        self.assertIn("模型", message)
        self.assertIn("未测试语音识别", message)

    def test_connection_error_redacts_provider_body(self):
        key = "connection-secret"
        client = _HttpClient(
            get_response=_Response(401, text=f"credential rejected: {key}")
        )
        config = ApiConfig(
            "openai_audio",
            "https://example.test/v1/audio/transcriptions",
            "asr",
            key,
        )
        with mock.patch("tingma.cloud_api._new_http_client", return_value=client):
            with self.assertRaises(ApiError) as raised:
                test_connection(config)

        self.assertNotIn(key, str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_realtime_connection_probe_only_handshakes(self):
        ws = _RealtimeWebSocket()
        config = ApiConfig(endpoint="wss://example.test/realtime", model="asr-live", api_key="secret")
        with mock.patch("tingma.cloud_api._create_websocket", return_value=ws):
            message = test_connection(config)

        self.assertEqual([], ws.sent)
        self.assertTrue(ws.closed)
        self.assertIn("未上传音频", message)

    def test_dashscope_connection_probe_only_handshakes(self):
        ws = _DashscopeWebSocket()
        config = ApiConfig(
            "dashscope_streaming",
            "wss://example.test/api-ws/v1/inference",
            "qwen-audio-3.0-asr-flash-streaming",
            "secret",
        )
        with mock.patch("tingma.cloud_api._create_websocket", return_value=ws):
            message = test_connection(config)

        self.assertEqual([], ws.controls)
        self.assertEqual([], ws.binary)
        self.assertTrue(ws.closed)
        self.assertIn("身份认证", message)
        self.assertIn("未测试模型", message)


if __name__ == "__main__":
    unittest.main()
