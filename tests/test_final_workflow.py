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
import chunk_worker  # noqa: E402
import workflow_input_gate  # noqa: E402


class FinalWorkflowTests(unittest.TestCase):
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
            + "\n".join(
                f'Narrator (actor is <<TGT_SPK1>>, natural): "{narrator_line}"' for _ in range(3)
            )
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

    def test_explicit_replan_archives_section_and_invalidates_pilot(self):
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
        self.assertFalse(state["pilot"]["approved"])

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
