import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_short_term_mood.mood import DEFAULT_AMBIENT_POOL, EMOTIONS, MoodState, MoodStore, emotion_name, mood_candidates
from astrbot_plugin_short_term_mood.prompts import APPRAISAL_SYSTEM, EXPRESSION_GUIDES, build_hint, parse_appraisal, profile_targets


class MoodTests(unittest.TestCase):
    def test_all_new_emotions_can_be_appraised_and_persisted(self):
        names = {"playfulness", "mischief", "confusion", "boredom", "surprise", "irritability",
                 "anticipation", "longing", "relief", "contentment", "hesitation", "jealousy",
                 "frustration", "guilt", "gratitude", "admiration", "determination", "tenderness",
                 "amusement", "sulking", "absorption", "nostalgia", "skepticism", "exasperation",
                 "competitiveness", "awkwardness"}
        profiles = {name: "按角色自然表达" for name in {"neutral", *EMOTIONS}}
        payload = {"deltas": dict.fromkeys(names, 0.2), "confidence": 0.9, "profiles": profiles}
        deltas, generated, _, _ = parse_appraisal(json.dumps(payload), True)
        state = MoodState(updated=100)
        state.apply(deltas, 100, 1)
        with tempfile.TemporaryDirectory() as directory:
            store = MoodStore(Path(directory) / "mood.sqlite3")
            store.save(state)
            self.assertEqual(set(store.load().levels), names)
            store.close()
        self.assertEqual(generated, profiles)

    def test_every_emotion_has_immediate_guidance_and_an_appraisal_key(self):
        self.assertEqual(set(EXPRESSION_GUIDES), set(EMOTIONS))
        self.assertEqual(len(EMOTIONS), 36)
        self.assertIn("/".join(EMOTIONS), APPRAISAL_SYSTEM)
        self.assertNotIn("$EMOTION_", APPRAISAL_SYSTEM)
        for name in EMOTIONS:
            with self.subTest(emotion=name):
                self.assertIn(EXPRESSION_GUIDES[name], build_hint({name: 0.4}, None, True))

    def test_common_emotion_aliases_work_in_the_random_pool(self):
        for text, expected in {" 放松 ": "contentment", "思念": "longing", "犯迷糊": "confusion",
                               "不服气": "determination", "纠结": "hesitation", "LONGING": "longing"}.items():
            with self.subTest(alias=text):
                self.assertEqual(emotion_name(text), expected)
                self.assertEqual(mood_candidates([text]), [expected])
        for invalid in (None, {}, 1, "unknown"):
            self.assertIsNone(emotion_name(invalid))

    def test_profiles_are_batched_with_current_mood_first_and_eventually_complete(self):
        levels = {"tenderness": 0.7, "jealousy": 0.2}
        profiles = {}
        targets = profile_targets(levels, profiles)
        self.assertEqual(targets[:3], ["neutral", "tenderness", "jealousy"])
        seen = []
        while targets:
            self.assertLessEqual(len(targets), 8)
            seen.extend(targets)
            profiles.update(dict.fromkeys(targets, "已生成人格表达"))
            targets = profile_targets(levels, profiles)
        self.assertEqual(set(seen), {"neutral", *EMOTIONS})
        self.assertEqual(len(seen), len(set(seen)))

    def test_batched_profile_parser_keeps_deltas_for_emotions_outside_batch(self):
        payload = {"deltas": {"tenderness": 0.2}, "confidence": 0.9,
                   "profiles": {"neutral": "自然聊天", "joy": "角色高兴时的语气"}}
        deltas, profiles, _, _ = parse_appraisal(json.dumps(payload), True, ["neutral", "joy"])
        self.assertEqual(deltas, {"tenderness": 0.2})
        self.assertEqual(set(profiles), {"neutral", "joy"})
        with self.assertRaises(ValueError):
            parse_appraisal(json.dumps(payload), True)
        with self.assertRaises(ValueError):
            parse_appraisal(json.dumps(payload), True, ["neutral", "joy", "tenderness"])
        payload["profiles"]["unknown"] = "不允许的键"
        with self.assertRaises(ValueError):
            parse_appraisal(json.dumps(payload), True, ["neutral", "joy"])

    def test_mixed_feelings_shape_expression_without_forcing_faint_undertones(self):
        hint = build_hint({"anger": 0.7, "tenderness": 0.3}, None, True,
                          inner_state="还有气但已经心软", response_tendency="想先听听，也可以给个台阶")
        self.assertIn("还有气但已经心软", hint)
        self.assertIn("想先听听，也可以给个台阶", hint)
        self.assertNotIn("混合心境的一种自然表现", hint)

    def test_expression_strength_scales_each_guide_without_changing_shared_mood(self):
        levels = {"anger": 0.8, "tenderness": 0.01, "joy": 0.0}
        profiles = {"anger": "以克制的措辞表达不满", "tenderness": "简短接住好意"}
        quiet = build_hint(levels, profiles, True, expression_strength=0.25)
        strong = build_hint(levels, profiles, True, expression_strength=2.0)
        self.assertIn("生气 0.20", quiet)
        self.assertIn("生气（轻微", quiet)
        self.assertIn("生气 1.00", strong)
        self.assertIn("生气（强烈", strong)
        for hint in (quiet, strong):
            self.assertIn("心软（轻微", hint)
            self.assertIn(profiles["anger"], hint)
            self.assertIn(profiles["tenderness"], hint)
            self.assertNotIn("开心 0.00", hint)
        self.assertEqual(levels, {"anger": 0.8, "tenderness": 0.01, "joy": 0.0})

    def test_expression_budget_includes_intensity_labels_and_preserves_full_summary(self):
        profiles = dict.fromkeys(EMOTIONS, "人格表达" * 70)
        hint = build_hint(dict.fromkeys(EMOTIONS, 0.5), profiles, False)
        guides = [line for line in hint.splitlines() if "（明显，按此分寸取用）：" in line]
        self.assertTrue(guides)
        self.assertLessEqual(sum(map(len, guides)), 420)
        for label in EMOTIONS.values():
            self.assertIn(f"{label} 0.50", hint)

    def test_banter_preference_changes_hint_and_zero_keeps_persona_free(self):
        hints = [build_hint({}, None, False, banter_level=value) for value in (0, 0.2, 0.6, 1)]
        self.assertEqual(len(set(hints)), 4)
        self.assertFalse(any(word in hints[0] for word in ("玩心偏低", "玩心适中", "玩心偏高")))
        for hint, phrase in zip(hints[1:], ("少主动逗弄", "不必主动制造玩笑", "可主动接梗或逗趣")):
            self.assertIn(phrase, hint)
            self.assertIn("先接住具体内容", hint)

    def test_positive_mixed_undertones_do_not_prevent_soothing_real_conflict(self):
        state = MoodState({"anger": 0.6, "grievance": 0.4}, {"anger": 100, "grievance": 100}, updated=100)
        state.apply({"anger": -0.2, "grievance": -0.15, "tenderness": 0.2, "relief": 0.15}, 101, 1, inertia=0.35)
        self.assertLess(state.levels["anger"], 0.6)
        self.assertLess(state.levels["grievance"], 0.4)
        self.assertGreater(state.levels["tenderness"], 0)
        self.assertIn("心软 0.", build_hint(state.levels, None, True))

    def test_random_return_starts_at_threshold_and_only_once(self):
        state = MoodState(updated=100, last_interaction=100)
        self.assertTrue(state.begin_interaction(3700, random_after=3600))
        self.assertEqual(state.levels, {})
        self.assertFalse(state.begin_interaction(3701, random_after=3600))

    def test_random_return_respects_first_use_disabled_recent_and_clock_rollback(self):
        for previous, now, threshold in [(0, 9000, 3600), (100, 9000, 0), (100, 3699, 3600), (100, 90, 3600)]:
            with self.subTest(previous=previous, now=now, threshold=threshold):
                state = MoodState(last_interaction=previous)
                self.assertFalse(state.begin_interaction(now, random_after=threshold))
                self.assertEqual(state.levels, {})
                self.assertEqual(state.last_interaction, max(previous, now))

    def test_remaining_affect_is_not_overwritten_by_random_return(self):
        state = MoodState({"anger": 0.8}, {"anger": 100}, updated=100, last_interaction=100)
        self.assertTrue(state.begin_interaction(9000, random_after=3600))
        self.assertEqual(state.levels, {"anger": 0.8})

    def test_return_opportunity_keeps_neutral_until_model_decides(self):
        state = MoodState(last_interaction=100)
        self.assertTrue(state.begin_interaction(9000, random_after=3600))
        self.assertEqual(state.levels, {})
        self.assertFalse(state.begin_interaction(9001, random_after=3600))

    def test_random_pool_accepts_chinese_names_and_ignores_duplicates_and_invalid_items(self):
        self.assertEqual(mood_candidates(["恶作剧", "mischief", "无效", {}, 1]), ["mischief"])
        for invalid in ([], ["无效"], "joy", None):
            self.assertEqual(mood_candidates(invalid), list(DEFAULT_AMBIENT_POOL))

    def test_random_source_survives_restart_and_clears_when_soothed_or_expired(self):
        state = MoodState({"irritability": 0.25}, {"irritability": 9000}, updated=9000,
                          last_interaction=9000, ambient_emotion="irritability")
        with tempfile.TemporaryDirectory() as directory:
            store = MoodStore(Path(directory) / "mood.sqlite3")
            store.save(state)
            state = store.load()
            store.close()
        self.assertEqual((state.ambient_emotion, state.last_interaction), ("irritability", 9000))
        reloaded = MoodState.from_dict(state.as_dict())
        stimulated = MoodState.from_dict(state.as_dict())
        stimulated.apply({"irritability": 0.1}, 9001, 1)
        self.assertEqual(stimulated.ambient_emotion, "")
        state.apply({"irritability": -0.35, "affection": 0.2}, 9001, 1)
        self.assertEqual(state.ambient_emotion, "")
        reloaded.recover(12000, 300, 1200)
        self.assertEqual(reloaded.ambient_emotion, "")

    def test_new_emotions_have_immediate_distinct_guidance_without_cached_profiles(self):
        for emotion, phrase in {"playfulness": "抛梗", "mischief": "揭梗", "confusion": "自己改口",
                                "boredom": "重新吸引", "surprise": "消化新信息", "irritability": "耐心少一点"}.items():
            with self.subTest(emotion=emotion):
                hint = build_hint({emotion: 0.3}, None, True)
                self.assertIn(phrase, hint)
                self.assertIn("具体缘由只取自当前会话", hint)
        hint = build_hint({"irritability": 0.3}, None, True, ambient_emotion="irritability")
        self.assertIn("无来由的坏心情不等于用户做错了什么", hint)

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

    def test_mixed_emotion_prompt_preserves_model_selected_feelings_and_neutral(self):
        hint = build_hint({"joy": 0.2, "anger": 0.9, "sadness": 0.5, "fatigue": 0.6}, None, True)
        self.assertIn("生气", hint)
        self.assertIn("开心 0.", hint)
        self.assertIn("不想聊时", hint)
        self.assertIn("平静随性地聊", build_hint({}, None, False))

    def test_current_message_reacts_immediately_and_preferences_shape_expression(self):
        hint = build_hint({}, None, True, preferences="少写动作描写，生气时直说")
        self.assertNotIn("当前消息发生的新刺激", hint)
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
        self.assertIn("表达可以活泼些", hint)
        self.assertIn("亲近 0.40", hint)
        self.assertNotIn("本来挺高兴", hint)
        self.assertNotIn("评估模型", hint)


if __name__ == "__main__":
    unittest.main()
