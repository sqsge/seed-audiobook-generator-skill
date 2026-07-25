#!/usr/bin/env python3
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audiobook_workflow as workflow  # noqa: E402
import audio_drama_skill as skill  # noqa: E402


class AdaptiveSilenceTests(unittest.TestCase):
    def test_natural_three_second_pause_does_not_block(self):
        report = workflow.adaptive_audio_signal_from_intervals(
            30.0,
            [{"start_sec": 10.0, "end_sec": 13.0, "duration_sec": 3.0}],
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["hard_reasons"], [])

    def test_long_tail_is_hard_failure_by_time_and_ratio(self):
        report = workflow.adaptive_audio_signal_from_intervals(
            30.0,
            [{"start_sec": 20.0, "end_sec": 30.0, "duration_sec": 10.0}],
        )
        self.assertEqual(report["status"], "fail")
        self.assertIn("excessive_trailing_silence", report["hard_reasons"])
        self.assertEqual(report["repair_class"], "local_tail_trim_then_reaudit")

    def test_repeated_long_internal_pauses_block(self):
        report = workflow.adaptive_audio_signal_from_intervals(
            40.0,
            [
                {"start_sec": 8.0, "end_sec": 13.0, "duration_sec": 5.0},
                {"start_sec": 22.0, "end_sec": 27.0, "duration_sec": 5.0},
            ],
        )
        self.assertIn("repeated_long_internal_silence", report["hard_reasons"])

    def test_sparse_long_pauses_in_one_hour_chapter_are_warnings_not_hard_failures(self):
        intervals = [
            {"start_sec": 300.0 * index, "end_sec": 300.0 * index + 5.0, "duration_sec": 5.0}
            for index in range(1, 7)
        ]
        report = workflow.adaptive_audio_signal_from_intervals(3600.0, intervals)
        self.assertNotIn("repeated_long_internal_silence", report["hard_reasons"])
        self.assertIn("long_internal_pause", report["warning_reasons"])

    def test_objective_failure_overrides_reviewer_pass(self):
        report = {
            "chunks": [{
                "chunk_id": "chunk_001",
                "status": "pass",
                "issues": [],
                "objective_audio": {"hard_reasons": ["excessive_trailing_silence"]},
            }],
            "unavailable_chunk_ids": [],
        }
        gated = workflow.performance_gate(report, "balanced", phase="batch")
        self.assertEqual(gated["gate_status"], "fail")
        self.assertEqual(gated["gate_failed_chunk_ids"], ["chunk_001"])

    def test_objective_core_gate_still_blocks_when_reviewer_is_off(self):
        report = {
            "chunks": [],
            "objective_audio_by_chunk": {
                "chunk_001": {"hard_reasons": ["excessive_trailing_silence"]},
            },
        }
        gated = workflow.performance_gate(report, "off", phase="batch")
        self.assertEqual(gated["gate_status"], "fail")
        self.assertEqual(gated["gate_failed_chunk_ids"], ["chunk_001"])

    def test_all_six_chapter_badcases_are_blocked(self):
        cases = json.loads((ROOT / "tests/fixtures/workflow_v2_badcases.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cases), 6)
        for case in cases:
            with self.subTest(case_id=case["case_id"]):
                report = workflow.adaptive_audio_signal_from_intervals(case["duration_sec"], case["intervals"])
                self.assertEqual(report["status"], "fail")
                self.assertIn(case["expected_reason"], report["hard_reasons"])


class PlanningCompilerTests(unittest.TestCase):
    def test_planner_model_defaults_to_new_candidate(self):
        self.assertEqual(workflow.REWRITE_MODEL, "dola-seed-2-1-turbo-260628")

    def test_short_chunk_gets_content_derived_duration_and_lint(self):
        prompt = 'Narrator (calm) narrates: "The door opened."\nBackground music remains audible.'
        contract = workflow.derive_render_plan_contract(
            prompt,
            {"music_bed": "low strings", "persistent_ambience": "room tone"},
            {"narrator_unit_count": 1, "dialogue_unit_count": 0, "soundscape_event_count": 1},
        )
        compiled, lint = workflow.compile_prompt_v2(prompt + "\nNo cadence after the voice.", contract)
        self.assertLess(contract["duration_range_sec"]["max"], 25)
        self.assertIn("no added spoken cadence", compiled.lower())
        self.assertIn("never extend the file with silence", compiled)
        self.assertEqual(lint["blockers"], [])


class OrchestrationV2Tests(unittest.TestCase):
    def test_selects_one_complexity_canary_per_section(self):
        state = {
            "sections": [
                {"chunks": [
                    {"chunk_key": "s1/c1", "input_metrics": {"soundscape_event_count": 1}},
                    {"chunk_key": "s1/c2", "input_metrics": {"soundscape_event_count": 4}},
                ]},
                {"chunks": [
                    {
                        "chunk_key": "s2/preserved",
                        "preserved_from_replan": True,
                        "input_metrics": {"dialogue_unit_count": 99},
                    },
                    {"chunk_key": "s2/c1", "input_metrics": {"dialogue_unit_count": 3}},
                ]},
            ]
        }
        self.assertEqual(skill.select_section_canaries(state), ["s1/c2", "s2/c1"])

    def test_batch_structural_failure_can_enter_automatic_replan(self):
        chunk = {
            "chunk_id": "chunk_002",
            "chunk_key": "section_001/chunk_002",
            "status": "needs_replan",
            "replan_feedback": {"stage": "batch", "summary": "spoken ending remains clipped"},
        }
        state = {
            "pilot": {"approved": True},
            "sections": [{"section_id": "section_001", "status": "needs_replan", "chunks": [chunk]}],
        }
        with patch.object(skill, "auto_replan_pilot_section", return_value=True) as auto_replan:
            resumed = skill.resume_pending_pilot_replan(Path("/tmp/run"), state)
        self.assertTrue(resumed)
        auto_replan.assert_called_once()

    def test_structural_replan_budget_allows_three_improving_rounds(self):
        section = {"section_id": "section_001", "replan_history": []}
        chunk = {"chunk_id": "chunk_001", "chunk_key": "section_001/chunk_001"}
        state = {
            "pilot": {"approved": False, "auto_replan_counts": {}},
            "replan_policy": {"max_structural_rounds_per_section": 3, "max_stagnant_rounds": 2},
        }

        def record_round(_run_dir, state_arg, _section_id, feedback, automatic):
            counts = state_arg["pilot"]["auto_replan_counts"]
            counts["section_001"] = counts.get("section_001", 0) + 1
            metrics = skill.failure_convergence_metrics(feedback)
            section["replan_history"].append({
                "failure_signature": skill.failure_signature(feedback),
                "failure_family": metrics["family"],
                "failure_score": metrics["score"],
            })

        with patch.object(skill, "replan_section", side_effect=record_round):
            with patch.object(skill, "save_state"), patch.object(skill, "append_event"):
                for tail in (12.0, 9.0, 6.0):
                    chunk["replan_feedback"] = {
                        "stage": "batch",
                        "summary": "long silence",
                        "performance": {
                            "objective_audio": {
                                "hard_reasons": ["excessive_trailing_silence"],
                                "trailing_silence_sec": tail,
                            }
                        },
                    }
                    self.assertTrue(skill.auto_replan_pilot_section(Path("/tmp/run"), state, section, chunk))
                self.assertFalse(skill.auto_replan_pilot_section(Path("/tmp/run"), state, section, chunk))
        self.assertEqual(state["pilot"]["auto_replan_counts"]["section_001"], 3)
        self.assertEqual(state["status"], "human_review_required")

    def test_two_non_improving_rounds_stop_before_another_plan(self):
        failure = {"stage": "pilot", "summary": "spoken ending remains clipped"}
        metrics = skill.failure_convergence_metrics(failure)
        section = {
            "section_id": "section_001",
            "stagnant_replan_rounds": 1,
            "replan_history": [{"failure_family": metrics["family"], "failure_score": metrics["score"]}],
        }
        chunk = {
            "chunk_id": "chunk_001",
            "chunk_key": "section_001/chunk_001",
            "replan_feedback": failure,
        }
        state = {
            "pilot": {"auto_replan_counts": {"section_001": 2}},
            "replan_policy": {"max_structural_rounds_per_section": 3, "max_stagnant_rounds": 2},
        }
        with patch.object(skill, "replan_section") as replan:
            with patch.object(skill, "save_state"), patch.object(skill, "append_event"):
                continued = skill.auto_replan_pilot_section(Path("/tmp/run"), state, section, chunk)
        self.assertFalse(continued)
        replan.assert_not_called()
        self.assertEqual(state["current_stage"], "structural_replan_not_converging")

    def test_identical_replanned_prompt_stops_before_audio(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            section_dir = run_dir / "sections/section_001"
            request_dir = section_dir / "06_generation_requests"
            request_dir.mkdir(parents=True)
            request = {
                "chunk_id": "chunk_001",
                "source_unit_ids": ["s0001"],
                "active_roles": ["Narrator"],
                "text_prompt": "Same complete prompt.",
                "expected_duration_sec": {"min": 8, "max": 12},
                "render_plan_contract": {"target_audible_duration_sec": 10},
                "input_metrics": {},
            }
            (request_dir / "chunk_001.json").write_text(json.dumps(request), encoding="utf-8")
            (section_dir / "manifest.json").write_text("{}", encoding="utf-8")
            (run_dir / "inputs/section_001").mkdir(parents=True)
            (run_dir / "inputs/section_001/story_config.json").write_text("{}", encoding="utf-8")
            fingerprint = skill.section_plan_fingerprint(section_dir)
            section = {
                "section_id": "section_001",
                "status": "planned",
                "work_dir": "sections/section_001",
                "story_config": "inputs/section_001/story_config.json",
                "chunks": [],
                "pending_replan_round": 1,
                "replan_history": [{"round": 1, "parent_plan_fingerprint": fingerprint}],
            }
            state = {"status": "running", "sections": [section]}
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(skill, "heartbeat"):
                with patch.object(skill, "run_subprocess", return_value=completed):
                    with patch.object(skill.workflow_input_gate, "evaluate", return_value={"status": "pass"}):
                        prepared = skill.prepare_section(run_dir, state, section)
        self.assertFalse(prepared)
        self.assertEqual(state["status"], "human_review_required")
        self.assertEqual(section["replan_history"][0]["status"], "rejected_no_plan_change")

    def test_final_chapter_audit_blocks_completed_state(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            section_audio = run_dir / "section.wav"
            final_audio = run_dir / "final.wav"
            section_audio.write_bytes(b"fixture")
            final_audio.write_bytes(b"fixture")
            state = {
                "run_id": "v2_final_audit",
                "status": "running",
                "sections": [{
                    "section_id": "section_001",
                    "status": "accepted",
                    "final_audio": str(section_audio),
                    "chunks": [{"chunk_key": "section_001/chunk_001", "status": "accepted"}],
                }],
            }
            hard = {"status": "fail", "hard_reasons": ["excessive_trailing_silence"]}
            with patch.object(skill, "stitch_ready_sections"):
                with patch.object(skill.chapter, "stitch_chapter", return_value=final_audio):
                    with patch.object(skill.workflow, "adaptive_audio_signal_report", return_value=hard):
                        completed = skill.run_batch(run_dir, state)
            self.assertFalse(completed)
            self.assertEqual(state["status"], "human_review_required")
            self.assertTrue((run_dir / "logs" / "final_chapter_audit.json").exists())


if __name__ == "__main__":
    unittest.main()
