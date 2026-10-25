"""A single reviewed Dropbox video can be approved without changing other items."""
import copy
import unittest

from dropbox_approve import approve_record


ID = "dbx-media-" + "f" * 24
SOURCE = "Reb Gamliel Rabinowitz"
PATH = "/נשא (חול) - בהעלותך תשפ-ג/with subtitles.mp4"
META = {"speaker": SOURCE, "title": "נשא (חול) - בהעלותך תשפ-ג",
        "year": 5783, "series": "Parsha"}
RECORD = {
    "id": ID, "sourceLabel": SOURCE, "sourcePath": PATH,
    "kind": "video", "originalDriveId": "o" * 24,
    "audioDriveId": "a" * 24, "posterDriveId": "p" * 24,
    "hls": {"masterDriveId": "m" * 24, "renditions": [
        {"playlistDriveId": "l" * 24, "mediaDriveId": "t" * 24,
         "playlist": "360p.m3u8", "media": "360p.ts", "height": 360}
    ]},
}


class DropboxApprovalTests(unittest.TestCase):
    def source(self):
        return {"version": 1, "items": {ID: copy.deepcopy(RECORD),
                "dbx-media-" + "a"*24: {"approved": False}}}

    def test_approval_is_one_record_only_and_validated(self):
        original = self.source()
        result, sid = approve_record(original, ID, SOURCE, PATH, META)
        self.assertEqual(sid[:7], "series-")
        self.assertTrue(result["items"][ID]["approved"])
        self.assertEqual(result["items"][ID]["approvedMetadata"], META)
        self.assertFalse(result["items"]["dbx-media-"+"a"*24]["approved"])
        self.assertNotIn("approved", original["items"][ID])

    def test_wrong_path_or_source_fails(self):
        for source, path in [(SOURCE, "/other.mp4"), ("Other", PATH)]:
            with self.assertRaises(ValueError):
                approve_record(self.source(), ID, source, path, META)

    def test_missing_hls_fails(self):
        original = self.source()
        original["items"][ID].pop("hls")
        with self.assertRaises(ValueError):
            approve_record(original, ID, SOURCE, PATH, META)

    def test_missing_audio_fails(self):
        original = self.source()
        original["items"][ID].pop("audioDriveId")
        with self.assertRaises(ValueError):
            approve_record(original, ID, SOURCE, PATH, META)

    def test_existing_different_approval_fails(self):
        original = self.source()
        original["items"][ID]["approved"] = True
        original["items"][ID]["approvedMetadata"] = dict(META)
        with self.assertRaises(ValueError):
            approve_record(original, ID, SOURCE, PATH, {**META, "title": "Changed"})

    def test_unimported_record_cannot_be_approved(self):
        with self.assertRaises(ValueError):
            approve_record({"version": 1, "items": {}}, ID, SOURCE, PATH, META)


if __name__ == "__main__":
    unittest.main()
