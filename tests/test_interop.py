"""The companion transport works with no AstrBot import or companion dependency."""
import importlib.util
import itertools
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("companion_protocol", Path(__file__).resolve().parents[1] / "interop.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CompanionLink, NAMES = module.CompanionLink, module.NAMES


class Event:
    unified_msg_origin = "test:FriendMessage:friend"

    def __init__(self):
        self.extras = {}

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value


class InteropTests(unittest.TestCase):
    def test_standard_task_metadata_is_not_human_activity(self):
        event = Event()
        event.set_extra("w1_event", {"source": "codex_bridge", "type": "task_event", "is_user_message": False})
        self.assertEqual(module.event_kind(event), "task_event")

    def test_ignored_notice_has_an_explicit_disposition_for_other_plugins(self):
        event = Event()
        self.assertFalse(module.notice_ignored(event))
        event.set_extra("w1_notice_disposition", {"action": "ignore", "source": "short_term_mood"})
        self.assertTrue(module.notice_ignored(event))

    def test_task_transcripts_are_excluded_from_relationship_history(self):
        transcript = '<codex_task_event>\n{"source": "codex_bridge", "is_user_message": false}\n</codex_task_event>'
        history = [{"role": "user", "content": "帮我检查代码"},
                   {"role": "assistant", "content": "好，我看看"},
                   {"role": "user", "content": "[时间与角色前缀] " + transcript},
                   {"role": "assistant", "content": "任务结果在这里"},
                   {"role": "user", "content": "辛苦啦"}]
        self.assertEqual(module.human_dialogue(history), history[:2] + history[-1:])

    def setUp(self):
        self.registry = {}
        self.context = SimpleNamespace(get_registered_star=self.registry.get, get_config=lambda **_: {"plugin_set": ["*"]})

    def register(self, role, value=None, config=None):
        peer = SimpleNamespace(config={} if config is None else config,
                               companion_snapshot=lambda origin: value or {"here": role},
                               companion_observe=Mock())
        self.registry[NAMES[role]] = SimpleNamespace(activated=True, star_cls=peer, plugin_id="W1/" + NAMES[role])
        return peer

    def test_every_subset_and_load_order_works_without_required_peers(self):
        roles = list(NAMES)
        for count in range(1, 5):
            for subset in itertools.permutations(roles, count):
                self.registry.clear()
                links = [CompanionLink(self.context, role) for role in subset]
                for role in subset:
                    self.register(role)
                for link in links:
                    self.assertEqual(set(link.context_for("origin")), set(subset) if count > 1 else set())

    def test_unloading_or_disabling_a_peer_removes_its_capability(self):
        link = CompanionLink(self.context, "play_together")
        peer = self.register("self_amusement")
        self.assertTrue(link.snapshot("self_amusement"))
        peer.config["interop_enabled"] = False
        self.assertFalse(link.snapshot("self_amusement"))
        peer.config.clear()
        self.registry[NAMES["self_amusement"]].activated = False
        self.assertFalse(link.snapshot("self_amusement"))

    def test_session_plugin_selection_is_respected(self):
        self.register("short_term_mood")
        link = CompanionLink(self.context, "play_together")
        self.context.get_config = lambda **_: {"plugin_set": [NAMES["play_together"]]}
        self.assertFalse(link.snapshot("short_term_mood", "restricted"))
        self.context.get_config = lambda **_: {"plugin_set": ["W1/" + NAMES["short_term_mood"]]}
        self.assertTrue(link.snapshot("short_term_mood", "allowed"))

    def test_two_idle_schedulers_cannot_generate_at_the_same_time(self):
        first = CompanionLink(self.context, "self_amusement")
        second = CompanionLink(self.context, "play_together")
        token = first.claim_idle()
        self.assertIsNotNone(token)
        self.assertIsNone(second.claim_idle())
        first.release_idle(token)
        self.assertIsNotNone(second.claim_idle())

    def test_human_activity_invalidates_an_already_generated_idle_message(self):
        first = CompanionLink(self.context, "self_amusement")
        second = CompanionLink(self.context, "play_together")
        token = first.claim_idle()
        second.human("test:FriendMessage:friend")
        self.assertFalse(first.idle_current(token))

    def test_all_observers_must_finish_before_attention_is_free(self):
        first = CompanionLink(self.context, "self_amusement")
        second = CompanionLink(self.context, "play_together")
        event = Event()
        first.begin(event)
        second.begin(event)
        first.end(event)
        self.assertIsNone(first.claim_idle())
        second.end(event)
        self.assertIsNotNone(first.claim_idle())

    def test_hot_reload_releases_only_the_old_instances_attention(self):
        old = CompanionLink(self.context, "play_together")
        new = CompanionLink(self.context, "play_together")
        new.begin(Event())
        old.close()
        self.assertIsNone(old.claim_idle())
        new.close()
        self.assertIsNotNone(old.claim_idle())

    def test_tool_call_keeps_the_conversation_busy_until_a_final_reply(self):
        link = CompanionLink(self.context, "self_amusement")
        event = Event()
        link.begin(event)
        link.end(event, SimpleNamespace(tools_call_args=[{"query": "something"}]))
        self.assertIsNone(link.claim_idle())
        link.end(event, SimpleNamespace(tools_call_args=[]))
        self.assertIsNotNone(link.claim_idle())

    def test_disabled_link_does_not_reserve_other_plugins_attention(self):
        standalone = CompanionLink(self.context, "self_amusement", {"interop_enabled": False})
        linked = CompanionLink(self.context, "play_together")
        token = standalone.claim_idle()
        self.assertIsNotNone(token)
        self.assertIsNotNone(linked.claim_idle())
        self.assertFalse(standalone.context_for("origin"))

    def test_shared_gap_applies_across_targets_but_not_manual_turns(self):
        first = CompanionLink(self.context, "self_amusement")
        second = CompanionLink(self.context, "play_together")
        first.sent("one", "来聊天")
        self.assertIsNone(second.claim_idle())
        event = Event()
        second.begin(event)
        self.assertTrue(second.state["turns"])
        second.end(event)
        second.config["interop_gap_minutes"] = 0
        self.assertIsNotNone(second.claim_idle())

    def test_duplicate_delivery_and_failed_peer_do_not_break_others(self):
        link = CompanionLink(self.context, "play_together")
        broken = self.register("short_term_mood")
        good = self.register("self_amusement")
        broken.companion_observe.side_effect = RuntimeError("unloading")
        for _ in range(2):
            link.publish("game_turn", "origin", {"reply": "好玩"}, "same-result")
        good.companion_observe.assert_called_once()

    def test_snapshot_is_a_copy_and_keeps_its_requested_conversation(self):
        seen = []
        value = {"impression": {"note": "一起玩得开心"}}
        peer = self.register("self_amusement")
        peer.companion_snapshot = lambda origin: seen.append(origin) or value
        link = CompanionLink(self.context, "play_together")
        result = link.snapshot("self_amusement", "only-this-conversation")
        result["impression"]["note"] = "changed"
        self.assertEqual(value["impression"]["note"], "一起玩得开心")
        self.assertEqual(seen, ["only-this-conversation"])

    def test_native_group_member_sessions_share_one_conversation(self):
        a = "wx:GroupMessage:account|group|room%40chatroom|alice"
        b = "wx:GroupMessage:account|group|room%40chatroom|bob"
        self.assertEqual(module.conversation_key(a), module.conversation_key(b))
        self.assertNotEqual(module.conversation_key(a), module.conversation_key("wx:FriendMessage:alice"))

    def test_background_task_event_is_not_a_user_reply(self):
        event = Event()
        event.set_extra("codex_bridge", {"kind": "task_result"})
        self.assertEqual(module.event_kind(event), "task_event")


if __name__ == "__main__":
    unittest.main()
