import asyncio
import copy
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
            "inner_state": "有点不满，还愿意说清楚",
            "response_tendency": "想直接表达感受",
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

    async def test_model_directed_transition_affects_next_reply_and_is_shared(self):
        self.appraisal.update(next_mood={"anger": 0.1, "tenderness": 0.75},
                              inner_state="气已经消了些，还想听听", response_tendency="愿意给个台阶")
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=100000):
            self.plugin.state = MoodState({"anger": 0.8}, {"anger": 100000}, updated=100000, last_interaction=100000)
            self.plugin.config.update(sensitivity=2, emotion_inertia=0.9)
            await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="刚才是我不好"))
            await self.settle()
            self.assertEqual(self.plugin.state.levels, self.appraisal["next_mood"])
            self.assertEqual(self.plugin.store.load().levels, self.appraisal["next_mood"])
            event = FakeEvent("b")
            await self.plugin.apply_mood(event, ProviderRequest(prompt="现在想聊点什么？"))
            self.assertIn("气已经消了些，还想听听", event.get_extra(EVENT_HINT))
            await self.settle()

    async def test_brief_reactions_persist_until_model_decides_to_change_them(self):
        self.appraisal.update(next_mood={"surprise": 0.5}, inner_state="还挺惊讶", response_tendency="想继续问问")
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=100000):
            self.plugin.state = MoodState({"surprise": 0.5}, {"surprise": 100000}, updated=100000, last_interaction=100000)
            for _ in range(3):
                await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="继续说呢"))
                await self.settle()
            self.assertEqual(self.plugin.state.levels, {"surprise": 0.5})
            self.appraisal["next_mood"] = {"contentment": 0.3}
            await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="原来这样啊"))
            await self.settle()
            self.assertEqual(self.plugin.state.levels, {"contentment": 0.3})

    async def test_low_confidence_snapshot_does_not_replace_existing_feelings(self):
        self.appraisal.update(next_mood={}, confidence=0.3)
        self.plugin.state = MoodState({"anger": 0.8}, {"anger": 100000}, updated=100000, last_interaction=100000)
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="继续聊"))
        await self.settle()
        self.assertEqual(self.plugin.state.levels, {"anger": 0.8})

    async def test_proactive_utterance_does_not_invent_a_model_selected_mood(self):
        self.appraisal.update(next_mood={"joy": 1})
        self.plugin.state = MoodState({"anger": 0.8}, {"anger": 100000}, updated=100000, last_interaction=100000)
        before = copy.deepcopy(self.plugin.state.as_dict())
        event = FakeEvent("a", "")
        event.message_obj = SimpleNamespace(message_id="proactive:model-decisions")
        await self.plugin.apply_mood(event, ProviderRequest(prompt="主动聊聊"))
        await self.settle()
        self.assertEqual(self.plugin.state.as_dict(), before)

    async def test_legacy_model_deltas_are_not_scaled_or_smoothed_by_host(self):
        self.plugin.config.update(sensitivity=2, emotion_inertia=0.9)
        self.appraisal["deltas"] = {"anger": -0.2, "tenderness": 0.1}
        self.plugin.state = MoodState({"anger": 0.8}, {"anger": 100000}, updated=100000, last_interaction=100000)
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="抱歉"))
        await self.settle()
        self.assertAlmostEqual(self.plugin.state.levels["anger"], 0.6)
        self.assertEqual(self.plugin.state.levels["tenderness"], 0.1)
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["sensitivity_preference"], 2)
        self.assertEqual(payload["emotional_inertia_preference"], 0.9)

    async def test_foreground_reads_never_recover_state_even_with_legacy_recovery_enabled(self):
        self.plugin.state = MoodState({"anger": 0.8}, {"anger": 1000}, updated=1000, last_interaction=1000)
        self.plugin.config["natural_recovery_enabled"] = True
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=100000):
            self.assertEqual(self.plugin.read_state().levels, {"anger": 0.8})
            self.plugin.companion_snapshot("a")
            self.assertEqual(self.plugin.state.levels, {"anger": 0.8})
            self.assertEqual(self.plugin.recover_state().levels, {})

    async def test_invalid_next_mood_cannot_partially_overwrite_persistent_state(self):
        self.plugin.state = MoodState({"joy": 0.4}, {"joy": 100000}, updated=100000, last_interaction=100000)
        self.appraisal["next_mood"] = {"joy": 0.9, "surprise": float("nan")}
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="你好"))
        await self.settle()
        self.assertEqual(self.plugin.state.levels, {"joy": 0.4})
        self.assertEqual(self.plugin.store.load().levels, {"joy": 0.4})

    async def test_return_selection_is_background_only_and_frontend_is_read_only(self):
        self.plugin.config.update(random_mood_pool=["迷糊"], random_mood_after_seconds=3600)
        self.plugin.state = MoodState(updated=1000, last_interaction=1000)
        self.appraisal["next_mood"] = {"playfulness": 0.4}
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=100000):
            before = copy.deepcopy(self.plugin.state.as_dict())
            first = FakeEvent("a")
            request = ProviderRequest(prompt="回来啦", system_prompt="原人设")
            await self.plugin.apply_mood(first, request)
            self.context.llm_generate.assert_not_awaited()
            self.assertEqual(self.plugin.state.as_dict(), before)
            self.assertEqual(request.system_prompt, "原人设")
            for word in ("candidate_moods", "return_context", "这次开场由你", "评估模型", "next_mood", "切换"):
                self.assertNotIn(word, first.get_extra(EVENT_HINT))
            await self.settle()
            payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
            self.assertEqual(payload["return_context"]["candidate_moods"], ["confusion"])
            self.assertEqual(self.plugin.state.levels, {"playfulness": 0.4})
            second = FakeEvent("b")
            await self.plugin.apply_mood(second, ProviderRequest(prompt="你好"))
            self.assertIn("俏皮 0.", second.get_extra(EVENT_HINT))
            await self.settle()

    async def test_foreground_never_waits_for_database_and_has_no_assessment_protocol(self):
        request = ProviderRequest(prompt="陪我聊聊", system_prompt="保持原人设与工具约定")
        event = FakeEvent("a")
        with patch.object(self.plugin.store, "load", side_effect=AssertionError("Foreground database read")), \
                patch.object(self.plugin, "begin_interaction", side_effect=AssertionError("Foreground mood change")), \
                patch.object(self.plugin, "recover_state", side_effect=AssertionError("Foreground recovery")):
            await self.plugin.apply_mood(event, request)
        self.assertEqual(request.system_prompt, "保持原人设与工具约定")
        hint = event.get_extra(EVENT_HINT)
        self.assertLess(len(hint), 1200)
        for marker in ("next_mood", "return_context", "candidate_moods", "评估模型", "情绪数值", "切换", "选择心情"):
            self.assertNotIn(marker, hint)
        await self.settle()

    async def test_observers_commands_and_internal_events_do_not_trigger_random_return(self):
        self.plugin.config.update(random_mood_pool=["mischief"], random_mood_after_seconds=3600)
        self.plugin.state = MoodState(updated=1000, last_interaction=1000)
        self.appraisal["deltas"] = {}
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=100000):
            self.plugin.companion_snapshot("a")
            _ = [text async for text in self.plugin.mood_status(FakeEvent("a"))]
            await self.plugin.apply_mood(FakeEvent("a", "/mood"), ProviderRequest(prompt="/mood"))
            task_event = FakeEvent("a")
            task_event.set_extra("codex_bridge", {"kind": "task_result"})
            await self.plugin.apply_mood(task_event, ProviderRequest(prompt="任务完成"))
            proactive = FakeEvent("a", "")
            proactive.message_obj = SimpleNamespace(message_id="proactive:123")
            await self.plugin.apply_mood(proactive, ProviderRequest(prompt="主动聊聊"))
            await self.settle()
            self.assertEqual(self.plugin.state.last_interaction, 1000)
            self.assertEqual(self.plugin.state.levels, {})
            returning = FakeEvent("a")
            await self.plugin.apply_mood(returning, ProviderRequest(prompt="回来啦"))
            self.assertNotIn("久别后这轮开场的背景", returning.get_extra(EVENT_HINT))
            await self.settle()

    async def test_random_return_can_be_disabled(self):
        self.plugin.config.update(random_mood_on_return=False, random_mood_pool=["mischief"])
        self.plugin.state = MoodState(updated=1000, last_interaction=1000)
        event = FakeEvent("a")
        await self.plugin.apply_mood(event, ProviderRequest(prompt="回来啦"))
        self.assertIn("平静随性地聊", event.get_extra(EVENT_HINT))
        await self.settle()

    async def test_return_context_uses_observed_arrival_time_in_background(self):
        self.plugin.config.update(random_mood_pool=["mischief"], random_mood_after_seconds=60)
        self.plugin.state = MoodState(updated=1000, last_interaction=1000)
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=1060):
            await self.plugin.apply_mood(FakeEvent("b"), ProviderRequest(prompt="新的交流"))
        self.assertEqual(self.plugin.state.last_interaction, 1000)
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["return_context"]["idle_seconds"], 60)
        self.assertEqual(self.plugin.state.last_interaction, 1060)

    async def test_new_chinese_emotion_names_work_with_admin_command(self):
        for emotion, name in [("恶作剧", "mischief"), ("迷糊", "confusion"), ("烦躁", "irritability"),
                              ("期待", "anticipation"), ("吃醋", "jealousy"), ("心软", "tenderness"),
                              (" 放松 ", "contentment"), ("思念", "longing"), ("GRATITUDE", "gratitude")]:
            _ = [result async for result in self.plugin.mood_set(FakeEvent("admin"), emotion, 0.4)]
            self.assertEqual(self.plugin.store.load().levels, {name: 0.4})

    async def test_old_persona_cache_is_regenerated_for_expanded_emotions(self):
        import hashlib

        event, request = FakeEvent("a"), ProviderRequest(prompt="你好")
        old_key = hashlib.sha256(json.dumps(["v5", self.persona["prompt"], "", request.model, "", "casual", 0.6], ensure_ascii=False).encode()).hexdigest()
        self.plugin.profiles[old_key] = ({"neutral": "旧表达"}, float("inf"))
        await self.plugin.apply_mood(event, request)
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertTrue(payload["need_profiles"])
        new_key = self.plugin.snapshot(FakeEvent("a"), ProviderRequest(prompt="你好")).key
        self.assertEqual(set(self.plugin.profiles[new_key][0]), {"neutral", *EMOTIONS})

    async def test_old_expression_contract_is_not_reused_after_persona_boundary_update(self):
        import hashlib

        request = ProviderRequest(prompt="你好")
        old_key = hashlib.sha256(json.dumps(
            ["v7", list(EMOTIONS), self.persona["prompt"], "", request.model, "", "casual", 0.6],
            ensure_ascii=False,
        ).encode()).hexdigest()
        self.plugin.profiles[old_key] = ({"neutral": "旧缓存中的固定亲昵称呼"}, float("inf"))
        event = FakeEvent("a")
        await self.plugin.apply_mood(event, request)
        self.assertNotIn("旧缓存中的固定亲昵称呼", event.get_extra(EVENT_HINT))
        self.assertNotEqual(self.plugin.snapshot(event, request).key, old_key)
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertTrue(payload["need_profiles"])

    async def test_banter_update_reaches_foreground_without_waiting_or_rewriting_persona(self):
        self.plugin.state = MoodState({"anger": 0.8}, updated=100)
        before = copy.deepcopy(self.plugin.state.as_dict())
        hints = []
        for level in (0.2, 0.9):
            self.plugin.config.update(banter_level=level, expression_strength=0.25)
            event = FakeEvent("a")
            request = ProviderRequest(prompt="刚才误会了，想认真说清楚", system_prompt="原始人格和职责")
            await self.plugin.apply_mood(event, request)
            hints.append(event.get_extra(EVENT_HINT))
            self.assertEqual(request.system_prompt, "原始人格和职责")
            self.assertEqual(self.plugin.state.as_dict(), before)
            self.context.llm_generate.assert_not_awaited()
            self.assertIn("生气（轻微", hints[-1])
            self.assertIn("澄清", hints[-1])
        self.assertNotEqual(hints[0], hints[1])
        await self.settle()

    async def test_persona_profiles_merge_across_bounded_batches_and_then_use_cache(self):
        batches = []

        async def honor_batch(**kwargs):
            payload = json.loads(kwargs["prompt"])
            targets = payload["profile_emotions"]
            self.assertLessEqual(len(targets), 8)
            self.assertEqual(payload["need_profiles"], bool(targets))
            batches.append(targets)
            result = {"deltas": {"gratitude": 0.05}, "confidence": 0.9}
            if targets:
                result["profiles"] = {name: "以这个角色的口吻表达" + name for name in targets}
            return SimpleNamespace(completion_text=json.dumps(result))

        self.context.llm_generate.side_effect = honor_batch
        self.plugin.state = MoodState({"tenderness": 0.4}, {"tenderness": 100000}, updated=100000, last_interaction=100000)
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=100000):
            for _ in range(6):
                await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="谢谢你，继续聊"))
                await self.settle()
            self.assertEqual(batches[0][:2], ["neutral", "tenderness"])
            self.assertEqual([len(batch) for batch in batches], [8, 8, 8, 8, 5, 0])
            key = self.plugin.snapshot(FakeEvent("a"), ProviderRequest(prompt="你好")).key
            self.assertEqual(set(self.plugin.profiles[key][0]), {"neutral", *EMOTIONS})
            self.assertEqual(self.plugin.store.profile(key, 100000), self.plugin.profiles[key][0])
            self.assertAlmostEqual(self.plugin.state.levels["gratitude"], 0.3)

    async def test_partial_profile_cache_survives_and_continues_from_missing_emotions(self):
        job = self.plugin.snapshot(FakeEvent("a"), ProviderRequest(prompt="你好"))
        import time

        created = time.time()
        partial = {"neutral": "平时的口吻", "joy": "高兴的口吻"}
        await self.plugin.write_store(self.plugin.store.save_profile, job.key, partial, created)
        self.plugin.profiles = self.plugin.store.load_profiles()
        self.appraisal["profiles"] = {name: "新的表达" for name in list(EMOTIONS)[1:9]}
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="继续聊"))
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertNotIn("neutral", payload["profile_emotions"])
        self.assertNotIn("joy", payload["profile_emotions"])
        self.assertEqual(len(self.plugin.profiles[job.key][0]), 10)
        self.assertEqual(self.plugin.profiles[job.key][0]["joy"], "高兴的口吻")

    async def test_game_interaction_triggers_return_mood_but_leisure_does_not(self):
        self.plugin.config.update(random_mood_pool=["mischief"], random_mood_after_seconds=3600)
        self.plugin.state = MoodState(updated=1000, last_interaction=1000)
        self.appraisal["deltas"] = {}
        packet = {"origin": "a", "id": "one", "kind": "leisure_experience", "data": {"human_text": ""}}
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=100000):
            self.plugin.companion_observe(packet)
            await self.settle()
            self.assertEqual(self.plugin.state.last_interaction, 1000)
            self.assertEqual(self.plugin.state.levels, {})
            self.plugin.companion_observe({**packet, "id": "two", "kind": "game_turn"})
            await self.settle()
            payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
            self.assertEqual(payload["return_context"]["candidate_moods"], ["mischief"])
            self.assertEqual(self.plugin.state.last_interaction, 100000)
            self.assertEqual(self.plugin.state.levels, {})

    async def test_disabled_plugin_ignores_game_notifications(self):
        self.plugin.config["enabled"] = False
        self.plugin.state = MoodState(updated=1000, last_interaction=1000)
        self.plugin.companion_observe({"origin": "a", "id": "one", "kind": "game_turn", "data": {"human_text": "一起玩"}})
        self.assertEqual(self.plugin.state.last_interaction, 1000)
        self.assertTrue(self.plugin.queue.empty())
        self.assertEqual(self.plugin.state.levels, {})

    async def test_background_task_is_not_framed_as_a_new_personal_stimulus(self):
        event = FakeEvent("a", "Codex 返回了任务结果")
        event.set_extra("codex_bridge", {"kind": "task_result"})
        request = ProviderRequest(prompt=event.message)
        await self.plugin.apply_mood(event, request)
        self.assertNotIn("当前消息发生的新刺激", request.extra_user_content_parts[-1].text)
        self.assertEqual(self.plugin.state.last_interaction, 0)
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["event_kind"], "task_event")

    async def test_long_gap_uses_fresh_appraisal_without_deleting_saved_history(self):
        old = [{"role": "user", "content": "你真讨厌"}, {"role": "assistant", "content": "有点生气"}]
        event = FakeEvent("a", "早上好")
        self.plugin.clocks.moment("a")["last_user"] = 1000
        request = ProviderRequest(prompt="早上好", contexts=old)
        with patch("astrbot_plugin_short_term_mood.main.time.time", return_value=30000):
            await self.plugin.apply_mood(event, request)
            await self.plugin.apply_mood(event, request)
        self.assertEqual(request.contexts, old)
        self.assertEqual(sum(part.text.startswith("<astrbot_time_context>") for part in request.extra_user_content_parts), 1)
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertEqual(payload["time_context"]["gap_seconds"], 29000)
        self.assertEqual(payload["recent_dialogue"], [])

    async def test_codex_does_not_touch_real_conversation_clock(self):
        self.plugin.clocks.moment("a")["last_user"] = 1000
        event = FakeEvent("a", "任务完成")
        event.set_extra("w1_event", {"type": "task_event", "is_user_message": False})
        await self.plugin.apply_mood(event, ProviderRequest(prompt="任务完成"))
        await self.plugin.finish_response(event, SimpleNamespace(completion_text="做好啦"))
        self.assertEqual(self.plugin.clocks.moment("a")["last_user"], 1000)
        self.assertEqual(self.plugin.clocks.moment("a")["last_reply"], 0)
        await self.settle()

    async def test_task_notice_can_be_directly_declined_without_affecting_real_dialogue(self):
        event = FakeEvent("a", "任务完成")
        event.set_extra("codex_bridge", {"kind": "task_result"})
        request = ProviderRequest(prompt="任务完成")
        await self.plugin.apply_mood(event, request)
        self.assertIn("不必套用客气的服务拒绝话术", event.get_extra(EVENT_HINT))
        self.assertIn("mood_ignore_codex_notice", event.get_extra(EVENT_HINT))
        ordinary = FakeEvent("b", "陪我聊会儿")
        await self.plugin.apply_mood(ordinary, ProviderRequest(prompt=ordinary.message))
        self.assertNotIn("astrbot_codex_notice_agency", ordinary.get_extra(EVENT_HINT))
        await self.settle()

    async def test_notice_refusal_can_be_disabled_without_disabling_emotion(self):
        self.plugin.config["allow_codex_notice_refusal"] = False
        event = FakeEvent("a", "任务完成")
        event.set_extra("codex_bridge", {"kind": "task_result"})
        await self.plugin.apply_mood(event, ProviderRequest(prompt="任务完成"))
        self.assertNotIn("astrbot_codex_notice_agency", event.get_extra(EVENT_HINT))
        self.assertIn("保持对话回应", event.get_extra(EVENT_HINT))
        self.assertFalse(self.plugin.can_decline_notice(event))
        await self.settle()

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
        self.plugin.config["natural_recovery_enabled"] = True
        self.plugin.state = MoodState({"anger": 0.8}, {"anger": 1}, updated=1)
        self.context.llm_generate.side_effect = RuntimeError("provider unavailable")
        event, request = FakeEvent("a"), ProviderRequest(prompt="你好")
        await self.plugin.apply_mood(event, request)
        self.assertIn("生气", event.get_extra(EVENT_HINT))
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

    async def test_emotional_agency_is_available_across_successive_turns(self):
        import time

        now = time.time()
        self.plugin.state = MoodState({"anger": 1}, {"anger": now}, updated=now)
        first, second = FakeEvent("a"), FakeEvent("b")
        await self.plugin.apply_mood(first, ProviderRequest(prompt="刺激"))
        await self.plugin.apply_mood(second, ProviderRequest(prompt="刺激"))
        self.assertIn("不想聊时", first.get_extra(EVENT_HINT))
        self.assertIn("不想聊时", second.get_extra(EVENT_HINT))
        self.assertNotIn("本轮保持回应意愿", second.get_extra(EVENT_HINT))

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
        self.assertIn("平静随性地聊", second.get_extra(EVENT_HINT))
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

    async def test_emotion_changes_both_tone_and_interaction_intent(self):
        from astrbot_plugin_short_term_mood.prompts import build_hint

        hint = build_hint({"anger": 0.9}, None, True, inner_state="仍有余气", response_tendency="想反问一句")
        self.assertIn("仍有余气", hint)
        self.assertIn("想反问一句", hint)
        self.assertIn("反问、争辩", hint)

    async def test_background_feeling_and_intent_shape_next_turn(self):
        await self.plugin.apply_mood(FakeEvent("a"), ProviderRequest(prompt="刺激"))
        await self.settle()
        next_event = FakeEvent("b")
        await self.plugin.apply_mood(next_event, ProviderRequest(prompt="你好"))
        hint = next_event.get_extra(EVENT_HINT)
        self.assertIn(self.appraisal["inner_state"], hint)
        self.assertIn(self.appraisal["response_tendency"], hint)
        await self.settle()

    async def test_fixed_emotional_frame_can_be_disabled(self):
        self.plugin.config["emotion_first"] = False
        request = ProviderRequest(prompt="你好", system_prompt="原本人格")
        await self.plugin.apply_mood(FakeEvent("a"), request)
        self.assertEqual(request.system_prompt, "原本人格")
        await self.settle()

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
        self.assertTrue(config["random_mood_on_return"])
        self.assertFalse(config["natural_recovery_enabled"])
        self.assertEqual(config["random_mood_after_seconds"], 21600)
        self.assertEqual(config["random_mood_strength"], 0.3)
        self.assertIn("mischief", config["random_mood_pool"])
        self.assertEqual(schema["analysis_provider_id"]["_special"], "select_provider")
        config["analysis_queue_size"] = 7
        config.save_config()
        reloaded = AstrBotConfig(str(config_path), schema=schema)
        self.assertEqual(reloaded["analysis_queue_size"], 7)


if __name__ == "__main__":
    unittest.main()
