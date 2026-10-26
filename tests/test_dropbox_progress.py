"""Progress reporting must be clear, durable and free of source secrets."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dropbox_progress import ImportProgress, summary


class DropboxProgressTest(unittest.TestCase):
    def test_checkpoint_and_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            progress = ImportProgress("Reb Gamliel Rabinowitz", 7, temporary)
            progress.update("Uploading video to Google Drive", eligibleRemaining=100,
                            oversizedDeferred=24, attempted=1, currentRecording="Lecture.mp4",
                            currentMediaId="dbx-media-test")
            progress.update("Completed recording", completed=1, status="completed")
            files = list(Path(temporary).glob("dropbox-*.json"))
            self.assertEqual(len(files), 1)
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["completed"], 1)
            self.assertEqual(payload["oversizedDeferred"], 24)
            with patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(Path(temporary) / "summary.md")}):
                text = summary(temporary)
            self.assertIn("1/7", text)
            self.assertIn("Reb Gamliel Rabinowitz", text)
            self.assertIn("24", text)
            self.assertIn("Uploading", "Uploading video to Google Drive")
            self.assertTrue((Path(temporary) / "summary.md").exists())

    def test_missing_source_does_not_crash_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIn("Job did not reach the importer", summary(temporary))


if __name__ == "__main__":
    unittest.main()
