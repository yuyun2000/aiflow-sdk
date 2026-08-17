from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import struct
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterable, Awaitable, Callable

from websockets.asyncio.client import ClientConnection, connect


LOGGER = logging.getLogger(__name__)
DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"


class AsrError(RuntimeError):
    """An ASR request could not be completed or was rejected by the provider."""

    def __init__(self, message: str, *, code: str = "asr_provider_error", status_code: int = 502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class AsrSettings:
    enabled: bool
    url: str
    api_key: str
    app_key: str
    access_key: str
    resource_id: str
    timeout_seconds: float
    segment_duration_ms: int

    @property
    def auth_configured(self) -> bool:
        return bool(self.api_key or (self.app_key and self.access_key))


class MessageType:
    CLIENT_FULL_REQUEST = 0b0001
    CLIENT_AUDIO_ONLY_REQUEST = 0b0010
    SERVER_FULL_RESPONSE = 0b1001
    SERVER_ERROR_RESPONSE = 0b1111


class MessageFlags:
    POS_SEQUENCE = 0b0001
    NEG_WITH_SEQUENCE = 0b0011


def _frame_header(message_type: int, flags: int) -> bytes:
    return bytes([(1 << 4) | 1, (message_type << 4) | flags, (1 << 4) | 1, 0])


def build_full_request(sequence: int, payload: dict[str, Any]) -> bytes:
    encoded = gzip.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return _frame_header(MessageType.CLIENT_FULL_REQUEST, MessageFlags.POS_SEQUENCE) + struct.pack(">iI", sequence, len(encoded)) + encoded


def build_audio_request(sequence: int, audio: bytes, *, is_last: bool) -> bytes:
    encoded = gzip.compress(audio)
    flags = MessageFlags.NEG_WITH_SEQUENCE if is_last else MessageFlags.POS_SEQUENCE
    wire_sequence = -sequence if is_last else sequence
    return _frame_header(MessageType.CLIENT_AUDIO_ONLY_REQUEST, flags) + struct.pack(">iI", wire_sequence, len(encoded)) + encoded


@dataclass(frozen=True)
class AsrResponse:
    code: int
    event: int
    is_last_package: bool
    sequence: int
    payload: dict[str, Any] | None


def parse_response(message: bytes) -> AsrResponse:
    if len(message) < 4:
        raise AsrError("ASR provider returned a truncated response", code="asr_invalid_response")
    header_size = (message[0] & 0x0F) * 4
    if header_size < 4 or len(message) < header_size:
        raise AsrError("ASR provider returned an invalid response header", code="asr_invalid_response")
    message_type = message[1] >> 4
    flags = message[1] & 0x0F
    serialization = message[2] >> 4
    compression = message[2] & 0x0F
    payload = message[header_size:]
    sequence = 0
    event = 0
    is_last = bool(flags & 0x02)
    if flags & 0x01:
        if len(payload) < 4:
            raise AsrError("ASR provider returned an invalid sequence", code="asr_invalid_response")
        sequence = struct.unpack(">i", payload[:4])[0]
        payload = payload[4:]
    if flags & 0x04:
        if len(payload) < 4:
            raise AsrError("ASR provider returned an invalid event", code="asr_invalid_response")
        event = struct.unpack(">i", payload[:4])[0]
        payload = payload[4:]
    code = 0
    if message_type == MessageType.SERVER_FULL_RESPONSE:
        if len(payload) < 4:
            raise AsrError("ASR provider returned an invalid response payload", code="asr_invalid_response")
        payload = payload[4:]
    elif message_type == MessageType.SERVER_ERROR_RESPONSE:
        if len(payload) < 8:
            raise AsrError("ASR provider returned an invalid error payload", code="asr_invalid_response")
        code, _size = struct.unpack(">iI", payload[:8])
        payload = payload[8:]
    else:
        raise AsrError(f"ASR provider returned unsupported message type {message_type}", code="asr_invalid_response")
    if not payload:
        return AsrResponse(code, event, is_last, sequence, None)
    try:
        if compression == 1:
            payload = gzip.decompress(payload)
        if serialization != 1:
            raise ValueError("unsupported serialization")
        decoded = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise AsrError("ASR provider returned an unreadable response", code="asr_invalid_response") from exc
    return AsrResponse(code, event, is_last, sequence, decoded if isinstance(decoded, dict) else None)


def wav_audio_info(data: bytes) -> tuple[int, int, int]:
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            return wav.getnchannels(), wav.getsampwidth() * 8, wav.getframerate()
    except (wave.Error, EOFError) as exc:
        raise AsrError("ASR currently accepts valid WAV audio only", code="invalid_audio", status_code=400) from exc


def _result_from_payload(payload: dict[str, Any] | None) -> tuple[str, str | None, list[dict[str, Any]]]:
    result = (payload or {}).get("result") or {}
    if not isinstance(result, dict):
        return "", None, []
    text = result.get("text") if isinstance(result.get("text"), str) else ""
    additions = result.get("additions") if isinstance(result.get("additions"), dict) else {}
    log_id = additions.get("log_id") if isinstance(additions.get("log_id"), str) else None
    utterances = result.get("utterances") if isinstance(result.get("utterances"), list) else []
    return text, log_id, [item for item in utterances if isinstance(item, dict)]


class SaucAsrClient:
    def __init__(self, settings: AsrSettings, *, connector: Callable[..., Awaitable[ClientConnection]] | None = None):
        self.settings = settings
        self.connector = connector or connect

    def _headers(self) -> dict[str, str]:
        headers = {
            "X-Api-Resource-Id": self.settings.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }
        if self.settings.api_key:
            headers["X-Api-Key"] = self.settings.api_key
        else:
            headers["X-Api-App-Key"] = self.settings.app_key
            headers["X-Api-Access-Key"] = self.settings.access_key
        return headers

    @staticmethod
    def _validate_audio_params(channels: int, bits: int, rate: int) -> None:
        if channels not in (1, 2) or bits not in (8, 16, 24, 32) or rate <= 0:
            raise AsrError("unsupported audio parameters", code="invalid_audio", status_code=400)

    async def transcribe(self, audio: bytes, *, filename: str = "audio.wav", language: str | None = None,
                         enable_punc: bool = True, enable_itn: bool = True,
                         enable_ddc: bool = True, show_utterances: bool = True) -> dict[str, Any]:
        channels, bits, rate = wav_audio_info(audio)
        async def one_chunk() -> AsyncIterable[bytes]:
            # The SAUC WAV mode expects the RIFF header in the audio frames.
            yield audio
        return await self.transcribe_stream(
            one_chunk(), channels=channels, bits=bits, rate=rate, audio_format="wav", language=language,
            enable_punc=enable_punc, enable_itn=enable_itn, enable_ddc=enable_ddc,
            show_utterances=show_utterances,
        )

    async def transcribe_stream(
        self,
        chunks: AsyncIterable[bytes],
        *,
        channels: int = 1,
        bits: int = 16,
        rate: int = 16000,
        audio_format: str = "pcm",
        language: str | None = None,
        enable_punc: bool = True,
        enable_itn: bool = True,
        enable_ddc: bool = True,
        show_utterances: bool = True,
    ) -> dict[str, Any]:
        if not self.settings.enabled:
            raise AsrError("ASR is disabled; configure asr.enabled=true", code="asr_disabled", status_code=503)
        if not self.settings.auth_configured:
            raise AsrError("ASR credentials are not configured", code="asr_not_configured", status_code=503)
        self._validate_audio_params(channels, bits, rate)
        if audio_format not in {"pcm", "wav"}:
            raise AsrError("unsupported audio format", code="invalid_audio", status_code=400)
        payload: dict[str, Any] = {
            "user": {"uid": "aiflow"},
            "audio": {"format": audio_format, "codec": "raw", "rate": rate, "bits": bits, "channel": channels},
            "request": {
                "model_name": "bigmodel", "enable_itn": enable_itn, "enable_punc": enable_punc,
                "enable_ddc": enable_ddc, "show_utterances": show_utterances,
            },
        }
        if language:
            payload["audio"]["language"] = language
        segment_size = max(1, channels * (bits // 8) * rate * self.settings.segment_duration_ms // 1000)
        try:
            async with self.connector(self.settings.url, additional_headers=self._headers(),
                                      open_timeout=self.settings.timeout_seconds,
                                      close_timeout=self.settings.timeout_seconds,
                                      max_size=8 * 1024 * 1024) as websocket:
                await websocket.send(build_full_request(1, payload))
                first = await asyncio.wait_for(websocket.recv(), self.settings.timeout_seconds)
                first_response = parse_response(first)
                if first_response.code:
                    raise AsrError("ASR provider rejected the request", code="asr_provider_rejected")
                # The full JSON request consumes sequence 1; audio starts at 2.
                sequence = 2
                pending = bytearray()
                saw_audio = False
                async for chunk in chunks:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise AsrError("audio stream yielded a non-bytes chunk", code="invalid_audio", status_code=400)
                    view = memoryview(chunk)
                    if view:
                        saw_audio = True
                    if pending:
                        needed = segment_size - len(pending)
                        pending.extend(view[:needed])
                        view = view[needed:]
                        if len(pending) == segment_size and view:
                            await websocket.send(build_audio_request(sequence, bytes(pending), is_last=False))
                            pending.clear()
                            sequence += 1
                    while len(view) > segment_size:
                        await websocket.send(build_audio_request(sequence, bytes(view[:segment_size]), is_last=False))
                        view = view[segment_size:]
                        sequence += 1
                    pending.extend(view)
                if not saw_audio:
                    raise AsrError("audio stream is empty", code="empty_file", status_code=400)
                await websocket.send(build_audio_request(sequence, bytes(pending), is_last=True))
                text, log_id, utterances = _result_from_payload(first_response.payload)
                duration_ms = None
                if first_response.payload and isinstance(first_response.payload.get("audio_info"), dict):
                    duration_ms = first_response.payload["audio_info"].get("duration")
                while True:
                    response = parse_response(await asyncio.wait_for(websocket.recv(), self.settings.timeout_seconds))
                    if response.code:
                        raise AsrError("ASR provider returned an error", code="asr_provider_rejected")
                    current_text, current_log_id, current_utterances = _result_from_payload(response.payload)
                    if current_text:
                        text = current_text
                    log_id = current_log_id or log_id
                    if current_utterances:
                        utterances = current_utterances
                    if response.payload and isinstance(response.payload.get("audio_info"), dict):
                        duration_ms = response.payload["audio_info"].get("duration", duration_ms)
                    if response.is_last_package:
                        break
                return {"text": text, "log_id": log_id, "duration_ms": duration_ms, "utterances": utterances}
        except AsrError:
            raise
        except asyncio.TimeoutError as exc:
            raise AsrError("ASR provider timed out", code="asr_timeout", status_code=504) from exc
        except Exception as exc:
            LOGGER.warning("ASR provider request failed: %s", exc)
            status = getattr(exc, "response", None)
            status_code = getattr(status, "status_code", None)
            if status_code in (401, 403):
                raise AsrError(
                    "ASR provider rejected the configured credentials",
                    code="asr_auth_failed",
                    status_code=502,
                ) from exc
            raise AsrError("ASR provider is unavailable", code="asr_unavailable") from exc
