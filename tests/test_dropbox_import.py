"""Offline guardrails for Dropbox shared link importer."""
import os
import unittest
from unittest.mock import patch

from dropbox_import import sources_from_env, source_id, file_id


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

    def test_deterministic_media_id(self):
        item = {"id": "id:example", "path": "/Year/Lecture.mov"}
        self.assertEqual(file_id("dbx-folder", item), file_id("dbx-folder", item))


if __name__ == "__main__":
    unittest.main()
