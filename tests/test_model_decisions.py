"""Model decisions stay authoritative; the host does not invent emotional arcs."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_short_term_mood.mood import MoodState, MoodStore
from astrbot_plugin_short_term_mood.prompts import build_hint, parse_appraisal_result


class ModelDecisionTests(unittest.TestCase):
    def test_model_can_keep_mix_or_replace_any_emotion_without_host_smoothing(self):
        state = MoodState({"anger": 0.9}, {"anger": 100}, updated=100)
        for decision in ({"anger": 0.9}, {"anger": 0.12, "tenderness": 0.77},
                         {"amusement": 0.9, "embarrassment": 0.01}, {}):
            result = parse_appraisal_result(json.dumps({"next_mood": decision, "confidence": 0.9}), False)
            state.adopt_mood(result.next_mood, 101)
            self.assertEqual(state.levels, decision)

    def test_model_does_not_have_to_follow_a_fixed_route(self):
        state = MoodState({"anger": 0.8}, {"anger": 100}, updated=100)
        state.adopt_mood({"skepticism": 0.7, "curiosity": 0.2}, 101)
        self.assertEqual(state.levels, {"skepticism": 0.7, "curiosity": 0.2})
        state.adopt_mood({"anger": 0.9}, 102)
        self.assertEqual(state.levels, {"anger": 0.9})

    def test_same_snapshot_does_not_renew_old_lifetimes(self):
        state = MoodState({"surprise": 0.4}, {"surprise": 100}, updated=100, recovery_ages={"surprise": 10})
        state.adopt_mood({"surprise": 0.4}, 101)
        self.assertEqual(state.stimulated, {"surprise": 100})
        self.assertEqual(state.recovery_ages, {"surprise": 10})
        state.adopt_mood({"surprise": 0.6}, 102)
        self.assertEqual(state.stimulated, {"surprise": 102})
        self.assertEqual(state.recovery_ages, {"surprise": 0})

    def test_new_snapshot_clears_stale_current_notes_and_removed_metadata(self):
        state = MoodState({"anger": 0.8}, {"anger": 100}, updated=100,
                          inner_state="很生气", response_tendency="想反驳")
        state.adopt_mood({"contentment": 0.3}, 101)
        self.assertEqual((state.inner_state, state.response_tendency), ("", ""))
        self.assertNotIn("anger", state.stimulated)
        state.remember_feeling("已经不想计较，想慢慢聊", "随口接话")
        with tempfile.TemporaryDirectory() as directory:
            store = MoodStore(Path(directory) / "mood.sqlite3")
            store.save(state)
            loaded = store.load()
            store.close()
        self.assertEqual(loaded.as_dict(), state.as_dict())

    def test_invalid_model_snapshot_is_rejected_without_partial_application(self):
        for mood in (None, [], {"unknown": 0.2}, {"joy": True}, {"joy": float("nan")},
                     {"joy": 1.1}, {"joy": -0.1}):
            with self.subTest(mood=mood), self.assertRaises(ValueError):
                parse_appraisal_result(json.dumps({"next_mood": mood, "confidence": 0.9}), False)

    def test_low_confidence_does_not_erase_current_mood(self):
        result = parse_appraisal_result(json.dumps({"next_mood": {}, "confidence": 0.2}), False)
        self.assertIsNone(result.next_mood)
        self.assertEqual(result.deltas, {})

    def test_snapshot_takes_priority_over_legacy_deltas(self):
        result = parse_appraisal_result(json.dumps({"next_mood": {"joy": 0.8}, "deltas": {"anger": 0.35}, "confidence": 0.9}), False)
        self.assertEqual(result.next_mood, {"joy": 0.8})
        self.assertEqual(result.deltas, {})

    def test_no_top_three_filter_or_forced_combination_drops_model_undertone(self):
        hint = build_hint({"anger": 0.8, "grievance": 0.7, "frustration": 0.6, "tenderness": 0.1}, None, True,
                          inner_state="有点心软，是否说出来还没想好", response_tendency="想接话，也可以先静静听")
        self.assertIn("心软 0.", hint)
        self.assertIn("有点心软，是否说出来还没想好", hint)
        self.assertNotIn("混合心境的一种自然表现", hint)

    def test_return_opportunity_does_not_sample_or_overwrite_mood(self):
        state = MoodState({"anger": 0.8}, {"anger": 100}, updated=100, last_interaction=100)
        self.assertTrue(state.begin_interaction(9000, random_after=3600))
        self.assertEqual(state.levels, {"anger": 0.8})
        self.assertFalse(state.begin_interaction(9001, random_after=3600))
