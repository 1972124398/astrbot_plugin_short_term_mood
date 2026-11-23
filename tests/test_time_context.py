import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_short_term_mood.time_context import ConversationClocks, time_hint


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": text}


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.clocks = ConversationClocks()
        self.origin = "test:FriendMessage:alice"
        self.old = [user("今天真烦，刚才说得太重了"), assistant("还有点委屈"), user("先去吃饭了"), assistant("嗯，去吧")]

    def prepare(self, now, history=None, prompt="早呀", origin=None):
        return self.clocks.prepare(origin or self.origin, self.old if history is None else history, prompt, now, 1800, 21600)

    def test_unknown_history_does_not_invent_a_time_gap(self):
        data = self.prepare(1000)
        self.assertEqual(data["phase"], "unknown")
        self.assertIsNone(data["gap_seconds"])

    def test_short_pause_fades_then_long_pause_sets_old_context_aside(self):
        self.prepare(1000, [])
        self.assertEqual(self.prepare(3000)["phase"], "fading")
        data = self.prepare(30000)
        self.assertEqual(data["phase"], "set_aside")
        self.assertEqual(data["recent_dialogue"], [])
        self.assertIn("暂时不惦记旧细节", time_hint(data))

    def test_new_conversation_stays_fresh_on_following_turns_and_history_is_not_mutated(self):
        self.prepare(1000, [])
        original = copy.deepcopy(self.old)
        self.prepare(30000, prompt="早呀")
        self.clocks.finish(self.origin, "来啦", 30001)
        history = self.old + [user("早呀"), assistant("来啦")]
        data = self.prepare(30002, history, "想聊个新话题")
        self.assertEqual(data["recent_dialogue"], history[-2:])
        self.assertEqual(self.old, original)
        self.assertTrue(data["fresh_segment_since"])

    def test_repeated_greetings_do_not_resurrect_an_old_argument(self):
        old = [user("早呀"), assistant("你好"), user("你真烦"), assistant("不想说了")]
        self.prepare(1000, [])
        self.prepare(30000, old, "早呀")
        self.clocks.finish(self.origin, "你好", 30001)
        data = self.prepare(30002, old + [user("早呀"), assistant("你好")], "今天开心吗")
        self.assertEqual(data["recent_dialogue"], [user("早呀"), assistant("你好")])

    def test_activity_elsewhere_does_not_reset_this_conversations_gap(self):
        self.prepare(1000, [])
        self.prepare(29999, [], origin="test:FriendMessage:bob")
        self.assertEqual(self.prepare(30000)["phase"], "set_aside")

    def test_clock_and_fresh_boundary_survive_reload(self):
        self.prepare(1000, [])
        self.prepare(30000)
        restored = ConversationClocks(copy.deepcopy(self.clocks.sessions))
        data = restored.prepare(self.origin, self.old + [user("早呀")], "继续说吧", 30010, 1800, 21600)
        self.assertEqual(data["recent_dialogue"], [user("早呀")])

    def test_existing_history_timestamp_supplies_first_install_gap(self):
        data = self.clocks.prepare(self.origin, self.old, "早", 30000, 1800, 21600, previous_at=1000)
        self.assertEqual(data["phase"], "set_aside")

    def test_clock_adjustment_does_not_produce_negative_elapsed_time(self):
        self.prepare(1000, [])
        self.assertEqual(self.prepare(999)["gap_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
