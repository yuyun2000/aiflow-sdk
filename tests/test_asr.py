from __future__ import annotations

import asyncio
import io
import json
import struct
import wave
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from aiflow_server.app import TOKEN_HEADER, create_app
from aiflow_server.asr import (
    AsrError,
    AsrSettings,
    SaucAsrClient,
    build_audio_request,
    build_full_request,
    parse_response,
)
from aiflow_server.config import load_settings


def wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 3200)
    return buffer.getvalue()


def provider_response(payload: dict, *, sequence: int, last: bool) -> bytes:
    encoded = __import__("gzip").compress(json.dumps(payload).encode())
    flags = 0b0011 if last else 0b0001
    wire_sequence = -sequence if last else sequence
    return bytes([0x11, (0b1001 << 4) | flags, 0x11, 0]) + struct.pack(">iI", wire_sequence, len(encoded)) + encoded


def test_sauc_frames_round_trip_and_last_sequence():
    full = build_full_request(1, {"request": {"model_name": "bigmodel"}})
    assert full[:4] == b"\x11\x11\x11\x00"
    audio = build_audio_request(3, b"pcm", is_last=True)
    assert struct.unpack(">i", audio[4:8])[0] == -3
    parsed = parse_response(provider_response({"result": {"text": "你好"}}, sequence=3, last=True))
    assert parsed.code == 0
    assert parsed.sequence == -3
    assert parsed.is_last_package is True
    assert parsed.payload == {"result": {"text": "你好"}}


class FakeWs:
    def __init__(self):
        self.sent: list[bytes] = []
        self.responses = [
            provider_response({"audio_info": {"duration": 200}, "result": {"text": ""}}, sequence=1, last=False),
            provider_response(
                {
                    "audio_info": {"duration": 200},
                    "result": {
                        "text": "打开客厅空调。",
                        "additions": {"log_id": "fake-log"},
                        "utterances": [{"text": "打开客厅空调。", "definite": True}],
                    },
                },
                sequence=1,
                last=True,
            ),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def send(self, data: bytes):
        self.sent.append(data)

    async def recv(self):
        return self.responses.pop(0)


def test_sauc_client_uses_headers_and_returns_final_result():
    asyncio.run(_test_sauc_client_uses_headers_and_returns_final_result())


async def _test_sauc_client_uses_headers_and_returns_final_result():
    websocket = FakeWs()
    calls = {}

    def connector(url, **kwargs):
        calls.update(url=url, **kwargs)
        return websocket

    client = SaucAsrClient(
        AsrSettings(True, "wss://example.invalid/asr", "api-key", "", "", "volc.seedasr.sauc.duration", 1, 200),
        connector=connector,
    )
    result = await client.transcribe(wav_bytes())
    assert result["text"] == "打开客厅空调。"
    assert result["log_id"] == "fake-log"
    assert result["duration_ms"] == 200
    assert calls["additional_headers"]["X-Api-Key"] == "api-key"
    assert calls["additional_headers"]["X-Api-Sequence"] == "-1"
    assert len(websocket.sent) == 3
    full_payload = __import__("gzip").decompress(websocket.sent[0][12:])
    assert json.loads(full_payload)["audio"]["format"] == "wav"


def test_sauc_client_streams_chunks_without_collecting_audio():
    asyncio.run(_test_sauc_client_streams_chunks_without_collecting_audio())


async def _test_sauc_client_streams_chunks_without_collecting_audio():
    websocket = FakeWs()

    def connector(_url, **_kwargs):
        return websocket

    async def chunks():
        yield b"\x01\x02" * 1600
        yield b"\x03\x04" * 1600

    client = SaucAsrClient(
        AsrSettings(True, "wss://example.invalid/asr", "api-key", "", "", "resource", 1, 200),
        connector=connector,
    )
    result = await client.transcribe_stream(chunks())
    assert result["text"] == "打开客厅空调。"
    # Full request plus one final 200 ms PCM frame.
    assert len(websocket.sent) == 2
    stream_payload = __import__("gzip").decompress(websocket.sent[0][12:])
    assert json.loads(stream_payload)["audio"]["format"] == "pcm"
    assert struct.unpack(">i", websocket.sent[-1][4:8])[0] == -2
    assert struct.unpack(">i", websocket.sent[-1][4:8])[0] == -2


def test_asr_route_requires_context_and_returns_provider_result(tmp_path):
    settings = replace(load_settings(), data_dir=tmp_path / "data")
    websocket = FakeWs()

    def connector(_url, **_kwargs):
        return websocket

    asr = SaucAsrClient(replace(settings.asr, enabled=True, api_key="fake-key"), connector=connector)
    app = create_app(settings, asr_client=asr)
    with TestClient(app) as client:
        unauthorized = client.post("/api/v3/asr", files={"file": ("audio.wav", wav_bytes(), "audio/wav")})
        assert unauthorized.status_code == 401
        context = client.post(
            "/api/v3/contexts",
            json={"device": {"device_id": "asr-device", "client_id": "asr-client", "product": "CoreS3"}},
        ).json()
        response = client.post(
            "/api/v3/asr",
            headers={TOKEN_HEADER: context["access_token"]},
            files={"file": ("audio.wav", wav_bytes(), "audio/wav")},
            data={"language": "zh-CN"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["text"] == "打开客厅空调。"


def test_asr_route_rejects_non_wav(tmp_path):
    settings = replace(load_settings(), data_dir=tmp_path / "data")
    app = create_app(settings)
    with TestClient(app) as client:
        context = client.post(
            "/api/v3/contexts",
            json={"device": {"device_id": "asr-device-2", "client_id": "asr-client-2", "product": "CoreS3"}},
        ).json()
        response = client.post(
            "/api/v3/asr",
            headers={TOKEN_HEADER: context["access_token"]},
            files={"file": ("audio.mp3", b"not-audio", "audio/mpeg")},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid_audio"


def test_asr_stream_route_forwards_raw_pcm_chunks(tmp_path):
    settings = replace(load_settings(), data_dir=tmp_path / "data")
    websocket = FakeWs()

    def connector(_url, **_kwargs):
        return websocket

    asr = SaucAsrClient(replace(settings.asr, enabled=True, api_key="fake-key"), connector=connector)
    app = create_app(settings, asr_client=asr)
    with TestClient(app) as client:
        context = client.post(
            "/api/v3/contexts",
            json={"device": {"device_id": "asr-stream-device", "client_id": "asr-stream-client", "product": "CoreS3"}},
        ).json()
        response = client.post(
            "/api/v3/asr/stream?format=pcm&rate=16000&bits=16&channel=1",
            headers={TOKEN_HEADER: context["access_token"], "Content-Type": "audio/pcm"},
            content=b"\x00\x00" * 3200,
        )
        assert response.status_code == 200, response.text
        assert response.json()["text"] == "打开客厅空调。"


def test_asr_disabled_is_explicit():
    asyncio.run(_test_asr_disabled_is_explicit())


async def _test_asr_disabled_is_explicit():
    client = SaucAsrClient(AsrSettings(False, "wss://example.invalid", "", "", "", "resource", 1, 200))
    with pytest.raises(AsrError, match="disabled") as caught:
        await client.transcribe(wav_bytes())
    assert caught.value.code == "asr_disabled"


def test_provider_auth_failure_is_reported_without_exposing_credentials():
    from types import SimpleNamespace

    class UnauthorizedConnector:
        def __call__(self, *_args, **_kwargs):
            error = RuntimeError("server rejected WebSocket connection: HTTP 401")
            error.response = SimpleNamespace(status_code=401)
            raise error

    async def run():
        client = SaucAsrClient(
            AsrSettings(True, "wss://example.invalid/asr", "secret-value", "", "", "resource", 1, 200),
            connector=UnauthorizedConnector(),
        )
        with pytest.raises(AsrError) as caught:
            await client.transcribe(wav_bytes())
        assert caught.value.code == "asr_auth_failed"
        assert "secret-value" not in str(caught.value)

    asyncio.run(run())
