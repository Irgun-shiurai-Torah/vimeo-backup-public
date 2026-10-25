import unittest
import json

from series_catalog import build_maps, clean_key, match_topic, PARSHA_ALIASES, HOLIDAY_ALIASES, speaker_names_from_folder_entries, choose_speaker_photo, read_review_metadata, UPLOAD_METADATA_MARKER


def source(name, file_id, year="5787", speaker="Rabbi Meilech Biderman", modified="2026-10-25T00:10:00Z"):
    return {"speaker": speaker, "year": year, "file": {
        "name": name, "id": file_id, "modifiedTime": modified,
        "createdTime": modified, "mimeType": "audio/mpeg" if name.endswith(".mp3") else "video/mp4"}}


class SeriesCatalogTests(unittest.TestCase):
    def test_speaker_photo_selects_safe_named_portrait_and_ignores_svg(self):
        candidates=[
            {"id":"generic","name":"random.png","mimeType":"image/png","size":"3200"},
            {"id":"unsafe","name":"speaker.svg","mimeType":"image/svg+xml","size":"1200"},
            {"id":"large","name":"speaker.jpg","mimeType":"image/jpeg","size":str(26*1024*1024)},
            {"id":"good","name":"portrait.webp","mimeType":"image/webp","size":"30000"},
        ]
        self.assertEqual(choose_speaker_photo(candidates),{"id":"good","mimeType":"image/webp"})
        self.assertIsNone(choose_speaker_photo([candidates[1]]))

    def test_speaker_folders_include_empty_and_exclude_system(self):
        folder = "application/vnd.google-apps.folder"
        records = [
            {"name":"Rabbi B","mimeType":folder,"id":"folder-b"},
            {"name":"Rabbi A","mimeType":folder,"id":"folder-a"},
            {"name":"_System","mimeType":folder,"id":"folder-private"},
            {"name":"_Pending","mimeType":folder,"id":"folder-upload"},
            {"name":"test.mp3","mimeType":"audio/mpeg","id":"not-a-folder"}
        ]
        self.assertEqual(speaker_names_from_folder_entries(records),["Rabbi A","Rabbi B"])

    def test_actual_hebrew_mp3_test_file(self):
        pub, private = build_maps(
            [source("\u202aר' מיילך בידרמן ליל שישי לך לך תשפ-ז.mp3", "internal-drive-id")],
            generated_at="2026-10-25T00:00:00Z")
        self.assertEqual(len(pub["items"]), 1)
        item = pub["items"][0]
        self.assertEqual(item["library"], "series")
        self.assertEqual(item["speaker"], "Rabbi Meilech Biderman")
        self.assertEqual(item["year"], 5787)
        self.assertEqual(item["parsha"], "Lech-Lecha")
        self.assertTrue(item["audioReady"])
        self.assertFalse(item["hasVideo"])
        self.assertEqual(item["originalFormat"], "mp3")
        self.assertNotIn("internal-drive-id", str(pub))
        self.assertEqual(private["items"][0]["audioDriveId"], "internal-drive-id")

    def test_mp4_mp3_pair_as_single_lecture(self):
        files = [
            source("Vayeira 5787.mp4", "video-secret"),
            source("Vayeira 5787.mp3", "audio-secret"),
        ]
        pub, priv = build_maps(files)
        self.assertEqual(len(pub["items"]), 1)
        self.assertTrue(pub["items"][0]["hasVideo"])
        self.assertTrue(pub["items"][0]["hasAudio"])
        self.assertEqual(priv["items"][0]["videoDriveId"], "video-secret")
        self.assertEqual(priv["items"][0]["audioDriveId"], "audio-secret")
        self.assertEqual(pub["items"][0]["parsha"], "Vayera")
        self.assertNotIn("audio-secret", str(pub))
        self.assertNotIn("video-secret", str(pub))

    def test_mts_processing_state(self):
        pub, _ = build_maps([source("Chanukah Shiur.mts", "secret")])
        item = pub["items"][0]
        self.assertEqual(item["holiday"], "Chanukah")
        self.assertTrue(item["hasVideo"])
        self.assertFalse(item["videoReady"])
        self.assertEqual(item["status"], "processing")
        self.assertIsNone(item["videoPlaybackUrl"])

    def test_mp3_has_stable_id_after_adding_video(self):
        first, private = build_maps([source("Lech Lecha.mp3", "a")])
        after, _ = build_maps([source("Lech Lecha.mp3", "a"), source("Lech Lecha.mts", "b")], private)
        self.assertEqual(first["items"][0]["id"], after["items"][0]["id"])

    def test_contributor_folder_not_city(self):
        data = [
            source("Pesach.mp3", "a", speaker="Rabbi A", year="5787"),
            source("Pesach.mp3", "b", speaker="Rabbi B", year="5786"),
        ]
        pub, _ = build_maps(data)
        self.assertEqual(len(pub["items"]), 2)
        self.assertTrue(all("city" not in row for row in pub["items"]))
        self.assertTrue(all(row["holiday"] == "Pesach" for row in pub["items"]))

    def test_non_media_files_and_incorrect_year_ignored(self):
        data = [source("lecture.pdf", "a"), source("lecture.mp3", "b", year="2026")]
        pub, _ = build_maps(data)
        self.assertEqual(pub["items"], [])

    def test_old_conversion_retained_until_video_changes(self):
        files = [source("Lech Lecha.mts", "v1")]
        initial, private = build_maps(files)
        private["items"][0]["generated"] = {
            "audioDriveId": "generated-mp3",
            "hls": {"masterDriveId": "generated-hls"}
        }
        refreshed, keep = build_maps(files, private)
        self.assertTrue(refreshed["items"][0]["audioReady"])
        self.assertTrue(refreshed["items"][0]["videoReady"])
        modified = [source("Lech Lecha.mts", "v1", modified="2026-10-26T00:00:00Z")]
        changed, no_keep = build_maps(modified, private)
        self.assertFalse(changed["items"][0]["videoReady"])
        self.assertFalse(no_keep["items"][0]["generated"])

    def test_full_upload_review_fields_preserved_in_public_map(self):
        metadata = {
            "titleEnglish": "Friday shiur", "titleHebrew": "שיעור ליל שישי",
            "speakerEnglish": "Rabbi Meilech Biderman", "speakerHebrew": "רבי מיילך בידרמן",
            "rabbiAlternate": "R Meilech", "lecturerAlternate": "Reb Meilech",
            "topic": "Emunah", "topicEnglish": "Faith", "topicHebrew": "אמונה",
            "category": "Weekly", "program": "Friday night", "series": "Lel Shishi",
            "language": "Yiddish", "organization": "Irgun", "location": "Jerusalem",
            "shiurCode": "RB-24-04", "lectureDate": "2026-10-25", "hidden": False
        }
        entry=source("Friday Shiur.mp3","uploaded-series-file")
        entry["file"]["description"]="A meaningful shiur" + UPLOAD_METADATA_MARKER + json.dumps(metadata)
        entry["file"]["appProperties"]={"irgunSeriesParsha":"Lech-Lecha","irgunSeriesHoliday":""}
        public, private=build_maps([entry])
        item=public["items"][0]
        self.assertEqual(item["description"],"A meaningful shiur")
        self.assertEqual(item["parsha"],"Lech-Lecha")
        for key,value in metadata.items():
            self.assertEqual(item[key],value,key)
        self.assertNotIn("uploaded-series-file",str(public))
        self.assertEqual(private["items"][0]["speakerHebrew"],metadata["speakerHebrew"])

    def test_hidden_upload_is_indexed_but_flagged_for_public_API(self):
        entry=source("Private Shiur.mp3","file-secret")
        entry["file"]["description"]="" + UPLOAD_METADATA_MARKER + json.dumps({"hidden": True})
        public, _=build_maps([entry])
        self.assertEqual(len(public["items"]),1)
        self.assertIs(public["items"][0]["hidden"],True)
        self.assertEqual(public["items"][0]["description"],"")

    def test_audio_video_pair_uses_metadata_attached_to_either_format(self):
        video=source("Same Shiur.mp4","video-id")
        audio=source("Same Shiur.mp3","audio-id")
        audio["file"]["description"]="By the speaker"+UPLOAD_METADATA_MARKER+json.dumps({"topic":"Parsha insight"})
        public,_=build_maps([video,audio])
        self.assertEqual(len(public["items"]),1)
        self.assertEqual(public["items"][0]["topic"],"Parsha insight")
        self.assertEqual(public["items"][0]["description"],"By the speaker")

    def test_invalid_review_metadata_does_not_expose_untrusted_keys(self):
        human,fields,hidden=read_review_metadata("Normal description")
        self.assertEqual((human,fields,hidden),("Normal description",{},False))
        human,fields,hidden=read_review_metadata("Plain"+UPLOAD_METADATA_MARKER+json.dumps({
            "secretToken":"must-not-export","titleHebrew":"שלום","lectureDate":"not a date","hidden":"yes"}))
        self.assertEqual(human,"Plain")
        self.assertEqual(fields["titleHebrew"],"שלום")
        self.assertNotIn("secretToken",fields)
        self.assertNotIn("lectureDate",fields)
        self.assertIs(hidden,False)

    def test_unicode_mark_and_holiday_aliases(self):
        self.assertEqual(match_topic("שיעור לפורים", HOLIDAY_ALIASES), "Purim")
        # Dropbox sources sometimes spell the holiday סיכות. Both whole-word
        # spellings must map to the existing canonical Sukkot category.
        self.assertEqual(match_topic("סוכות תשפ-ב", HOLIDAY_ALIASES), "Sukkot")
        self.assertEqual(match_topic("סיכות תשפ-ב", HOLIDAY_ALIASES), "Sukkot")
        self.assertEqual(match_topic("שיעור בסיכות תשפ-ב", HOLIDAY_ALIASES), "Sukkot")
        self.assertIsNone(match_topic("סיכויות", HOLIDAY_ALIASES))
        self.assertEqual(match_topic("פרשת לך לך", PARSHA_ALIASES), "Lech-Lecha")
        self.assertEqual(match_topic("פרשת וירא", PARSHA_ALIASES), "Vayera")


if __name__ == "__main__":
    unittest.main()
