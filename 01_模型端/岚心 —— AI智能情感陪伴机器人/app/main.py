from __future__ import annotations

import base64
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .errors import ProviderError
from .persona import Persona
from .playback import SpeakerPlayer
from .providers import GenericSTTProvider, DoubaoTTSProvider, OpenAICompatibleLLM, OpenAICompatibleSTTProvider
from .volc_stt import DoubaoStreamingSTTProvider
from .workspace import WorkspaceFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


class TextChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ConfigUpdateRequest(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    transcript: str
    reply: str
    audio: str | None
    audio_media_type: str | None = None
    error: str | None = None
    played: bool = False


class RobotChatResponse(BaseModel):
    transcript: str
    reply: str
    audio: str | None = None
    audio_media_type: str | None = None
    error: str | None = None
    played: bool = False


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages: list[dict[str, str]] = []
        self.sources: list[str] = []
        self._load()

    def add(self, user_text: str, assistant_text: str, source: str = "web") -> None:
        self.messages.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ])
        self.sources.extend([source, source])
        self.messages = self.messages[-200:]
        self.sources = self.sources[-200:]
        self._save()

    def reset(self) -> None:
        self.messages.clear()
        self.sources.clear()
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove conversation history file")

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            messages = data.get("messages", []) if isinstance(data, dict) else []
            if isinstance(messages, list):
                self.messages = [
                    {"role": item["role"], "content": item["content"]}
                    for item in messages
                    if isinstance(item, dict)
                    and item.get("role") in {"user", "assistant"}
                    and isinstance(item.get("content"), str)
                    and item["content"].strip()
                ][-200:]
            sources = data.get("sources") if isinstance(data, dict) else None
            if isinstance(sources, list) and len(sources) == len(self.messages):
                self.sources = [str(item) if item else "" for item in sources][-200:]
            else:
                self.sources = [""] * len(self.messages)
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            self.messages = []
            self.sources = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"messages": self.messages, "sources": self.sources}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


settings = Settings.from_env()
workspace = WorkspaceFiles(ROOT)
persona = Persona(workspace=workspace)
session = SessionStore(ROOT / "data" / "conversation.json")
llm = OpenAICompatibleLLM(settings, persona)
tts = DoubaoTTSProvider(settings)
speaker = SpeakerPlayer()


def _make_stt_provider(current: Settings):
    if current.stt_provider in {"openai", "openai_compatible", "openai-compatible"}:
        return OpenAICompatibleSTTProvider(current)
    if current.stt_provider in {"doubao", "doubao_streaming", "volcengine_streaming", "volcengine_ws"}:
        return DoubaoStreamingSTTProvider(current)
    return GenericSTTProvider(current)


stt = _make_stt_provider(settings)


CONFIG_FIELDS = [
    {"key": "LLM_BASE_URL", "label": "中转站 API 地址", "group": "required", "secret": False, "placeholder": "https://你的中转站/v1"},
    {"key": "LLM_API_KEY", "label": "中转站 API 密钥", "group": "required", "secret": True, "placeholder": "sk-..."},
    {"key": "LLM_MODEL", "label": "大语言模型名称", "group": "required", "secret": False, "placeholder": "deepseek-v4-pro"},
    {"key": "DOUBAO_TTS_BASE_URL", "label": "豆包 TTS 地址", "group": "required", "secret": False, "placeholder": "https://openspeech.bytedance.com/api/v3/tts/unidirectional"},
    {"key": "DOUBAO_TTS_API_KEY", "label": "豆包 TTS API 密钥", "group": "required", "secret": True, "placeholder": "你的 X-Api-Key"},
    {"key": "DOUBAO_TTS_RESOURCE_ID", "label": "豆包 TTS Resource ID", "group": "required", "secret": False, "placeholder": "seed-tts-2.0"},
    {"key": "DOUBAO_TTS_VOICE_ID", "label": "豆包音色 ID", "group": "required", "secret": False, "placeholder": "zh_female_xiaohe_uranus_bigtts"},
    {"key": "DOUBAO_TTS_PROVIDER", "label": "TTS 提供方式", "group": "optional", "secret": False, "placeholder": "http"},
    {"key": "DOUBAO_TTS_APP_ID", "label": "豆包 App ID", "group": "optional", "secret": False, "placeholder": "没有就留空"},
    {"key": "DOUBAO_TTS_ACCESS_KEY", "label": "豆包 Access Key", "group": "optional", "secret": True, "placeholder": "没有就留空"},
    {"key": "DOUBAO_TTS_FORMAT", "label": "音频格式", "group": "optional", "secret": False, "placeholder": "mp3"},
    {"key": "DOUBAO_TTS_SAMPLE_RATE", "label": "采样率", "group": "optional", "secret": False, "placeholder": "24000"},
    {"key": "DOUBAO_TTS_TIMEOUT_SECONDS", "label": "TTS 超时时间（秒）", "group": "optional", "secret": False, "placeholder": "60"},
    {"key": "DOUBAO_TTS_PAYLOAD_MODE", "label": "TTS 请求模式", "group": "optional", "secret": False, "placeholder": "standard"},
    {"key": "LLM_TIMEOUT_SECONDS", "label": "模型超时时间（秒）", "group": "optional", "secret": False, "placeholder": "60"},
    {"key": "DOUBAO_TTS_EMOTION_MODE", "label": "情绪指令模式", "group": "optional", "secret": False, "placeholder": "disabled 或 tag"},
    {"key": "DOUBAO_TTS_REFERENCE_MODE", "label": "语音上下文模式", "group": "optional", "secret": False, "placeholder": "disabled"},
    {"key": "STT_PROVIDER", "label": "语音识别提供方式", "group": "optional", "secret": False, "placeholder": "doubao_streaming"},
    {"key": "STT_BASE_URL", "label": "语音识别 WebSocket 地址", "group": "optional", "secret": False, "placeholder": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"},
    {"key": "STT_API_KEY", "label": "语音识别 API 密钥", "group": "optional", "secret": True, "placeholder": "支持语音识别的 API 密钥"},
    {"key": "STT_APP_ID", "label": "语音识别 App ID（旧版）", "group": "optional", "secret": False, "placeholder": "新版只填 STT_API_KEY 时留空"},
    {"key": "STT_ACCESS_KEY", "label": "语音识别 Access Key（旧版）", "group": "optional", "secret": True, "placeholder": "新版只填 STT_API_KEY 时留空"},
    {"key": "STT_RESOURCE_ID", "label": "语音识别 Resource ID", "group": "optional", "secret": False, "placeholder": "volc.seedasr.sauc.duration"},
    {"key": "STT_MODEL", "label": "语音识别模型", "group": "optional", "secret": False, "placeholder": "bigmodel"},
    {"key": "STT_TIMEOUT_SECONDS", "label": "语音识别超时时间（秒）", "group": "optional", "secret": False, "placeholder": "60"},
    {"key": "HOST", "label": "服务监听地址", "group": "optional", "secret": False, "placeholder": "127.0.0.1"},
    {"key": "PORT", "label": "服务端口", "group": "optional", "secret": False, "placeholder": "8001"},
    {"key": "MAX_AUDIO_BYTES", "label": "最大音频大小（字节）", "group": "optional", "secret": False, "placeholder": "12000000"},
]
CONFIG_KEYS = {field["key"] for field in CONFIG_FIELDS}


def _env_value(key: str) -> str:
    return os.getenv(key, "").strip()


def _config_public() -> dict[str, object]:
    fields = []
    for field in CONFIG_FIELDS:
        value = _env_value(field["key"])
        fields.append({**field, "value": "" if field["secret"] else value, "configured": bool(value)})
    return {"fields": fields, "env_path": str(ROOT / ".env")}


def _write_env(values: dict[str, str]) -> None:
    env_path = ROOT / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    for key, value in values.items():
        if key not in CONFIG_KEYS:
            continue
        line = f"{key}={value}\n"
        pattern = rf"(?m)^\s*{re.escape(key)}=.*$"
        if re.search(pattern, existing):
            existing = re.sub(pattern, line.rstrip("\n"), existing, count=1)
        else:
            if existing and not existing.endswith("\n"):
                existing += "\n"
            existing += line
    env_path.write_text(existing, encoding="utf-8")


def _reload_settings() -> None:
    global settings, stt
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    settings = Settings.from_env()
    llm.settings = settings
    tts.settings = settings
    stt = _make_stt_provider(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("voice girlfriend server started")
    yield


app = FastAPI(title="云端语音女友", version="0.2.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=ROOT / "web" / "assets"), name="assets")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "llm_configured": settings.llm_configured,
        "tts_configured": settings.tts_configured,
        "stt_configured": settings.stt_configured,
        "pipeline": "audio -> stt -> llm -> text",
        "text_pipeline": "text -> llm -> tts -> audio",
        "robot_pipeline": "wav -> stt -> llm -> 24kHz PCM -> ESP32 speaker + screen text",
        "pc_speaker": speaker.available,
        "session_messages": len(session.messages),
        "workspace_files": workspace.file_names(),
    }


@app.get("/api/config")
async def config_status() -> dict[str, object]:
    return _config_public()


@app.put("/api/config")
async def update_config(request: ConfigUpdateRequest) -> dict[str, object]:
    values: dict[str, str] = {}
    for key, value in request.values.items():
        if key not in CONFIG_KEYS or not isinstance(value, str):
            continue
        cleaned = value.strip()
        # Empty secret fields mean "leave the existing key unchanged".
        if not cleaned and next(field for field in CONFIG_FIELDS if field["key"] == key)["secret"]:
            continue
        if "\n" in cleaned or "\r" in cleaned:
            raise HTTPException(status_code=400, detail=f"{key} 不能包含换行符。")
        values[key] = cleaned
    _write_env(values)
    _reload_settings()
    restart_required = sorted(set(values).intersection({"HOST", "PORT"}))
    return {
        "ok": True,
        "updated": sorted(values),
        "restart_required": restart_required,
        "config": _config_public(),
    }


@app.get("/api/workspace")
async def workspace_status() -> dict[str, object]:
    return {
        "root": str(ROOT),
        "files": [
            {"name": name, "exists": path.exists(), "modified_at": path.stat().st_mtime if path.exists() else None}
            for name, path in workspace.paths.items()
        ],
    }


@app.get("/api/conversation")
async def conversation() -> dict[str, object]:
    return {
        "messages": [
            {**message, "source": session.sources[index] if index < len(session.sources) else ""}
            for index, message in enumerate(session.messages)
        ]
    }


async def chat(
    text: str,
    *,
    include_audio: bool = True,
    response_audio_format: str | None = None,
    play_on_pc: bool = False,
    allow_partial: bool = False,
    source: str = "web",
) -> ChatResponse:
    """Run one chat turn.

    ``play_on_pc`` synthesizes speech in PCM and plays it on this computer: the
    robot board has no speaker yet, so the computer speaks for it. The reply is
    still returned as text so the robot can show it on its screen.
    ``allow_partial`` keeps the recognized text usable when the language model is
    unavailable, which lets the microphone chain be tested on its own.
    """
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="请输入聊天内容")
    try:
        generated = await llm.complete_reply(session.messages + [{"role": "user", "content": text}])
    except ProviderError as exc:
        if not allow_partial:
            raise
        logger.warning("大语言模型不可用，仅返回识别文字：%s", exc)
        return ChatResponse(transcript=text, reply="", audio=None, error=f"回复失败：{exc}")

    audio_payload: str | None = None
    audio_media_type: str | None = None
    played = False
    error: str | None = None

    if play_on_pc or include_audio:
        try:
            if play_on_pc:
                spoken = await tts.synthesize(
                    generated.text,
                    emotion=generated.emotion,
                    context=generated.context,
                    audio_format="pcm",
                    sample_rate=settings.doubao_tts_sample_rate,
                )
                played = speaker.play_pcm(spoken.data, settings.doubao_tts_sample_rate)
                if not played:
                    error = "电脑没有播放出声音，请检查扬声器或音量。"
            else:
                audio_result = await tts.synthesize(
                    generated.text,
                    emotion=generated.emotion,
                    context=generated.context,
                    audio_format=response_audio_format,
                    sample_rate=settings.doubao_tts_sample_rate if response_audio_format == "pcm" else None,
                )
                audio_payload = base64.b64encode(audio_result.data).decode("ascii")
                audio_media_type = audio_result.media_type
        except ProviderError as exc:
            logger.warning("语音合成失败，仅返回文字：%s", exc)
            error = str(exc)

    session.add(text, generated.text, source=source)
    return ChatResponse(
        transcript=text,
        reply=generated.text,
        audio=audio_payload,
        audio_media_type=audio_media_type,
        error=error,
        played=played,
    )


@app.post("/api/chat/text", response_model=ChatResponse)
async def chat_text(request: TextChatRequest) -> ChatResponse:
    try:
        return await chat(request.text)
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/api/chat/audio", response_model=RobotChatResponse)
async def chat_audio(request: Request) -> RobotChatResponse:
    audio = await request.body()
    if len(audio) > settings.max_audio_bytes:
        raise HTTPException(status_code=413, detail="音频文件太大。")
    if not audio:
        raise HTTPException(status_code=400, detail="没有收到音频文件。")
    headers = {key.lower(): value.strip().lower() for key, value in request.headers.items()}
    play_on_pc = headers.get("x-play-audio", "") in {"true", "1", "yes", "on"}
    response_audio_format = headers.get("x-response-audio", "")
    include_audio = response_audio_format in {"true", "1", "yes", "on", "pcm"}
    if response_audio_format != "pcm":
        response_audio_format = None
    allow_partial = headers.get("x-allow-partial", "") in {"true", "1", "yes", "on"}
    try:
        transcript = await stt.transcribe(
            audio,
            "recording.wav",
            request.headers.get("content-type", "audio/wav"),
        )
        result = await chat(
            transcript,
            include_audio=include_audio,
            response_audio_format=response_audio_format,
            play_on_pc=play_on_pc,
            allow_partial=allow_partial,
            source="robot",
        )
        return RobotChatResponse(
            transcript=result.transcript,
            reply=result.reply,
            audio=result.audio,
            audio_media_type=result.audio_media_type,
            error=result.error,
            played=result.played,
        )
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/api/reset")
async def reset() -> dict[str, bool]:
    session.reset()
    return {"ok": True}


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
