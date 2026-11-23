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

    def test_strong_emotion_has_inertia_during_repair(self):
        state = MoodState({"anger": 0.8}, {"anger": 1000}, updated=1000)
        state.apply({"anger": -0.3, "affection": 0.2}, 1001, 1, inertia=0.5)
        self.assertAlmostEqual(state.levels["anger"], 0.62)
        self.assertAlmostEqual(state.levels["affection"], 0.2)

    def test_emotional_notes_survive_restart_and_clear_after_recovery(self):
        state = MoodState({"grievance": 0.7}, {"grievance": 1000}, updated=1000,
                          inner_state="还在意刚才的轻视", response_tendency="想说出自己的感受")
        reloaded = MoodState.from_dict(state.as_dict())
        self.assertEqual(reloaded.inner_state, state.inner_state)
        reloaded.recover(3000, 300, 1200)
        self.assertEqual(reloaded.inner_state, "")
        self.assertEqual(reloaded.response_tendency, "")

    def test_parser_rejects_nonfinite_out_of_range_or_incomplete_data(self):
        base = {"deltas": {"anger": 0.2}, "confidence": 0.9, "allow_refusal": True}
        for override in (
            {"deltas": {"anger": float("nan")}},
            {"deltas": {"anger": 2}},
            {"deltas": {"unknown": 0}},
            {"confidence": True},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                parse_appraisal(json.dumps({**base, **override}), False)
        with self.assertRaises(ValueError):
            parse_appraisal(json.dumps(base), True)
        result = parse_appraisal(json.dumps({**base, "confidence": 0.5}), False)
        self.assertEqual(result, ({"anger": 0}, None, "", ""))

    def test_active_dialogue_sustains_emotion_and_idle_time_recovers_it(self):
        state = MoodState({"anger": 0.8}, {"anger": 100}, updated=100, last_interaction=100)
        for now in range(200, 2001, 100):
            state.recover(now, half_life=300, recovery_seconds=600, idle_seconds=300)
            state.last_interaction = now
            self.assertEqual(state.levels["anger"], 0.8)
        state.recover(2600, half_life=300, recovery_seconds=600, idle_seconds=300)
        self.assertAlmostEqual(state.levels["anger"], 0.4)
        state.recover(2900, half_life=300, recovery_seconds=600, idle_seconds=300)
        self.assertEqual(state.levels, {})

    def test_soothing_changes_sustained_emotion_during_active_conversation(self):
        state = MoodState({"anger": 0.8}, {"anger": 100}, updated=100, last_interaction=100)
        state.recover(200, 300, 600, idle_seconds=300)
        state.apply({"anger": -0.3, "affection": 0.2}, 200, 1)
        self.assertAlmostEqual(state.levels["anger"], 0.5)
        self.assertAlmostEqual(state.levels["affection"], 0.2)

    def test_old_state_migrates_and_restart_preserves_active_recovery_age(self):
        state = MoodState.from_dict({"levels": {"anger": 0.8}, "stimulated": {"anger": 100}, "updated": 100})
        state.recover(400, 300, 600, idle_seconds=300)
        self.assertAlmostEqual(state.levels["anger"], 0.4)
        self.assertEqual(state.recovery_ages["anger"], 300)
        reloaded = MoodState.from_dict(state.as_dict())
        reloaded.recover(700, 300, 600, idle_seconds=300)
        self.assertEqual(reloaded.levels, {})

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
        self.assertNotIn("开心，表达强度", hint)
        self.assertIn("直接拒绝", hint)
        self.assertIn("心境已恢复平稳", build_hint({}, None, False))

    def test_current_message_reacts_immediately_and_preferences_shape_expression(self):
        hint = build_hint({}, None, True, preferences="少写动作描写，生气时直说")
        self.assertIn("当前消息发生的新刺激可以立即引起情绪反应", hint)
        self.assertIn("生气时直说", hint)
        self.assertIn("害羞", str(EMOTIONS))

    def test_low_confidence_cannot_replace_emotional_undertone(self):
        payload = {"deltas": {"anger": 0.3}, "confidence": 0.2,
                   "inner_state": "不确定的推断", "response_tendency": "错误的倾向"}
        deltas, profiles, inner_state, tendency = parse_appraisal(json.dumps(payload), False)
        self.assertEqual(deltas["anger"], 0)
        self.assertEqual((inner_state, tendency), ("", ""))

    def test_recent_feelings_keep_a_short_progression_and_skip_repeats(self):
        state = MoodState({"affection": 0.5}, {"affection": 100}, updated=100)
        for index in range(7):
            state.remember_feeling(f"心境 {index}", "想接着聊")
        state.remember_feeling("心境 6", "想接着聊")
        self.assertEqual(len(state.feeling_history), 5)
        reloaded = MoodState.from_dict(state.as_dict())
        self.assertEqual(reloaded.feeling_history[0]["feeling"], "心境 2")
        reloaded.recover(2000, 300, 1200)
        self.assertEqual(reloaded.feeling_history, [])

    def test_chat_style_and_playfulness_shape_everyday_expression(self):
        hint = build_hint({"affection": 0.4, "grievance": 0.3}, None, True,
                          conversation_style="lively", banter_level=0.8,
                          feeling_history=[{"feeling": "本来挺高兴"}, {"feeling": "有点别扭"}])
        self.assertIn("雀跃、接梗", hint)
        self.assertIn("想靠近又有点别扭", hint)
        self.assertIn("本来挺高兴", hint)
        self.assertIn("0.80", hint)


if __name__ == "__main__":
    unittest.main()
