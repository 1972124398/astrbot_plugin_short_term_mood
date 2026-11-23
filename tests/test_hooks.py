import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
SDK_AVAILABLE = importlib.util.find_spec("astrbot") is not None

if SDK_AVAILABLE:
    from astrbot.api.provider import ProviderRequest
    from astrbot.core.agent.message import TextPart
    from astrbot_plugin_short_term_mood.main import EVENT_HINT, ShortTermMoodPlugin
    from astrbot_plugin_short_term_mood.mood import EMOTIONS, MoodState


class FakeEvent:
    def __init__(self, origin, message="你好"):
        self.unified_msg_origin = origin
        self.message = message
        self.extras = {"_trace_persona_id": "test"}

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_message_str(self):
        return self.message

    def is_stopped(self):
        return False


@unittest.skipUnless(SDK_AVAILABLE, "Requires AstrBot SDK for integration checks")
class HookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.persona = {"prompt": "你是语气温柔但有自己边界的角色。"}
        self.appraisal = {
            "deltas": {"anger": 0.3},
            "confidence": 0.9,
            "allow_refusal": True,
            "profiles": {name: f"以角色口吻表现{name}" for name in {"neutral", *EMOTIONS}},
        }
        self.context = SimpleNamespace(
            persona_manager=SimpleNamespace(get_persona_v3_by_id=lambda _: self.persona),
            get_current_chat_provider_id=AsyncMock(return_value="test-provider"),
            llm_generate=AsyncMock(side_effect=lambda **_: SimpleNamespace(completion_text=json.dumps(self.appraisal))),
        )
        with patch("astrbot_plugin_short_term_mood.main.StarTools.get_data_dir", return_value=Path(self.directory.name)):
            self.plugin = ShortTermMoodPlugin(self.context, {"analyze_images": True})

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.directory.cleanup()

    async def test_different_conversations_and_personas_share_one_state(self):
        first, second = FakeEvent("wechat:private:a"), FakeEvent("qq:group:b")
        await self.plugin.apply_mood(first, ProviderRequest(prompt="你真讨厌"))
        self.persona = {"prompt": "你是克制而冷淡的角色。"}
        await self.plugin.apply_mood(second, ProviderRequest(prompt="你真讨厌"))
        state = self.plugin.store.load()
        self.assertGreater(state.levels["anger"], 0.59)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertGreater(payload["current_mood"]["anger"], 0.29)
        self.assertIn("冷淡", payload["persona"])

    async def test_only_appends_temporary_content_and_repeated_hook_is_idempotent(self):
        event = FakeEvent("a")
        existing = TextPart(text="来自记忆或 PDF 的内容")
        tools = object()
        request = ProviderRequest(
            prompt="合并后的多条消息",
            system_prompt="人格、工具和表情说明",
            contexts=[{"role": "user", "content": "先前消息"}],
            image_urls=["https://example.invalid/a.png"],
            audio_urls=["a.wav"],
            extra_user_content_parts=[existing],
        )
        request.func_tool = tools
        await self.plugin.apply_mood(event, request)
        await self.plugin.apply_mood(event, request)
        self.context.llm_generate.assert_awaited_once()
        self.assertEqual(len(request.extra_user_content_parts), 2)
        self.assertIs(request.extra_user_content_parts[0], existing)
        self.assertTrue(request.extra_user_content_parts[-1].model_dump_for_context()["_no_save"])
        self.assertEqual(request.system_prompt, "人格、工具和表情说明")
        self.assertEqual(request.prompt, "合并后的多条消息")
        self.assertEqual(request.contexts, [{"role": "user", "content": "先前消息"}])
        self.assertEqual(request.image_urls, ["https://example.invalid/a.png"])
        self.assertEqual(request.audio_urls, ["a.wav"])
        self.assertIs(request.func_tool, tools)

    async def test_concurrent_requests_do_not_lose_updates(self):
        await asyncio.gather(*(
            self.plugin.apply_mood(FakeEvent(str(index)), ProviderRequest(prompt="刺激"))
            for index in range(3)
        ))
        self.assertGreater(self.plugin.store.load().levels["anger"], 0.89)

    async def test_commands_and_opt_out_skip_assessment(self):
        for event in [FakeEvent("a", "/codex task"), FakeEvent("b", "!command")]:
            await self.plugin.apply_mood(event, ProviderRequest(prompt="something"))
        event = FakeEvent("c")
        event.set_extra("short_term_mood_skip", True)
        await self.plugin.apply_mood(event, ProviderRequest(prompt="something"))
        self.context.llm_generate.assert_not_awaited()

    async def test_provider_failure_keeps_reply_and_recovers_old_affect(self):
        self.plugin.store.save(MoodState({"anger": 0.8}, {"anger": 1}, updated=1))
        self.context.llm_generate.side_effect = RuntimeError("provider unavailable")
        event, request = FakeEvent("a"), ProviderRequest(prompt="你好")
        await self.plugin.apply_mood(event, request)
        self.assertIn("心境已恢复平稳", event.get_extra(EVENT_HINT))
        await self.plugin.apply_mood(FakeEvent("b"), ProviderRequest(prompt="你好"))
        self.context.llm_generate.assert_awaited_once()
        self.assertEqual(self.plugin.store.load().levels, {})

    async def test_persona_cache_regenerates_when_main_prompt_changes(self):
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="你好"))
        await self.plugin.apply_mood(FakeEvent("b"), ProviderRequest(prompt="你好"))
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertFalse(payload["need_profiles"])
        self.persona = {"prompt": "新主提示词"}
        await self.plugin.apply_mood(FakeEvent("c"), ProviderRequest(prompt="你好"))
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertTrue(payload["need_profiles"])

    async def test_proactive_chat_uses_shared_mood_without_inventing_stimuli(self):
        await self.plugin.apply_mood(FakeEvent("private:a"), ProviderRequest(prompt="刺激"))
        event = FakeEvent("private:b", "")
        event.message_obj = SimpleNamespace(message_id="proactive:123")
        await self.plugin.apply_mood(event, ProviderRequest(prompt="主动聊天生成指令"))
        self.context.llm_generate.assert_awaited_once()
        self.assertIn("生气", event.get_extra(EVENT_HINT))
        self.assertLessEqual(self.plugin.store.load().levels["anger"], 0.3)

    async def test_refusal_policy_is_only_a_temporary_hint(self):
        import time

        now = time.time()
        self.plugin.store.save(MoodState({"anger": 1}, {"anger": now}, updated=now))
        self.plugin.config.update({"refusal_probability": 1, "refusal_cooldown_seconds": 0})
        with patch("astrbot_plugin_short_term_mood.main.random.random", return_value=0):
            first, second = FakeEvent("a"), FakeEvent("b")
            await self.plugin.apply_mood(first, ProviderRequest(prompt="刺激"))
            await self.plugin.apply_mood(second, ProviderRequest(prompt="刺激"))
        self.assertIn("本轮允许短暂婉拒", first.get_extra(EVENT_HINT))
        self.assertIn("本轮保持回应意愿", second.get_extra(EVENT_HINT))


if __name__ == "__main__":
    unittest.main()
