"""Read-only and offline regressions for the separate Dropbox 7GB importer."""
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dropbox_import import run
from dropbox_large import (
    FIVE_GB, SEVEN_GB, GIB, InsufficientRunnerDisk,
    large_master_playlist, make_large_outputs, require_large_source_space,
    require_rendition_space, size_buckets, space_needed_for_large_source,
)
from dropbox_size_report import format_size_table


URL = "https://www.dropbox.com/scl/fo/abc/xyz?rlkey=example"
SOURCE = {
    "id": "dbx-" + "a" * 16,
    "url": URL,
    "label": "Reb Mielech - Leil Shishi",
    "password": "",
}


def media(name, size, index):
    return {"id": f"id:lecture-{index}", "path": f"/5786/{name}",
            "name": name, "size": size, "rev": "r1", "modified": "",
            "kind": "video"}


class DropboxLargeTest(unittest.TestCase):
    def test_large_import_schedules_five_per_source_sequentially(self):
        from pathlib import Path
        workflow = Path(".github/workflows/dropbox-oversized.yml").read_text(
            encoding="utf-8")
        self.assertIn("cron: '43 */2 * * *'", workflow)
        self.assertEqual(workflow.count("source_key:"), 3)
        self.assertIn("max-parallel: 3", workflow)
        self.assertIn("irgun-dropbox-source-${{ matrix.source_key }}", workflow)
        self.assertIn('--source-label "$SOURCE_LABEL" --max-files 5', workflow)
        self.assertIn("--min-source-bytes 2200000000", workflow)
        self.assertIn('LARGE_LIMIT_GB: ${{ inputs.max_gb || \'7\' }}', workflow)
        self.assertIn("--reserve-gib 68", workflow)
        self.assertIn("timeout-minutes: 350", workflow)

    def test_tier_counts_use_decimal_5_and_7_gb_thresholds(self):
        sizes = [1_000_000, 2_200_000_000, 2_200_000_001,
                 FIVE_GB, FIVE_GB + 1, SEVEN_GB, SEVEN_GB + 1]
        counts = size_buckets([(SOURCE, {"size": n}) for n in sizes])
        self.assertEqual(counts["normalUpTo2_2GB"], 2)
        self.assertEqual(counts["oversize2_2To5GB"], 2)
        self.assertEqual(counts["oversize5To7GB"], 2)
        self.assertEqual(counts["above5GB"], 3)
        self.assertEqual(counts["above7GB"], 1)
        self.assertEqual(counts["largestGB"], 7.0)

    def test_disk_preflight_does_not_allow_7gb_solely_because_14gb_total(self):
        required = space_needed_for_large_source(SEVEN_GB)
        self.assertGreater(required, SEVEN_GB + 2 * GIB)
        with patch("dropbox_large.shutil.disk_usage",
                   return_value=SimpleNamespace(free=required - 1)):
            with self.assertRaises(InsufficientRunnerDisk):
                require_large_source_space(SEVEN_GB)
        with patch("dropbox_large.shutil.disk_usage",
                   return_value=SimpleNamespace(free=required + GIB)):
            require_large_source_space(SEVEN_GB)

    def test_hls_rendition_disk_guard_checks_duration_and_bitrate(self):
        quality = {"height": 1080, "maxrate": 4000}
        with patch("dropbox_large.shutil.disk_usage",
                   return_value=SimpleNamespace(free=2 * GIB)):
            with self.assertRaises(InsufficientRunnerDisk):
                require_rendition_space(120 * 60, quality)
        with patch("dropbox_large.shutil.disk_usage",
                   return_value=SimpleNamespace(free=30 * GIB)):
            require_rendition_space(120 * 60, quality)

    def test_sequential_hls_retains_qualities_and_cleans_local_outputs(self):
        qualities = [
            {"height": 360, "width": 640, "bitrate": 550, "maxrate": 700},
            {"height": 720, "width": 1280, "bitrate": 1600, "maxrate": 2200},
        ]
        store = MagicMock()
        uploaded = {}
        def upload(path, parent, name, mime=None):
            uploaded[name] = Path(path).read_bytes()
            return "drive-" + name
        store.upload.side_effect = upload
        progress = MagicMock()
        def encode_audio(src, out):
            dest = Path(out) / "audio.mp3"
            dest.write_bytes(b"audio")
            return str(dest)
        def poster(src, out, duration):
            dest = Path(out) / "poster.jpg"
            dest.write_bytes(b"poster")
            return str(dest)
        called = []
        def make_one_hls(src, out, selections):
            self.assertEqual(len(selections), 1)
            q = selections[0]
            called.append(q["height"])
            h = q["height"]
            Path(out, f"{h}p.m3u8").write_text("#EXTM3U\n#EXT-X-BYTERANGE", encoding="utf-8")
            Path(out, f"{h}p.ts").write_bytes(b"transport")
            master = Path(out, "master.m3u8")
            master.write_text("one-quality temporary master")
            return str(master)
        with tempfile.TemporaryDirectory() as temp, \
             patch("dropbox_large.probe", return_value=(
                 {"width": 1920, "height": 1080}, True, 3600.0)), \
             patch("dropbox_large.quality_ladder", return_value=qualities), \
             patch("dropbox_large.make_mp3", side_effect=encode_audio), \
             patch("dropbox_large.make_poster", side_effect=poster), \
             patch("dropbox_large.make_hls", side_effect=make_one_hls), \
             patch("dropbox_large.require_rendition_space"):
            src = Path(temp) / "source.mts"
            src.write_bytes(b"video")
            result = make_large_outputs(store, "private-drive", media("video.mts", 12, 1),
                                        str(src), temp, progress)
            self.assertEqual(called, [360, 720])
            self.assertEqual(len(result["hls"]["renditions"]), 2)
            self.assertEqual(set(uploaded), {
                "audio.mp3", "poster.jpg", "360p.m3u8", "360p.ts",
                "720p.m3u8", "720p.ts", "master.m3u8"
            })
            self.assertNotIn(b"one-quality", uploaded["master.m3u8"])
            self.assertIn(b"360p.m3u8", uploaded["master.m3u8"])
            self.assertIn(b"720p.m3u8", uploaded["master.m3u8"])
            self.assertFalse((Path(temp) / "hls" / "360p.ts").exists())
            self.assertFalse((Path(temp) / "hls" / "720p.ts").exists())
            self.assertFalse((Path(temp) / "hls" / "audio.mp3").exists())
            self.assertTrue(progress.update.called)

    def test_import_selects_only_2point2_to_7gb_and_checkpoints_once(self):
        pending = [
            media("small.mp4", 1_000_000_000, 1),
            media("mid.mp4", 3_500_000_000, 2),
            media("larger.mp4", 5_500_000_000, 3),
            media("too-big.mp4", 8_000_000_000, 4),
        ]
        sizes = {x["path"]: x["size"] for x in pending}
        def write_sparse(src, path, dest):
            with open(dest, "wb") as handle:
                handle.truncate(sizes[path])
        with patch("dropbox_import.sources_from_env", return_value=[SOURCE]), \
             patch("dropbox_import.DropboxLinks") as dbx, \
             patch("dropbox_import.DriveStore") as drive, \
             patch("dropbox_import.require_large_source_space"), \
             patch("dropbox_import.check_drive_headroom"), \
             patch("dropbox_import.save_original", return_value="original"), \
             patch("dropbox_import.make_large_outputs", return_value={
                 "audioDriveId": "mp3",
                 "posterDriveId": "poster",
                 "hls": {"masterDriveId": "master", "renditions": [{}]},
             }):
            dbx.return_value.list_tree.return_value = (1, pending)
            dbx.return_value.download.side_effect = write_sparse
            drive.return_value.folder.return_value = "private"
            drive.return_value.read_map.return_value = {"version": 1, "items": {}}
            run(mode="import", max_files=1, source_label=SOURCE["label"],
                min_source_bytes=2_200_000_000,
                max_source_bytes=7_000_000_000, reserve_gib=68, large_mode=True)
            dbx.return_value.download.assert_called_once()
            self.assertEqual(dbx.return_value.download.call_args.args[1], "/5786/mid.mp4")
            self.assertEqual(drive.return_value.write_map.call_count, 1)

    def test_reject_unsafe_large_mode_before_any_network(self):
        with patch("dropbox_import.sources_from_env") as sources:
            with self.assertRaisesRegex(ValueError, "2.2 to 7 GB"):
                run(mode="import", large_mode=True,
                    min_source_bytes=0, max_source_bytes=SEVEN_GB)
            with self.assertRaisesRegex(ValueError, "2.2 to 7 GB"):
                run(mode="import", large_mode=True,
                    min_source_bytes=2_200_000_000,
                    max_source_bytes=SEVEN_GB + 1)
            sources.assert_not_called()

    def test_inventory_uses_only_approved_sources_and_not_credentials(self):
        recordings = [
            media("a.mts", 3_000_000_000, 1),
            media("b.mts", 6_000_000_000, 2),
            media("c.mts", 9_000_000_000, 3),
        ]
        text = format_size_table([SOURCE], lambda src: (2, recordings))
        self.assertIn("2.2–5 GB", text)
        self.assertIn("5–7 GB", text)
        self.assertIn("Over 7 GB", text)
        self.assertIn("**Above 5 GB:** 2 recordings", text)
        self.assertNotIn("dropbox.com", text)
        with self.assertRaisesRegex(ValueError, "unapproved"):
            format_size_table([dict(SOURCE, label="Wrong person")],
                              lambda src: (0, []))


if __name__ == "__main__":
    unittest.main()
