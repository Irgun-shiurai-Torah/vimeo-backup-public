"""Approval boundary and public/private separation tests for Dropbox Series."""
import json
import unittest

from dropbox_series_bridge import merge_approved_dropbox, parshas_from_title, series_id


class ApprovedDropboxBridgeTest(unittest.TestCase):
    MEDIA_ID = "dbx-media-" + "a" * 24

    def catalog(self):
        return (
            {"version": 1, "library": "series", "items": [], "speakers": []},
            {"version": 1, "library": "series", "items": [], "speakerPhotos": {}},
        )

    def video(self):
        return {
            "kind": "video",
            "sourceLabel": "Reb Mielech - Leil Shishi",
            "sourcePath": "/Records/2024/Thursday shiur.mov",
            "originalDriveId": "o" * 24,
            "audioDriveId": "a" * 24,
            "posterDriveId": "p" * 24,
            "sourceModified": "2026-10-25T10:00:00Z",
            "generatedAt": "2026-10-25T13:00:00Z",
            "hls": {
                "masterDriveId": "m" * 24,
                "renditions": [
                    {"height": 360, "playlist": "360p.m3u8",
                     "playlistDriveId": "l" * 24,
                     "media": "360p.ts", "mediaDriveId": "t" * 24}
                ],
            },
        }

    def test_unapproved_import_does_not_appear_on_site(self):
        public, private = self.catalog()
        unapproved = self.video()
        unapproved["approvedMetadata"] = {
            "speaker": "Reb Mielech", "title": "Thursday shiur", "year": 5787,
        }
        pub, priv = merge_approved_dropbox(
            public, private,
            {"version": 1, "items": {self.MEDIA_ID: unapproved}},
        )
        self.assertEqual(pub["items"], [])
        self.assertEqual(priv["items"], [])
        self.assertEqual(pub["speakers"], [])

    def test_approved_video_reuses_existing_hls_audio_and_poster(self):
        public, private = self.catalog()
        approved = {
            **self.video(),
            "approved": True,
            "approvedMetadata": {
                "speaker": "Reb Mielech", "title": "Thursday shiur",
                "year": 5787, "series": "Leil Shishi",
            },
        }
        pub, priv = merge_approved_dropbox(
            public, private,
            {"version": 1, "items": {self.MEDIA_ID: approved}},
        )
        self.assertEqual(len(pub["items"]), 1)
        entry, secure = pub["items"][0], priv["items"][0]
        self.assertRegex(entry["id"], r"^series-[a-f0-9]{24}$")
        self.assertTrue(entry["hlsReady"])
        self.assertTrue(entry["audioReady"])
        self.assertTrue(entry["posterReady"])
        self.assertEqual(entry["series"], "Leil Shishi")
        self.assertEqual(secure["generated"]["hls"]["masterDriveId"], "m" * 24)
        self.assertEqual(secure["generated"]["audioDriveId"], "a" * 24)
        self.assertEqual(secure["originalDriveId"], "o" * 24)
        self.assertEqual(pub["speakers"], ["Reb Mielech"])
        # Neither Dropbox URLs nor private Google Drive IDs belong in GitHub.
        public_json = json.dumps(pub)
        for fragment in ["o" * 24, "m" * 24, "a" * 24,
                         "sourcePath", "dropbox.com", "rlkey"]:
            self.assertNotIn(fragment, public_json)

    def test_approved_record_must_have_reviewed_metadata(self):
        public, private = self.catalog()
        entry = {**self.video(), "approved": True}
        with self.assertRaises(ValueError):
            merge_approved_dropbox(
                public, private, {"version": 1, "items": {self.MEDIA_ID: entry}}
            )

    def test_approved_record_must_finish_hls(self):
        public, private = self.catalog()
        entry = {**self.video(), "approved": True,
                 "approvedMetadata": {
                     "speaker": "Rav A", "title": "Shiur", "year": 5787,
                 }}
        entry.pop("hls")
        with self.assertRaises(ValueError):
            merge_approved_dropbox(
                public, private, {"version": 1, "items": {self.MEDIA_ID: entry}}
            )

    def test_invalid_approved_year_rejected(self):
        public, private = self.catalog()
        entry = {**self.video(), "approved": True,
                 "approvedMetadata": {
                     "speaker": "Rav A", "title": "Shiur", "year": 2026,
                 }}
        with self.assertRaises(ValueError):
            merge_approved_dropbox(
                public, private, {"version": 1, "items": {self.MEDIA_ID: entry}}
            )

    def test_audio_only_record(self):
        public, private = self.catalog()
        entry = {**self.video(), "kind": "audio", "sourcePath": "/Lecture.wav",
                 "approved": True,
                 "approvedMetadata": {
                     "speaker": "Rav A", "title": "Lecture", "year": 5787,
                 }}
        entry.pop("hls")
        pub, priv = merge_approved_dropbox(
            public, private, {"version": 1, "items": {self.MEDIA_ID: entry}}
        )
        self.assertEqual(len(pub["items"]), 1)
        self.assertFalse(pub["items"][0]["hasVideo"])
        self.assertTrue(pub["items"][0]["audioReady"])
        self.assertIsNone(priv["items"][0]["videoDriveId"])
        self.assertEqual(priv["items"][0]["audioDriveId"], "o" * 24)

    def test_explicit_two_parshas_are_one_shiur_in_both_categories(self):
        title = "נשא (חול) - בהעלותך תשפ-ג"
        self.assertEqual(parshas_from_title(title), ["Nasso", "Beha'alotcha"])
        source = {
            **self.video(), "approved": True,
            "sourcePath": "/נשא (חול) - בהעלותך תשפ-ג/with subtitles.mp4",
            "approvedMetadata": {
                "speaker": "Reb Gamliel Rabinowitz", "title": title,
                "year": 5783, "parsha": "Beha'alotcha",
            }
        }
        public, private = merge_approved_dropbox(
            *self.catalog(), {"version": 1, "items": {self.MEDIA_ID: source}})
        self.assertEqual(len(public["items"]), 1)
        self.assertEqual(len(private["items"]), 1)
        item = public["items"][0]
        self.assertEqual(item["parsha"], "Nasso")
        self.assertEqual(item["parshas"], ["Nasso", "Beha'alotcha"])
        self.assertEqual(private["items"][0]["parshas"], item["parshas"])
        self.assertEqual(
            sum("Nasso" in entry["parshas"] for entry in public["items"]), 1)
        self.assertEqual(
            sum("Beha'alotcha" in entry["parshas"] for entry in public["items"]), 1)

    def test_single_parsha_and_non_parsha_titles_do_not_create_extra_tags(self):
        self.assertEqual(parshas_from_title("נשא תשפ-ג"), ["Nasso"])
        self.assertEqual(parshas_from_title("Ordinary Thursday lecture"), [])
        self.assertEqual(parshas_from_title("נשא - נשא"), ["Nasso"])

    def test_excluded_goldhirsch_event_not_published_even_if_approved(self):
        record = {
            **self.video(),
            "sourceLabel": "Reb Yitzchok Ahron Goldhirsch",
            "sourcePath": "/Shinyev Yurtziet/MovaviClips_Video_20250114-101140.mp4",
            "approved": True,
            "approvedMetadata": {
                "speaker": "Reb Yitzchok Ahron Goldhirsch",
                "title": "Shinyev Yurtziet",
                "year": 5785,
            },
        }
        public, private = merge_approved_dropbox(
            *self.catalog(), {"version": 1, "items": {self.MEDIA_ID: record}})
        self.assertEqual(public["items"], [])
        self.assertEqual(private["items"], [])

    def test_stable_series_ids(self):
        self.assertEqual(series_id(self.MEDIA_ID), series_id(self.MEDIA_ID))
        with self.assertRaises(ValueError):
            series_id("invalid")


if __name__ == "__main__":
    unittest.main()
