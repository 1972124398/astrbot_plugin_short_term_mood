import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_short_term_mood.mood import EMOTIONS, MoodState, MoodStore
from astrbot_plugin_short_term_mood.prompts import build_hint, parse_appraisal


class MoodTests(unittest.TestCase):
    def test_recovery_includes_time_offline_and_has_a_hard_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mood.sqlite3"
            store = MoodStore(path)
            store.save(MoodState({"anger": 0.8}, {"anger": 100}, updated=100))
            store.close()
            reopened = MoodStore(path)
            state = reopened.load()
            state.recover(400, half_life=300, recovery_seconds=1200)
            self.assertAlmostEqual(state.levels["anger"], 0.4)
            state.recover(1300, half_life=300, recovery_seconds=1200)
            self.assertEqual(state.levels, {})
            reopened.close()

    def test_unrelated_stimulus_does_not_extend_old_emotion(self):
        state = MoodState({"anger": 0.8}, {"anger": 100}, updated=100)
        state.recover(350, 300, 600)
        state.apply({"anger": 0.0, "joy": 0.3}, 350, 1)
        state.recover(700, 300, 600)
        self.assertNotIn("anger", state.levels)
        self.assertIn("joy", state.levels)

    def test_reassurance_and_weak_stimuli_do_not_renew_expiry(self):
        state = MoodState({"anger": 0.8}, {"anger": 100}, updated=100)
        state.apply({"anger": -0.1}, 200, 1)
        state.apply({"anger": 0.01}, 300, 1)
        self.assertEqual(state.stimulated["anger"], 100)
        state.apply({"anger": 0.1}, 400, 1)
        self.assertEqual(state.stimulated["anger"], 400)

    def test_affect_is_bounded_and_clock_rollback_does_not_amplify_it(self):
        state = MoodState({"anger": 0.9}, {"anger": 100}, updated=100)
        state.apply({"anger": 0.35}, 100, 2)
        self.assertEqual(state.levels["anger"], 1)
        state.recover(90, 300, 1200)
        self.assertEqual(state.levels["anger"], 1)
        state.apply({"anger": -0.35}, 100, 2)
        state.apply({"anger": -0.35}, 100, 2)
        self.assertEqual(state.levels, {})

    def test_refusal_cooldown_and_consecutive_limit_are_shared(self):
        state = MoodState({"anger": 0.9}, {"anger": 1000}, updated=1000)
        options = dict(allowed=True, enabled=True, threshold=0.65, probability=1, cooldown=180, draw=0)
        self.assertTrue(state.choose_refusal(1000, **options))
        self.assertFalse(state.choose_refusal(1200, **options))
        self.assertTrue(state.choose_refusal(1201, **options))
        self.assertFalse(state.choose_refusal(1202, **options))
        self.assertFalse(state.choose_refusal(1203, **options))
        self.assertFalse(state.choose_refusal(1500, **{**options, "allowed": False}))
        self.assertFalse(state.choose_refusal(1500, **{**options, "enabled": False}))

    def test_positive_dominant_emotion_and_probability_prevent_refusal(self):
        state = MoodState({"anger": 0.8, "joy": 0.9})
        options = dict(allowed=True, enabled=True, threshold=0.65, probability=0.3, cooldown=0, draw=0)
        self.assertFalse(state.choose_refusal(1000, **options))
        state.levels = {"anger": 0.8}
        self.assertFalse(state.choose_refusal(1000, **{**options, "draw": 0.25}))

    def test_parser_rejects_nonfinite_out_of_range_or_incomplete_data(self):
        base = {"deltas": {"anger": 0.2}, "confidence": 0.9, "allow_refusal": True}
        for override in (
            {"deltas": {"anger": float("nan")}},
            {"deltas": {"anger": 2}},
            {"deltas": {"unknown": 0}},
            {"confidence": True},
            {"allow_refusal": "false"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                parse_appraisal(json.dumps({**base, **override}), False)
        with self.assertRaises(ValueError):
            parse_appraisal(json.dumps(base), True)
        result = parse_appraisal(json.dumps({**base, "confidence": 0.5}), False)
        self.assertEqual(result[:2], ({"anger": 0}, False))

    def test_profiles_are_cached_and_bounded(self):
        profiles = {name: "自然表达" for name in {"neutral", *EMOTIONS}}
        with tempfile.TemporaryDirectory() as directory:
            store = MoodStore(Path(directory) / "mood.sqlite3")
            for index in range(105):
                store.save_profile(str(index), profiles, 1000 + index)
            count = store.connection.execute("SELECT count(*) FROM profiles").fetchone()[0]
            self.assertEqual(count, 100)
            self.assertEqual(store.profile("104", 1200), profiles)
            self.assertIsNone(store.profile("104", 700000))
            store.clear_profiles()
            self.assertIsNone(store.profile("104", 1200))
            store.close()

    def test_mixed_emotion_prompt_selects_three_and_restores_neutral(self):
        hint = build_hint({"joy": 0.2, "anger": 0.9, "sadness": 0.5, "fatigue": 0.6}, None, True)
        self.assertIn("生气", hint)
        self.assertNotIn("开心", hint)
        self.assertIn("本轮允许短暂婉拒", hint)
        self.assertIn("心境已恢复平稳", build_hint({}, None, False))


if __name__ == "__main__":
    unittest.main()
