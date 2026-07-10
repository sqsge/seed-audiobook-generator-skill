import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import audiobook_workflow  # noqa: E402
import llm_chat  # noqa: E402


class PerformanceQaRegressionTests(unittest.TestCase):
    def test_failed_performance_review_repairs_prompt_and_rate(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            run_dir = Path(temp_dir)
            (run_dir / "05_director_prompt_chunks").mkdir()
            (run_dir / "06_generation_requests").mkdir()
            prompt_path = run_dir / "05_director_prompt_chunks" / "chunk_001.txt"
            request_path = run_dir / "06_generation_requests" / "chunk_001.json"
            prompt_path.write_text("A complete line ends here.", encoding="utf-8")
            request_path.write_text(
                json.dumps({"audio_config": {"speech_rate": 6}, "text_prompt": "A complete line ends here."}),
                encoding="utf-8",
            )
            report = {
                "chunks": [
                    {
                        "chunk_id": "chunk_001",
                        "status": "fail",
                        "issues": [{"type": "rushed_delivery", "severity": "major"}],
                        "repair_focus": ["give the final line time to land"],
                    }
                ]
            }

            result = audiobook_workflow.apply_performance_repairs(run_dir, report)

            repaired = request_path.read_text(encoding="utf-8")
            self.assertEqual(result["repairs"][0]["chunk_id"], "chunk_001")
            self.assertIn("Performance repair", repaired)
            self.assertIn("complete each line", repaired)
            self.assertEqual(json.loads(repaired)["audio_config"]["speech_rate"], 0)

    def test_local_audio_is_serialized_as_input_audio(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=ROOT) as audio_file:
            audio_file.write(b"RIFFtest")
            audio_file.flush()
            messages = llm_chat._build_messages("review", None, [], audios=[audio_file.name])
            audio = messages[0]["content"][0]
            self.assertEqual(audio["type"], "input_audio")
            self.assertEqual(audio["input_audio"]["format"], "wav")

    def test_unavailable_reviewer_does_not_trigger_wasteful_regeneration(self):
        report = {
            "chunks": [
                {"chunk_id": "chunk_001", "status": "unavailable"},
                {"chunk_id": "chunk_002", "status": "fail"},
            ]
        }
        self.assertEqual(audiobook_workflow.failed_performance_chunk_ids(report), {"chunk_002"})


if __name__ == "__main__":
    unittest.main()
