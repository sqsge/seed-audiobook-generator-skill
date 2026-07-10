#!/usr/bin/env python3
import json
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
