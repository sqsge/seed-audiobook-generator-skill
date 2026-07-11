#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audiobook_workflow  # noqa: E402
import resumable_audio_drama  # noqa: E402


class QualityProfileTests(unittest.TestCase):
    def test_asr_is_skipped_by_default_profile(self):
        report = audiobook_workflow.asr_gate({"status": "fail"}, "off")
        self.assertEqual(report["gate_status"], "skipped")

    def test_balanced_profile_ignores_minor_diagnostic_issue(self):
        report = {
            "chunks": [
                {
                    "chunk_id": "chunk_001",
                    "status": "fail",
                    "issues": [{"type": "hard_boundary_or_abrupt_cut", "severity": "minor"}],
                }
            ],
            "unavailable_chunk_ids": [],
        }
        gated = audiobook_workflow.performance_gate(report, "balanced")
        self.assertEqual(gated["gate_status"], "pass")

    def test_balanced_profile_blocks_major_hard_cut(self):
        report = {
            "chunks": [
                {
                    "chunk_id": "chunk_001",
                    "status": "fail",
                    "issues": [{"type": "hard_boundary_or_abrupt_cut", "severity": "major"}],
                }
            ],
            "unavailable_chunk_ids": [],
        }
        gated = audiobook_workflow.performance_gate(report, "balanced")
        self.assertEqual(gated["gate_failed_chunk_ids"], ["chunk_001"])


class ResumeStateTests(unittest.TestCase):
    def base_state(self) -> dict:
        return {
            "run_id": "resume_test",
            "status": "planned",
            "profile": {"asr_mode": "off", "performance_mode": "balanced"},
            "limits": {"max_review_cycles": 2},
            "sections": [
                {
                    "section_id": "section_001",
                    "status": "planned",
                    "story_config": "inputs/section_001/story_config.json",
                    "work_dir": "sections/section_001",
                    "attempts": 0,
                    "review_cycles": 0,
                    "final_audio": None,
                    "last_error": None,
                }
            ],
            "final_audio": None,
        }

    def test_interruption_is_checkpointed_for_resume(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            state = self.base_state()
            (run_dir / "inputs/section_001").mkdir(parents=True)
            (run_dir / "inputs/section_001/story_config.json").write_text("{}", encoding="utf-8")
            with patch.object(resumable_audio_drama.subprocess, "run", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    resumable_audio_drama.run_sections(run_dir, state, generate=True)
            saved = resumable_audio_drama.read_json(run_dir / "run_state.json")
            self.assertEqual(saved["status"], "interrupted")
            self.assertEqual(saved["sections"][0]["status"], "interrupted")
            self.assertEqual(saved["current_section"], "section_001")

    def test_prepare_resume_skips_already_prepared_section(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            state = self.base_state()
            state["sections"][0]["status"] = "prepared"
            with patch.object(resumable_audio_drama.subprocess, "run") as run_mock:
                code = resumable_audio_drama.run_sections(run_dir, state, generate=False)
            self.assertEqual(code, 0)
            run_mock.assert_not_called()

    def test_review_reports_are_preserved_before_retry(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            section_dir = run_dir / "sections/section_001"
            report = section_dir / "logs/performance_review_report.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"gate_status": "fail"}), encoding="utf-8")
            resumable_audio_drama.preserve_review_checkpoint(run_dir, section_dir, "section_001", 2)
            snapshots = list((run_dir / "history/section_001").glob("attempt_002_*/logs/performance_review_report.json"))
            self.assertEqual(len(snapshots), 1)

    def test_initialize_with_registry_does_not_call_voice_planner(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source.txt"
            source.write_text("A bell rang. Alice opened the door and spoke.", encoding="utf-8")
            registry = temp / "voices.json"
            registry.write_text(
                json.dumps(
                    {
                        "roles": {
                            "Narrator": {
                                "default_speaker": "en_male_knightley_uranus_bigtts",
                                "reference_mode": "speaker",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            args = Namespace(
                run_id="local_init",
                source_file=str(source),
                source_title="Local Test",
                voice_registry=str(registry),
                target_chars=2400,
                max_chars=2800,
                asr_mode="off",
                performance_mode="balanced",
                max_review_cycles=2,
            )
            with patch.object(resumable_audio_drama, "RUN_ROOT", temp / "runs"):
                with patch.object(resumable_audio_drama, "infer_voice_registry") as planner:
                    run_dir, state = resumable_audio_drama.initialize(args)
            planner.assert_not_called()
            self.assertEqual(state["profile"]["asr_mode"], "off")
            self.assertTrue((run_dir / "run_state.json").exists())

    def test_existing_run_id_rejects_changed_source(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source.txt"
            source.write_text("First source snapshot.", encoding="utf-8")
            run_dir = temp / "runs/immutable_run"
            run_dir.mkdir(parents=True)
            resumable_audio_drama.atomic_json(
                run_dir / "run_state.json",
                {"run_id": "immutable_run", "source_sha256": "not-the-current-hash"},
            )
            args = Namespace(run_id="immutable_run", source_file=str(source))
            with patch.object(resumable_audio_drama, "RUN_ROOT", temp / "runs"):
                with self.assertRaises(SystemExit):
                    resumable_audio_drama.initialize(args)

    def test_review_cycle_limit_stops_without_provider_call(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            state = self.base_state()
            state["sections"][0]["status"] = "needs_review"
            state["sections"][0]["review_cycles"] = 2
            with patch.object(resumable_audio_drama.subprocess, "run") as run_mock:
                code = resumable_audio_drama.run_sections(run_dir, state, generate=True)
            self.assertEqual(code, 4)
            run_mock.assert_not_called()
            self.assertEqual(resumable_audio_drama.read_json(run_dir / "run_state.json")["current_stage"], "human_review_required")

    def test_explicit_extra_review_cycle_reaches_workflow(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            state = self.base_state()
            state["sections"][0]["status"] = "needs_review"
            state["sections"][0]["review_cycles"] = 2
            completed = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "test stop"})()
            with patch.object(resumable_audio_drama.subprocess, "run", return_value=completed) as run_mock:
                code = resumable_audio_drama.run_sections(
                    run_dir,
                    state,
                    generate=True,
                    allow_extra_review_cycle=True,
                )
            self.assertEqual(code, 1)
            run_mock.assert_called_once()

    def test_partial_section_uses_partial_resume_entrypoint(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            state = self.base_state()
            section_dir = run_dir / "sections/section_001"
            section_dir.mkdir(parents=True)
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(resumable_audio_drama.subprocess, "run", return_value=completed) as run_mock:
                code = resumable_audio_drama.run_sections(run_dir, state, generate=False)
            self.assertEqual(code, 0)
            command = run_mock.call_args.args[0]
            self.assertIn("--resume-partial-run-id", command)
            self.assertNotIn("--run-id", command)

    def test_explicit_prompt_rebuild_uses_cached_partial_entrypoint_first(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            state = self.base_state()
            state["sections"][0]["status"] = "needs_review"
            failed = type("Completed", (), {"returncode": 1, "stdout": "", "stderr": "rebuild test stop"})()
            with patch.object(resumable_audio_drama.subprocess, "run", return_value=failed) as run_mock:
                code = resumable_audio_drama.run_sections(
                    run_dir,
                    state,
                    generate=True,
                    rebuild_director_prompts=True,
                )
            self.assertEqual(code, 1)
            command = run_mock.call_args.args[0]
            self.assertIn("--resume-partial-run-id", command)
            self.assertNotIn("--generate", command)


if __name__ == "__main__":
    unittest.main()
