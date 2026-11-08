from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

import websockets

from .errors import ConfigurationError, ProviderError

logger = logging.getLogger(__name__)


@dataclass
class StreamedAudio:
    data: bytes
    media_type: str


class MessageType(IntEnum):
    FULL_CLIENT_REQUEST = 0x1
    FULL_SERVER_RESPONSE = 0x9
    AUDIO_ONLY_SERVER = 0xB
    ERROR = 0xF


class MessageFlag(IntEnum):
    NO_SEQUENCE = 0x0
    POSITIVE_SEQUENCE = 0x1
    NEGATIVE_SEQUENCE = 0x3
    WITH_EVENT = 0x4


class EventType(IntEnum):
    START_CONNECTION = 1
    FINISH_CONNECTION = 2
    CONNECTION_STARTED = 50
    CONNECTION_FAILED = 51
    CONNECTION_FINISHED = 52
    START_SESSION = 100
    FINISH_SESSION = 102
    SESSION_STARTED = 150
    SESSION_FINISHED = 152
    SESSION_FAILED = 153
    TASK_REQUEST = 200
    TTS_ENDED = 359


@dataclass
class VolcTTSMessage:
    """Volcengine v1 binary message used by the X-Api WebSocket interface."""

    message_type: MessageType
    flag: MessageFlag
    event: int | None = None
    session_id: str = ""
    connect_id: str = ""
    sequence: int | None = None
    error_code: int | None = None
    payload: bytes = b""

    @classmethod
    def event_message(cls, event: EventType, payload: dict[str, Any], session_id: str = "") -> "VolcTTSMessage":
        return cls(
            message_type=MessageType.FULL_CLIENT_REQUEST,
            flag=MessageFlag.WITH_EVENT,
            event=event,
            session_id=session_id,
            payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def marshal(self) -> bytes:
        buffer = io.BytesIO()
        buffer.write(bytes((0x11, (int(self.message_type) << 4) | int(self.flag), 0x10, 0)))
        if self.flag in (MessageFlag.POSITIVE_SEQUENCE, MessageFlag.NEGATIVE_SEQUENCE):
            buffer.write(struct.pack(">i", self.sequence or 0))
        if self.flag == MessageFlag.WITH_EVENT:
            buffer.write(struct.pack(">i", self.event or 0))
            if self.event not in (EventType.START_CONNECTION, EventType.FINISH_CONNECTION):
                encoded_session = self.session_id.encode("utf-8")
                buffer.write(struct.pack(">I", len(encoded_session)))
                buffer.write(encoded_session)
        if self.message_type == MessageType.ERROR:
            buffer.write(struct.pack(">I", self.error_code or 0))
        buffer.write(struct.pack(">I", len(self.payload)))
        buffer.write(self.payload)
        return buffer.getvalue()

    @classmethod
    def parse(cls, data: bytes) -> "VolcTTSMessage":
        if len(data) < 8:
            raise ProviderError("豆包 TTS 返回了不完整的 WebSocket 消息。")
        header_size = (data[0] & 0x0F) * 4
        if header_size < 4 or len(data) < header_size + 4:
            raise ProviderError("豆包 TTS 返回了无法识别的消息头。")
        try:
            message_type = MessageType(data[1] >> 4)
            flag = MessageFlag(data[1] & 0x0F)
        except ValueError as exc:
            raise ProviderError("豆包 TTS 返回了未知的 WebSocket 消息类型。") from exc

        buffer = io.BytesIO(data[header_size:])
        message = cls(message_type=message_type, flag=flag)
        if flag in (MessageFlag.POSITIVE_SEQUENCE, MessageFlag.NEGATIVE_SEQUENCE):
            message.sequence = _read_i32(buffer)
        if message_type == MessageType.ERROR:
            message.error_code = _read_u32(buffer)
        if flag == MessageFlag.WITH_EVENT:
            message.event = _read_i32(buffer)
            if message.event not in (
                EventType.START_CONNECTION,
                EventType.FINISH_CONNECTION,
                EventType.CONNECTION_STARTED,
                EventType.CONNECTION_FAILED,
                EventType.CONNECTION_FINISHED,
            ):
                message.session_id = _read_string(buffer)
            if message.event in (
                EventType.CONNECTION_STARTED,
                EventType.CONNECTION_FAILED,
                EventType.CONNECTION_FINISHED,
            ):
                message.connect_id = _read_string(buffer)
        payload_size = _read_u32(buffer)
        message.payload = _read_exact(buffer, payload_size)
        return message


def _read_exact(buffer: io.BytesIO, size: int) -> bytes:
    value = buffer.read(size)
    if len(value) != size:
        raise ProviderError("豆包 TTS 返回了截断的 WebSocket 消息。")
    return value


def _read_u32(buffer: io.BytesIO) -> int:
    return struct.unpack(">I", _read_exact(buffer, 4))[0]


def _read_i32(buffer: io.BytesIO) -> int:
    return struct.unpack(">i", _read_exact(buffer, 4))[0]


def _read_string(buffer: io.BytesIO) -> str:
    return _read_exact(buffer, _read_u32(buffer)).decode("utf-8", "replace")


class DoubaoBidirectionalTTSProvider:
    """TTS 2.0 WebSocket provider using X-Api-Key authentication."""

    def __init__(self, settings: Any):
        self.settings = settings

    async def synthesize(self, text: str) -> StreamedAudio:
        if not self.settings.tts_configured:
            raise ConfigurationError("还没有配置豆包 TTS，请先填写 API Key、Resource ID 和音色 ID。")
        text = text.strip()
        if not text:
            raise ProviderError("没有可合成的文字。", status_code=400)
        if len(text) > 2000:
            raise ProviderError("回复太长，暂时无法合成语音。", status_code=400)
        if not self.settings.doubao_tts_base_url.startswith(("ws://", "wss://")):
            raise ConfigurationError("新版豆包 TTS 需要 wss://openspeech.bytedance.com/api/v3/tts/bidirection。")

        headers = {
            "X-Api-Key": self.settings.doubao_tts_api_key,
            "X-Api-Resource-Id": self.settings.doubao_tts_resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        session_id = str(uuid.uuid4())
        audio_chunks: list[bytes] = []
        try:
            async with websockets.connect(
                self.settings.doubao_tts_base_url,
                additional_headers=headers,
                open_timeout=self.settings.doubao_tts_timeout_seconds,
                ping_interval=None,
                max_size=None,
            ) as websocket:
                await websocket.send(VolcTTSMessage.event_message(EventType.START_CONNECTION, {}).marshal())
                await self._wait_for_event(websocket, EventType.CONNECTION_STARTED)
                session_payload = {
                    "req_params": {
                        "model": "seed-tts-2.0-standard",
                        "speaker": self.settings.doubao_tts_voice_id,
                        "audio_params": {
                            "format": self.settings.doubao_tts_format,
                            "sample_rate": self.settings.doubao_tts_sample_rate,
                        },
                        "explicit_language": "zh-cn",
                        "disable_markdown_filter": True,
                    }
                }
                await websocket.send(VolcTTSMessage.event_message(EventType.START_SESSION, session_payload, session_id).marshal())
                await self._wait_for_event(websocket, EventType.SESSION_STARTED)
                await websocket.send(VolcTTSMessage.event_message(EventType.TASK_REQUEST, {"text": text}, session_id).marshal())
                # FinishSession marks the final text chunk in this streaming API.
                await websocket.send(VolcTTSMessage.event_message(EventType.FINISH_SESSION, {}, session_id).marshal())
                await self._collect_session_audio(websocket, audio_chunks)
                await websocket.send(VolcTTSMessage.event_message(EventType.FINISH_CONNECTION, {}).marshal())
                await self._wait_for_event(websocket, EventType.CONNECTION_FINISHED)
        except asyncio.TimeoutError as exc:
            raise ProviderError("豆包 TTS 响应超时，请稍后再试。", status_code=504) from exc
        except websockets.InvalidStatus as exc:
            raise ProviderError(f"豆包 TTS WebSocket 鉴权或资源配置失败：HTTP {exc.response.status_code}。", status_code=502) from exc
        except websockets.WebSocketException as exc:
            raise ProviderError(f"豆包 TTS WebSocket 连接失败：{exc}。", status_code=502) from exc

        audio = b"".join(audio_chunks)
        if not audio:
            raise ProviderError("豆包 TTS 没有返回音频，请检查 API Key 权限、Resource ID 和音色 ID。", status_code=502)
        return StreamedAudio(audio, self._media_type())

    async def _wait_for_event(self, websocket: Any, expected: EventType) -> None:
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.settings.doubao_tts_timeout_seconds)
            if isinstance(raw, str):
                continue
            message = VolcTTSMessage.parse(raw)
            logger.info(
                "Doubao TTS event: type=%s flag=%s event=%s sequence=%s payload_bytes=%s",
                message.message_type.name,
                message.flag.name,
                message.event,
                message.sequence,
                len(message.payload),
            )
            if message.message_type == MessageType.FULL_SERVER_RESPONSE and message.payload:
                logger.info("Doubao TTS event payload: %s", message.payload[:400].decode("utf-8", "replace"))
            if message.message_type == MessageType.ERROR or message.event in (
                EventType.CONNECTION_FAILED,
                EventType.SESSION_FAILED,
            ):
                raise ProviderError(self._upstream_error(message.payload), status_code=502)
            if message.event == expected:
                return

    async def _collect_session_audio(self, websocket: Any, audio_chunks: list[bytes]) -> None:
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.settings.doubao_tts_timeout_seconds)
            if isinstance(raw, str):
                continue
            message = VolcTTSMessage.parse(raw)
            logger.info(
                "Doubao TTS event: type=%s flag=%s event=%s sequence=%s payload_bytes=%s",
                message.message_type.name,
                message.flag.name,
                message.event,
                message.sequence,
                len(message.payload),
            )
            if message.message_type == MessageType.ERROR or message.event in (
                EventType.CONNECTION_FAILED,
                EventType.SESSION_FAILED,
            ):
                raise ProviderError(self._upstream_error(message.payload), status_code=502)
            if message.message_type == MessageType.AUDIO_ONLY_SERVER:
                audio_chunks.append(message.payload)
            elif message.message_type == MessageType.FULL_SERVER_RESPONSE and message.event == 352:
                # Some gateway versions put the TTSResponse payload in a full
                # response frame. Keep it only when it is raw audio; JSON is
                # subtitle/timing metadata and must not enter the audio stream.
                if not message.payload.lstrip().startswith((b"{", b"[")):
                    audio_chunks.append(message.payload)
            if message.event == EventType.SESSION_FINISHED:
                return

    @staticmethod
    def _upstream_error(payload: bytes) -> str:
        try:
            data = json.loads(payload.decode("utf-8"))
            if isinstance(data, dict):
                return str(data.get("message") or data.get("msg") or data.get("error") or "豆包 TTS 请求被拒绝")[:400]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        text = payload.decode("utf-8", "replace").strip()
        return text[:400] or "豆包 TTS 请求被拒绝，请检查 API Key 权限、Resource ID 和音色 ID。"

    def _media_type(self) -> str:
        return {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg_opus": "audio/ogg"}.get(
            self.settings.doubao_tts_format,
            "audio/mpeg",
        )
