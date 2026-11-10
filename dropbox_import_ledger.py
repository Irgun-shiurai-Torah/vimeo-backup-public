"""Permanent import history separate from deletable Google Drive recordings.

The initial 2,301 recordings were frozen on 10 November 2026. The frozen
IDs are authoritative even when original/processed Drive files or their
private source map have subsequently been removed by an operator.

For recordings imported later, a completed entry in the private source map
also blocks repeat imports, regardless of a Dropbox revision change.
"""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

BASELINE_PATH = Path(__file__).resolve().parent / "data" / "dropbox-initial-imported-2301.json"
SOURCE_COUNTS = {
    "dbx-814df21e3bc3ba49": 1486,  # Reb Meilech - Short Word for Shabbos
    "dbx-bbe81b4e377b38e9": 459,   # Reb Gamliel Rabinowitz
    "dbx-be2bc188b958b30c": 356,   # Reb Meilech - Leil Shishi
}
EXPECTED_DIGEST = "270e422997eb4f3dc62773fc7dafb2d368704e4108b2d69456a972397c894d74"


@lru_cache(maxsize=1)
def frozen_initial_ids():
    """Fail closed if the permanent import baseline is lost or modified."""
    try:
        document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        if document.get("version") != 1 or document.get("total") != 2301:
            raise ValueError("Incorrect frozen Dropbox baseline header")
        sources = document["sources"]
        if set(sources) != set(SOURCE_COUNTS):
            raise ValueError("Frozen Dropbox source list changed")
        imported = []
        for source, required in SOURCE_COUNTS.items():
            ids = sources[source]
            if not isinstance(ids, list) or len(ids) != required:
                raise ValueError("Frozen Dropbox source count changed: " + source)
            if any(not isinstance(mid, str) or
                   not re.fullmatch(r"dbx-media-[a-f0-9]{24}", mid) for mid in ids):
                raise ValueError("Invalid Dropbox ID in frozen baseline")
            imported.extend(ids)
        if len(set(imported)) != 2301:
            raise ValueError("Duplicate Dropbox IDs in frozen baseline")
        checksum = hashlib.sha256(
            ("\n".join(sorted(imported)) + "\n").encode("utf-8")
        ).hexdigest()
        if checksum != EXPECTED_DIGEST or document.get("sha256_sorted_ids") != checksum:
            raise ValueError("Frozen Dropbox import history checksum mismatch")
        return frozenset(imported)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Permanent Dropbox import history is missing or invalid; "
                           "refusing to import any recordings") from exc


def already_imported(media_id, kind, previous=None):
    """Treat a completed import as permanent, not dependent on Drive assets.

    Previously imported recordings are skipped even if the source revision
    changes, and even if deleted media is still present in Dropbox. Incomplete
    imports from *after* the frozen baseline may be retried.
    """
    if media_id in frozen_initial_ids():
        return True
    previous = previous or {}
    return bool(previous.get("originalDriveId") and
                (kind == "audio" or (previous.get("hls") or {}).get("masterDriveId")))
