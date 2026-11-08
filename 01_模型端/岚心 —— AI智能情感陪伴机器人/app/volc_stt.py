from __future__ import annotations

import asyncio
import gzip
import io
import json
import struct
import uuid
import wave
from typing import Any

import websockets

from .errors import ConfigurationError, ProviderError


class DoubaoStreamingSTTProvider:
    """Doubao SAUC streaming STT provider for local WAV/PCM uploads."""

    def __init__(self, settings: Any):
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        headers = {
            "X-Api-Resource-Id": self.settings.stt_resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        if self.settings.stt_api_key:
            headers["X-Api-Key"] = self.settings.stt_api_key
        else:
            headers["X-Api-App-Key"] = self.settings.stt_app_id
            headers["X-Api-Access-Key"] = self.settings.stt_access_key
        return headers

    @staticmethod
    def _frame(message_type: int, flags: int, payload: bytes, *, json_payload: bool, gzip_payload: bool) -> bytes:
        header = bytes((0x11, (message_type << 4) | flags, (0x10 if json_payload else 0x00) | (0x01 if gzip_payload else 0x00), 0x00))
        encoded = gzip.compress(payload) if gzip_payload else payload
        return header + struct.pack(">I", len(encoded)) + encoded

    def _start_frame(self) -> bytes:
        payload = {
            "user": {"uid": "cloud-voice-girlfriend"},
            "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1},
            "request": {
                "model_name": self.settings.stt_model or "bigmodel",
                "enable_itn": True,
                "enable_punc": True,
                "enable_ddc": False,
                "show_utterances": False,
                "result_type": "full",
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._frame(1, 0, encoded, json_payload=True, gzip_payload=True)

    @staticmethod
    def _audio_frame(pcm: bytes, *, final: bool) -> bytes:
        return DoubaoStreamingSTTProvider._frame(2, 0x2 if final else 0, pcm, json_payload=False, gzip_payload=True)

    @staticmethod
    def _wav_to_pcm(audio: bytes, filename: str, content_type: str) -> bytes:
        if audio[:4] != b"RIFF" or b"WAVE" not in audio[:16]:
            if len(audio) % 2:
                raise ProviderError("语音数据不是有效的 16-bit PCM。", status_code=400)
            return audio
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav:
                channels, sample_width, rate, _, _, _ = wav.getparams()
                if channels != 1 or sample_width != 2 or rate != 16000:
                    raise ProviderError("豆包流式识别只支持 16kHz、16-bit、单声道 WAV。", status_code=400)
                return wav.readframes(wav.getnframes())
        except (wave.Error, EOFError) as exc:
            raise ProviderError("上传的 WAV 音频格式无法读取。", status_code=400) from exc

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        if not self.settings.stt_configured:
            raise ConfigurationError(
                "还没有配置豆包流式 STT，请填写 STT_API_KEY（新版）或 STT_APP_ID、STT_ACCESS_KEY（旧版），以及 STT_RESOURCE_ID。"
            )
        if not self.settings.stt_base_url.startswith(("ws://", "wss://")):
            raise ConfigurationError("豆包流式 STT 地址必须是 ws:// 或 wss://。")
        pcm = self._wav_to_pcm(audio, filename, content_type)
        if not pcm:
            raise ProviderError("音频中没有可识别的 PCM 数据。", status_code=400)

        chunk_size = 16000 * 2 * 200 // 1000
        latest_text = ""
        try:
            async with websockets.connect(
                self.settings.stt_base_url,
                additional_headers=self._headers(),
                open_timeout=self.settings.stt_timeout_seconds,
                close_timeout=5,
                ping_interval=None,
                max_size=None,
            ) as websocket:
                await websocket.send(self._start_frame())
                for start in range(0, len(pcm), chunk_size):
                    chunk = pcm[start : start + chunk_size]
                    await websocket.send(self._audio_frame(chunk, final=start + chunk_size >= len(pcm)))
                latest_text = await self._collect_result(websocket)
        except asyncio.TimeoutError as exc:
            raise ProviderError("豆包语音识别响应超时，请稍后再试。", status_code=504) from exc
        except websockets.InvalidStatus as exc:
            status = getattr(exc.response, "status_code", "未知")
            raise ProviderError(f"豆包语音识别 WebSocket 鉴权或资源配置失败：HTTP {status}。", status_code=502) from exc
        except websockets.WebSocketException as exc:
            raise ProviderError(f"豆包语音识别连接失败：{exc}。", status_code=502) from exc

        if not latest_text:
            raise ProviderError("豆包语音识别没有返回文字结果。", status_code=502)
        return latest_text

    async def _collect_result(self, websocket: Any) -> str:
        latest_text = ""
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self.settings.stt_timeout_seconds)
            if isinstance(raw, str):
                continue
            message_type, flags, payload = self._parse_frame(raw)
            if message_type == 15:
                raise ProviderError(self._decode_error(payload), status_code=502)
            if message_type != 9:
                continue
            try:
                data = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProviderError("豆包语音识别返回了无法解析的结果。", status_code=502) from exc
            text = self._find_text(data)
            if text:
                latest_text = text
            if flags == 3:
                return latest_text

    @staticmethod
    def _parse_frame(data: bytes) -> tuple[int, int, bytes]:
        if len(data) < 8:
            raise ProviderError("豆包语音识别返回了不完整的二进制消息。", status_code=502)
        header_size = (data[0] & 0x0F) * 4
        if header_size < 4 or len(data) < header_size + 4:
            raise ProviderError("豆包语音识别返回了无法识别的消息头。", status_code=502)
        message_type, flags, compression = data[1] >> 4, data[1] & 0x0F, data[2] & 0x0F
        offset = header_size
        if message_type == 9 and flags in (1, 3):
            if len(data) < offset + 4:
                raise ProviderError("豆包语音识别响应缺少序号。", status_code=502)
            offset += 4
        if message_type == 15:
            if len(data) < offset + 4:
                raise ProviderError("豆包语音识别错误消息不完整。", status_code=502)
            offset += 4
        if len(data) < offset + 4:
            raise ProviderError("豆包语音识别响应缺少负载长度。", status_code=502)
        size = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        payload = data[offset : offset + size]
        if len(payload) != size:
            raise ProviderError("豆包语音识别响应负载被截断。", status_code=502)
        if compression == 1 and payload:
            try:
                payload = gzip.decompress(payload)
            except OSError as exc:
                raise ProviderError("豆包语音识别响应压缩数据无效。", status_code=502) from exc
        return message_type, flags, payload

    @staticmethod
    def _find_text(payload: Any) -> str:
        if isinstance(payload, dict):
            result = payload.get("result")
            if isinstance(result, dict):
                value = result.get("text")
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("text", "transcript", "sentence"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                found = DoubaoStreamingSTTProvider._find_text(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = DoubaoStreamingSTTProvider._find_text(value)
                if found:
                    return found
        return ""

    @staticmethod
    def _decode_error(payload: bytes) -> str:
        try:
            data = json.loads(payload.decode("utf-8"))
            if isinstance(data, dict):
                return str(data.get("message") or data.get("msg") or data.get("error") or "豆包语音识别请求被拒绝")[:400]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return payload.decode("utf-8", "replace")[:400] or "豆包语音识别请求被拒绝。"
