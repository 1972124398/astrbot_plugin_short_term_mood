import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
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

    def plain_result(self, text):
        return text


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

    async def settle(self):
        await asyncio.wait_for(self.plugin.queue.join(), timeout=2)
        if self.plugin.save_task is not None:
            await self.plugin.save_task

    async def test_different_conversations_and_personas_share_one_state(self):
        first, second = FakeEvent("wechat:private:a"), FakeEvent("qq:group:b")
        await self.plugin.apply_mood(first, ProviderRequest(prompt="你真讨厌"))
        await self.settle()
        self.persona = {"prompt": "你是克制而冷淡的角色。"}
        await self.plugin.apply_mood(second, ProviderRequest(prompt="你真讨厌"))
        await self.settle()
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
        self.context.llm_generate.assert_not_awaited()
        await self.settle()
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
        await self.settle()
        self.assertGreater(self.plugin.store.load().levels["anger"], 0.89)

    async def test_commands_and_opt_out_skip_assessment(self):
        for event in [FakeEvent("a", "/codex task"), FakeEvent("b", "!command")]:
            await self.plugin.apply_mood(event, ProviderRequest(prompt="something"))
        event = FakeEvent("c")
        event.set_extra("short_term_mood_skip", True)
        await self.plugin.apply_mood(event, ProviderRequest(prompt="something"))
        self.context.llm_generate.assert_not_awaited()

    async def test_provider_failure_keeps_reply_and_recovers_old_affect(self):
        self.plugin.state = MoodState({"anger": 0.8}, {"anger": 1}, updated=1)
        self.context.llm_generate.side_effect = RuntimeError("provider unavailable")
        event, request = FakeEvent("a"), ProviderRequest(prompt="你好")
        await self.plugin.apply_mood(event, request)
        self.assertIn("心境已恢复平稳", event.get_extra(EVENT_HINT))
        await self.settle()
        await self.plugin.apply_mood(FakeEvent("b"), ProviderRequest(prompt="你好"))
        await self.settle()
        self.context.llm_generate.assert_awaited_once()
        self.assertEqual(self.plugin.store.load().levels, {})

    async def test_persona_cache_regenerates_when_main_prompt_changes(self):
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="你好"))
        await self.settle()
        await self.plugin.apply_mood(FakeEvent("b"), ProviderRequest(prompt="你好"))
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertFalse(payload["need_profiles"])
        self.persona = {"prompt": "新主提示词"}
        await self.plugin.apply_mood(FakeEvent("c"), ProviderRequest(prompt="你好"))
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertTrue(payload["need_profiles"])

    async def test_proactive_chat_uses_shared_mood_without_inventing_stimuli(self):
        await self.plugin.apply_mood(FakeEvent("private:a"), ProviderRequest(prompt="刺激"))
        await self.settle()
        event = FakeEvent("private:b", "")
        event.message_obj = SimpleNamespace(message_id="proactive:123")
        await self.plugin.apply_mood(event, ProviderRequest(prompt="主动聊天生成指令"))
        await self.settle()
        self.context.llm_generate.assert_awaited_once()
        self.assertIn("生气", event.get_extra(EVENT_HINT))
        self.assertLessEqual(self.plugin.store.load().levels["anger"], 0.3)

    async def test_refusal_policy_is_only_a_temporary_hint(self):
        import time

        now = time.time()
        self.plugin.state = MoodState({"anger": 1}, {"anger": now}, updated=now)
        self.plugin.config.update({"refusal_probability": 1, "refusal_cooldown_seconds": 0})
        with patch("astrbot_plugin_short_term_mood.main.random.random", return_value=0):
            first, second = FakeEvent("a"), FakeEvent("b")
            await self.plugin.apply_mood(first, ProviderRequest(prompt="刺激"))
            await self.plugin.apply_mood(second, ProviderRequest(prompt="刺激"))
        self.assertIn("本轮允许短暂婉拒", first.get_extra(EVENT_HINT))
        self.assertIn("本轮保持回应意愿", second.get_extra(EVENT_HINT))

    async def test_slow_background_model_does_not_block_any_frontend_conversation(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_model(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text=json.dumps(self.appraisal))

        self.context.llm_generate.side_effect = slow_model
        first, second = FakeEvent("private:a"), FakeEvent("group:b")
        await asyncio.wait_for(self.plugin.apply_mood(first, ProviderRequest(prompt="刺激")), 0.1)
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(self.plugin.apply_mood(second, ProviderRequest(prompt="新的互动")), 0.1)
        self.assertFalse(release.is_set())
        self.assertIn("心境已恢复平稳", second.get_extra(EVENT_HINT))
        release.set()
        await self.settle()
        later = FakeEvent("private:c")
        await self.plugin.apply_mood(later, ProviderRequest(prompt="你好"))
        self.assertIn("生气", later.get_extra(EVENT_HINT))
        await self.settle()

    async def test_reset_discards_late_results_without_waiting_for_the_model(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_model(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text=json.dumps(self.appraisal))

        self.context.llm_generate.side_effect = slow_model
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="刺激"))
        await started.wait()

        async def reset():
            return [result async for result in self.plugin.mood_reset(FakeEvent("admin"))]

        await asyncio.wait_for(reset(), 0.5)
        release.set()
        await self.settle()
        self.assertEqual(self.plugin.state.levels, {})
        self.assertEqual(self.plugin.profiles, {})

    async def test_background_uses_immutable_request_snapshot(self):
        request = ProviderRequest(prompt="原始消息", image_urls=["original.png"])
        event = FakeEvent("a")
        await self.plugin.apply_mood(event, request)
        request.prompt = "后续改写"
        request.image_urls.clear()
        self.persona = {"prompt": "后续切换的人格"}
        await self.settle()
        arguments = self.context.llm_generate.call_args.kwargs
        payload = json.loads(arguments["prompt"])
        self.assertEqual(payload["current_input"], "原始消息")
        self.assertNotIn("后续切换", payload["persona"])
        self.assertEqual(arguments["image_urls"], ["original.png"])

    async def test_stale_result_cannot_reintroduce_emotion(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_model(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text=json.dumps(self.appraisal))

        self.context.llm_generate.side_effect = slow_model
        original_snapshot = self.plugin.snapshot
        with patch.object(self.plugin, "snapshot", side_effect=lambda *args: replace(original_snapshot(*args), queued_at=0)):
            # Accept the old job initially, then tighten the window while the model is running.
            with patch.object(self.plugin, "job_current", return_value=True):
                await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="刺激"))
                await started.wait()
        self.plugin.config["analysis_max_age_seconds"] = 1
        release.set()
        await self.settle()
        self.assertEqual(self.plugin.state.levels, {})

    async def test_configured_timeout_only_cancels_background_detection(self):
        started = asyncio.Event()

        async def blocked_model(**kwargs):
            started.set()
            await asyncio.Event().wait()

        self.context.llm_generate.side_effect = blocked_model
        self.plugin.config["analysis_timeout_seconds"] = 1
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="刺激"))
        await started.wait()
        await asyncio.wait_for(self.plugin.apply_mood(FakeEvent("b"), ProviderRequest(prompt="你好")), 0.1)
        await self.settle()
        self.context.llm_generate.assert_awaited_once()
        self.assertGreater(self.plugin.retry_after, 0)
        self.assertEqual(self.plugin.state.levels, {})

    async def test_unload_cancels_pending_model_and_closes_worker(self):
        started = asyncio.Event()

        async def blocked_model(**kwargs):
            started.set()
            await asyncio.Event().wait()

        self.context.llm_generate.side_effect = blocked_model
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="刺激"))
        await started.wait()
        await asyncio.wait_for(self.plugin.terminate(), 0.5)
        self.assertTrue(self.plugin.worker.done())
        self.assertEqual(self.plugin.queue.qsize(), 0)

    async def test_cancelled_write_keeps_database_serialized_until_thread_finishes(self):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original_save = self.plugin.store.save

        def slow_save(state):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=2):
                raise TimeoutError("Test did not release the storage thread")
            original_save(state)

        with patch.object(self.plugin.store, "save", side_effect=slow_save):
            task = asyncio.create_task(self.plugin.persist_state())
            try:
                await started.wait()
                task.cancel()
                await asyncio.sleep(0)
                self.assertTrue(self.plugin.persistence_lock.locked())
                self.assertFalse(task.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.plugin.persistence_lock.locked())

    async def test_queue_capacity_keeps_recent_interactions(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_model(**kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(completion_text=json.dumps(self.appraisal))

        self.context.llm_generate.side_effect = slow_model
        self.plugin.config["analysis_queue_size"] = 1
        await self.plugin.apply_mood(FakeEvent("first"), ProviderRequest(prompt="第一条"))
        await started.wait()
        await self.plugin.apply_mood(FakeEvent("old"), ProviderRequest(prompt="应淘汰"))
        await self.plugin.apply_mood(FakeEvent("latest"), ProviderRequest(prompt="最新"))
        self.assertEqual(self.plugin.queue.qsize(), 1)
        release.set()
        await self.settle()
        payloads = [json.loads(call.kwargs["prompt"]) for call in self.context.llm_generate.call_args_list]
        self.assertEqual([payload["current_input"] for payload in payloads], ["第一条", "最新"])

    async def test_old_refusal_eligibility_is_never_reused_for_another_request(self):
        from astrbot_plugin_short_term_mood.prompts import build_hint

        hint = build_hint({"anger": 0.9}, None, True)
        self.assertIn("当前请求判断", hint)
        self.assertIn("工具", hint)

    async def test_native_configuration_schema_preserves_existing_settings(self):
        from astrbot.api import AstrBotConfig

        schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
        config_path = Path(self.directory.name) / "config.json"
        config_path.write_text(json.dumps({"analysis_timeout_seconds": 42, "half_life_seconds": 900}), encoding="utf-8")
        config = AstrBotConfig(str(config_path), schema=schema)
        self.assertEqual(config["analysis_timeout_seconds"], 42)
        self.assertEqual(config["half_life_seconds"], 900)
        self.assertTrue(config["sustain_during_conversation"])
        self.assertEqual(config["conversation_idle_seconds"], 300)
        self.assertEqual(schema["analysis_provider_id"]["_special"], "select_provider")
        config["analysis_queue_size"] = 7
        config.save_config()
        reloaded = AstrBotConfig(str(config_path), schema=schema)
        self.assertEqual(reloaded["analysis_queue_size"], 7)


if __name__ == "__main__":
    unittest.main()
