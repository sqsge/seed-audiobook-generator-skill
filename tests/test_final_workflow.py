#!/usr/bin/env python3
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audio_drama_skill  # noqa: E402
import audiobook_workflow  # noqa: E402
import chunk_worker  # noqa: E402
import workflow_input_gate  # noqa: E402


class FinalWorkflowTests(unittest.TestCase):
    def test_section_six_cached_attempt_is_revalidated_offline_with_source_kind_priority(self):
        fixture_dir = ROOT / "tests/fixtures/section6_cached_rewrite"
        response_path = fixture_dir / "seed2_rewrite_response_attempt_1.txt"
        self.assertTrue(response_path.exists(), "authoritative section 6 cached response fixture is missing")
        self.assertEqual(
            hashlib.sha256(response_path.read_bytes()).hexdigest(),
            "73c38d18a73b6a656b16b51016191a7878c279fe689eb44d7911119dadde39ab",
        )
        config = json.loads((fixture_dir / "story_config.json").read_text(encoding="utf-8"))
        source_units = json.loads((fixture_dir / "02_source_units.json").read_text(encoding="utf-8"))
        cached = json.loads(response_path.read_text(encoding="utf-8"))
        globals_to_restore = (
            "STORY_CONFIG",
            "SCENE_ID",
            "PRODUCTION_MODE",
            "SOURCE_TITLE",
            "SOURCE_URL",
            "SOURCE_EXCERPT",
            "ROLE_SPECS",
            "ROLE_KEY_TO_ROLE",
            "ROLE_SPOKEN_NAMES",
            "NARRATOR_LABEL",
            "SOURCE_UNITS_OVERRIDE",
        )
        saved = {name: copy.deepcopy(getattr(audiobook_workflow, name)) for name in globals_to_restore}
        try:
            audiobook_workflow.apply_story_config(config)
            with patch.object(
                audiobook_workflow.subprocess,
                "run",
                side_effect=AssertionError("offline fixture attempted a provider subprocess"),
            ) as provider:
                with patch.object(
                    audiobook_workflow.llm_chat,
                    "chat_text",
                    side_effect=AssertionError("offline fixture attempted an LLM call"),
                ) as llm:
                    normalized = audiobook_workflow.normalize_rewrite(cached, source_units)
                    audiobook_workflow.validate_rewrite(normalized, source_units)
            self.assertEqual(provider.call_count, 0)
            self.assertEqual(llm.call_count, 0)
        finally:
            for name, value in saved.items():
                setattr(audiobook_workflow, name, value)

        chunks = normalized["director_prompt_chunks"]
        self.assertGreater(len(chunks), 0)
        expected_ids = [unit["source_unit_id"] for unit in source_units]
        self.assertEqual([unit_id for chunk in chunks for unit_id in chunk["source_unit_ids"]], expected_ids)
        source_by_id = {unit["source_unit_id"]: unit for unit in source_units}
        for parsed in normalized["parsed_source_units"]:
            source = source_by_id[parsed["source_unit_id"]]
            for key in ("source_kind", "source_span", "paragraph_id"):
                if key in source:
                    self.assertEqual(parsed.get(key), source[key])
        for chunk in chunks:
            self.assertLessEqual(
                chunk["input_metrics"]["audio_drama_estimated_duration_ceiling_sec"],
                audiobook_workflow.SAFE_CONTENT_CEILING_SEC,
            )
            self.assertLessEqual(len(chunk["active_roles"]), 3)
            self.assertIn("Time budget:", chunk["text_prompt"])
            self.assertTrue(chunk["text_prompt"].splitlines()[-1].startswith("Final audible coda:"))
        attribution_like_narrative = next(
            item for item in normalized["parsed_source_units"] if item["source_unit_id"] == "s0036"
        )
        self.assertEqual(attribution_like_narrative["source_kind"], "narrative_text")
        self.assertEqual(attribution_like_narrative["speaker"], "Narrator")

    def test_contiguous_short_dialogue_stays_in_same_chunk(self):
        units = {
            "s1": {"source_unit_id": "s1", "type": "dialogue", "speaker": "Hagrid", "paragraph_id": "p1", "source_text": "Snape killed?", "adapted_text": "Snape killed?"},
            "s2": {"source_unit_id": "s2", "type": "dialogue", "speaker": "Hagrid", "paragraph_id": "p1", "source_text": "What are you talking about?", "adapted_text": "What are you talking about?"},
            "s3": {"source_unit_id": "s3", "type": "dialogue", "speaker": "Harry", "paragraph_id": "p1", "source_text": "Dumbledore.", "adapted_text": "Dumbledore."},
        }
        plans = [
            {"chunk_id": "chunk_001", "source_unit_ids": ["s1"]},
            {"chunk_id": "chunk_002", "source_unit_ids": ["s2", "s3"]},
        ]
        with patch.object(audiobook_workflow, "compose_director_prompt", return_value=("short prompt", [])):
            repaired = audiobook_workflow.repair_continuous_dialogue_boundaries(plans, units)
        self.assertEqual(repaired[0]["source_unit_ids"], ["s1", "s2"])
        self.assertEqual(repaired[1]["source_unit_ids"], ["s3"])

    def test_single_dangling_spoken_word_does_not_loop(self):
        self.assertEqual(audiobook_workflow.complete_spoken_bridge("He"), "He.")

    def test_complete_narrator_questions_are_not_dangling(self):
        self.assertFalse(
            audiobook_workflow.narrator_quote_is_incomplete(
                "Was that the sea, slapping hard on the rock like that?"
            )
        )
        self.assertFalse(
            audiobook_workflow.narrator_quote_is_incomplete(
                "My -- my mom and dad weren't famous, were they?"
            )
        )
        self.assertTrue(audiobook_workflow.narrator_quote_is_incomplete("He ran through"))

    def test_final_input_gate_accepts_complete_questions_ending_in_pronouns(self):
        self.assertTrue(workflow_input_gate.quote_is_complete("Was that the sea, slapping hard like that?"))
        self.assertTrue(workflow_input_gate.quote_is_complete("My parents weren't famous, were they?"))
        self.assertFalse(workflow_input_gate.quote_is_complete("He ran through"))

    def test_source_units_preserve_sentences_from_one_quote_turn(self):
        with patch.object(audiobook_workflow, "SOURCE_EXCERPT", 'Hagrid spoke. "What? Were they?"'):
            with patch.object(audiobook_workflow, "STORY_CONFIG", {"prompt_language": "en"}):
                units = audiobook_workflow.build_source_units()
        quoted = [unit for unit in units if unit.get("source_kind") == "quoted_text"]
        self.assertEqual([unit["source_text"] for unit in quoted], ["What?", "Were they?"])
        self.assertEqual(len({unit.get("quote_span_id") for unit in quoted}), 1)

    def test_same_quote_turn_preserves_deterministic_middle_response_speaker(self):
        units = [
            {
                "source_kind": "quoted_text", "source_text": "You are famous.", "speaker": "Hagrid",
                "speaker_confidence": "high", "quote_attribution_text": "said Hagrid", "paragraph_id": "p1",
                "source_span": {"char_start": 1, "char_end": 16},
            },
            {
                "source_kind": "quoted_text", "source_text": "What?", "speaker": "Harry",
                "speaker_confidence": "high", "paragraph_id": "p1",
                "source_span": {"char_start": 20, "char_end": 25},
            },
            {
                "source_kind": "quoted_text", "source_text": "My parents weren't famous, were they?", "speaker": "Harry",
                "speaker_confidence": "high", "paragraph_id": "p1",
                "source_span": {"char_start": 26, "char_end": 63},
            },
            {
                "source_kind": "quoted_text", "source_text": "You don't know?", "speaker": "Hagrid",
                "speaker_confidence": "high", "quote_attribution_text": "said Hagrid", "paragraph_id": "p1",
                "source_span": {"char_start": 67, "char_end": 82},
            },
            {
                "source_kind": "narrative_text", "source_text": "Hagrid stared at Harry.", "speaker": "Narrator",
                "paragraph_id": "p1", "source_span": {"char_start": 85, "char_end": 108},
            },
        ]
        role_specs = {
            "Narrator": {"label": "Narrator", "attribution_keywords": []},
            "Harry": {"label": "Harry Potter", "attribution_keywords": ["Harry"]},
            "Hagrid": {"label": "Rubeus Hagrid", "attribution_keywords": ["Hagrid"]},
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            repaired = audiobook_workflow.enforce_supported_named_speakers(units)
        self.assertEqual(repaired[1]["speaker"], "Harry")
        self.assertEqual(repaired[2]["speaker"], "Harry")
        self.assertEqual(repaired[1]["speaker_confidence"], "high")
        self.assertEqual(repaired[2]["speaker_confidence"], "high")

    def test_final_input_gate_preserves_same_quote_turn_speaker_evidence(self):
        units = [
            {
                "source_kind": "quoted_text", "source_text": "You are famous.", "speaker": "hagrid",
                "speaker_confidence": "high", "quote_attribution_text": "said Hagrid", "paragraph_id": "p1",
                "quote_span_id": "p1_q02",
            },
            {
                "source_kind": "quoted_text", "source_text": "What?", "speaker": "harry",
                "speaker_confidence": "high", "paragraph_id": "p1", "quote_span_id": "p1_q03",
            },
            {
                "source_kind": "quoted_text", "source_text": "My parents weren't famous, were they?", "speaker": "harry",
                "speaker_confidence": "high", "paragraph_id": "p1", "quote_span_id": "p1_q03",
            },
            {
                "source_kind": "quoted_text", "source_text": "You don't know?", "speaker": "hagrid",
                "speaker_confidence": "high", "quote_attribution_text": "Hagrid asked", "paragraph_id": "p1",
                "quote_span_id": "p1_q04",
            },
            {
                "source_kind": "narrative_text", "source_text": "Hagrid stared at Harry.", "speaker": "Narrator",
                "paragraph_id": "p1",
            },
        ]
        role_specs = {
            "Narrator": {"label": "Narrator", "attribution_keywords": []},
            "harry": {"label": "Harry Potter", "attribution_keywords": ["Harry"]},
            "hagrid": {"label": "Rubeus Hagrid", "attribution_keywords": ["Hagrid"]},
        }
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 1, role_specs))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 2, role_specs))

    def test_unattributed_named_dialogue_is_low_confidence_without_evidence(self):
        unit = audiobook_workflow.normalize_speaker_attribution(
            {"source_unit_id": "s1", "source_kind": "quoted_text", "speaker": "Ginny Weasley"}
        )
        self.assertEqual(unit["speaker_confidence"], "low")

    def test_explicit_attribution_is_high_confidence(self):
        unit = audiobook_workflow.normalize_speaker_attribution(
            {
                "source_unit_id": "s1",
                "source_kind": "quoted_text",
                "speaker": "Harry Potter",
                "quote_attribution_text": "yelled Harry",
            }
        )
        self.assertEqual(unit["speaker_confidence"], "high")
        self.assertEqual(unit["speaker_evidence"], "yelled Harry")

    def test_named_speaker_requires_adjacent_source_evidence(self):
        units = [
            {"source_kind": "narrative_text", "source_text": "Harry raised his wand."},
            {"source_kind": "quoted_text", "source_text": "Petrificus Totalus!", "speaker": "Harry Potter"},
            {"source_kind": "narrative_text", "source_text": "The spell struck the attacker."},
            {"source_kind": "narrative_text", "source_text": "Harry fell beneath the werewolf."},
            {"source_kind": "quoted_text", "source_text": "Petrificus Totalus!", "speaker": "Ginny Weasley"},
            {"source_kind": "narrative_text", "source_text": "Harry pushed the werewolf away."},
        ]
        role_specs = {
            "Harry Potter": {"label": "Harry Potter", "attribution_keywords": ["Harry"]},
            "Ginny Weasley": {"label": "Ginny Weasley", "attribution_keywords": ["Ginny"]},
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 1))
            self.assertFalse(audiobook_workflow.named_speaker_has_source_evidence(units, 4))

    def test_named_speaker_inherits_verified_quote_across_attribution_bridge(self):
        units = [
            {
                "source_kind": "narrative_text",
                "source_text": "Dudley thought this was very funny.",
                "speaker": "Narrator",
            },
            {
                "source_kind": "quoted_text",
                "source_text": "They stuff people's heads down the toilet.",
                "speaker": "dudley",
            },
            {
                "source_kind": "narrative_text",
                "source_text": "he told Harry.",
                "speaker": "Narrator",
            },
            {
                "source_kind": "quoted_text",
                "source_text": "Want to come upstairs and practice?",
                "speaker": "dudley",
            },
        ]
        role_specs = {"dudley": {"label": "Dudley Dursley", "attribution_keywords": ["Dudley"]}}
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 3))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 3, role_specs))

    def test_named_speaker_uses_registered_kinship_alias(self):
        units = [
            {"source_kind": "narrative_text", "source_text": "His aunt rapped on the door.", "speaker": "Narrator"},
            {"source_kind": "quoted_text", "source_text": "Up!", "speaker": "petunia"},
            {"source_kind": "narrative_text", "source_text": "she screeched.", "speaker": "Narrator"},
        ]
        role_specs = {"petunia": {"label": "Petunia Dursley", "attribution_keywords": ["Aunt Petunia"]}}
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 1))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 1, role_specs))

    def test_named_speaker_accepts_only_two_party_verified_turn_alternation(self):
        units = [
            {"source_kind": "narrative_text", "source_text": "His aunt was outside.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Are you up?", "speaker": "petunia", "paragraph_id": "p1"},
            {"source_kind": "narrative_text", "source_text": "she demanded.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Nearly.", "speaker": "harry", "paragraph_id": "p1"},
            {"source_kind": "narrative_text", "source_text": "said Harry.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Get a move on.", "speaker": "petunia", "paragraph_id": "p1"},
        ]
        role_specs = {
            "petunia": {"label": "Petunia Dursley", "attribution_keywords": ["Aunt Petunia"]},
            "harry": {"label": "Harry Potter", "attribution_keywords": ["Harry"]},
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 5))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 5, role_specs))

    def test_adjacent_role_name_in_other_paragraph_is_not_evidence(self):
        units = [
            {"source_kind": "narrative_text", "source_text": "Harry left the room.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Who is there?", "speaker": "harry", "paragraph_id": "p2"},
        ]
        role_specs = {"harry": {"label": "Harry Potter", "attribution_keywords": ["Harry"]}}
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            self.assertFalse(audiobook_workflow.named_speaker_has_source_evidence(units, 1))
        self.assertFalse(workflow_input_gate.named_speaker_has_source_evidence(units, 1, role_specs))

    def test_pronoun_bridge_and_verified_two_party_sandwich(self):
        units = [
            {"source_kind": "narrative_text", "source_text": "Dudley was counting his presents.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "narrative_text", "source_text": "His face fell.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Thirty-six.", "speaker": "dudley", "paragraph_id": "p1"},
            {"source_kind": "narrative_text", "source_text": "he said.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Two less.", "speaker": "dudley", "paragraph_id": "p1"},
            {"source_kind": "narrative_text", "source_text": "Aunt Petunia watched him.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Darling, count this one.", "speaker": "petunia", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "All right.", "speaker": "dudley", "paragraph_id": "p1"},
            {"source_kind": "narrative_text", "source_text": "said Dudley.", "speaker": "Narrator", "paragraph_id": "p1"},
        ]
        role_specs = {
            "dudley": {"label": "Dudley Dursley", "attribution_keywords": ["Dudley"]},
            "petunia": {"label": "Aunt Petunia", "attribution_keywords": ["Petunia"]},
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 2))
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 4))
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 6))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 2, role_specs))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 4, role_specs))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 6, role_specs))

    def test_ocr_double_apostrophe_closes_english_quote(self):
        source = '"Is that all right\'\' Dudley thought for a moment. "So I will."'
        with patch.object(audiobook_workflow, "SOURCE_EXCERPT", source):
            with patch.object(audiobook_workflow, "is_english_prompt", return_value=True):
                units = audiobook_workflow.build_source_units()
        self.assertEqual([unit["source_kind"] for unit in units], ["quoted_text", "narrative_text", "quoted_text"])
        self.assertEqual(units[1]["source_text"], "Dudley thought for a moment.")

    def test_english_double_apostrophe_and_curly_single_quotes_parse(self):
        source = "''First line.'' Then ‘Second line.’"
        with patch.object(audiobook_workflow, "SOURCE_EXCERPT", source):
            with patch.object(audiobook_workflow, "is_english_prompt", return_value=True):
                units = audiobook_workflow.build_source_units()
        quoted = [unit["source_text"] for unit in units if unit["source_kind"] == "quoted_text"]
        self.assertEqual(quoted, ["First line.", "Second line."])

    def test_double_apostrophe_quote_allows_contraction(self):
        source = "''I'm fine.''"
        with patch.object(audiobook_workflow, "SOURCE_EXCERPT", source):
            with patch.object(audiobook_workflow, "is_english_prompt", return_value=True):
                units = audiobook_workflow.build_source_units()
        self.assertEqual(units[0]["source_kind"], "quoted_text")
        self.assertEqual(units[0]["source_text"], "I'm fine.")

    def test_model_omitted_fallback_is_not_promoted_by_neighbor_heuristic(self):
        units = [
            {"source_kind": "narrative_text", "source_text": "Dudley waited.", "speaker": "Narrator"},
            {
                "source_kind": "quoted_text",
                "source_text": "It looked like hard work.",
                "speaker": "Narrator",
                "model_omitted_fallback": True,
            },
            {"source_kind": "narrative_text", "source_text": "Dudley frowned.", "speaker": "Narrator"},
        ]
        with patch.object(audiobook_workflow, "ROLE_SPECS", {"Narrator": {}, "dudley": {"attribution_keywords": ["Dudley"]}}):
            repaired = audiobook_workflow.repair_quoted_speakers(units)
        self.assertEqual(repaired[1]["speaker"], "Narrator")

    def test_terminal_vocative_and_aba_turn_are_supported(self):
        units = [
            {"source_kind": "quoted_text", "source_text": "Get the mail, Dudley,", "speaker": "vernon", "paragraph_id": "p1"},
            {"source_kind": "narrative_text", "source_text": "said Uncle Vernon.", "speaker": "Narrator", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Make Harry get it.", "speaker": "dudley", "paragraph_id": "p1"},
            {"source_kind": "quoted_text", "source_text": "Get the mail, Harry.", "speaker": "vernon", "paragraph_id": "p1"},
        ]
        role_specs = {
            "vernon": {"label": "Uncle Vernon", "attribution_keywords": ["Vernon"]},
            "dudley": {"label": "Dudley", "attribution_keywords": ["Dudley"]},
            "harry": {"label": "Harry", "attribution_keywords": ["Harry"]},
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 2))
            self.assertTrue(audiobook_workflow.named_speaker_has_source_evidence(units, 3))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 2, role_specs))
        self.assertTrue(workflow_input_gate.named_speaker_has_source_evidence(units, 3, role_specs))

    def test_unsupported_named_quote_and_punctuation_downgrade_to_narrator(self):
        units = [
            {"source_kind": "narrative_text", "source_text": "He looked at the tub.", "speaker": "Narrator"},
            {"source_kind": "quoted_text", "source_text": "What's this?", "speaker": "harry", "speaker_confidence": "high"},
            {"source_kind": "narrative_text", "source_text": "he asked Aunt Petunia.", "speaker": "Narrator"},
            {"source_kind": "quoted_text", "source_text": "--.", "speaker": "dudley", "speaker_confidence": "high"},
        ]
        role_specs = {
            "Narrator": {"label": "Narrator"},
            "harry": {"label": "Harry", "attribution_keywords": ["Harry"]},
            "dudley": {"label": "Dudley", "attribution_keywords": ["Dudley"]},
            "petunia": {"label": "Aunt Petunia", "attribution_keywords": ["Petunia"]},
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            repaired = audiobook_workflow.enforce_supported_named_speakers(units)
        self.assertEqual(repaired[1]["speaker"], "Narrator")
        self.assertEqual(repaired[3]["speaker"], "Narrator")

    def test_oversized_audio_plan_is_split_before_static_gate(self):
        # Each source unit fits by itself, while the combined scene models the
        # current ~58s fixture and therefore must be split before admission.
        words = " ".join(["story"] * 30)
        parsed = {
            f"s{index:04d}": {
                "source_unit_id": f"s{index:04d}",
                "source_kind": "narrative_text",
                "source_text": f"{words}.",
                "adapted_text": f"{words}.",
                "speaker": "Narrator",
                "sfx_before": [],
                "sfx_during": [],
                "sfx_after": [],
            }
            for index in range(1, 5)
        }
        plans = [{"chunk_id": "chunk_001", "title": "Long scene", "source_unit_ids": list(parsed)}]
        with patch.object(audiobook_workflow, "ROLE_SPECS", {"Narrator": {"key": "narrator"}}):
            unsplit_prompt, _roles = audiobook_workflow.compose_director_prompt(plans[0], parsed, 1, 1)
            unsplit_ceiling = audiobook_workflow.audio_drama_coverage_metrics(
                unsplit_prompt, plans[0]
            )["audio_drama_estimated_duration_ceiling_sec"]
            self.assertGreater(unsplit_ceiling, audiobook_workflow.SAFE_CONTENT_CEILING_SEC)
            repaired = audiobook_workflow.enforce_estimated_duration_limit(plans, parsed)
        self.assertGreater(len(repaired), 1)
        self.assertEqual(
            [unit_id for plan in repaired for unit_id in plan["source_unit_ids"]],
            list(parsed),
        )
        self.assertTrue(all(len(plan["source_unit_ids"]) < 4 for plan in repaired))
        with patch.object(audiobook_workflow, "ROLE_SPECS", {"Narrator": {"key": "narrator"}}):
            for index, plan in enumerate(repaired, start=1):
                prompt, _roles = audiobook_workflow.compose_director_prompt(plan, parsed, index, len(repaired))
                metrics = audiobook_workflow.audio_drama_coverage_metrics(prompt, plan)
                self.assertLessEqual(
                    metrics["audio_drama_estimated_duration_ceiling_sec"],
                    audiobook_workflow.SAFE_CONTENT_CEILING_SEC,
                )
                contract = audiobook_workflow.audible_layer_contract(plan)
                final_prompt, outro, timing = audiobook_workflow.apply_chunk_specific_audible_outro(
                    prompt, contract, plan=plan
                )
                self.assertTrue(final_prompt.startswith("Time budget:"))
                self.assertEqual(final_prompt.splitlines()[-1], outro)
                self.assertAlmostEqual(
                    timing["cadence_start_sec"],
                    timing["target_end_sec"] - timing["cadence_duration_sec"],
                    places=1,
                )
                self.assertEqual(timing["speech_action_percent"], "70-75")
                self.assertEqual(timing["audible_closure_percent"], "20-25")

    def test_indivisible_narration_uses_safe_render_schedule_instead_of_false_positive(self):
        words = " ".join(["story"] * 50)
        parsed = {
            "s0001": {
                "source_unit_id": "s0001",
                "source_kind": "narrative_text",
                "source_text": f"{words}.",
                "adapted_text": f"{words}.",
                "speaker": "Narrator",
                "sfx_before": [],
                "sfx_during": [],
                "sfx_after": [],
            }
        }
        plans = [
            {
                "chunk_id": "chunk_001",
                "title": "Indivisible narration",
                "source_unit_ids": ["s0001"],
                "persistent_ambience": "quiet room tone",
                "music_bed": "soft piano score",
                "sound_design": "subtle movement",
            }
        ]
        role_specs = {
            "Narrator": {
                "key": "narrator",
                "label": "Narrator",
                "description": "literary narrator",
            }
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            prompt, _roles = audiobook_workflow.compose_director_prompt(plans[0], parsed, 1, 1)
            metrics = audiobook_workflow.audio_drama_coverage_metrics(prompt, plans[0])
            self.assertGreater(
                metrics["audio_drama_estimated_duration_ceiling_sec"],
                audiobook_workflow.SAFE_CONTENT_CEILING_SEC,
            )
            repaired = audiobook_workflow.enforce_estimated_duration_limit(plans, parsed)
        self.assertEqual(len(repaired), 1)
        warning = repaired[0]["duration_gate_warnings"][0]
        self.assertEqual(warning["code"], "single_source_unit_uses_safe_render_schedule")
        self.assertTrue(warning["admissible"])
        self.assertLessEqual(
            warning["render_contract_max_sec"],
            audiobook_workflow.SAFE_DURATION_CEILING_SEC,
        )

    def test_truly_oversized_indivisible_narration_still_blocks(self):
        words = " ".join(["story"] * 180)
        parsed = {
            "s0001": {
                "source_unit_id": "s0001",
                "source_kind": "narrative_text",
                "source_text": f"{words}.",
                "adapted_text": f"{words}.",
                "speaker": "Narrator",
                "sfx_before": [],
                "sfx_during": [],
                "sfx_after": [],
            }
        }
        plans = [{"chunk_id": "chunk_001", "title": "Too long", "source_unit_ids": ["s0001"]}]
        role_specs = {
            "Narrator": {
                "key": "narrator",
                "label": "Narrator",
                "description": "literary narrator",
            }
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            with self.assertRaisesRegex(SystemExit, "Base prompt exceeds Seed Audio prompt limit"):
                audiobook_workflow.enforce_estimated_duration_limit(plans, parsed)

    def test_compiled_contract_metadata_does_not_reblock_safe_multi_unit_chunk(self):
        prompt = (
            'Narrator (actor is <<TGT_SPK1>>): "'
            + " ".join(["story"] * 56)
            + '." Then a soft door closes. Background music and room ambience remain audible.'
        )
        plan = {
            "source_unit_ids": ["s0001", "s0002", "s0003", "s0004"],
            "music_bed": "soft piano score",
            "persistent_ambience": "quiet room tone",
            "sfx_story_events": ["a soft door closes"],
        }
        contract = {
            "estimated_speech_sec": 18.0,
            "planned_pre_roll_sec": 1.0,
            "planned_post_roll_sec": 4.0,
            "target_audible_duration_sec": 28.0,
            "duration_range_sec": {"min": 20.0, "max": 36.0},
        }
        metrics = audiobook_workflow.audio_drama_coverage_metrics(prompt, plan)
        self.assertGreater(
            metrics["audio_drama_estimated_duration_ceiling_sec"],
            audiobook_workflow.SAFE_CONTENT_CEILING_SEC,
        )
        evidence = audiobook_workflow.compiled_render_contract_duration_evidence(prompt, plan, contract)
        self.assertTrue(evidence["admissible"])
        self.assertEqual(evidence["code"], "compiled_prompt_uses_safe_render_contract")

    def test_duration_split_prefers_narrative_boundary_over_dialogue_opener(self):
        words = "short source"
        speakers = ["Narrator", "Narrator", "Narrator", "harry", "Narrator", "harry"]
        parsed = {
            f"s{index:04d}": {
                "source_unit_id": f"s{index:04d}",
                "source_kind": "narrative_text" if speaker == "Narrator" else "quoted_text",
                "source_text": f"{words}.",
                "adapted_text": f"{words}.",
                "speaker": speaker,
                "sfx_before": [],
                "sfx_during": [],
                "sfx_after": [],
            }
            for index, speaker in enumerate(speakers, start=1)
        }
        plans = [{"chunk_id": "chunk_001", "title": "Long scene", "source_unit_ids": list(parsed)}]
        role_specs = {
            "Narrator": {"key": "narrator", "label": "Narrator", "description": "narrator"},
            "harry": {"key": "harry", "label": "Harry", "description": "young voice"},
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            with patch.object(
                audiobook_workflow,
                "compose_director_prompt",
                side_effect=lambda plan, _parsed, _index, _total: (
                    f'Narrator (actor is <<TGT_SPK1>>): "{" ".join(["story"] * (20 * len(plan["source_unit_ids"])))}."',
                    ["Narrator"],
                ),
            ):
                repaired = audiobook_workflow.enforce_estimated_duration_limit(plans, parsed)
        for left, right in zip(repaired, repaired[1:]):
            left_ids = left["source_unit_ids"]
            right_ids = right["source_unit_ids"]
            movable_setup_left = len(left_ids) > 1 and parsed[left_ids[-1]]["speaker"] == "Narrator"
            dialogue_opener = parsed[right_ids[0]]["speaker"] != "Narrator"
            self.assertFalse(movable_setup_left and dialogue_opener)

    def test_rebuilt_prompt_keeps_binding_for_attribution_only_split(self):
        parsed = {
            "s0001": {
                "source_unit_id": "s0001",
                "speaker": "Narrator",
                "source_text": "said Dudley.",
                "adapted_text": "said Dudley.",
                "sfx_before": [],
                "sfx_during": [],
                "sfx_after": [],
            }
        }
        plan = {
            "source_unit_ids": ["s0001"],
            "persistent_ambience": "quiet room tone",
            "sound_design": "subtle movement",
            "pace_note": "natural",
        }
        role_specs = {"Narrator": {"key": "narrator", "label": "Narrator", "description": "literary narrator"}}
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            prompt, roles = audiobook_workflow.compose_english_director_prompt(plan, parsed, 1, 1)
        self.assertEqual(roles, ["Narrator"])
        self.assertIn("actor is <<TGT_SPK1>>", prompt)

    def test_cached_only_rewrite_never_falls_back_to_provider(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "logs").mkdir()
            (run_dir / "logs/seed2_rewrite_response.txt").write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"SEED_REWRITE_CACHED_ONLY": "1"}):
                with patch.object(audiobook_workflow, "compact_rewrite_prompt", return_value="prompt"):
                    with patch.object(audiobook_workflow, "normalize_rewrite", side_effect=SystemExit("bad cached plan")):
                        with patch.object(audiobook_workflow.subprocess, "run") as provider:
                            with self.assertRaisesRegex(SystemExit, "Cached rewrite response failed"):
                                audiobook_workflow.call_seed2_rewrite(run_dir, [])
            provider.assert_not_called()

    def test_cached_only_rewrite_missing_cache_never_calls_provider(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "logs").mkdir()
            with patch.dict(os.environ, {"SEED_REWRITE_CACHED_ONLY": "1"}):
                with patch.object(audiobook_workflow, "compact_rewrite_prompt", return_value="prompt"):
                    with patch.object(audiobook_workflow.subprocess, "run") as provider:
                        with self.assertRaisesRegex(SystemExit, "required but missing"):
                            audiobook_workflow.call_seed2_rewrite(run_dir, [])
            provider.assert_not_called()

    def test_rewrite_retries_actual_http_429_toomanyrequests_once(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            failed = type(
                "Completed",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": 'HTTP 429: {"error":{"code":"ServerOverloaded","type":"TooManyRequests"}}',
                },
            )()
            succeeded = type("Completed", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
            with patch.dict(os.environ, {"SEED_REWRITE_ATTEMPTS": "2"}):
                with patch.object(audiobook_workflow, "compact_rewrite_prompt", return_value="base prompt"):
                    with patch.object(audiobook_workflow, "normalize_rewrite", return_value={"valid": True}):
                        with patch.object(audiobook_workflow, "validate_rewrite"):
                            with patch.object(audiobook_workflow.time, "sleep"):
                                with patch.object(
                                    audiobook_workflow.subprocess,
                                    "run",
                                    side_effect=[failed, succeeded],
                                ) as provider:
                                    result = audiobook_workflow.call_seed2_rewrite(run_dir, [])
            self.assertEqual(result, {"valid": True})
            self.assertEqual(provider.call_count, 2)
            logs = run_dir / "logs"
            self.assertIn("HTTP 429", (logs / "seed2_rewrite_error_attempt_1.txt").read_text())
            first_prompt = (logs / "seed2_rewrite_prompt_attempt_1.txt").read_text()
            second_prompt = (logs / "seed2_rewrite_prompt_attempt_2.txt").read_text()
            self.assertEqual(first_prompt, "base prompt")
            self.assertNotEqual(second_prompt, first_prompt)
            self.assertIn("TooManyRequests", second_prompt)
            self.assertTrue((logs / "seed2_rewrite_response_attempt_2.txt").exists())
            self.assertFalse((logs / "seed2_rewrite_response_attempt_1.txt").exists())

    def test_rewrite_resume_continues_at_next_attempt_without_overwrite(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            logs = run_dir / "logs"
            logs.mkdir()
            (logs / "seed2_rewrite_prompt.txt").write_text("base prompt", encoding="utf-8")
            (logs / "seed2_rewrite_prompt_attempt_1.txt").write_text("immutable failed prompt", encoding="utf-8")
            (logs / "seed2_rewrite_error_attempt_1.txt").write_text(
                "HTTP 429: TooManyRequests", encoding="utf-8"
            )
            succeeded = type("Completed", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
            with patch.dict(os.environ, {"SEED_REWRITE_ATTEMPTS": "2"}):
                with patch.object(audiobook_workflow, "compact_rewrite_prompt", return_value="base prompt"):
                    with patch.object(audiobook_workflow, "normalize_rewrite", return_value={"valid": True}):
                        with patch.object(audiobook_workflow, "validate_rewrite"):
                            with patch.object(audiobook_workflow.subprocess, "run", return_value=succeeded) as provider:
                                result = audiobook_workflow.call_seed2_rewrite(run_dir, [])
                                reused = audiobook_workflow.call_seed2_rewrite(run_dir, [])
            self.assertEqual(result, {"valid": True})
            self.assertEqual(reused, {"valid": True})
            provider.assert_called_once()
            self.assertEqual(
                (logs / "seed2_rewrite_prompt_attempt_1.txt").read_text(),
                "immutable failed prompt",
            )
            self.assertFalse((logs / "seed2_rewrite_response_attempt_1.txt").exists())
            self.assertTrue((logs / "seed2_rewrite_prompt_attempt_2.txt").exists())
            self.assertTrue((logs / "seed2_rewrite_response_attempt_2.txt").exists())
            self.assertIn("Previous attempt failed", (logs / "seed2_rewrite_prompt_attempt_2.txt").read_text())

    def test_rewrite_resume_without_old_error_still_does_not_repeat_prompt(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            logs = run_dir / "logs"
            logs.mkdir()
            (logs / "seed2_rewrite_prompt.txt").write_text("base prompt", encoding="utf-8")
            (logs / "seed2_rewrite_prompt_attempt_1.txt").write_text("base prompt", encoding="utf-8")
            succeeded = type("Completed", (), {"returncode": 0, "stdout": "{}", "stderr": ""})()
            with patch.dict(os.environ, {"SEED_REWRITE_ATTEMPTS": "2"}):
                with patch.object(audiobook_workflow, "compact_rewrite_prompt", return_value="base prompt"):
                    with patch.object(audiobook_workflow, "normalize_rewrite", return_value={"valid": True}):
                        with patch.object(audiobook_workflow, "validate_rewrite"):
                            with patch.object(audiobook_workflow.subprocess, "run", return_value=succeeded):
                                audiobook_workflow.call_seed2_rewrite(run_dir, [])
            self.assertEqual((logs / "seed2_rewrite_prompt_attempt_1.txt").read_text(), "base prompt")
            self.assertTrue((logs / "seed2_rewrite_error_attempt_1.txt").exists())
            second_prompt = (logs / "seed2_rewrite_prompt_attempt_2.txt").read_text()
            self.assertNotEqual(second_prompt, "base prompt")
            self.assertIn("Prior rewrite attempt 1 produced no validated response", second_prompt)

    def test_rewrite_resume_with_exhausted_persisted_budget_makes_no_request(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            logs = run_dir / "logs"
            logs.mkdir()
            (logs / "seed2_rewrite_prompt.txt").write_text("base prompt", encoding="utf-8")
            for attempt in (1, 2):
                (logs / f"seed2_rewrite_prompt_attempt_{attempt}.txt").write_text(
                    f"attempt {attempt}", encoding="utf-8"
                )
                (logs / f"seed2_rewrite_error_attempt_{attempt}.txt").write_text(
                    "HTTP 429: TooManyRequests", encoding="utf-8"
                )
            with patch.dict(os.environ, {"SEED_REWRITE_ATTEMPTS": "2"}):
                with patch.object(audiobook_workflow, "compact_rewrite_prompt", return_value="base prompt"):
                    with patch.object(audiobook_workflow.subprocess, "run") as provider:
                        with self.assertRaisesRegex(SystemExit, "attempt budget exhausted"):
                            audiobook_workflow.call_seed2_rewrite(run_dir, [])
            provider.assert_not_called()

    def test_runner_logs_are_numbered_and_legacy_files_are_immutable(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            logs = Path(temp_dir)
            first = audio_drama_skill.shared.write_runner_log_attempt(logs, "section_007.prepare", "out1", "err1")
            second = audio_drama_skill.shared.write_runner_log_attempt(logs, "section_007.prepare", "out2", "err2")
            self.assertEqual(first["attempt"], 1)
            self.assertEqual(second["attempt"], 2)
            self.assertEqual((logs / "section_007.prepare.stdout.txt").read_text(), "out1")
            self.assertEqual((logs / "section_007.prepare.stderr.txt").read_text(), "err1")
            self.assertEqual((logs / "section_007.prepare.attempt_001.stdout.txt").read_text(), "out1")
            self.assertEqual((logs / "section_007.prepare.attempt_002.stdout.txt").read_text(), "out2")

    def test_runner_logs_continue_after_legacy_only_evidence(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            logs = Path(temp_dir)
            (logs / "section_007.prepare.stdout.txt").write_text("legacy out", encoding="utf-8")
            (logs / "section_007.prepare.stderr.txt").write_text("legacy err", encoding="utf-8")
            written = audio_drama_skill.shared.write_runner_log_attempt(
                logs, "section_007.prepare", "new out", "new err"
            )
            self.assertEqual(written["attempt"], 2)
            self.assertEqual((logs / "section_007.prepare.stdout.txt").read_text(), "legacy out")
            self.assertEqual((logs / "section_007.prepare.stderr.txt").read_text(), "legacy err")
            self.assertEqual((logs / "section_007.prepare.attempt_002.stdout.txt").read_text(), "new out")
            self.assertEqual((logs / "section_007.prepare.attempt_002.stderr.txt").read_text(), "new err")

    def test_retryable_rewrite_error_matches_provider_spellings(self):
        self.assertTrue(audiobook_workflow.retryable_rewrite_error("HTTP 429: TooManyRequests"))
        self.assertTrue(audiobook_workflow.retryable_rewrite_error("HTTP Error 429: Too Many Requests"))
        self.assertTrue(audiobook_workflow.retryable_rewrite_error("ServerOverloaded"))
        self.assertTrue(audiobook_workflow.retryable_rewrite_error("Chat failed: The read operation timed out"))
        self.assertTrue(audiobook_workflow.retryable_rewrite_error("Seed 2.0 Pro rewrite attempt 1 exceeded 600s"))
        self.assertFalse(audiobook_workflow.retryable_rewrite_error("HTTP 400: invalid request"))

    def test_too_little_spoken_dialogue_routes_to_planner_retry(self):
        self.assertTrue(
            audiobook_workflow.retryable_rewrite_validation_error(
                "chunk_019 has too little real spoken dialogue: spoken_quote_word_ratio=0.016, min=0.02"
            )
        )
        self.assertFalse(
            audiobook_workflow.retryable_rewrite_validation_error(
                "chunk_019 exceeds the provider hard duration ceiling"
            )
        )

    def test_prepare_all_preserves_section_waiting_for_chunk_repair(self):
        state = {
            "sections": [
                {
                    "section_id": "section_001",
                    "status": "needs_chunk_repair",
                    "chunks": [
                        {
                            "chunk_id": "chunk_001",
                            "chunk_key": "section_001/chunk_001",
                            "status": "needs_chunk_repair",
                        }
                    ],
                }
            ]
        }
        with patch.object(audio_drama_skill, "prepare_section") as prepare:
            self.assertTrue(audio_drama_skill.prepare_all(Path("/tmp/run"), state))
        prepare.assert_not_called()

    def test_exhausted_provider_chunk_repair_promotes_to_structural_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            delivery_dir = run_dir / "sections" / "section_001" / "logs" / "chunk_delivery"
            delivery_dir.mkdir(parents=True)
            (delivery_dir / "chunk_001.json").write_text(
                json.dumps(
                    {
                        "status": "needs_chunk_repair",
                        "technical": {"status": "pass", "duration_sec": 11.5},
                        "performance": {
                            "chunks": [
                                {
                                    "chunk_id": "chunk_001",
                                    "summary": "The final spoken line is truncated.",
                                    "issues": [{"type": "hard_boundary_or_abrupt_cut", "severity": "major"}],
                                }
                            ]
                        },
                        "review_attempts": [
                            "logs/performance_reviews/chunk_001/attempt_001_initial",
                            "logs/performance_reviews/chunk_001/attempt_002_provider_repair",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            chunk = {
                "chunk_id": "chunk_001",
                "chunk_key": "section_001/chunk_001",
                "status": "needs_chunk_repair",
                "last_error": "The final spoken line is truncated.",
            }
            section = {
                "section_id": "section_001",
                "work_dir": "sections/section_001",
                "status": "needs_chunk_repair",
                "chunks": [chunk],
            }
            state = {"status": "needs_chunk_repair", "sections": [section]}
            with patch.object(audio_drama_skill, "save_state"), patch.object(audio_drama_skill, "append_event"):
                promoted = audio_drama_skill.promote_exhausted_chunk_repair(
                    run_dir, state, section, chunk
                )
            self.assertTrue(promoted)
            self.assertEqual(chunk["status"], "needs_replan")
            self.assertEqual(section["status"], "needs_replan")
            self.assertEqual(state["status"], "needs_replan")
            self.assertEqual(chunk["replan_feedback"]["stage"], "batch")
            self.assertEqual(len(chunk["replan_feedback"]["exhausted_chunk_repair_attempts"]), 2)

    def test_partial_resume_rejects_changed_source_before_reusing_units(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "01_source_excerpt.txt").write_text("old source", encoding="utf-8")
            (run_dir / "02_source_units.json").write_text("[]", encoding="utf-8")
            with patch.object(audiobook_workflow, "SOURCE_EXCERPT", "changed source"):
                with self.assertRaisesRegex(SystemExit, "source mismatch"):
                    audiobook_workflow.source_units_for_partial_resume(run_dir)

    def test_partial_resume_rejects_units_without_source_proof(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "02_source_units.json").write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "source proof is missing"):
                audiobook_workflow.source_units_for_partial_resume(run_dir)

    def test_role_identity_resolves_registry_key_not_hardcoded_label(self):
        role_specs = {
            "hagrid": {"label": "Rubeus Hagrid", "attribution_keywords": ["Hagrid"]},
            "harry": {"label": "Harry Potter", "attribution_keywords": ["Harry"]},
        }
        with patch.object(audiobook_workflow, "ROLE_SPECS", role_specs):
            self.assertEqual(audiobook_workflow.role_key_for_identity("Hagrid"), "hagrid")
            self.assertEqual(audiobook_workflow.role_key_for_identity("Harry Potter"), "harry")

    def test_casting_samples_gate_batch_until_human_approval(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "voice_registry.json").write_text(
                json.dumps(
                    {
                        "roles": {
                            "Narrator": {
                                "key": "narrator",
                                "default_speaker": "en_male_knightley_uranus_bigtts",
                                "reference_prompt": "The tower stood silent.",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            state = {"casting": {"required": True, "approved": False, "samples": {}}}

            def fake_run(command, cwd=None, text=None, capture_output=None, timeout=None):
                output = Path(command[command.index("--out") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"RIFFdry-voice-sample")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch.object(audio_drama_skill.subprocess, "run", side_effect=fake_run):
                with patch.object(audio_drama_skill, "save_state"):
                    with patch.object(audio_drama_skill, "append_event"):
                        prepared = audio_drama_skill.prepare_casting_samples(run_dir, state)
        self.assertTrue(prepared)
        self.assertEqual(state["status"], "awaiting_casting_approval")
        self.assertFalse(state["casting"]["approved"])
        self.assertEqual(state["casting"]["samples"]["Narrator"]["audio"], "casting_samples/narrator.wav")
        self.assertEqual(state["casting"]["samples"]["Narrator"]["speaker"], "en_male_knightley_uranus_bigtts")

    def test_casting_approval_is_invalidated_by_registry_change(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            registry = run_dir / "voice_registry.json"
            registry.write_text('{"roles":{"Narrator":{"default_speaker":"voice-a"}}}', encoding="utf-8")
            approved_hash = audio_drama_skill.casting_registry_sha256(run_dir)
            state = {"casting": {"approved": True, "approved_registry_sha256": approved_hash}}
            self.assertTrue(audio_drama_skill.casting_is_approved(run_dir, state))
            registry.write_text('{"roles":{"Narrator":{"default_speaker":"voice-b"}}}', encoding="utf-8")
            self.assertFalse(audio_drama_skill.casting_is_approved(run_dir, state))

    def test_update_casting_registry_archives_changed_samples_and_updates_inputs(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            old_roles = {
                "Narrator": {
                    "key": "narrator",
                    "default_speaker": "en_male_knightley_uranus_bigtts",
                    "reference_prompt": "Old prompt.",
                }
            }
            new_roles = {
                "Narrator": {
                    "key": "narrator",
                    "default_speaker": "en_male_david_uranus_bigtts",
                    "reference_prompt": "New prompt.",
                }
            }
            (run_dir / "casting_samples").mkdir(parents=True)
            (run_dir / "planning/casting_prompts").mkdir(parents=True)
            (run_dir / "inputs/section_001").mkdir(parents=True)
            (run_dir / "voice_registry.json").write_text(json.dumps({"roles": old_roles}), encoding="utf-8")
            (run_dir / "casting_samples/narrator.wav").write_bytes(b"old-wave")
            (run_dir / "casting_samples/narrator.wav.meta.json").write_text("{}", encoding="utf-8")
            (run_dir / "planning/casting_prompts/narrator.txt").write_text("Old prompt.", encoding="utf-8")
            (run_dir / "inputs/section_001/story_config.json").write_text(
                json.dumps({"roles": old_roles}), encoding="utf-8"
            )
            registry_path = run_dir / "replacement.json"
            registry_path.write_text(json.dumps({"roles": new_roles}), encoding="utf-8")
            state = {
                "casting": {
                    "approved": True,
                    "approved_at": "now",
                    "approved_registry_sha256": "old",
                    "samples": {"Narrator": {"speaker": "en_male_knightley_uranus_bigtts"}},
                }
            }
            with patch.object(audio_drama_skill, "save_state"):
                with patch.object(audio_drama_skill, "append_event"):
                    changed = audio_drama_skill.update_casting_registry(run_dir, state, registry_path)
            history = list((run_dir / "history/casting_registry").glob("*"))
            self.assertEqual(changed, ["Narrator"])
            self.assertEqual(len(history), 1)
            self.assertEqual((history[0] / "casting_samples/narrator.wav").read_bytes(), b"old-wave")
            updated_roles = json.loads((run_dir / "inputs/section_001/story_config.json").read_text())["roles"]
            self.assertEqual(updated_roles["Narrator"]["default_speaker"], "en_male_david_uranus_bigtts")
            self.assertEqual(updated_roles["Narrator"]["reference_mode"], "speaker")
            self.assertFalse(state["casting"]["approved"])
            self.assertEqual(state["status"], "casting_registry_updated")

    def test_import_casting_samples_requires_identical_registry_and_records_hash(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            target = root / "target"
            for run in (source, target):
                (run / "casting_samples").mkdir(parents=True)
            roles = {
                "Narrator": {
                    "key": "narrator",
                    "default_speaker": "en_male_knightley_uranus_bigtts",
                    "reference_mode": "speaker",
                }
            }
            registry = json.dumps({"roles": roles})
            (source / "voice_registry.json").write_text(registry, encoding="utf-8")
            (target / "voice_registry.json").write_text(registry, encoding="utf-8")
            (source / "casting_samples/narrator.wav").write_bytes(b"shared-wave")
            (source / "casting_samples/narrator.wav.meta.json").write_text("{}", encoding="utf-8")
            source_state = {
                "run_id": "source-run",
                "casting": {"samples": {"Narrator": {"speaker": "en_male_knightley_uranus_bigtts"}}},
            }
            (source / "run_state.json").write_text(json.dumps(source_state), encoding="utf-8")
            target_state = {"casting": {"approved": False, "samples": {}}}
            with patch.object(audio_drama_skill, "save_state"):
                with patch.object(audio_drama_skill, "append_event"):
                    imported = audio_drama_skill.import_casting_samples(target, target_state, source)
            self.assertEqual(imported, ["Narrator"])
            self.assertEqual((target / "casting_samples/narrator.wav").read_bytes(), b"shared-wave")
            self.assertEqual(target_state["casting"]["samples"]["Narrator"]["imported_from_run"], "source-run")
            self.assertEqual(len(target_state["casting"]["samples"]["Narrator"]["sha256"]), 64)
            self.assertEqual(target_state["status"], "awaiting_casting_approval")

    def test_casting_timeout_is_bounded_and_resumable(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "voice_registry.json").write_text(
                json.dumps(
                    {
                        "roles": {
                            "Narrator": {
                                "key": "narrator",
                                "default_speaker": "en_male_knightley_uranus_bigtts",
                                "reference_prompt": "The tower stood silent.",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            state = {"casting": {"required": True, "approved": False, "samples": {}}}
            timeout = audio_drama_skill.subprocess.TimeoutExpired(cmd="seed_audio", timeout=10)
            with patch.dict("os.environ", {"SEED_CASTING_TIMEOUT": "10"}):
                with patch.object(audio_drama_skill.subprocess, "run", side_effect=timeout):
                    with patch.object(audio_drama_skill, "save_state"):
                        prepared = audio_drama_skill.prepare_casting_samples(run_dir, state)
        self.assertFalse(prepared)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["casting"]["failed_role"], "Narrator")
        self.assertEqual(state["casting"]["last_error"], "casting_sample_timeout:Narrator:10s")

    def make_gate_fixture(self, root: Path, narrator_line: str) -> Path:
        (root / "06_generation_requests").mkdir(parents=True)
        (root / "02_source_units.json").write_text(
            json.dumps(
                [
                    {"source_unit_id": "s0001"},
                    {"source_unit_id": "s0002"},
                    {"source_unit_id": "s0003"},
                ]
            ),
            encoding="utf-8",
        )
        (root / "04_voice_registry.json").write_text(
            json.dumps({"voices": [{"role": "Narrator", "speaker": "voice-a"}]}),
            encoding="utf-8",
        )
        prompt = (
            "All audible speech must be English only. Do not translate or speak Chinese.\n"
            "Voice continuity: Narrator uses <<TGT_SPK1>>. one voice at a time; no overlapping narration and dialogue.\n"
            "Ambient sound: wind. Background music: strings. Sound design: footsteps.\n"
            "Begin with a brief sound-only establishing beat: ambience and score arrive before speech.\n"
            + "\n".join(
                f'Narrator (actor is <<TGT_SPK1>>, natural): "{narrator_line}"' for _ in range(3)
            )
            + "\nThe sound of footsteps answers the action clearly while the ambience continues.\n"
            + "The sound of a door answers the action clearly while the ambience continues.\n"
            + "The score remains clearly audible in the timeline here, then swells with the story turn.\n"
            + "After the last voice, no further speech occurs; the foreground action reverberates and decays completely.\n"
            + "The established ambience remains alone for a brief natural breath instead of cutting to silence.\n"
            + "The clearly audible shared score carries beyond the final word into a complete sound-only coda.\n"
        )
        (root / "06_generation_requests/chunk_001.json").write_text(
            json.dumps(
                {
                    "chunk_id": "chunk_001",
                    "source_unit_ids": ["s0001", "s0002", "s0003"],
                    "active_roles": ["Narrator"],
                    "text_prompt": prompt,
                    "input_metrics": {
                        "narrator_unit_count": 3,
                        "dialogue_unit_count": 0,
                        "audio_drama_estimated_duration_ceiling_sec": 40,
                        "sfx_cue_count": 2,
                        "music_reaction_lines": 1,
                        "sound_only_opening": True,
                        "sound_only_coda": True,
                        "tail_after_last_spoken_chars": 420,
                    },
                    "expected_duration_sec": {"min": 20, "max": 60},
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_pre_generation_gate_rejects_incomplete_spoken_input(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section = self.make_gate_fixture(Path(temp_dir), "Students huddled against the")
            report = workflow_input_gate.evaluate(section)
        self.assertEqual(report["status"], "fail")
        self.assertIn("incomplete_narrator_line", report["chunks"][0]["failures"])

    def test_pre_generation_gate_accepts_complete_covered_input(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section = self.make_gate_fixture(Path(temp_dir), "Students huddled against the walls.")
            report = workflow_input_gate.evaluate(section)
        self.assertEqual(report["status"], "pass")
        self.assertFalse(report["policy"]["provider_calls_allowed_on_fail"])

    def test_pre_generation_gate_rejects_unsupported_specific_speaker(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section = self.make_gate_fixture(Path(temp_dir), "Students huddled against the walls.")
            (section / "03_scene_parse.json").write_text(
                json.dumps(
                    {
                        "parsed_source_units": [
                            {
                                "source_unit_id": "s0001",
                                "source_kind": "quoted_text",
                                "speaker": "Ginny Weasley",
                                "speaker_confidence": "medium",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report = workflow_input_gate.evaluate(section)
        self.assertEqual(report["status"], "fail")
        self.assertIn("unsupported_specific_speaker_attribution", report["section_failures"])
        self.assertEqual(report["unsupported_attribution_unit_ids"], ["s0001"])

    def test_pre_generation_gate_warns_when_only_repair_reserve_is_reduced(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section = self.make_gate_fixture(Path(temp_dir), "Students huddled against the walls.")
            request_path = section / "06_generation_requests/chunk_001.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            padding = " quiet" * 500
            request["text_prompt"] += padding[: workflow_input_gate.BASE_PROMPT_BUDGET - len(request["text_prompt"]) + 20]
            request_path.write_text(json.dumps(request), encoding="utf-8")
            report = workflow_input_gate.evaluate(section)
        self.assertEqual(report["status"], "pass")
        self.assertIn("reduced_repair_reserve", report["chunks"][0]["warnings"])

    def test_pre_generation_gate_rejects_unsafe_duration_ceiling(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section = self.make_gate_fixture(Path(temp_dir), "Students huddled against the walls.")
            request_path = section / "06_generation_requests/chunk_001.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["input_metrics"]["audio_drama_estimated_duration_ceiling_sec"] = 70.4
            request_path.write_text(json.dumps(request), encoding="utf-8")
            report = workflow_input_gate.evaluate(section)
        self.assertEqual(report["status"], "fail")
        self.assertIn("estimated_duration_exceeds_safe_delivery_window", report["chunks"][0]["failures"])

    def test_pre_generation_gate_does_not_enforce_fixed_sound_quotas(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section = self.make_gate_fixture(Path(temp_dir), "Students huddled against the walls.")
            request_path = section / "06_generation_requests/chunk_001.json"
            request = json.loads(request_path.read_text(encoding="utf-8"))
            request["input_metrics"].update(
                {
                    "sfx_cue_count": 0,
                    "music_reaction_lines": 0,
                    "sound_only_opening": False,
                    "sound_only_coda": False,
                    "tail_after_last_spoken_chars": 20,
                }
            )
            request_path.write_text(json.dumps(request), encoding="utf-8")
            report = workflow_input_gate.evaluate(section)
        self.assertEqual(report["status"], "pass")

    def test_pre_generation_gate_rejects_movable_setup_left_before_dialogue_opener(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section = self.make_gate_fixture(Path(temp_dir), "Students huddled against the walls.")
            source_units = [
                {"source_unit_id": "s0001", "source_kind": "narrative_text", "source_text": "Night fell."},
                {"source_unit_id": "s0002", "source_kind": "narrative_text", "source_text": "Harry reached the door."},
                {"source_unit_id": "s0003", "source_kind": "quoted_text", "source_text": "Who is there?"},
            ]
            (section / "02_source_units.json").write_text(json.dumps(source_units), encoding="utf-8")
            (section / "03_scene_parse.json").write_text(
                json.dumps(
                    {
                        "parsed_source_units": [
                            {**source_units[0], "speaker": "Narrator", "speaker_confidence": "high"},
                            {**source_units[1], "speaker": "Narrator", "speaker_confidence": "high"},
                            {**source_units[2], "speaker": "harry", "speaker_confidence": "high", "quote_attribution_text": "asked Harry"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (section / "04_voice_registry.json").write_text(
                json.dumps({"voices": [{"role": "Narrator", "speaker": "voice-a"}, {"role": "harry", "speaker": "voice-b"}]}),
                encoding="utf-8",
            )
            first_path = section / "06_generation_requests/chunk_001.json"
            first = json.loads(first_path.read_text(encoding="utf-8"))
            first["source_unit_ids"] = ["s0001", "s0002"]
            first_path.write_text(json.dumps(first), encoding="utf-8")
            second = dict(first)
            second["chunk_id"] = "chunk_002"
            second["source_unit_ids"] = ["s0003"]
            second["active_roles"] = ["harry"]
            second["text_prompt"] = first["text_prompt"].replace("Narrator", "Harry")
            second["input_metrics"] = {**first["input_metrics"], "narrator_unit_count": 0, "dialogue_unit_count": 1}
            (section / "06_generation_requests/chunk_002.json").write_text(json.dumps(second), encoding="utf-8")
            report = workflow_input_gate.evaluate(section)
        chunk = next(item for item in report["chunks"] if item["chunk_id"] == "chunk_002")
        self.assertIn("dialogue_opening_missing_narrative_setup", chunk["failures"])

    def test_pilot_selection_covers_three_distinct_complexities(self):
        state = {
            "sections": [
                {
                    "chunks": [
                        {"chunk_key": "s1/narration", "input_metrics": {"narrator_unit_count": 9, "dialogue_unit_count": 0, "source_sfx_cue_count": 1}},
                        {"chunk_key": "s1/dialogue", "input_metrics": {"narrator_unit_count": 1, "dialogue_unit_count": 8, "source_sfx_cue_count": 0}},
                        {"chunk_key": "s1/action", "input_metrics": {"narrator_unit_count": 2, "dialogue_unit_count": 2, "source_sfx_cue_count": 12}},
                    ]
                }
            ]
        }
        self.assertEqual(
            audio_drama_skill.select_pilots(state),
            ["s1/narration", "s1/dialogue", "s1/action"],
        )

    def test_reconcile_changes_stale_running_state_to_interrupted(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            state = {"status": "running", "current_stage": "generate_chunk"}
            audio_drama_skill.save_state(run_dir, state)
            reconciled = audio_drama_skill.reconcile(run_dir, state)
        self.assertEqual(reconciled["status"], "interrupted")

    def test_live_lease_prevents_duplicate_runner(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            audio_drama_skill.shared.atomic_json(
                audio_drama_skill.lease_path(run_dir),
                {"pid": os.getpid(), "heartbeat_at": "now"},
            )
            with self.assertRaises(SystemExit):
                with audio_drama_skill.run_lease(run_dir, {}):
                    pass

    def test_second_chunk_failure_routes_to_repair_audio(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section_dir = Path(temp_dir)
            (section_dir / "06_generation_requests").mkdir()
            (section_dir / "06_generation_requests/chunk_001.json").write_text("{}", encoding="utf-8")
            failed_review = {
                "gate_status": "fail",
                "gate_failed_chunk_ids": ["chunk_001"],
                "chunks": [
                    {
                        "chunk_id": "chunk_001",
                        "status": "fail",
                        "summary": "spoken ending remains clipped",
                        "repair_instruction": "complete the ending",
                    }
                ],
            }
            with patch.object(chunk_worker.workflow, "generate_reference_audio"):
                with patch.object(chunk_worker.workflow, "generate_scene_audio") as generate:
                    with patch.object(chunk_worker, "technical_gate", return_value={"status": "pass"}):
                        with patch.object(chunk_worker, "review_once", side_effect=[failed_review, failed_review]):
                            code, report = chunk_worker.generate_chunk(section_dir, "chunk_001", "balanced")
            self.assertEqual(code, 3)
            self.assertEqual(report["status"], "repair_audio")
            self.assertEqual(generate.call_count, 2)

    def test_tail_only_failure_uses_local_finish_before_replan(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section_dir = Path(temp_dir)
            (section_dir / "06_generation_requests").mkdir()
            (section_dir / "06_generation_requests/chunk_001.json").write_text("{}", encoding="utf-8")
            failed_review = {
                "gate_status": "fail",
                "gate_failed_chunk_ids": ["chunk_001"],
                "chunks": [{
                    "chunk_id": "chunk_001",
                    "status": "fail",
                    "issues": [{
                        "type": "hard_boundary_or_abrupt_cut",
                        "severity": "major",
                        "evidence": "The closing background score ends with a hard cut after all speech is complete.",
                    }],
                    "summary": "closing score needs a natural fade",
                    "repair_instruction": "Apply a gentle fade to the completed score.",
                }],
            }
            passed_review = {
                "gate_status": "pass",
                "gate_failed_chunk_ids": [],
                "chunks": [{"chunk_id": "chunk_001", "status": "pass", "issues": []}],
            }
            with patch.object(chunk_worker.workflow, "generate_reference_audio"):
                with patch.object(chunk_worker.workflow, "generate_scene_audio") as generate:
                    with patch.object(chunk_worker, "technical_gate", return_value={"status": "pass"}):
                        with patch.object(chunk_worker, "review_once", side_effect=[failed_review, failed_review, passed_review]):
                            with patch.object(chunk_worker.workflow, "repair_delivery_tail", return_value={"status": "applied"}) as local_repair:
                                code, report = chunk_worker.generate_chunk(section_dir, "chunk_001", "balanced")
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(len(report["review_attempts"]), 3)
        local_repair.assert_called_once()

    def test_chapter_two_score_cut_is_tail_only_despite_dialogue_wording(self):
        report = {
            "gate_status": "fail",
            "chunks": [{
                "chunk_id": "chunk_001",
                "issues": [{
                    "type": "hard_boundary_or_abrupt_cut",
                    "severity": "minor",
                    "evidence": (
                        "The chunk ends with an abrupt hard cut to silence immediately after the final dialogue line, "
                        "truncating the natural trailing fade of the background score."
                    ),
                }],
                "summary": (
                    "All dialogue performances, audibility, pacing, and required background music meet specifications, "
                    "only the final chunk boundary has an abrupt hard cut issue."
                ),
                "repair_instruction": "Retain the full natural trailing fade of the background underscore.",
            }],
        }
        self.assertTrue(chunk_worker.is_tail_finish_only_failure(report, "chunk_001"))

    def test_clipped_dialogue_never_uses_local_tail_finish(self):
        report = {
            "gate_status": "fail",
            "chunks": [{
                "chunk_id": "chunk_001",
                "issues": [{
                    "type": "hard_boundary_or_abrupt_cut",
                    "severity": "major",
                    "evidence": "The final dialogue is clipped mid-word before the ambience cuts off.",
                }],
                "summary": "The sentence is incomplete.",
                "repair_instruction": "Regenerate the complete spoken sentence.",
            }],
        }
        self.assertFalse(chunk_worker.is_tail_finish_only_failure(report, "chunk_001"))

    def test_explicit_local_tail_recovery_archives_audio_and_review(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            section_dir = Path(temp_dir)
            (section_dir / "07_audio_parts").mkdir()
            (section_dir / "logs/chunk_delivery").mkdir(parents=True)
            (section_dir / "logs").mkdir(exist_ok=True)
            audio = section_dir / "07_audio_parts/chunk_001.wav"
            audio.write_bytes(b"RIFF-retained")
            retained = {
                "status": "repair_audio",
                "chunk_id": "chunk_001",
                "technical": {"status": "pass"},
                "performance": {
                    "gate_status": "fail",
                    "chunks": [{
                        "chunk_id": "chunk_001",
                        "issues": [{
                            "type": "hard_boundary_or_abrupt_cut",
                            "severity": "minor",
                            "evidence": "After all speech is complete, the background score ends with a hard cut.",
                        }],
                        "summary": "Only the closing score needs a natural fade.",
                        "repair_instruction": "Fade the completed score.",
                    }],
                },
                "local_tail_repair": None,
                "review_attempts": ["logs/performance_reviews/chunk_001/attempt_001_provider_repair"],
            }
            (section_dir / "logs/chunk_delivery/chunk_001.json").write_text(json.dumps(retained), encoding="utf-8")
            passed = {"gate_status": "pass", "gate_failed_chunk_ids": [], "chunks": [{"chunk_id": "chunk_001", "issues": []}]}
            with patch.object(chunk_worker.workflow, "repair_delivery_tail", return_value={"status": "applied"}):
                with patch.object(chunk_worker, "technical_gate", return_value={"status": "pass", "path": str(audio)}):
                    with patch.object(chunk_worker, "review_once", return_value=passed):
                        code, result = chunk_worker.recover_local_tail(section_dir, "chunk_001", "balanced")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(len(result["review_attempts"]), 2)
        self.assertIn("local-tail-recovery", result["recovery_archive"])

    def test_local_tail_repair_refuses_long_silence_after_missing_tail(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            audio = Path(temp_dir) / "chunk.wav"
            audio.write_bytes(b"retained")
            with patch.object(audiobook_workflow, "audio_decodes", return_value=True):
                with patch.object(audiobook_workflow, "audio_duration", return_value=39.24):
                    with patch.object(
                        audiobook_workflow,
                        "silence_intervals",
                        return_value=[{"start_sec": 30.35, "end_sec": 39.24, "duration_sec": 8.89}],
                    ):
                        with patch.object(audiobook_workflow, "run") as ffmpeg:
                            result = audiobook_workflow.repair_delivery_tail(audio)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "no_audible_tail_to_fade")
        ffmpeg.assert_not_called()

    def test_explicit_batch_replan_preserves_accepted_prefix_and_qa_feedback(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            section_dir = run_dir / "sections/section_003"
            (section_dir / "07_audio_parts").mkdir(parents=True)
            accepted_audio = section_dir / "07_audio_parts/chunk_001.wav"
            accepted_audio.write_bytes(b"accepted-audio")
            (section_dir / "02_source_units.json").write_text(
                json.dumps([
                    {"source_unit_id": "s0001", "source_kind": "narrative_text", "source_text": "Accepted opening."},
                    {
                        "source_unit_id": "s0002", "source_kind": "quoted_text", "source_text": "Failed middle.",
                        "quote_span_id": "p01_q01", "source_span": {"paragraph_id": "p01", "char_start": 10, "char_end": 24},
                    },
                    {"source_unit_id": "s0003", "source_kind": "narrative_text", "source_text": "Remaining ending."},
                ]),
                encoding="utf-8",
            )
            (section_dir / "06_generation_requests").mkdir()
            (section_dir / "06_generation_requests/chunk_001.json").write_text(
                json.dumps({"chunk_id": "chunk_001", "source_unit_ids": ["s0001"]}), encoding="utf-8"
            )
            (section_dir / "06_generation_requests/chunk_002.json").write_text(
                json.dumps({"chunk_id": "chunk_002", "source_unit_ids": ["s0002", "s0003"]}), encoding="utf-8"
            )
            config_path = run_dir / "inputs/section_003/story_config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(
                json.dumps({"source_excerpt": "Accepted opening. Failed middle. Remaining ending."}), encoding="utf-8"
            )
            state = {
                "status": "needs_replan",
                "sections": [
                    {
                        "section_id": "section_003",
                        "status": "needs_replan",
                        "work_dir": "sections/section_003",
                        "story_config": "inputs/section_003/story_config.json",
                        "chunks": [
                            {
                                "chunk_id": "chunk_001",
                                "chunk_key": "section_003/chunk_001",
                                "status": "accepted",
                                "audio": str(accepted_audio),
                            },
                            {
                                "chunk_id": "chunk_002",
                                "chunk_key": "section_003/chunk_002",
                                "status": "needs_replan",
                                "replan_feedback": {"summary": "final narration was clipped"},
                            },
                        ],
                        "final_audio": "old.wav",
                        "last_error": "clipped",
                    }
                ],
                "pilot": {"approved": True, "selected_chunk_keys": ["section_003/chunk_002"]},
            }
            audio_drama_skill.replan_section(run_dir, state, "section_003")
            archives = list((run_dir / "history/replan").glob("*_section_003"))
            updated_config = json.loads(config_path.read_text(encoding="utf-8"))
            preserved_audio_exists = Path(state["sections"][0]["preserved_chunks"][0]["audio"]).exists()
        self.assertEqual(len(archives), 1)
        self.assertEqual(state["sections"][0]["status"], "planned")
        self.assertEqual(state["sections"][0]["chunks"], [])
        self.assertEqual(len(state["sections"][0]["preserved_chunks"]), 1)
        self.assertEqual(state["sections"][0]["preserved_chunks"][0]["source_unit_ids"], ["s0001"])
        self.assertTrue(preserved_audio_exists)
        self.assertEqual(updated_config["replan_feedback"]["failure"]["summary"], "final narration was clipped")
        self.assertEqual(updated_config["source_excerpt"], "Failed middle.\nRemaining ending.")
        self.assertEqual([unit["source_unit_id"] for unit in updated_config["source_units_override"]], ["s0002", "s0003"])
        self.assertEqual(updated_config["source_units_override"][0]["source_kind"], "quoted_text")
        self.assertEqual(updated_config["source_units_override"][0]["quote_span_id"], "p01_q01")
        self.assertEqual(updated_config["source_units_override"][0]["source_span"]["char_start"], 10)
        self.assertTrue(state["pilot"]["approved"])

    def test_full_section_replan_discards_prefix_only_when_explicit(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "sections/section_003").mkdir(parents=True)
            state = {
                "status": "needs_replan",
                "sections": [{"section_id": "section_003", "status": "needs_replan", "work_dir": "sections/section_003", "chunks": []}],
                "pilot": {"approved": True},
            }
            audio_drama_skill.replan_section(run_dir, state, "section_003", full_section=True)
        self.assertEqual(state["sections"][0].get("preserved_chunks", []), [])

    def test_second_replan_recovers_original_source_provenance_from_history(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            section_dir = run_dir / "sections/section_001"
            (section_dir / "06_generation_requests").mkdir(parents=True)
            (section_dir / "02_source_units.json").write_text(
                json.dumps([{"source_unit_id": "s0001", "source_kind": "narrative_text", "source_text": "What?"}]),
                encoding="utf-8",
            )
            (section_dir / "06_generation_requests/chunk_001.json").write_text(
                json.dumps({"chunk_id": "chunk_001", "source_unit_ids": ["s0001"]}), encoding="utf-8"
            )
            original_archive = run_dir / "history/replan/first_section_001"
            original_archive.mkdir(parents=True)
            original_units = [
                {
                    "source_unit_id": "s0026", "source_kind": "quoted_text", "source_text": "What?",
                    "quote_span_id": "p01_q05", "source_span": {"paragraph_id": "p01", "char_start": 100, "char_end": 105},
                },
                {
                    "source_unit_id": "s0027", "source_kind": "narrative_text", "source_text": "Harry groaned.",
                    "source_span": {"paragraph_id": "p01", "char_start": 107, "char_end": 121},
                },
            ]
            (original_archive / "02_source_units.json").write_text(json.dumps(original_units), encoding="utf-8")
            preserved_audio = original_archive / "preserved.wav"
            preserved_audio.write_bytes(b"audio")
            config_path = run_dir / "inputs/section_001/story_config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"source_excerpt": "What? Harry groaned."}), encoding="utf-8")
            state = {
                "status": "needs_replan",
                "sections": [{
                    "section_id": "section_001",
                    "status": "needs_replan",
                    "work_dir": "sections/section_001",
                    "story_config": "inputs/section_001/story_config.json",
                    "replan_scope": {"first_failed_source_unit_id": "s0026"},
                    "chunks": [
                        {
                            "chunk_id": "preserved_001", "chunk_key": "section_001/preserved_001",
                            "status": "accepted", "audio": str(preserved_audio), "source_unit_ids": ["s0006"],
                        },
                        {
                            "chunk_id": "chunk_001", "chunk_key": "section_001/chunk_001",
                            "status": "needs_replan", "replan_feedback": {"summary": "tail cut"},
                        },
                    ],
                }],
                "pilot": {"approved": False, "selected_chunk_keys": ["section_001/chunk_001"]},
            }
            audio_drama_skill.replan_section(run_dir, state, "section_001")
            updated = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual([unit["source_unit_id"] for unit in updated["source_units_override"]], ["s0026", "s0027"])
        self.assertEqual(updated["source_units_override"][0]["source_kind"], "quoted_text")
        self.assertEqual(updated["source_units_override"][0]["quote_span_id"], "p01_q05")
        self.assertEqual(updated["source_units_override"][0]["source_span"]["char_start"], 100)

    def test_pilot_failure_auto_replans_once_with_qa_feedback(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            section_dir = run_dir / "sections/section_001"
            section_dir.mkdir(parents=True)
            (section_dir / "failed.wav").write_bytes(b"audio")
            config_path = run_dir / "inputs/section_001/story_config.json"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(json.dumps({"auto_planning": True}), encoding="utf-8")
            section = {
                "section_id": "section_001",
                "status": "needs_replan",
                "work_dir": "sections/section_001",
                "story_config": "inputs/section_001/story_config.json",
                "chunks": [],
            }
            chunk = {
                "chunk_id": "chunk_003",
                "chunk_key": "section_001/chunk_003",
                "last_error": "final spell was clipped",
                "replan_feedback": {"summary": "final spell was clipped"},
            }
            state = {
                "status": "needs_replan",
                "sections": [section],
                "pilot": {
                    "approved": False,
                    "selected_chunk_keys": [chunk["chunk_key"]],
                    "auto_replan_limit_per_section": 1,
                    "auto_replan_counts": {},
                },
            }
            first = audio_drama_skill.auto_replan_pilot_section(run_dir, state, section, chunk)
            updated_config = json.loads(config_path.read_text(encoding="utf-8"))
            second = audio_drama_skill.auto_replan_pilot_section(run_dir, state, section, chunk)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(state["pilot"]["auto_replan_counts"]["section_001"], 1)
        self.assertEqual(updated_config["replan_feedback"]["failure"]["summary"], "final spell was clipped")
        self.assertEqual(section["status"], "planned")

    def test_resume_enters_pending_pilot_replan(self):
        state = {
            "pilot": {"approved": False},
            "sections": [
                {
                    "section_id": "section_001",
                    "status": "needs_replan",
                    "chunks": [
                        {
                            "chunk_id": "chunk_003",
                            "status": "needs_replan",
                            "replan_feedback": {"stage": "pilot"},
                        }
                    ],
                }
            ],
        }
        with patch.object(audio_drama_skill, "auto_replan_pilot_section", return_value=True) as auto_replan:
            resumed = audio_drama_skill.resume_pending_pilot_replan(Path("/tmp/run"), state)
        self.assertTrue(resumed)
        auto_replan.assert_called_once()

    def test_planner_failure_never_consumes_pilot_replan(self):
        state = {
            "pilot": {"approved": False},
            "sections": [
                {
                    "section_id": "section_001",
                    "status": "needs_replan",
                    "replan_feedback": {"stage": "planner_validation"},
                    "chunks": [
                        {
                            "chunk_id": "chunk_003",
                            "status": "needs_replan",
                            "replan_feedback": {"stage": "planner_validation"},
                        }
                    ],
                }
            ],
        }
        with patch.object(audio_drama_skill, "auto_replan_pilot_section") as auto_replan:
            resumed = audio_drama_skill.resume_pending_pilot_replan(Path("/tmp/run"), state)
        self.assertFalse(resumed)
        auto_replan.assert_not_called()

    def test_legacy_selected_pilot_failure_without_stage_can_resume(self):
        chunk = {
            "chunk_id": "chunk_003",
            "chunk_key": "section_001/chunk_003",
            "status": "needs_replan",
            "replan_feedback": {"summary": "clipped word"},
        }
        state = {
            "pilot": {"approved": False, "selected_chunk_keys": [chunk["chunk_key"]]},
            "sections": [{"section_id": "section_001", "status": "needs_replan", "chunks": [chunk]}],
        }
        with patch.object(audio_drama_skill, "auto_replan_pilot_section", return_value=True) as auto_replan:
            resumed = audio_drama_skill.resume_pending_pilot_replan(Path("/tmp/run"), state)
        self.assertTrue(resumed)
        auto_replan.assert_called_once()

    def test_static_gate_failure_becomes_replannable_pilot_state(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "inputs/section_001").mkdir(parents=True)
            (run_dir / "inputs/section_001/story_config.json").write_text("{}", encoding="utf-8")
            section = {
                "section_id": "section_001",
                "status": "planned",
                "story_config": "inputs/section_001/story_config.json",
                "work_dir": "sections/section_001",
                "chunks": [],
            }
            state = {"status": "running", "sections": [section]}
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            gate = {
                "status": "fail",
                "failed_chunk_ids": ["chunk_004"],
                "section_failures": [],
                "chunks": [{"chunk_id": "chunk_004", "status": "fail", "failures": ["missing_timeline_music_reaction"]}],
            }
            with patch.object(audio_drama_skill, "heartbeat"):
                with patch.object(audio_drama_skill, "run_subprocess", return_value=completed):
                    with patch.object(workflow_input_gate, "evaluate", return_value=gate):
                        prepared = audio_drama_skill.prepare_section(run_dir, state, section)
        self.assertFalse(prepared)
        self.assertEqual(section["chunks"][0]["status"], "needs_replan")
        self.assertEqual(section["replan_feedback"]["stage"], "static_input_gate")

    def test_planner_voice_conflict_is_replan_not_transport_block(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "inputs/section_001").mkdir(parents=True)
            (run_dir / "inputs/section_001/story_config.json").write_text("{}", encoding="utf-8")
            section = {
                "section_id": "section_001",
                "status": "planned",
                "story_config": "inputs/section_001/story_config.json",
                "work_dir": "sections/section_001",
                "chunks": [],
            }
            state = {"status": "running", "sections": [section]}
            completed = type(
                "Completed",
                (),
                {
                    "returncode": 3,
                    "stdout": "",
                    "stderr": "chunk_003 has multiple active roles using the same speaker id",
                },
            )()
            with patch.object(audio_drama_skill, "heartbeat"):
                with patch.object(audio_drama_skill, "run_subprocess", return_value=completed):
                    prepared = audio_drama_skill.prepare_section(run_dir, state, section)
        self.assertFalse(prepared)
        self.assertEqual(state["status"], "needs_replan")
        self.assertEqual(section["replan_feedback"]["stage"], "planner_validation")

    def test_cached_static_gate_retry_runs_only_once(self):
        section = {
            "section_id": "section_001",
            "status": "needs_replan",
            "chunks": [],
            "replan_feedback": {"stage": "static_input_gate"},
        }
        state = {"sections": [section]}
        with patch.object(audio_drama_skill, "save_state"):
            with patch.object(audio_drama_skill, "append_event"):
                with patch.object(audio_drama_skill, "prepare_section", return_value=True) as prepare:
                    first = audio_drama_skill.retry_cached_static_gate_once(Path("/tmp/run"), state)
                    second = audio_drama_skill.retry_cached_static_gate_once(Path("/tmp/run"), state)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(
            section["cached_static_gate_retry_version"],
            audio_drama_skill.CACHED_STATIC_GATE_RETRY_VERSION,
        )
        prepare.assert_called_once()

    def test_cached_static_gate_retry_upgrades_legacy_boolean_marker(self):
        section = {
            "section_id": "section_001",
            "status": "needs_replan",
            "chunks": [],
            "replan_feedback": {"stage": "static_input_gate"},
            "cached_static_gate_retry_used": True,
        }
        state = {"sections": [section]}
        with patch.object(audio_drama_skill, "save_state"):
            with patch.object(audio_drama_skill, "append_event"):
                with patch.object(audio_drama_skill, "prepare_section", return_value=True) as prepare:
                    attempted = audio_drama_skill.retry_cached_static_gate_once(Path("/tmp/run"), state)
        self.assertTrue(attempted)
        self.assertEqual(
            section["cached_static_gate_retry_version"],
            audio_drama_skill.CACHED_STATIC_GATE_RETRY_VERSION,
        )
        prepare.assert_called_once()

    def test_cached_planner_validation_retry_is_local_and_versioned(self):
        section = {
            "section_id": "section_002",
            "status": "failed",
            "chunks": [],
            "last_error": "Quoted dialogue assigned without verifiable adjacent source evidence",
        }
        state = {"sections": [section]}
        with patch.object(audio_drama_skill, "save_state"):
            with patch.object(audio_drama_skill, "append_event"):
                with patch.object(audio_drama_skill, "prepare_section", return_value=True) as prepare:
                    attempted = audio_drama_skill.retry_cached_static_gate_once(Path("/tmp/run"), state)
        self.assertTrue(attempted)
        self.assertEqual(section["replan_feedback"]["stage"], "cached_planner_validation")
        prepare.assert_called_once_with(
            Path("/tmp/run"), state, section, cached_rewrite_only=True
        )

    def test_prepare_immediately_revalidates_deterministic_failure_cached_only(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "inputs/section_001").mkdir(parents=True)
            (run_dir / "inputs/section_001/story_config.json").write_text("{}", encoding="utf-8")
            section_dir = run_dir / "sections/section_001"
            (section_dir / "logs").mkdir(parents=True)
            (section_dir / "logs/seed2_rewrite_response.txt").write_text("{}", encoding="utf-8")
            section = {
                "section_id": "section_001",
                "status": "planned",
                "story_config": "inputs/section_001/story_config.json",
                "work_dir": "sections/section_001",
                "chunks": [],
            }
            state = {"status": "running", "sections": [section]}
            failed = type(
                "Completed",
                (),
                {
                    "returncode": 3,
                    "stdout": "",
                    "stderr": "Quoted dialogue assigned without verifiable adjacent source evidence",
                },
            )()
            with patch.object(audio_drama_skill, "heartbeat"):
                with patch.object(audio_drama_skill, "save_state"):
                    with patch.object(audio_drama_skill, "append_event"):
                        with patch.object(audio_drama_skill, "run_subprocess", side_effect=[failed, failed]) as runner:
                            prepared = audio_drama_skill.prepare_section(run_dir, state, section)
        self.assertFalse(prepared)
        self.assertEqual(runner.call_count, 2)
        self.assertIsNone(runner.call_args_list[0].kwargs["env"])
        self.assertEqual(runner.call_args_list[1].kwargs["env"]["SEED_REWRITE_CACHED_ONLY"], "1")

    def test_provider_transport_failure_remains_resumable_not_replan(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "inputs/section_001").mkdir(parents=True)
            (run_dir / "inputs/section_001/story_config.json").write_text("{}", encoding="utf-8")
            section = {
                "section_id": "section_001",
                "status": "prepared",
                "work_dir": "sections/section_001",
                "story_config": "inputs/section_001/story_config.json",
            }
            chunk = {
                "chunk_id": "chunk_001",
                "chunk_key": "section_001/chunk_001",
                "status": "input_validated",
                "attempts": 0,
                "last_error": None,
            }
            state = {
                "status": "running",
                "profile": {"performance_mode": "balanced"},
                "sections": [section],
            }
            failed = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "temporary network failure"})()
            with patch.object(audio_drama_skill, "heartbeat"):
                with patch.object(audio_drama_skill, "run_subprocess", return_value=failed):
                    accepted = audio_drama_skill.generate_chunk(run_dir, state, section, chunk)
        self.assertFalse(accepted)
        self.assertEqual(chunk["status"], "input_validated")
        self.assertEqual(state["status"], "blocked")
        self.assertNotEqual(section["status"], "needs_replan")

    def test_pilot_chunk_failure_persists_pilot_stage_before_audio_repair(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            section_dir = run_dir / "sections/section_001"
            (section_dir / "logs/chunk_delivery").mkdir(parents=True)
            (section_dir / "logs/chunk_delivery/chunk_001.json").write_text(
                json.dumps(
                    {
                        "status": "repair_audio",
                        "performance": {"chunks": [{"summary": "final word clipped"}]},
                        "technical": {},
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "inputs/section_001").mkdir(parents=True)
            (run_dir / "inputs/section_001/story_config.json").write_text("{}", encoding="utf-8")
            section = {
                "section_id": "section_001",
                "status": "prepared",
                "work_dir": "sections/section_001",
                "story_config": "inputs/section_001/story_config.json",
            }
            chunk = {
                "chunk_id": "chunk_001",
                "chunk_key": "section_001/chunk_001",
                "status": "input_validated",
                "attempts": 0,
                "last_error": None,
            }
            state = {"status": "running", "profile": {"performance_mode": "balanced"}, "sections": [section]}
            completed = type("Completed", (), {"returncode": 3, "stdout": "", "stderr": ""})()
            with patch.object(audio_drama_skill, "heartbeat"):
                with patch.object(audio_drama_skill, "save_state"):
                    with patch.object(audio_drama_skill, "append_event"):
                        with patch.object(audio_drama_skill, "run_subprocess", return_value=completed) as runner:
                            accepted = audio_drama_skill.generate_chunk(run_dir, state, section, chunk, phase="pilot")
        self.assertFalse(accepted)
        self.assertEqual(chunk["repair_feedback"]["stage"], "pilot")
        command = runner.call_args.args[0]
        self.assertEqual(command[command.index("--phase") + 1], "pilot")


if __name__ == "__main__":
    unittest.main()
