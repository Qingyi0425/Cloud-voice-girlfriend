from __future__ import annotations

import base64
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .errors import ProviderError
from .persona import Persona
from .providers import DoubaoTTSProvider, OpenAICompatibleLLM
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


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages: list[dict[str, str]] = []
        self._load()

    def add(self, user_text: str, assistant_text: str) -> None:
        self.messages.extend([
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ])
        self.messages = self.messages[-200:]
        self._save()

    def reset(self) -> None:
        self.messages.clear()
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
        except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError):
            self.messages = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"messages": self.messages}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


settings = Settings.from_env()
workspace = WorkspaceFiles(ROOT)
persona = Persona(workspace=workspace)
session = SessionStore(ROOT / "data" / "conversation.json")
llm = OpenAICompatibleLLM(settings, persona)
tts = DoubaoTTSProvider(settings)


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
    {"key": "STT_PROVIDER", "label": "语音识别提供方式", "group": "optional", "secret": False, "placeholder": "暂时不用可留空"},
    {"key": "STT_BASE_URL", "label": "语音识别 API 地址", "group": "optional", "secret": False, "placeholder": "暂时不用可留空"},
    {"key": "STT_API_KEY", "label": "语音识别 API 密钥", "group": "optional", "secret": True, "placeholder": "暂时不用可留空"},
    {"key": "STT_RESOURCE_ID", "label": "语音识别 Resource ID", "group": "optional", "secret": False, "placeholder": "volc.seedasr.auc"},
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
    global settings
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=True)
    settings = Settings.from_env()
    llm.settings = settings
    tts.settings = settings


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
        "pipeline": "text -> llm -> tts -> audio",
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
    return {"messages": session.messages}


async def chat(text: str) -> ChatResponse:
    text = text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="请输入聊天内容")
    generated = await llm.complete_reply(session.messages + [{"role": "user", "content": text}])
    audio_result = await tts.synthesize(generated.text, emotion=generated.emotion, context=generated.context)
    session.add(text, generated.text)
    return ChatResponse(
        transcript=text,
        reply=generated.text,
        audio=base64.b64encode(audio_result.data).decode("ascii"),
        audio_media_type=audio_result.media_type,
    )


@app.post("/api/chat/text", response_model=ChatResponse)
async def chat_text(request: TextChatRequest) -> ChatResponse:
    try:
        return await chat(request.text)
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@app.post("/api/reset")
async def reset() -> dict[str, bool]:
    session.reset()
    return {"ok": True}


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
