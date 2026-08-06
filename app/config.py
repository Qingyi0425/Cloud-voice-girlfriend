from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name, str(default)).strip()
    try:
        return float(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: float
    doubao_tts_base_url: str
    doubao_tts_provider: str
    doubao_tts_api_key: str
    doubao_tts_app_id: str
    doubao_tts_access_key: str
    doubao_tts_resource_id: str
    doubao_tts_voice_id: str
    doubao_tts_format: str
    doubao_tts_sample_rate: int
    doubao_tts_timeout_seconds: float
    doubao_tts_payload_mode: str
    doubao_tts_emotion_mode: str
    doubao_tts_reference_mode: str
    stt_provider: str
    stt_base_url: str
    stt_api_key: str
    stt_resource_id: str
    stt_model: str
    stt_timeout_seconds: float
    host: str
    port: int
    max_audio_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            llm_base_url=os.getenv("LLM_BASE_URL", "").strip().rstrip("/"),
            llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
            llm_model=os.getenv("LLM_MODEL", "").strip(),
            llm_timeout_seconds=_float_env("LLM_TIMEOUT_SECONDS", 60),
            doubao_tts_base_url=os.getenv("DOUBAO_TTS_BASE_URL", "https://openspeech.bytedance.com/api/v3/tts/unidirectional").strip().rstrip("/"),
            doubao_tts_provider=os.getenv("DOUBAO_TTS_PROVIDER", "http").strip().lower(),
            doubao_tts_api_key=os.getenv("DOUBAO_TTS_API_KEY", "").strip(),
            doubao_tts_app_id=os.getenv("DOUBAO_TTS_APP_ID", "").strip(),
            doubao_tts_access_key=os.getenv("DOUBAO_TTS_ACCESS_KEY", "").strip(),
            doubao_tts_resource_id=os.getenv("DOUBAO_TTS_RESOURCE_ID", "").strip(),
            doubao_tts_voice_id=os.getenv("DOUBAO_TTS_VOICE_ID", "").strip(),
            doubao_tts_format=os.getenv("DOUBAO_TTS_FORMAT", "mp3").strip().lower(),
            doubao_tts_sample_rate=_int_env("DOUBAO_TTS_SAMPLE_RATE", 24000),
            doubao_tts_timeout_seconds=_float_env("DOUBAO_TTS_TIMEOUT_SECONDS", 60),
            doubao_tts_payload_mode=os.getenv("DOUBAO_TTS_PAYLOAD_MODE", "standard").strip().lower(),
            doubao_tts_emotion_mode=os.getenv("DOUBAO_TTS_EMOTION_MODE", "prefix").strip().lower(),
            doubao_tts_reference_mode=os.getenv("DOUBAO_TTS_REFERENCE_MODE", "disabled").strip().lower(),
            stt_provider=os.getenv("STT_PROVIDER", "generic").strip().lower(),
            stt_base_url=os.getenv("STT_BASE_URL", "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit").strip().rstrip("/"),
            stt_api_key=os.getenv("STT_API_KEY", "").strip(),
            stt_resource_id=os.getenv("STT_RESOURCE_ID", "volc.seedasr.auc").strip(),
            stt_model=os.getenv("STT_MODEL", "bigmodel").strip(),
            stt_timeout_seconds=_float_env("STT_TIMEOUT_SECONDS", 60),
            host=os.getenv("HOST", "127.0.0.1").strip(),
            port=_int_env("PORT", 8000),
            max_audio_bytes=_int_env("MAX_AUDIO_BYTES", 12_000_000),
        )

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def tts_configured(self) -> bool:
        return bool(self.doubao_tts_base_url and self.doubao_tts_api_key and self.doubao_tts_resource_id and self.doubao_tts_voice_id)

    @property
    def stt_configured(self) -> bool:
        return bool(self.stt_base_url and self.stt_api_key and self.stt_resource_id and self.stt_model)
