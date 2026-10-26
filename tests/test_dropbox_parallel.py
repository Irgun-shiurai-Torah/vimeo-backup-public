"""Offline guarantees for three parallel, independently checkpointed Dropbox imports."""
import unittest

from dropbox_import import private_map_name, combine_legacy_and_source_map
from series_drive import combine_private_dropbox_maps


A = "dbx-" + "a" * 16
B = "dbx-" + "b" * 16
C = "dbx-" + "c" * 16
MID_A = "dbx-media-" + "1" * 24
MID_B = "dbx-media-" + "2" * 24
MID_C = "dbx-media-" + "3" * 24


def item(source, version):
    return {"sourceId": source, "rev": version, "originalDriveId": "recording-placeholder"}


def mapped(**items):
    return {"version": 1, "items": items}


class ParallelDropboxMapsTest(unittest.TestCase):
    def test_each_source_has_its_own_checkpoint_name(self):
        self.assertNotEqual(private_map_name(A), private_map_name(B))
        self.assertTrue(private_map_name(A).endswith(A + ".json"))
        for invalid in ("a", "dbx-../escape", "dbx-" + "G" * 16):
            with self.assertRaises(ValueError):
                private_map_name(invalid)

    def test_legacy_completed_files_migrate_only_to_matching_source(self):
        legacy = mapped(**{
            MID_A: item(A, "old"),
            MID_B: item(B, "old"),
        })
        merged = combine_legacy_and_source_map(legacy, mapped(), A)
        self.assertEqual(set(merged["items"]), {MID_A})
        self.assertEqual(merged["items"][MID_A]["rev"], "old")

    def test_previous_source_checkpoint_wins_over_stale_legacy(self):
        legacy = mapped(**{MID_A: item(A, "old"), MID_B: item(B, "old")})
        a = combine_legacy_and_source_map(
            legacy, mapped(**{MID_A: item(A, "new")}), A)
        b = combine_legacy_and_source_map(legacy, mapped(), B)
        joined = combine_private_dropbox_maps({
            "dropbox-private-map.json": legacy,
            private_map_name(A): a,
            private_map_name(B): b,
        })
        self.assertEqual(set(joined["items"]), {MID_A, MID_B})
        self.assertEqual(joined["items"][MID_A]["rev"], "new")
        self.assertEqual(joined["items"][MID_B]["rev"], "old")

    def test_multiple_new_workers_merge_without_shared_write(self):
        merged = combine_private_dropbox_maps({
            private_map_name(A): mapped(**{MID_A: item(A, "1")}),
            private_map_name(B): mapped(**{MID_B: item(B, "2")}),
            private_map_name(C): mapped(**{MID_C: item(C, "3")}),
        })
        self.assertEqual(len(merged["items"]), 3)
        self.assertEqual({row["sourceId"] for row in merged["items"].values()},
                         {A, B, C})

    def test_rejects_foreign_source_in_source_map(self):
        with self.assertRaisesRegex(ValueError, "foreign"):
            combine_legacy_and_source_map(
                mapped(), mapped(**{MID_B: item(B, "1")}), A)
        with self.assertRaisesRegex(ValueError, "another source"):
            combine_private_dropbox_maps({
                private_map_name(A): mapped(**{MID_B: item(B, "1")}),
            })

    def test_rejects_same_media_record_in_two_source_maps(self):
        # Each shard validates its own source, so a duplicate ID must
        # also be rejected rather than hidden by last-write-wins.
        with self.assertRaisesRegex(ValueError, "two source shards"):
            combine_private_dropbox_maps({
                private_map_name(A): mapped(**{MID_A: item(A, "1")}),
                private_map_name(B): mapped(**{MID_A: item(B, "2")}),
            })

    def test_rejects_corrupt_map_and_handles_empty_folder(self):
        self.assertIsNone(combine_private_dropbox_maps({}))
        with self.assertRaises(ValueError):
            combine_private_dropbox_maps({
                private_map_name(A): {"version": 2, "items": {}},
            })
        with self.assertRaises(ValueError):
            combine_private_dropbox_maps({
                "unknown-private-map.json": mapped(),
            })


if __name__ == "__main__":
    unittest.main()
