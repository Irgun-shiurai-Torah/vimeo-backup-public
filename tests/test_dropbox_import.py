"""Offline guardrails for Dropbox shared link importer."""
import os
import unittest
from unittest.mock import patch

from dropbox_import import sources_from_env, source_id, file_id, check_drive_headroom, is_excluded_nonlecture


class SharedLinksTest(unittest.TestCase):
    URL = "https://www.dropbox.com/scl/fo/abc/somecode?rlkey=key123&st=temporary&dl=0"

    def test_ignores_transient_url_parameters(self):
        self.assertEqual(source_id(self.URL), source_id(
            "https://www.dropbox.com/scl/fo/abc/somecode?rlkey=key123&e=1&dl=1"))

    def test_only_explicit_links(self):
        links = sources_from_env({"DROPBOX_SHARED_LINK": self.URL})
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["url"], self.URL)

    def test_multiple_sources_and_deduplication(self):
        import json
        links = sources_from_env({
            "DROPBOX_SHARED_LINK": self.URL,
            "DROPBOX_SOURCES_JSON": json.dumps([
                {"url": self.URL, "label": "Rav A"},
                {"url": "https://www.dropbox.com/scl/fo/another/entry?rlkey=b", "name": "Rav B"},
                {"url": self.URL, "label": "Rav A"}
            ])
        })
        self.assertEqual(len(links), 2)
        self.assertEqual({entry["label"] for entry in links}, {"Rav A", "Rav B"})

    def test_multisource_overrides_legacy_single_link(self):
        import json
        links = sources_from_env({
            "DROPBOX_SHARED_LINK": self.URL,
            "DROPBOX_SOURCES_JSON": json.dumps([
                {"url": "https://www.dropbox.com/scl/fo/new/source?rlkey=new", "label": "Only new"}
            ])
        })
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["label"], "Only new")

    def test_rejects_unsafe_collection_label(self):
        import json
        with self.assertRaises(ValueError):
            sources_from_env({"DROPBOX_SOURCES_JSON": json.dumps([
                {"url": self.URL, "label": "Bad" + chr(10) + "Label"}])})

    def test_reject_non_dropbox(self):
        with self.assertRaises(ValueError):
            sources_from_env({"DROPBOX_SHARED_LINK": "https://evil.example/scl/fo/a/b"})

    def test_scan_mode_does_not_touch_drive_or_downloads(self):
        from dropbox_import import run
        fake_source = {"id": "dbx-test", "url": self.URL,
                       "label": "Test collection", "password": ""}
        fake_video = {"id": "id:lecture", "path": "/inner/lecture.mov",
                      "name": "lecture.mov", "size": 1024, "rev": "rev1",
                      "kind": "video"}
        with patch("dropbox_import.sources_from_env", return_value=[fake_source]), \
             patch("dropbox_import.DropboxLinks") as dropbox, \
             patch("dropbox_import.DriveStore") as drive:
            dropbox.return_value.list_tree.return_value = (1, [fake_video])
            run(mode="scan")
            dropbox.return_value.list_tree.assert_called_once_with(fake_source)
            dropbox.return_value.download.assert_not_called()
            drive.assert_not_called()

    def test_scan_fails_on_missing_source_label_before_network(self):
        from dropbox_import import run
        with patch("dropbox_import.sources_from_env", return_value=[
            {"id": "dbx-a", "url": self.URL, "label": "Other", "password": ""}]), \
             patch("dropbox_import.DropboxLinks") as dropbox:
            with self.assertRaises(ValueError):
                run(mode="scan", source_label="Reb Gamliel Rabinowitz")
            dropbox.assert_not_called()

    def test_source_label_restricts_scan_to_one_folder(self):
        from dropbox_import import run
        two = [{"id": "dbx-a", "url": self.URL, "label": "Reb Gamliel Rabinowitz", "password": ""},
               {"id": "dbx-b", "url": self.URL, "label": "Someone else", "password": ""}]
        with patch("dropbox_import.sources_from_env", return_value=two), \
             patch("dropbox_import.DropboxLinks") as dropbox, \
             patch("dropbox_import.DriveStore") as drive:
            dropbox.return_value.list_tree.return_value = (0, [])
            run(mode="scan", source_label="Reb Gamliel Rabinowitz")
            dropbox.return_value.list_tree.assert_called_once_with(two[0])
            drive.assert_not_called()

    def test_automatic_drive_quota_reserve_is_enforced(self):
        from unittest.mock import MagicMock
        store = MagicMock()
        store.api.about.return_value.get.return_value.execute.return_value = {
            "storageQuota": {"limit": str(100 * 1024**3), "usage": str(70 * 1024**3)}}
        with self.assertRaisesRegex(RuntimeError, "safety reserve"):
            check_drive_headroom(store, 200_000_000, reserve_gib=50)
        check_drive_headroom(store, 200_000_000, reserve_gib=20)

    def test_unlimited_drive_quota_accepted(self):
        from unittest.mock import MagicMock
        store = MagicMock()
        store.api.about.return_value.get.return_value.execute.return_value = {
            "storageQuota": {"usage": str(70 * 1024**3)}}
        check_drive_headroom(store, 200_000_000, reserve_gib=50)

    def test_default_quota_checks_do_not_call_drive(self):
        from unittest.mock import MagicMock
        store = MagicMock()
        check_drive_headroom(store, 200_000_000, reserve_gib=0)
        store.api.about.assert_not_called()

    def test_recurring_imports_use_only_five_owner_authorized_sources(self):
        from pathlib import Path
        workflow = Path(".github/workflows/dropbox-all-series.yml").read_text()
        for label in ("Reb Mielech - Leil Shishi",
                      "Reb Mielech - Short Word for Shabbos",
                      "Reb Gamliel Rabinowitz", "Reb Nachum Cohen",
                      "Reb Yitzchok Ahron Goldhirsch"):
            self.assertIn("'" + label + "'", workflow)
        self.assertEqual(workflow.count("    runs-on:"), 1,
                         "Keep import writes serial to protect the private Drive map")
        self.assertIn('cron: "17 * * * *"', workflow)
        self.assertIn('slot=$(( 10#$(date -u +%H) % ${#sources[@]} ))', workflow)
        self.assertIn("--max-files 1", workflow)
        self.assertIn("--reserve-gib 50", workflow)
        self.assertIn("--max-source-bytes 2200000000", workflow)
        self.assertIn("secrets.DROPBOX_SOURCES_JSON", workflow)
        self.assertNotIn("secrets.DROPBOX_SHARED_LINK", workflow)

    def test_goldhirsch_shinyev_occasion_is_not_ingested(self):
        goldhirsch = {"label": "Reb Yitzchok Ahron Goldhirsch"}
        self.assertTrue(is_excluded_nonlecture(goldhirsch,
            {"path": "/Shinyev Yurtziet/MovaviClips_Video_20250114.mp4"}))
        self.assertFalse(is_excluded_nonlecture(goldhirsch,
            {"path": "/Parshas Bereishis/Lecture.mp4"}))
        self.assertFalse(is_excluded_nonlecture(
            {"label": "Reb Mielech - Leil Shishi"},
            {"path": "/Shinyev Yurtziet/Other Lecture.mp4"}))

    def test_scan_filters_excluded_occasion_without_download(self):
        from dropbox_import import run
        source = {"id": "dbx-test", "label": "Reb Yitzchok Ahron Goldhirsch",
                  "url": self.URL, "password": ""}
        entries = [
            {"id": "id:occasion", "path": "/Shinyev Yurtziet/Clip.mp4",
             "name": "Clip.mp4", "size": 90000000, "rev": "r1", "kind": "video"},
            {"id": "id:lecture", "path": "/Parsha/Lecture.mp4",
             "name": "Lecture.mp4", "size": 120000000, "rev": "r2", "kind": "video"}
        ]
        with patch("dropbox_import.sources_from_env", return_value=[source]), \
             patch("dropbox_import.DropboxLinks") as dbx, \
             patch("dropbox_import.DriveStore") as drive:
            dbx.return_value.list_tree.return_value = (2, entries)
            run(mode="scan", source_label=source["label"])
            dbx.return_value.download.assert_not_called()
            drive.assert_not_called()

    def test_deterministic_media_id(self):
        item = {"id": "id:example", "path": "/Year/Lecture.mov"}
        self.assertEqual(file_id("dbx-folder", item), file_id("dbx-folder", item))


if __name__ == "__main__":
    unittest.main()
