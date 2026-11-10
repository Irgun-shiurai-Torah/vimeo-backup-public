"""Regression tests for three-hour, once-only Dropbox import behaviour."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import dropbox_import
import dropbox_import_ledger as ledger
import dropbox_incremental as incremental
import dropbox_over7 as over7


OLD_SOURCE = {
    "id": "dbx-bbe81b4e377b38e9",
    "label": "Reb Gamliel Rabinowitz",
    "url": "https://www.dropbox.com/scl/fo/abc/xyz?rlkey=example",
    "password": "",
}
OLD_ITEM = {
    "id": "id:tIbGmbOmXFAAAAAAAAAXQA",
    "path": "/new-path-after-rename/video.mp4",
    "name": "video.mp4",
    "size": 126609444,
    "kind": "video",
    "rev": "changed-source-revision",
    "modified": "2026-11-10T00:00:00Z",
}


class IncrementalDropboxTest(unittest.TestCase):
    def test_frozen_history_contains_exactly_2301_completed_recordings(self):
        ids = ledger.frozen_initial_ids()
        self.assertEqual(len(ids), 2301)
        self.assertEqual(sum(ledger.SOURCE_COUNTS.values()), 2301)
        self.assertIn("dbx-media-b10df8d9b922db4b49f24e64", ids)
        self.assertIn("dbx-media-11acefa7e785f1ec6531a7a1", ids)
        self.assertIn("dbx-media-eef7a3b6c39c8ae653b0de4f", ids)

    def test_baseline_is_fail_closed_if_missing_or_tampered(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_a_baseline = Path(tmp) / "baseline.json"
            not_a_baseline.write_text(json.dumps({
                "version": 1, "total": 2301, "sources": {}
            }))
            ledger.frozen_initial_ids.cache_clear()
            try:
                with patch.object(ledger, "BASELINE_PATH", not_a_baseline):
                    with self.assertRaisesRegex(RuntimeError, "missing or invalid"):
                        ledger.frozen_initial_ids()
            finally:
                ledger.frozen_initial_ids.cache_clear()

    def test_old_id_never_returns_even_if_drive_mapping_and_media_deleted(self):
        media_id = dropbox_import.file_id(OLD_SOURCE["id"], OLD_ITEM)
        self.assertEqual(media_id, "dbx-media-b10df8d9b922db4b49f24e64")
        self.assertTrue(ledger.already_imported(media_id, "video", None))
        self.assertTrue(ledger.already_imported(media_id, "video", {}))
        changed = dict(OLD_ITEM, path="/another-place/new-name.mp4",
                       rev="another-revision")
        self.assertEqual(dropbox_import.file_id(OLD_SOURCE["id"], changed), media_id)

        with patch.object(dropbox_import, "sources_from_env", return_value=[OLD_SOURCE]), \
             patch.object(dropbox_import, "DropboxLinks") as dbx, \
             patch.object(dropbox_import, "DriveStore") as drive:
            dbx.return_value.list_tree.return_value = (1, [changed])
            drive.return_value.folder.return_value = "private-folder"
            # Simulate all former Drive maps and media being manually deleted.
            drive.return_value.read_map.return_value = {"version": 1, "items": {}}
            dropbox_import.run(mode="import", source_label=OLD_SOURCE["label"],
                               max_files=3, max_source_bytes=2200000000,
                               reserve_gib=68)
            dbx.return_value.download.assert_not_called()
            drive.return_value.write_map.assert_not_called()

    def test_new_id_is_eligible_and_post_import_revision_changes_are_ignored(self):
        new_id = "dbx-media-" + "0" * 24
        self.assertNotIn(new_id, ledger.frozen_initial_ids())
        self.assertFalse(ledger.already_imported(new_id, "video", None))
        # Once a newer recording has a complete source map entry, deleting its
        # physical Drive media does not cause a duplicate import.
        completed = {"originalDriveId": "once-uploaded",
                     "hls": {"masterDriveId": "once-encoded"}, "rev": "old"}
        self.assertTrue(ledger.already_imported(new_id, "video", completed))
        self.assertTrue(ledger.already_imported(new_id, "audio",
                                                {"originalDriveId": "saved"}))
        self.assertFalse(ledger.already_imported(new_id, "video",
                                                 {"originalDriveId": "partial"}))

    def test_oversized_checks_all_three_sources_and_skips_frozen_history(self):
        self.assertEqual(set(over7.SOURCE_LABELS),
                         {"short-word", "leil-shishi", "gamliel"})
        old = dict(OLD_ITEM, size=8_000_000_000)
        self.assertEqual(over7.candidates(OLD_SOURCE, [old], {}), [])
        new = dict(old, id="id:brand-new-oversized-file",
                   path="/new-lecture-2026.mp4", rev="first")
        self.assertEqual(len(over7.candidates(OLD_SOURCE, [new], {})), 1)
        mid = dropbox_import.file_id(OLD_SOURCE["id"], new)
        done = {"originalDriveId": "uploaded",
                "hls": {"masterDriveId": "master"}, "rev": "earlier"}
        self.assertEqual(over7.candidates(OLD_SOURCE, [new], {mid: done}), [])
        huge = dict(new, id="id:brand-new-very-large", size=35_000_000_000)
        self.assertEqual(len(over7.candidates(OLD_SOURCE, [huge], {})), 1)
        # Above 20 GB is no longer ignored, but disk safeguards still block
        # an import that cannot fit before downloading the original.
        with patch.object(over7.shutil, "disk_usage",
                          return_value=SimpleNamespace(free=10 * 1024**3)):
            with self.assertRaisesRegex(RuntimeError, "Insufficient runner disk"):
                over7.ensure_disk(huge["size"])

    def test_only_one_three_hour_schedule_and_same_per_source_lock(self):
        main = Path(".github/workflows/dropbox-incremental-3h.yml").read_text()
        regular = Path(".github/workflows/dropbox-all-series.yml").read_text()
        large = Path(".github/workflows/dropbox-oversized.yml").read_text()
        self.assertIn("cron: '11 */3 * * *'", main)
        self.assertEqual(main.count("          - source_key:"), 3)
        self.assertIn("irgun-dropbox-source-${{ matrix.source_key }}", main)
        self.assertIn("max-parallel: 3", main)
        self.assertIn("cancel-in-progress: false", main)
        self.assertIn("timeout-minutes: 350", main)
        self.assertIn("python -u dropbox_incremental.py", main)
        self.assertNotIn("  schedule:", regular)
        self.assertNotIn("  schedule:", large)
        self.assertIn("workflow_dispatch:", regular)
        self.assertIn("workflow_dispatch:", large)
        self.assertNotIn("secrets.DROPBOX_SHARED_LINK", main)

    def test_scheduler_checks_over7_and_other_sizes_for_every_source(self):
        for source in incremental.SOURCE_LABELS:
            stages = incremental.stage_commands(source)
            self.assertEqual([x[0] for x in stages],
                             ["over-7GB", "up-to-2.2GB", "2.2-to-7GB"])
            self.assertIn("--confirm", stages[0][1])
            self.assertIn("IMPORT_OVER_7_GB", stages[0][1])
            self.assertIn("--max-source-bytes", stages[1][1])
            self.assertIn("2200000000", stages[1][1])
            self.assertIn("7000000000", stages[2][1])
            self.assertIn(incremental.SOURCE_LABELS[source], stages[2][1])

    def test_controller_handles_failed_tier_without_starving_others(self):
        with patch.object(incremental, "frozen_initial_ids",
                          return_value=ledger.frozen_initial_ids()), \
             patch.object(incremental.subprocess, "run",
                          side_effect=[SimpleNamespace(returncode=1),
                                       SimpleNamespace(returncode=0),
                                       SimpleNamespace(returncode=0)]) as call, \
             patch.object(incremental.time, "monotonic", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "over-7GB"):
                incremental.run("gamliel")
            self.assertEqual(call.call_count, 3)

    def test_controller_never_runs_without_frozen_history(self):
        with patch.object(incremental, "frozen_initial_ids",
                          side_effect=RuntimeError("missing history")), \
             patch.object(incremental.subprocess, "run") as call:
            with self.assertRaisesRegex(RuntimeError, "missing history"):
                incremental.run("gamliel")
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
