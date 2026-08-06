import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.errors import ConfigurationError
import app.main as main
from app.main import SessionStore, app
from app.persona import Persona
from app.providers import AudioResult, DoubaoTTSProvider, GeneratedReply, OpenAICompatibleLLM, VolcengineAUCSTTProvider


def settings(**overrides):
    values = dict(
        llm_base_url="https://llm.test/v1", llm_api_key="key", llm_model="model", llm_timeout_seconds=5,
        doubao_tts_base_url="https://tts.test", doubao_tts_provider="http", doubao_tts_api_key="tts-key", doubao_tts_app_id="app",
        doubao_tts_access_key="access", doubao_tts_resource_id="resource", doubao_tts_voice_id="female",
        doubao_tts_format="mp3", doubao_tts_sample_rate=24000, doubao_tts_timeout_seconds=5,
        doubao_tts_payload_mode="standard", doubao_tts_emotion_mode="disabled", doubao_tts_reference_mode="disabled",
        stt_provider="volcengine",
        stt_base_url="https://stt.test/submit", stt_api_key="stt-key",
        stt_resource_id="volc.seedasr.auc", stt_model="bigmodel", stt_timeout_seconds=5,
        host="127.0.0.1", port=8000, max_audio_bytes=1000,
    )
    values.update(overrides)
    return Settings(**values)


class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.history_dir = tempfile.TemporaryDirectory()
        main.session = SessionStore(Path(self.history_dir.name) / "conversation.json")

    def tearDown(self):
        self.history_dir.cleanup()

    async def asyncSetUp(self):
        main.session.reset()

    async def test_llm_parses_openai_compatible_response_and_keeps_persona(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "早点休息呀。"}}]}))
        provider = OpenAICompatibleLLM(settings(), Persona())
        with patch("app.providers.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            reply = await provider.complete([{"role": "user", "content": "我困了"}])
        self.assertEqual(reply, "早点休息呀。")

    async def test_llm_parses_structured_voice_metadata(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices": [{"message": {"content": '{"reply":"好嘛，我在听。","emotion":"亲昵、轻微撒娇","context":"熟悉的伴侣在轻松聊天"}'}}]
        }))
        provider = OpenAICompatibleLLM(settings(), Persona())
        with patch("app.providers.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            reply = await provider.complete_reply([{"role": "user", "content": "陪我聊会儿"}])
        self.assertEqual(reply, GeneratedReply("好嘛，我在听。", "亲昵、轻微撒娇", "熟悉的伴侣在轻松聊天"))

    def test_plain_llm_text_falls_back_without_metadata(self):
        reply = OpenAICompatibleLLM._parse_reply("回复：我在呢。")
        self.assertEqual(reply, GeneratedReply("我在呢。"))

    def test_tts_does_not_read_private_emotion_instruction_by_default(self):
        provider = DoubaoTTSProvider(settings())
        payload = provider._payload("好嘛，我在听。", emotion="亲昵、轻微撒娇")
        self.assertEqual(payload["req_params"]["text"], "好嘛，我在听。")

    def test_tts_tag_mode_uses_short_control_tag_only(self):
        provider = DoubaoTTSProvider(settings(doubao_tts_emotion_mode="tag"))
        payload = provider._payload("我记得。", emotion="温柔、稍慢")
        self.assertEqual(payload["req_params"]["text"], "[#温柔]\n我记得。")

    def test_tts_strips_leaked_commands_from_reply(self):
        provider = DoubaoTTSProvider(settings())
        payload = provider._payload("[#温柔]\n我在呢。", emotion="温柔")
        self.assertEqual(payload["req_params"]["text"], "我在呢。")

    async def test_llm_requires_configuration(self):
        provider = OpenAICompatibleLLM(settings(llm_api_key=""), Persona())
        with self.assertRaises(ConfigurationError):
            await provider.complete([])

    async def test_tts_decodes_base64_audio(self):
        encoded = base64.b64encode(b"fake-audio").decode()
        captured = {}

        def handler(request):
            captured["headers"] = request.headers
            captured["body"] = request.read()
            return httpx.Response(200, json={"data": encoded})

        transport = httpx.MockTransport(handler)
        provider = DoubaoTTSProvider(settings())
        with patch("app.providers.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            result = await provider.synthesize("你好")
        self.assertEqual(result, AudioResult(b"fake-audio", "audio/mpeg"))
        self.assertEqual(captured["headers"]["X-Api-Key"], "tts-key")
        self.assertEqual(captured["headers"]["X-Api-Resource-Id"], "resource")
        self.assertTrue(captured["headers"]["X-Api-Request-Id"])
        self.assertIn(b'"req_params"', captured["body"])
        self.assertIn(b'"speaker":"female"', captured["body"])

    async def test_tts_decodes_chunked_json_audio(self):
        first = base64.b64encode(b"first-").decode()
        second = base64.b64encode(b"second").decode()
        body = "\n".join([
            '{"code":0,"data":"' + first + '","message":""}',
            '{"code":0,"data":"' + second + '","message":""}',
            '{"code":20000000,"message":"OK"}',
        ])
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
        provider = DoubaoTTSProvider(settings())
        with patch("app.providers.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            result = await provider.synthesize("你好")
        self.assertEqual(result, AudioResult(b"first-second", "audio/mpeg"))

    async def test_text_chat_uses_llm_and_tts(self):
        main.llm.complete_reply = AsyncMock(return_value=GeneratedReply("我在呢。"))
        main.tts.synthesize = AsyncMock(return_value=AudioResult(b"audio", "audio/mpeg"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat/text", json={"text": "在吗"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "我在呢。")
        self.assertEqual(base64.b64decode(response.json()["audio"]), b"audio")

    async def test_conversation_is_saved_and_restored(self):
        main.session.add("我今天很累", "那就先歇一会儿，我陪着你。")
        restored = SessionStore(Path(self.history_dir.name) / "conversation.json")
        self.assertEqual(restored.messages, main.session.messages)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/conversation")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"], main.session.messages)

    async def test_reset_removes_saved_conversation(self):
        main.session.add("记住这一句", "我记住了。")
        history_path = main.session.path
        self.assertTrue(history_path.exists())
        main.session.reset()
        self.assertEqual(main.session.messages, [])
        self.assertFalse(history_path.exists())

    async def test_stt_builds_volcengine_submit_request(self):
        captured = {}

        def handler(request):
            captured["headers"] = request.headers
            captured["body"] = request.read()
            return httpx.Response(200, json={"result": {"text": "你好"}})

        transport = httpx.MockTransport(handler)
        provider = VolcengineAUCSTTProvider(settings())
        with patch("app.providers.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            text = await provider.transcribe_url("https://example.com/audio.mp3")
        self.assertEqual(text, "你好")
        self.assertEqual(captured["headers"]["X-Api-Key"], "stt-key")
        self.assertEqual(captured["headers"]["X-Api-Resource-Id"], "volc.seedasr.auc")
        self.assertEqual(captured["headers"]["X-Api-Sequence"], "-1")
        self.assertIn(b'"model_name":"bigmodel"', captured["body"])

    async def test_websocket_message_round_trip_matches_x_api_protocol(self):
        from app.volc_tts import EventType, MessageType, VolcTTSMessage

        encoded = VolcTTSMessage.event_message(
            EventType.START_SESSION,
            {"req_params": {"speaker": "female"}},
            "session-123",
        ).marshal()
        decoded = VolcTTSMessage.parse(encoded)
        self.assertEqual(decoded.message_type, MessageType.FULL_CLIENT_REQUEST)
        self.assertEqual(decoded.event, EventType.START_SESSION)
        self.assertEqual(decoded.session_id, "session-123")
        self.assertEqual(decoded.payload, b'{"req_params":{"speaker":"female"}}')

    async def test_websocket_task_then_finish_session_marks_final_text(self):
        from app.volc_tts import EventType, VolcTTSMessage

        session_id = "session-456"
        task = VolcTTSMessage.event_message(EventType.TASK_REQUEST, {"text": "你好"}, session_id)
        finish = VolcTTSMessage.event_message(EventType.FINISH_SESSION, {}, session_id)
        self.assertEqual(VolcTTSMessage.parse(task.marshal()).event, EventType.TASK_REQUEST)
        self.assertEqual(VolcTTSMessage.parse(finish.marshal()).event, EventType.FINISH_SESSION)
        self.assertEqual(VolcTTSMessage.parse(finish.marshal()).session_id, session_id)


if __name__ == "__main__":
    unittest.main()
