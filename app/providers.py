from __future__ import annotations

import base64
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .errors import ConfigurationError, ProviderError
from .persona import Persona

logger = logging.getLogger(__name__)


@dataclass
class AudioResult:
    data: bytes
    media_type: str


@dataclass
class GeneratedReply:
    """User-facing text plus private delivery metadata for TTS."""

    text: str
    emotion: str = ""
    context: str = ""


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error.get("code") or "上游服务返回错误")[:300]
            return str(payload.get("message") or payload.get("msg") or "上游服务返回错误")[:300]
    except (ValueError, json.JSONDecodeError):
        pass
    return response.text[:300] or "上游服务返回错误"


def _raise_for_upstream(response: httpx.Response, provider: str) -> None:
    if response.is_success:
        return
    logger.warning("%s upstream returned HTTP %s: %s", provider, response.status_code, _error_detail(response))
    if response.status_code in (401, 403):
        raise ProviderError(f"{provider} 鉴权失败，请检查 API 配置。", status_code=502)
    if response.status_code == 429:
        raise ProviderError(f"{provider} 当前请求过多，请稍后再试。", status_code=429)
    raise ProviderError(f"{provider} 暂时不可用：{_error_detail(response)}", status_code=502)


class OpenAICompatibleLLM:
    def __init__(self, settings: Settings, persona: Persona):
        self.settings = settings
        self.persona = persona

    async def complete(self, conversation: list[dict[str, str]]) -> str:
        return (await self.complete_reply(conversation)).text

    async def complete_reply(self, conversation: list[dict[str, str]]) -> GeneratedReply:
        if not self.settings.llm_configured:
            raise ConfigurationError("还没有配置大语言模型，请先填写 LLM_BASE_URL、LLM_API_KEY 和 LLM_MODEL。")

        messages = [{"role": "system", "content": self.persona.system_prompt() + self._voice_output_prompt()}]
        messages.extend(self.persona.examples())
        # Keep a useful recent window while the backend stores the full local transcript.
        messages.extend(conversation[-40:])
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": 0.8,
            "max_tokens": 500,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.llm_timeout_seconds) as client:
                response = await client.post(f"{self.settings.llm_base_url}/chat/completions", headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError("大语言模型响应超时，请稍后再试。", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("暂时无法连接大语言模型，请检查网络或接口地址。", status_code=502) from exc

        _raise_for_upstream(response, "大语言模型")
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("大语言模型返回了无法识别的内容。", status_code=502) from exc
        if isinstance(text, list):
            text = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in text)
        text = str(text).strip()
        if not text:
            raise ProviderError("大语言模型没有返回文字。", status_code=502)
        return self._parse_reply(text)

    @staticmethod
    def _voice_output_prompt() -> str:
        return """

## 语音表现输出
除非无法遵守，否则只返回一个 JSON 对象，不要使用 Markdown 代码块：
{"reply":"给用户看的回复","emotion":"语气描述","context":"可选的情绪上文"}
- reply 是唯一会显示和合成的正文，不要在其中写 [#...]、字段名或解释。
- emotion 用简短中文描述声音情绪，例如“温柔、放松、稍慢”“亲昵、害羞、轻微调侃”“平静、清晰”。
- context 只写 TTS 需要理解的语境或停顿提示，不要重复 reply；没有必要时留空。
- 情绪要符合当前对话，普通消息使用自然语气，不要每句都使用强烈情绪。
- 如果输出 JSON 失败，至少输出干净的回复正文。
"""

    @classmethod
    def _parse_reply(cls, raw: str) -> GeneratedReply:
        candidate = raw.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
        try:
            payload = json.loads(candidate)
        except (ValueError, json.JSONDecodeError):
            return GeneratedReply(text=cls._clean_plain_reply(raw))
        if not isinstance(payload, dict):
            return GeneratedReply(text=cls._clean_plain_reply(raw))
        reply = payload.get("reply") or payload.get("text") or payload.get("content")
        if not isinstance(reply, str) or not reply.strip():
            return GeneratedReply(text=cls._clean_plain_reply(raw))
        emotion = payload.get("emotion", "")
        context = payload.get("context", "")
        return GeneratedReply(
            text=cls._clean_plain_reply(reply),
            emotion=str(emotion).strip() if emotion else "",
            context=str(context).strip() if context else "",
        )

    @staticmethod
    def _clean_plain_reply(text: str) -> str:
        cleaned = text.strip()
        if cleaned.startswith("回复："):
            cleaned = cleaned[3:].strip()
        import re
        cleaned = re.sub(r"\[#.*?\]", "", cleaned)
        cleaned = re.sub(r"(?im)^\s*(?:emotion|context|语气|情绪|语音指令)\s*[:：].*$", "", cleaned)
        return cleaned.strip()


class DoubaoTTSProvider:
    """Adapter for Doubao TTS 2.0 unidirectional HTTP synthesis."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "X-Api-Request-Id": str(uuid.uuid4()),
        }
        if self.settings.doubao_tts_api_key:
            headers["X-Api-Key"] = self.settings.doubao_tts_api_key
        if self.settings.doubao_tts_resource_id:
            headers["X-Api-Resource-Id"] = self.settings.doubao_tts_resource_id
        return headers

    def _payload(self, text: str, *, emotion: str = "", context: str = "") -> dict[str, Any]:
        tts_text = self._format_tts_text(text, emotion=emotion, context=context)
        return {
            "req_params": {
                "text": tts_text,
                "speaker": self.settings.doubao_tts_voice_id,
                "audio_params": {
                    "format": self.settings.doubao_tts_format,
                    "sample_rate": self.settings.doubao_tts_sample_rate,
                },
            }
        }

    async def synthesize(self, text: str, *, emotion: str = "", context: str = "") -> AudioResult:
        if not self.settings.tts_configured:
            raise ConfigurationError("还没有配置豆包 TTS，请先填写 DOUBAO_TTS_API_KEY 和 DOUBAO_TTS_VOICE_ID。")
        text = text.strip()
        if not text:
            raise ProviderError("没有可合成的文字。", status_code=400)
        formatted_text = self._format_tts_text(text, emotion=emotion, context=context)
        if len(formatted_text) > 2000:
            raise ProviderError("回复太长，暂时无法合成语音。", status_code=400)

        try:
            async with httpx.AsyncClient(timeout=self.settings.doubao_tts_timeout_seconds) as client:
                response = await client.post(
                    self.settings.doubao_tts_base_url,
                    headers=self._headers(),
                    json=self._payload(text, emotion=emotion, context=context),
                )
        except httpx.TimeoutException as exc:
            raise ProviderError("语音合成超时，请稍后再试。", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("暂时无法连接豆包 TTS，请检查网络或接口地址。", status_code=502) from exc

        _raise_for_upstream(response, "豆包 TTS")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type.startswith("audio/") or response.content[:4] in (b"ID3\x04", b"RIFF", b"\xff\xfb"):
            return AudioResult(response.content, content_type or self._media_type())

        audio = self._decode_chunked_response(response)
        return AudioResult(audio, self._media_type())

    def _decode_chunked_response(self, response: httpx.Response) -> bytes:
        try:
            chunks = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("豆包 TTS 返回了无法识别的音频格式。", status_code=502) from exc

        audio_chunks: list[bytes] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            code = chunk.get("code")
            if code not in (None, 0, 20000000):
                message = str(chunk.get("message") or chunk.get("msg") or code)
                raise ProviderError(f"豆包 TTS 合成失败：{message}", status_code=502)
            encoded = chunk.get("data")
            if not isinstance(encoded, str) or not encoded:
                continue
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise ProviderError("豆包 TTS 返回的音频编码无效。", status_code=502) from exc
            if decoded:
                audio_chunks.append(decoded)

        audio = b"".join(audio_chunks)
        if not audio:
            raise ProviderError("豆包 TTS 没有返回音频，请检查音色和请求字段。", status_code=502)
        return audio

    def _format_tts_text(self, text: str, *, emotion: str, context: str) -> str:
        """Use documented voice-command syntax without exposing it to the UI."""
        spoken = self._strip_voice_commands(text)
        if self.settings.doubao_tts_emotion_mode != "tag" or not emotion.strip():
            return spoken
        tag = self._emotion_tag(emotion)
        return f"{tag}\n{spoken}" if tag else spoken

    @staticmethod
    def _strip_voice_commands(text: str) -> str:
        import re
        return re.sub(r"\[#.*?\]", "", text or "").strip()

    @staticmethod
    def _emotion_tag(emotion: str) -> str:
        value = emotion.strip()
        for item in ("开心", "高兴", "悲伤", "难过", "生气", "愤怒", "害羞", "惊讶", "平静", "温柔"):
            if item in value:
                return f"[#{item}]"
        return ""

    @staticmethod
    def _find_audio(data: Any) -> str | None:
        if isinstance(data, dict):
            for key in ("data", "audio", "audio_data", "base64_audio", "binary_data"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
                found = DoubaoTTSProvider._find_audio(value)
                if found:
                    return found
            for value in data.values():
                found = DoubaoTTSProvider._find_audio(value)
                if found:
                    return found
        elif isinstance(data, list):
            for value in data:
                found = DoubaoTTSProvider._find_audio(value)
                if found:
                    return found
        return None

    def _media_type(self) -> str:
        return {"mp3": "audio/mpeg", "wav": "audio/wav", "ogg": "audio/ogg"}.get(self.settings.doubao_tts_format, "audio/mpeg")


class VolcengineAUCSTTProvider:
    """Submit a publicly reachable audio URL to Volcengine AUC bigmodel STT."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        raise ConfigurationError(
            "当前火山 STT 接口只接受公网 audio_url，不能直接识别浏览器本地录音。"
            "请使用 /api/stt/url，或配置支持文件上传的 STT 服务。"
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Api-Key": self.settings.stt_api_key,
            "X-Api-Resource-Id": self.settings.stt_resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }

    def _payload(self, audio_url: str) -> dict[str, Any]:
        return {
            "user": {"uid": "cloud-voice-girlfriend"},
            "audio": {
                "url": audio_url,
                "format": "mp3",
                "codec": "raw",
                "rate": 16000,
                "bits": 16,
                "channel": 1,
            },
            "request": {
                "model_name": self.settings.stt_model,
                "enable_itn": True,
                "enable_punc": False,
                "enable_ddc": False,
                "enable_speaker_info": False,
                "enable_channel_split": False,
                "show_utterances": False,
                "vad_segment": False,
                "sensitive_words_filter": "",
            },
        }

    async def transcribe_url(self, audio_url: str) -> str:
        if not self.settings.stt_configured:
            raise ConfigurationError("还没有配置豆包 STT，请先填写 STT_API_KEY。")
        audio_url = audio_url.strip()
        if not audio_url.lower().startswith(("http://", "https://")):
            raise ProviderError("STT 需要一个豆包服务器可以访问的公网音频 URL。", status_code=400)
        try:
            async with httpx.AsyncClient(timeout=self.settings.stt_timeout_seconds) as client:
                response = await client.post(self.settings.stt_base_url, headers=self._headers(), json=self._payload(audio_url))
        except httpx.TimeoutException as exc:
            raise ProviderError("语音识别提交超时，请稍后再试。", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("暂时无法连接豆包语音识别服务。", status_code=502) from exc

        _raise_for_upstream(response, "语音识别")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("语音识别返回了无法识别的内容。", status_code=502) from exc
        text = self._find_text(payload)
        if text:
            return text
        message = self._find_message(payload)
        raise ProviderError(message or "语音识别没有返回文字结果。", status_code=502)

    @staticmethod
    def _find_text(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("text", "sentence", "transcript"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                found = VolcengineAUCSTTProvider._find_text(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = VolcengineAUCSTTProvider._find_text(value)
                if found:
                    return found
        return ""

    @staticmethod
    def _find_message(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("message", "msg", "error_message"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value[:300]
            for value in payload.values():
                found = VolcengineAUCSTTProvider._find_message(value)
                if found:
                    return found
        return ""


class GenericSTTProvider(VolcengineAUCSTTProvider):
    """Compatibility name used by the existing FastAPI wiring."""

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> str:
        raise ConfigurationError("当前豆包 STT 接口需要公网音频 URL，暂不能直接识别浏览器本地录音。")
