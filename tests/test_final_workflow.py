#!/usr/bin/env python3
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

            def fake_run(command, _cwd):
                output = Path(command[command.index("--out") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"RIFFdry-voice-sample")
                return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch.object(audio_drama_skill, "run_subprocess", side_effect=fake_run):
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
                                "speaker_confidence": "low",
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

    def test_second_chunk_failure_routes_to_needs_replan(self):
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
            self.assertEqual(report["status"], "needs_replan")
            self.assertEqual(generate.call_count, 2)

    def test_explicit_batch_replan_archives_section_and_preserves_pilot_approval(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            section_dir = run_dir / "sections/section_003"
            section_dir.mkdir(parents=True)
            (section_dir / "retained.wav").write_bytes(b"audio")
            state = {
                "status": "needs_replan",
                "sections": [
                    {
                        "section_id": "section_003",
                        "status": "needs_replan",
                        "work_dir": "sections/section_003",
                        "chunks": [{"status": "needs_replan"}],
                        "final_audio": "old.wav",
                        "last_error": "clipped",
                    }
                ],
                "pilot": {"approved": True, "selected_chunk_keys": ["section_003/chunk_002"]},
            }
            audio_drama_skill.replan_section(run_dir, state, "section_003")
            archives = list((run_dir / "history/replan").glob("*_section_003/retained.wav"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(state["sections"][0]["status"], "planned")
        self.assertEqual(state["sections"][0]["chunks"], [])
        self.assertTrue(state["pilot"]["approved"])

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
                    "chunks": [{"chunk_id": "chunk_003", "status": "needs_replan"}],
                }
            ],
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
        prepare.assert_called_once()

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


if __name__ == "__main__":
    unittest.main()
