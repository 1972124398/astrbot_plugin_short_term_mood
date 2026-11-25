import copy
import json
import unittest

import test_hooks as fixtures
from test_hooks import SDK_AVAILABLE, FakeEvent

if SDK_AVAILABLE:
    from astrbot.api.provider import ProviderRequest

    from astrbot_plugin_short_term_mood.main import EVENT_HINT
    from astrbot_plugin_short_term_mood.mood import MoodState


@unittest.skipUnless(SDK_AVAILABLE, "Requires AstrBot SDK")
class IsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.HookTests.asyncSetUp
    asyncTearDown = fixtures.HookTests.asyncTearDown
    settle = fixtures.HookTests.settle

    async def test_notes_and_profiles_do_not_cross_users_or_personas(self):
        self.appraisal.update(
            next_mood={"curiosity": 0.7},
            inner_state="A_PRIVATE_7391",
            response_tendency="ASK_A_TOKEN_7391",
        )
        self.appraisal["profiles"]["curiosity"] = "A_PROFILE_7391"
        await self.plugin.apply_mood(
            FakeEvent("a"), ProviderRequest(prompt="A_PRIVATE_7391")
        )
        await self.settle()
        saved = copy.deepcopy(self.plugin.state.as_dict())
        self.plugin.state = MoodState.from_dict(saved)  # Simulate state reload.
        a = FakeEvent("a")
        await self.plugin.apply_mood(a, ProviderRequest(prompt="继续"))
        self.assertIn("A_PRIVATE_7391", a.get_extra(EVENT_HINT))
        await self.settle()
        for event in (FakeEvent("b"), FakeEvent("a")):
            if event.unified_msg_origin == "a":
                self.persona = {"prompt": "不同的人格"}
            await self.plugin.apply_mood(event, ProviderRequest(prompt="你好"))
            hint = event.get_extra(EVENT_HINT)
            self.assertNotIn("7391", hint)
            self.assertIn("好奇 0.70", hint)
            await self.settle()
            payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
            self.assertEqual(payload["previous_inner_state"], "")
            self.assertEqual(payload["previous_response_tendency"], "")
            self.assertEqual(payload["feeling_history"], [])

    async def test_companion_exports_only_abstract_mood(self):
        self.plugin.state = MoodState(
            levels={"curiosity": 0.7},
            inner_state="A_PRIVATE_7391",
            response_tendency="ASK_A_TOKEN_7391",
        )
        for origin in ("a", "b", ""):
            data = self.plugin.companion_snapshot(origin)
            self.assertEqual(data["levels"], {"curiosity": 0.7})
            self.assertNotIn("7391", json.dumps(data))

    async def test_unowned_legacy_notes_are_not_assigned_to_first_user(self):
        self.plugin.state = MoodState.from_dict(
            {
                "levels": {"joy": 0.3},
                "inner_state": "OLD_PRIVATE_7391",
                "response_tendency": "OLD_7391",
                "feeling_history": [{"feeling": "OLD_7391", "tendency": "OLD_7391"}],
            }
        )
        event = FakeEvent("first")
        await self.plugin.apply_mood(event, ProviderRequest(prompt="你好"))
        self.assertNotIn("7391", event.get_extra(EVENT_HINT))
        await self.settle()
        payload = json.loads(self.context.llm_generate.call_args.kwargs["prompt"])
        self.assertNotIn("7391", json.dumps(payload))

    async def test_public_status_does_not_disclose_last_user_notes(self):
        self.plugin.state = MoodState(
            levels={"joy": 0.4},
            inner_state="A_PRIVATE_7391",
            response_tendency="ASK_A_TOKEN_7391",
        )
        replies = [reply async for reply in self.plugin.mood_status(FakeEvent("b"))]
        self.assertNotIn("7391", "".join(replies))
        self.assertIn("40%", "".join(replies))
