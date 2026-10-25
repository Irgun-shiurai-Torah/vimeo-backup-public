"""Allow only the two owner-approved Series speakers."""
import unittest

from series_visibility import (
    ALLOWED_DROPBOX_SOURCES, ALLOWED_SERIES_SPEAKERS,
    filter_series_maps, permitted_series_speaker,
)


class SeriesSpeakerAllowlistTest(unittest.TestCase):
    def test_source_labels(self):
        self.assertEqual(ALLOWED_DROPBOX_SOURCES, {
            "Reb Gamliel Rabinowitz", "Reb Mielech - Leil Shishi",
            "Reb Mielech - Short Word for Shabbos",
        })
        self.assertFalse(permitted_series_speaker("Rav Elozer Nissen Rubin"))
        self.assertFalse(permitted_series_speaker("Reb Nachum Cohen"))
        self.assertFalse(permitted_series_speaker("Reb Yitzchok Ahron Goldhirsch"))
        self.assertTrue(permitted_series_speaker("Reb Gamliel Rabinowitz"))
        self.assertTrue(permitted_series_speaker("Rabbi Meilech Biderman"))

    def test_public_and_private_maps_both_exclude_unapproved_speakers(self):
        speakers = [
            "Rabbi Meilech Biderman", "Reb Gamliel Rabinowitz",
            "Reb Nachum Cohen", "Reb Yitzchok Ahron Goldhirsch",
            "Rav Elozer Nissen Rubin",
        ]
        public = {
            "items": [{"id": f"series-{i:024x}", "speaker": s} for i, s in enumerate(speakers)],
            "speakers": speakers,
            "speakerPhotos": speakers,
        }
        private = {
            "items": [{"id": f"series-{i:024x}", "speaker": s, "originalDriveId": f"file-{i}"}
                      for i, s in enumerate(speakers)],
            "speakerPhotos": {s: {"id": f"photo-{i}"} for i, s in enumerate(speakers)}
        }
        pub, priv = filter_series_maps(public, private)
        self.assertEqual(len(pub["items"]), 2)
        self.assertEqual(len(priv["items"]), 2)
        self.assertEqual({x["speaker"] for x in pub["items"]}, ALLOWED_SERIES_SPEAKERS)
        self.assertEqual({x["speaker"] for x in priv["items"]}, ALLOWED_SERIES_SPEAKERS)
        self.assertEqual(set(pub["speakers"]), ALLOWED_SERIES_SPEAKERS)
        self.assertEqual(set(pub["speakerPhotos"]), ALLOWED_SERIES_SPEAKERS)
        self.assertEqual(set(priv["speakerPhotos"]), ALLOWED_SERIES_SPEAKERS)
        self.assertEqual(len(public["items"]), 5, "Original source maps are not destroyed")
        self.assertEqual(len(private["items"]), 5, "Original source maps are not destroyed")


if __name__ == "__main__":
    unittest.main()
