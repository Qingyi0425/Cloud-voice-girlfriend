import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.errors import ConfigurationError, ProviderError
import app.main as main
from app.main import SessionStore, app
from app.persona import Persona
from app.providers import AudioResult, DoubaoTTSProvider, GeneratedReply, OpenAICompatibleLLM, OpenAICompatibleSTTProvider, VolcengineAUCSTTProvider


def settings(**overrides):
    values = dict(
        llm_base_url="https://llm.test/v1", llm_api_key="key", llm_model="model", llm_timeout_seconds=5,
        doubao_tts_base_url="https://tts.test", doubao_tts_provider="http", doubao_tts_api_key="tts-key", doubao_tts_app_id="app",
        doubao_tts_access_key="access", doubao_tts_resource_id="resource", doubao_tts_voice_id="female",
        doubao_tts_format="mp3", doubao_tts_sample_rate=24000, doubao_tts_timeout_seconds=5,
        doubao_tts_payload_mode="standard", doubao_tts_emotion_mode="disabled", doubao_tts_reference_mode="disabled",
        stt_provider="volcengine",
        stt_base_url="https://stt.test/submit", stt_api_key="stt-key",
        stt_app_id="stt-app", stt_access_key="stt-access",
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
        self.assertEqual(
            [(item["role"], item["content"]) for item in response.json()["messages"]],
            [(item["role"], item["content"]) for item in main.session.messages],
        )

    async def test_conversation_marks_which_client_sent_each_turn(self):
        main.session.add("机器人说的话", "嗯", source="robot")
        main.session.add("网页说的话", "好", source="web")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/conversation")
        self.assertEqual([item["source"] for item in response.json()["messages"]], ["robot", "robot", "web", "web"])

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

    async def test_openai_compatible_stt_uploads_audio_file(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            captured["headers"] = request.headers
            captured["body"] = request.read()
            return httpx.Response(200, json={"text": "你好，小岚"})

        transport = httpx.MockTransport(handler)
        provider = OpenAICompatibleSTTProvider(settings(
            stt_provider="openai_compatible",
            stt_base_url="https://stt.test/v1",
            stt_model="whisper-test",
        ))
        with patch("app.providers.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
            text = await provider.transcribe(b"wav-bytes", "recording.wav", "audio/wav")
        self.assertEqual(text, "你好，小岚")
        self.assertEqual(captured["url"], "https://stt.test/v1/audio/transcriptions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer stt-key")
        self.assertIn(b'name="model"', captured["body"])
        self.assertIn(b'whisper-test', captured["body"])
        self.assertIn(b'wav-bytes', captured["body"])

    async def test_openai_compatible_stt_requires_a_configured_model(self):
        provider = OpenAICompatibleSTTProvider(settings(stt_provider="openai_compatible", stt_model=""))
        with self.assertRaises(ConfigurationError):
            await provider.transcribe(b"wav-bytes", "recording.wav", "audio/wav")

    def test_doubao_streaming_stt_frame_encodes_gzip_payload(self):
        import gzip
        import json
        from app.volc_stt import DoubaoStreamingSTTProvider

        provider = DoubaoStreamingSTTProvider(settings(
            stt_provider="doubao_streaming",
            stt_base_url="wss://stt.test/api/v3/sauc/bigmodel",
        ))
        frame = provider._start_frame()
        self.assertEqual(frame[:4], bytes((0x11, 0x10, 0x11, 0)))
        size = int.from_bytes(frame[4:8], "big")
        payload = json.loads(gzip.decompress(frame[8:8 + size]))
        self.assertEqual(payload["audio"], {
            "format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1
        })
        self.assertEqual(payload["request"]["model_name"], "bigmodel")

    async def test_doubao_streaming_stt_sends_pcm_chunks_and_reads_final_result(self):
        import gzip
        import io
        import json
        import struct
        import wave
        from app.volc_stt import DoubaoStreamingSTTProvider

        pcm = b"\x01\x00" * 6400
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(pcm)

        final_json = gzip.compress(json.dumps({"result": {"text": "你好，小岚"}}, ensure_ascii=False).encode())
        server_frame = bytes((0x11, 0x93, 0x11, 0)) + struct.pack(">iI", 3, len(final_json)) + final_json

        class FakeWebSocket:
            def __init__(self):
                self.sent = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def send(self, value):
                self.sent.append(value)

            async def recv(self):
                return server_frame

        socket = FakeWebSocket()
        provider = DoubaoStreamingSTTProvider(settings(
            stt_provider="doubao_streaming",
            stt_base_url="wss://stt.test/api/v3/sauc/bigmodel",
            stt_resource_id="volc.seedasr.sauc.duration",
        ))
        with patch("app.volc_stt.websockets.connect", return_value=socket) as connect:
            text = await provider.transcribe(wav_buffer.getvalue(), "recording.wav", "audio/wav")

        self.assertEqual(text, "你好，小岚")
        self.assertEqual(len(socket.sent), 3)  # start request and two 200 ms audio frames
        self.assertEqual(socket.sent[1][1] & 0x0F, 0)
        self.assertEqual(socket.sent[2][1] & 0x0F, 2)
        self.assertEqual(connect.call_args.args[0], "wss://stt.test/api/v3/sauc/bigmodel")
        self.assertEqual(connect.call_args.kwargs["additional_headers"]["X-Api-Key"], "stt-key")

    async def test_doubao_streaming_stt_rejects_wrong_wav_sample_rate(self):
        import io
        import wave
        from app.volc_stt import DoubaoStreamingSTTProvider

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(44100)
            wav.writeframes(b"\x00\x00" * 100)
        provider = DoubaoStreamingSTTProvider(settings(
            stt_provider="doubao_streaming",
            stt_base_url="wss://stt.test/api/v3/sauc/bigmodel",
        ))
        with self.assertRaises(ProviderError) as raised:
            await provider.transcribe(buffer.getvalue(), "recording.wav", "audio/wav")
        self.assertIn("16kHz", str(raised.exception))

    async def test_audio_chat_accepts_raw_wav_and_returns_reply(self):
        main.stt.transcribe = AsyncMock(return_value="我今天有点累")
        main.llm.complete_reply = AsyncMock(return_value=GeneratedReply("那先歇一会儿，我陪着你。"))
        main.tts.synthesize = AsyncMock(return_value=AudioResult(b"audio", "audio/mpeg"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat/audio", content=b"wav-data", headers={"Content-Type": "audio/wav"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transcript"], "我今天有点累")
        self.assertEqual(response.json()["reply"], "那先歇一会儿，我陪着你。")
        self.assertIsNone(response.json()["audio"])
        main.stt.transcribe.assert_awaited_once_with(b"wav-data", "recording.wav", "audio/wav")
        main.tts.synthesize.assert_not_awaited()

    async def test_audio_chat_can_include_speech_for_browser_voice_input(self):
        main.stt.transcribe = AsyncMock(return_value="你好小岚")
        main.llm.complete_reply = AsyncMock(return_value=GeneratedReply("我在呢。"))
        main.tts.synthesize = AsyncMock(return_value=AudioResult(b"speech", "audio/mpeg"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/chat/audio",
                content=b"wav-data",
                headers={"Content-Type": "audio/wav", "X-Response-Audio": "true"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["transcript"], "你好小岚")
        self.assertEqual(base64.b64decode(response.json()["audio"]), b"speech")
        main.tts.synthesize.assert_awaited_once()

    async def test_audio_chat_returns_pcm_for_robot_speaker(self):
        main.stt.transcribe = AsyncMock(return_value="你好小岚")
        main.llm.complete_reply = AsyncMock(return_value=GeneratedReply("我在呢。"))
        main.tts.synthesize = AsyncMock(return_value=AudioResult(b"\x01\x00" * 240, "audio/L16"))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/chat/audio",
                content=b"wav-data",
                headers={"Content-Type": "audio/wav", "X-Response-Audio": "pcm"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(base64.b64decode(response.json()["audio"]), b"\x01\x00" * 240)
        self.assertEqual(response.json()["audio_media_type"], "audio/L16")
        self.assertEqual(main.tts.synthesize.await_args.kwargs["audio_format"], "pcm")
        self.assertEqual(main.tts.synthesize.await_args.kwargs["sample_rate"], main.settings.doubao_tts_sample_rate)

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


    async def test_audio_chat_plays_reply_on_computer_speaker(self):
        from app.providers import AudioResult

        main.stt.transcribe = AsyncMock(return_value="你好小岚")
        main.llm.complete_reply = AsyncMock(return_value=GeneratedReply("我在呢。"))
        main.tts.synthesize = AsyncMock(return_value=AudioResult(b"\x00\x01" * 800, "audio/L16"))
        played = {}
        with patch.object(main.speaker, "play_pcm", side_effect=lambda pcm, rate: played.update(pcm=pcm, rate=rate) or True):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/api/chat/audio",
                    content=b"wav-data",
                    headers={"Content-Type": "audio/wav", "X-Play-Audio": "true"},
                )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["reply"], "我在呢。")
        self.assertTrue(payload["played"])
        self.assertIsNone(payload["audio"])
        self.assertEqual(played["rate"], 24000)
        self.assertEqual(len(played["pcm"]), 1600)
        self.assertEqual(main.tts.synthesize.await_args.kwargs["audio_format"], "pcm")

    async def test_audio_chat_keeps_transcript_when_language_model_is_down(self):
        main.stt.transcribe = AsyncMock(return_value="我今天有点累")
        main.llm.complete_reply = AsyncMock(side_effect=ProviderError("模型响应超时", status_code=504))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/chat/audio",
                content=b"wav-data",
                headers={"Content-Type": "audio/wav", "X-Allow-Partial": "true", "X-Play-Audio": "true"},
            )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["transcript"], "我今天有点累")
        self.assertEqual(payload["reply"], "")
        self.assertIn("模型响应超时", payload["error"])
        self.assertFalse(payload["played"])

    async def test_audio_chat_still_fails_without_partial_header(self):
        main.stt.transcribe = AsyncMock(return_value="你好")
        main.llm.complete_reply = AsyncMock(side_effect=ProviderError("模型响应超时", status_code=504))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat/audio", content=b"wav-data", headers={"Content-Type": "audio/wav"})
        self.assertEqual(response.status_code, 504)

    def test_pcm_to_wav_builds_playable_container(self):
        import io
        import wave

        from app.playback import pcm_to_wav

        data = pcm_to_wav(b"\x00\x01" * 1600, 24000)
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(data[8:12], b"WAVE")
        with wave.open(io.BytesIO(data), "rb") as wav:
            self.assertEqual(wav.getframerate(), 24000)
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getnframes(), 1600)


if __name__ == "__main__":
    unittest.main()
