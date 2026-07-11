#!/usr/bin/env python3
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audiobook_workflow  # noqa: E402
import llm_chat  # noqa: E402


class PackagePerformanceQaTests(unittest.TestCase):
    def test_audio_message_uses_input_audio_block(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
            handle.write(b"RIFFtest")
            handle.flush()
            messages = llm_chat._build_messages("review", None, [], audios=[handle.name])
        audio_block = messages[-1]["content"][0]
        self.assertEqual(audio_block["type"], "input_audio")
        self.assertEqual(audio_block["input_audio"]["format"], "wav")
        self.assertTrue(audio_block["input_audio"]["data"])

    def test_chat_text_forwards_explicit_socket_timeout(self):
        with patch.object(llm_chat, "_resolve_base_url", return_value="https://example.test"):
            with patch.object(llm_chat, "_resolve_api_key", return_value="secret"):
                with patch.object(llm_chat, "_resolve_model", return_value="model"):
                    with patch.object(llm_chat, "_chat_completion", return_value={"choices": []}) as completion:
                        llm_chat.chat_text("hello", timeout=17)
        self.assertEqual(completion.call_args.kwargs["timeout"], 17)

    def test_performance_failure_yields_chunk_repair_note(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "06_generation_requests").mkdir()
            (run_dir / "logs").mkdir()
            (run_dir / "06_generation_requests" / "chunk_001.json").write_text(
                json.dumps({"active_roles": ["Narrator"], "pace_note": "measured natural pace"}),
                encoding="utf-8",
            )
            part = run_dir / "chunk_001.wav"
            part.write_bytes(b"RIFFtest")
            reviewed = json.dumps(
                {
                    "verdict": "fail",
                    "issues": [{"type": "clipped_word_or_sentence_ending", "severity": "major", "evidence": "last word cuts"}],
                    "repair_instruction": "Finish sentence endings before the ambience tail.",
                    "summary": "Tail is clipped.",
                }
            )
            with patch.object(audiobook_workflow.llm_chat, "chat_audio", return_value=reviewed):
                report = audiobook_workflow.performance_audio_audit(run_dir, [part])
            self.assertEqual(report["failed_chunk_ids"], ["chunk_001"])
            self.assertEqual(
                audiobook_workflow.performance_repair_notes(report)["chunk_001"],
                "Finish sentence endings before the ambience tail.",
            )

    def test_reviewer_unavailable_keeps_preview_but_blocks_delivery(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "06_generation_requests").mkdir()
            (run_dir / "logs").mkdir()
            (run_dir / "06_generation_requests" / "chunk_001.json").write_text("{}", encoding="utf-8")
            part = run_dir / "chunk_001.wav"
            part.write_bytes(b"RIFFtest")
            with patch.object(audiobook_workflow.llm_chat, "chat_audio", side_effect=RuntimeError("service unavailable")):
                report = audiobook_workflow.performance_audio_audit(run_dir, [part])
            self.assertEqual(report["preview_status"], "available")
            self.assertEqual(report["delivery_status"], "fail")
            self.assertEqual(report["unavailable_chunk_ids"], ["chunk_001"])

    def test_malformed_reviewer_reply_is_unavailable_not_a_pass(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "06_generation_requests").mkdir()
            (run_dir / "logs").mkdir()
            (run_dir / "06_generation_requests" / "chunk_001.json").write_text("{}", encoding="utf-8")
            part = run_dir / "chunk_001.wav"
            part.write_bytes(b"RIFFtest")
            with patch.object(audiobook_workflow.llm_chat, "chat_audio", return_value="I cannot judge this audio."):
                report = audiobook_workflow.performance_audio_audit(run_dir, [part])
            self.assertEqual(report["status"], "unavailable")
            self.assertEqual(report["delivery_status"], "fail")
            self.assertEqual(report["unavailable_chunk_ids"], ["chunk_001"])

    def test_asr_dialogue_coverage_accepts_case_and_punctuation_variation(self):
        coverage = audiobook_workflow.transcript_dialogue_coverage(
            "Harry said the tower is falling now.",
            {"must_keep_dialogue": [{"text": "Harry, the tower is falling now!"}]},
        )
        self.assertEqual(coverage["must_keep_dialogue_covered_count"], 1)

    def test_action_narration_rate_is_capped(self):
        self.assertEqual(audiobook_workflow.safe_english_speech_rate(20, narrator_units=2), 8)

    def test_tail_guard_completes_dangling_dialogue(self):
        prompt = audiobook_workflow.ensure_complete_audio_tail('Villain: "You cannot escape-" End segment. No fade out.')
        self.assertIn('"You cannot escape."', prompt)
        self.assertNotIn("End segment", prompt)
        self.assertNotIn("No fade out", prompt)
        self.assertIn("without cutting the final word or phoneme", prompt)

    def test_semantic_duplicate_narration_bridge_is_not_appended(self):
        prompt = 'Narrator (actor is <<TGT_SPK1>>): "Ginny dodged hexes from Amycus. He laughed as he cast."'
        chunk = {"narration_bridge": ["Ginny was dodging hexes from Amycus, who laughed as he cast."]}
        result = audiobook_workflow.ensure_narration_bridges_in_prompt(
            prompt, chunk, {"Narrator": "<<TGT_SPK1>>"}
        )
        self.assertNotIn("Spoken English narration bridges", result)

    def test_locked_role_prompt_is_compact_and_keeps_audio_rules(self):
        config = {
            "roles": {
                "Narrator": {
                    "default_speaker": "en_male_knightley_uranus_bigtts",
                    "description": "clear dramatic narrator",
                },
                "Hero": {
                    "default_speaker": "en_male_josh_uranus_bigtts",
                    "description": "young urgent hero",
                },
            }
        }
        units = [
            {"source_unit_id": "s0001", "source_order": 1, "source_kind": "narrative_text", "source_text": "The door opened."},
            {"source_unit_id": "s0002", "source_order": 2, "source_kind": "quoted_text", "source_text": "Run!"},
        ]
        with patch.object(audiobook_workflow, "STORY_CONFIG", config):
            prompt = audiobook_workflow.compact_locked_rewrite_prompt(units)
        self.assertLess(len(prompt), 7000)
        self.assertIn("stage-one analysis", prompt)
        self.assertIn("builds each Audio 1.0 prompt separately", prompt)
        self.assertNotIn('"final_audio_prompt":""', prompt)
        self.assertNotIn("Voice continuity:", prompt)
        self.assertIn("continuous scene-specific score", prompt)
        self.assertIn("Never place two active roles with the same provider speaker", prompt)

    def test_dynamic_role_prompt_uses_compact_stage_one_contract(self):
        config = {"auto_planning": True}
        units = [
            {"source_unit_id": "s0001", "source_kind": "narrative_text", "source_text": "The door opened."},
            {"source_unit_id": "s0002", "source_kind": "quoted_text", "source_text": "Run!"},
        ]
        with patch.object(audiobook_workflow, "STORY_CONFIG", config):
            prompt = audiobook_workflow.compact_rewrite_prompt(units)
        self.assertLess(len(prompt), 6000)
        self.assertIn("stage-one analysis", prompt)
        self.assertIn("builds each Audio 1.0 prompt separately", prompt)
        self.assertNotIn('"final_audio_prompt":""', prompt)

    def test_rewrite_wall_timeout_is_total_deadline(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            audiobook_workflow.call_with_wall_timeout(1, lambda: time.sleep(3))
        self.assertLess(time.monotonic() - started, 2.5)

    def test_read_risk_hard_ending_language_is_normalized(self):
        cleaned = audiobook_workflow.clean_english_director_text(
            "Hold tension, no hard ending, then use a hard fade."
        )
        self.assertNotIn("hard ending", cleaned.lower())
        self.assertNotIn("hard fade", cleaned.lower())
        self.assertIn("natural", cleaned.lower())

    def test_cached_seed_rewrite_response_is_revalidated_without_model_call(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "logs").mkdir()
            (run_dir / "logs/seed2_rewrite_response.txt").write_text('{"cached":true}', encoding="utf-8")
            with patch.object(audiobook_workflow, "compact_rewrite_prompt", return_value="compact"):
                with patch.object(audiobook_workflow, "normalize_rewrite", return_value={"normalized": True}):
                    with patch.object(audiobook_workflow, "validate_rewrite"):
                        with patch.object(audiobook_workflow.llm_chat, "chat_text") as chat:
                            result = audiobook_workflow.call_seed2_rewrite(run_dir, [])
            self.assertEqual(result, {"normalized": True})
            chat.assert_not_called()
            checkpoint = json.loads((run_dir / "logs/seed2_rewrite_checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["status"], "reused")

    def test_seed_rewrite_attempt_limit_is_configurable(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "logs").mkdir()
            with patch.dict("os.environ", {"SEED_REWRITE_ATTEMPTS": "2", "SEED_REWRITE_TIMEOUT": "10"}):
                with patch.object(audiobook_workflow, "compact_rewrite_prompt", return_value="compact"):
                    failed = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "IncompleteRead"})()
                    with patch.object(audiobook_workflow.subprocess, "run", return_value=failed) as run:
                        with patch.object(audiobook_workflow.time, "sleep"):
                            with self.assertRaisesRegex(RuntimeError, "IncompleteRead"):
                                audiobook_workflow.call_seed2_rewrite(run_dir, [])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_dangling_narration_bridge_is_completed_from_source(self):
        bridge = audiobook_workflow.complete_spoken_bridge(
            "Terrified students huddled against the",
            "Terrified students huddled against the walls, covering their faces.",
        )
        self.assertEqual(bridge, "Terrified students huddled against the walls, covering their faces.")

    def test_official_fallback_caps_narrator_bridges_at_three(self):
        parsed = {
            f"s000{index}": {
                "speaker": "Narrator",
                "source_text": f"Complete source sentence {index}.",
                "adapted_text": f"Complete source sentence {index}.",
            }
            for index in range(1, 5)
        }
        chunk = {
            "source_unit_ids": list(parsed),
            "persistent_ambience": "cold corridor wind",
            "music_bed": "continuous low strings",
            "sound_design": "footsteps and distant impacts",
            "pace_note": "natural measured pace",
            "source_beats": [f"Complete source sentence {index}." for index in range(1, 5)],
            "narration_bridge": ["First bridge against the", "Second bridge", "Third bridge", "Fourth bridge"],
            "sfx_story_events": ["footsteps", "distant impact"],
        }
        prompt = audiobook_workflow.build_official_english_prompt_from_plan(
            chunk,
            parsed,
            ["Narrator"],
            {"Narrator": "<<TGT_SPK1>>"},
            1,
            2,
        )
        narrator_lines = prompt.count("Narrator (")
        self.assertGreaterEqual(narrator_lines, 1)
        self.assertLessEqual(narrator_lines, 3)
        self.assertNotIn("against the\"", prompt)

    def test_repair_prompt_fits_limit_without_removing_spoken_lines(self):
        spoken = 'Narrator (actor is <<TGT_SPK1>>, tense): "A complete sentence."'
        base = "\n".join(
            [
                "All audible speech must be English only. Do not translate or speak Chinese.",
                "Voice continuity: Narrator uses <<TGT_SPK1>>. one voice at a time; no overlapping narration and dialogue.",
                "Ambient sound: continuous wind and stone ambience.",
                "Background music: continuous low strings remain audible.",
                "Sound design: " + "foreground impacts " * 30,
                spoken,
                *("The sound of a distant impact stays below the voice." for _ in range(25)),
                "After the final spoken line finishes completely, keep the ambience briefly.",
            ]
        )
        repaired = audiobook_workflow.build_budgeted_repair_prompt(
            base,
            "Complete the final words and avoid an abrupt cut.",
        )
        self.assertLessEqual(len(repaired), audiobook_workflow.MAX_PROMPT_CHARS)
        self.assertIn(spoken, repaired)
        self.assertIn("Performance correction:", repaired)

    def test_base_prompt_reserves_repair_budget(self):
        spoken = 'Narrator (actor is <<TGT_SPK1>>, tense): "A complete sentence."'
        base = "\n".join(
            [
                "All audible speech must be English only. Do not translate or speak Chinese.",
                "Voice continuity: Narrator uses <<TGT_SPK1>>. one voice at a time; no overlapping narration and dialogue.",
                "Ambient sound: wind. Background music: strings.",
                spoken,
                *("The sound of a distant impact stays below the voice." for _ in range(80)),
            ]
        )
        fitted = audiobook_workflow.fit_base_prompt_budget(base)
        self.assertLessEqual(len(fitted), audiobook_workflow.BASE_PROMPT_BUDGET)
        self.assertIn(spoken, fitted)

    def test_complete_narrator_quotes_adds_sentence_closure(self):
        prompt = 'Narrator (actor is <<TGT_SPK1>>, tense): "Harry slips on wet stone"'
        completed = audiobook_workflow.complete_narrator_quotes(prompt)
        self.assertIn('"Harry slips on wet stone."', completed)

    def test_adjacent_context_does_not_overwrite_specific_planner_speaker(self):
        parsed = [
            {"source_unit_id": "s1", "source_kind": "narrative_text", "source_text": "Harry fell backward."},
            {
                "source_unit_id": "s2",
                "source_kind": "quoted_text",
                "source_text": "Petrificus Totalus!",
                "speaker": "Ginny Weasley",
                "speaker_evidence": "rescue spell caster",
            },
            {"source_unit_id": "s3", "source_kind": "narrative_text", "source_text": "Harry pushed the attacker away."},
        ]
        repaired = audiobook_workflow.repair_quoted_speakers(parsed)
        self.assertEqual(repaired[1]["speaker"], "Ginny Weasley")

    def test_dialogue_attribution_is_not_a_narration_bridge(self):
        self.assertTrue(audiobook_workflow.is_dialogue_attribution_text("yelled Harry."))
        self.assertTrue(audiobook_workflow.is_dialogue_attribution_text("Harry yelled."))
        self.assertFalse(audiobook_workflow.is_dialogue_attribution_text("Harry ran into the corridor."))

    def test_request_capacity_rejects_distinct_roles_sharing_one_speaker(self):
        roles = list(audiobook_workflow.ROLE_SPECS)[:2]
        parsed = {
            "s1": {"speaker": roles[0]},
            "s2": {"speaker": roles[1]},
        }
        with patch.object(audiobook_workflow, "configured_speaker", return_value="shared-speaker"):
            with patch.object(audiobook_workflow, "reference_mode", return_value="speaker"):
                self.assertFalse(audiobook_workflow.request_voice_capacity_ok(["s1", "s2"], parsed))

    def test_active_roles_drop_planned_role_that_does_not_speak(self):
        roles = list(audiobook_workflow.ROLE_SPECS)
        self.assertGreaterEqual(len(roles), 3)
        parsed = {
            "s1": {"speaker": roles[0]},
            "s2": {"speaker": roles[1]},
        }
        chunk = {
            "source_unit_ids": ["s1", "s2"],
            "active_roles": [roles[0], roles[1], roles[2]],
        }
        self.assertEqual(audiobook_workflow.active_roles_for_chunk(chunk, parsed), roles[:2])

    def test_dialogue_opening_chunk_inherits_preceding_narrative_unit(self):
        role = next(name for name in audiobook_workflow.ROLE_SPECS if name != "Narrator")
        parsed = {
            "s1": {"source_unit_id": "s1", "speaker": "Narrator", "source_text": "The corridor narrowed."},
            "s2": {"source_unit_id": "s2", "speaker": "Narrator", "source_text": "A figure raised a wand."},
            "s3": {"source_unit_id": "s3", "speaker": role, "source_text": "Stop!", "source_kind": "quoted_text"},
        }
        plans = [
            {"chunk_id": "chunk_001", "source_unit_ids": ["s1", "s2"]},
            {"chunk_id": "chunk_002", "source_unit_ids": ["s3"]},
        ]
        shifted = audiobook_workflow.shift_narrative_setup_to_dialogue_openers(plans, parsed)
        self.assertEqual(shifted[0]["source_unit_ids"], ["s1"])
        self.assertEqual(shifted[1]["source_unit_ids"], ["s2", "s3"])


if __name__ == "__main__":
    unittest.main()
