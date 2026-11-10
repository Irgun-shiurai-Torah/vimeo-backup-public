"""Offline safety and resumability tests for incremental Dropbox >7 GB imports."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import dropbox_over7 as over7


SOURCE = {
    "id": "dbx-" + "a" * 16,
    "url": "https://www.dropbox.com/scl/fo/abc/xyz?rlkey=example",
    "label": "Reb Mielech - Leil Shishi", "password": "",
}


def media(size, index=1, rev="rev-1"):
    return {"id": f"id:recording-{index}", "path": f"/5786/{index}.mts",
            "name": f"{index}.mts", "size": size, "rev": rev,
            "modified": "2026-01-01T00:00:00Z", "kind": "video"}


class FakeRequest:
    def __init__(self, data):
        self.data = data

    def execute(self, num_retries=0):
        return self.data


class FakeFiles:
    def __init__(self, store):
        self.store = store

    def get_media(self, fileId):
        return FakeRequest(self.store.content[fileId])

    def get(self, fileId, fields):
        if fileId not in self.store.content:
            raise ValueError("Missing Drive ID")
        return FakeRequest({"id": fileId, "size": str(len(self.store.content[fileId])),
                            "trashed": False})


class FakeDrive:
    def __init__(self):
        self.paths = {}
        self.content = {}
        self.api = SimpleNamespace(files=lambda: FakeFiles(self))
        self.written_maps = []
        self.fail_once = None

    def folder(self, parent, name):
        return (parent or "") + "/" + name

    def find(self, parent, name):
        fid = self.paths.get((parent, name))
        return {"id": fid} if fid else None

    def upload(self, path, parent, name=None, mime=None):
        if name == self.fail_once:
            self.fail_once = None
            raise RuntimeError("Simulated interrupted upload")
        fid = self.paths.setdefault((parent, name), "drive-" + str(len(self.paths) + 1))
        self.content[fid] = Path(path).read_bytes()
        return fid

    def write_map(self, parent, mapping, filename):
        self.written_maps.append((parent, copy.deepcopy(mapping), filename))


def write_fake_original(drive, path):
    drive.content["original-id"] = b"safe original"
    return "original-id"


class Over7Test(unittest.TestCase):
    def test_source_limits_and_revision_skips(self):
        rows = [media(7_000_000_000, 1), media(7_000_000_001, 2),
                media(20_000_000_000, 3), media(20_000_000_001, 4),
                dict(media(8_000_000_000, 5), kind="audio")]
        mid = over7.file_id(SOURCE["id"], rows[1])
        result = over7.candidates(SOURCE, rows, {})
        self.assertEqual([r[1]["id"] for r in result], ["id:recording-2", "id:recording-3", "id:recording-4"])
        result = over7.candidates(SOURCE, rows, {mid: {
            "rev": "rev-1", "originalDriveId": "original", "hls": {"masterDriveId": "master"}}})
        self.assertEqual(len(result), 2)
        self.assertEqual([r[1]["id"] for r in result], ["id:recording-3", "id:recording-4"])
        # A completed recording is once-only, even after its Dropbox revision changes.
        earlier = {"rev": "old-revision", "originalDriveId": "original",
                   "hls": {"masterDriveId": "master"}}
        self.assertEqual(len(over7.candidates(SOURCE, rows, {mid: earlier})), 2)

    def test_denies_import_without_explicit_confirmation_before_network(self):
        with patch.object(over7, "sources_from_env") as sources:
            with self.assertRaisesRegex(ValueError, "confirmation"):
                over7.run("short-word", "import", "not-confirmed")
            with self.assertRaisesRegex(ValueError, "Source must"):
                over7.run("not-allowed")
            sources.assert_not_called()

    def test_plan_does_not_touch_google_drive_or_download_files(self):
        rows = [media(8_000_000_000), media(5_000_000_000, 2)]
        with patch.object(over7, "sources_from_env", return_value=[SOURCE]), \
             patch.object(over7, "DropboxLinks") as dbx, \
             patch.object(over7, "DriveStore") as drive:
            dbx.return_value.list_tree.return_value = (1, rows)
            self.assertEqual(over7.run("leil-shishi"), "plan")
            dbx.return_value.download.assert_not_called()
            drive.assert_not_called()

    def test_changed_revision_checkpoint_is_rejected(self):
        item = media(8_000_000_000)
        mid = over7.file_id(SOURCE["id"], item)
        state = over7.fresh_state(SOURCE, mid, item)
        over7.validate_state(state, SOURCE, mid, item)
        with self.assertRaisesRegex(ValueError, "does not match"):
            over7.validate_state(state, SOURCE, mid, media(8_000_000_000, rev="new"))
        state["renditions"] = [{"height": 360}, {"height": 360}]
        with self.assertRaisesRegex(ValueError, "Duplicated"):
            over7.validate_state(state, SOURCE, mid, item)

    def test_resume_after_interrupted_second_rendition_and_publish_only_when_complete(self):
        item = media(7_900_000_000)
        mid = over7.file_id(SOURCE["id"], item)
        dbx = MagicMock()
        store = FakeDrive()
        parent = store.folder(store.folder("/Processed", SOURCE["id"]), mid)
        q = [
            {"height": 360, "width": 640, "bitrate": 550, "maxrate": 700, "buf": 1100},
            {"height": 720, "width": 1280, "bitrate": 1600, "maxrate": 2200, "buf": 3200},
        ]
        encoded = []

        def fake_download(source, path, destination):
            with open(destination, "wb") as handle:
                handle.truncate(item["size"])

        def fake_audio(src, output):
            path = Path(output) / "audio.mp3"
            path.write_bytes(b"audio payload")
            return str(path)

        def fake_poster(src, output, duration):
            path = Path(output) / "poster.jpg"
            path.write_bytes(b"poster payload")
            return str(path)

        def fake_hls(src, output, choices):
            height = choices[0]["height"]
            encoded.append(height)
            Path(output, f"{height}p.m3u8").write_text("#EXTM3U\n#EXT-X-BYTERANGE\n")
            Path(output, f"{height}p.ts").write_bytes(b"transport payload")
            Path(output, "master.m3u8").write_text("placeholder")

        dbx.download.side_effect = fake_download
        store.fail_once = "720p.ts"
        mapping = {"version": 1, "items": {}}
        with patch.object(over7, "ensure_disk"), \
             patch.object(over7, "check_drive_headroom"), \
             patch.object(over7, "require_rendition_space"), \
             patch.object(over7, "save_original", side_effect=lambda *args: write_fake_original(store, None)) as original, \
             patch.object(over7, "probe", return_value=({"width": 1280, "height": 720}, True, 1800.0)), \
             patch.object(over7, "quality_ladder", return_value=q), \
             patch.object(over7, "make_mp3", side_effect=fake_audio), \
             patch.object(over7, "make_poster", side_effect=fake_poster), \
             patch.object(over7, "make_hls", side_effect=fake_hls):
            with self.assertRaisesRegex(RuntimeError, "Simulated interrupted"):
                over7.process_one(dbx, store, SOURCE, mid, item, mapping, "/Maps",
                                  "/Originals", "/Processed", time.monotonic() + 9999, 4)
            self.assertEqual(encoded, [360, 720])
            self.assertEqual(len(store.written_maps), 0)
            checkpoint = over7.load_state(store, parent, SOURCE, mid, item)
            self.assertEqual([r["height"] for r in checkpoint["renditions"]], [360])
            self.assertEqual(checkpoint["originalDriveId"], "original-id")
            self.assertEqual(over7.process_one(
                dbx, store, SOURCE, mid, item, mapping, "/Maps", "/Originals",
                "/Processed", time.monotonic() + 9999, 4), "complete")
            self.assertEqual(encoded, [360, 720, 720])
            self.assertEqual(original.call_count, 1)  # Do not upload original again.
            self.assertEqual(len(store.written_maps), 1)
            published = store.written_maps[0][1]["items"][mid]
            self.assertEqual(len(published["hls"]["renditions"]), 2)
            self.assertTrue(published["audioDriveId"])
            self.assertTrue(published["posterDriveId"])
            self.assertTrue(published["hls"]["masterDriveId"])
            self.assertEqual(len(store.paths), len(set(store.paths.values())))

    def test_incomplete_checkpoint_cannot_appear_in_private_source_map(self):
        item = media(8_000_000_000)
        mid = over7.file_id(SOURCE["id"], item)
        incomplete = over7.fresh_state(SOURCE, mid, item)
        incomplete["originalDriveId"] = "original-id"
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            over7.commit_completed(FakeDrive(), "/Maps", {"version": 1, "items": {}},
                                   SOURCE, mid, item, incomplete)

    def test_missing_checkpointed_drive_asset_is_rejected(self):
        store = FakeDrive()
        with self.assertRaises((ValueError, RuntimeError)):
            over7.verify_checkpoint_references(store, {"originalDriveId": "missing-id", "renditions": []})

    def test_workflow_is_dispatch_only_and_uses_existing_source_lock(self):
        wf = Path(".github/workflows/dropbox-over7-manual.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", wf)
        self.assertNotIn("schedule:", wf)
        self.assertNotIn("  push:", wf)
        self.assertIn("irgun-dropbox-source-${{ inputs.source_key }}", wf)
        self.assertIn("IMPORT_OVER_7_GB", wf)
        self.assertIn("timeout-minutes: 350", wf)


if __name__ == "__main__":
    unittest.main()
