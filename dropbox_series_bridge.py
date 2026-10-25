"""Connect explicitly approved Dropbox imports to the TEST Series website.

This is a pure catalog bridge: imported files stay in private Google Drive.
It never publishes recordings just because a Dropbox link was scanned or
converted. Every item requires an explicit approval and reviewed metadata in
the private Dropbox import map before it joins the public Series catalog.
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re

from series_catalog import HOLIDAY_ALIASES, PARSHA_ALIASES, clean_key, match_topic


DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
REVIEW_TEXT_FIELDS = {
    "titleEnglish": 350, "titleHebrew": 350, "speakerEnglish": 160,
    "speakerHebrew": 160, "description": 2400, "topic": 260,
    "topicEnglish": 260, "topicHebrew": 260, "category": 160,
    "program": 200, "series": 200, "language": 80,
    "location": 160, "organization": 160, "shiurCode": 100,
    "lectureDate": 20,
}


def series_id(dropbox_media_id):
    if not re.fullmatch(r"dbx-media-[a-f0-9]{24}", str(dropbox_media_id)):
        raise ValueError("Invalid Dropbox media ID")
    return "series-" + hashlib.sha256(
        ("ist-dropbox-series-v1:" + dropbox_media_id).encode("utf-8")
    ).hexdigest()[:24]


def drive_id(value):
    return isinstance(value, str) and bool(DRIVE_ID.fullmatch(value))


def required_text(value, limit, field):
    if not isinstance(value, str):
        raise ValueError("Approved Dropbox " + field + " is required")
    text = value.strip()
    if not text or len(text) > limit or any(ord(c) < 32 for c in text):
        raise ValueError("Invalid approved Dropbox " + field)
    return text


def reviewed_optional(meta, key, limit):
    value = meta.get(key)
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid approved Dropbox " + key)
    return value.strip()


def parshas_from_title(title):
    """Find all explicitly named Parshas, in title order, without duplicates.

    Unlike a combined *weekly* reading, these titles can refer to two separate
    Parshas (e.g. 'נשא (חול) - בהעלותך'). Each should be browseable, while the
    recording itself stays a single entry in the Series catalog.
    """
    normalized = clean_key(title)
    matches = {}
    for canonical, aliases in PARSHA_ALIASES.items():
        for name in aliases:
            alias = clean_key(name)
            if not alias:
                continue
            found = re.search(r"(?<!\\S)" + re.escape(alias) + r"(?!\\S)", normalized)
            if found and (canonical not in matches or found.start() < matches[canonical]):
                matches[canonical] = found.start()
    return [name for name, _ in sorted(matches.items(), key=lambda pair: pair[1])][:4]


def merge_approved_dropbox(public, private, dropbox_map):
    """Return copies of Series maps with reviewed, fully encoded Dropbox items.

    Raises on invalid *approved* records. An incomplete or malformed approval
    cannot silently publish an original or leak internal storage paths.
    """
    if not dropbox_map:
        return public, private
    if not isinstance(dropbox_map, dict) or dropbox_map.get("version") != 1:
        raise ValueError("Invalid private Dropbox import map version")
    entries = dropbox_map.get("items")
    if not isinstance(entries, dict) or len(entries) > 100000:
        raise ValueError("Invalid private Dropbox import map items")
    pub = {**public, "items": list(public["items"])}
    priv = {**private, "items": list(private["items"])}
    ids = {str(row.get("id")) for row in pub["items"]}
    count = 0

    for source_id, source in sorted(entries.items()):
        if not isinstance(source, dict) or source.get("approved") is not True:
            continue
        reviewed = source.get("approvedMetadata")
        if not isinstance(reviewed, dict):
            raise ValueError("Approved Dropbox entry has no reviewed metadata")
        speaker = required_text(reviewed.get("speaker"), 140, "speaker")
        title = required_text(reviewed.get("title"), 350, "title")
        year = reviewed.get("year")
        if isinstance(year, bool) or not isinstance(year, int) or not 5700 <= year <= 5999:
            raise ValueError("Approved Dropbox year must be a Hebrew year")
        if not drive_id(source.get("originalDriveId")):
            raise ValueError("Approved Dropbox original is not in Drive")
        kind = source.get("kind")
        if kind not in ("video", "audio"):
            raise ValueError("Unsupported approved Dropbox media kind")
        sid = series_id(source_id)
        if sid in ids:
            raise ValueError("Duplicate Series ID for approved Dropbox media")
        ids.add(sid)

        filename = os.path.basename(str(source.get("sourcePath") or ""))
        filename = required_text(filename, 350, "original filename")
        original_ext = os.path.splitext(filename)[1].lower().lstrip(".")
        outputs = source.get("hls") if kind == "video" else None
        has_hls = bool(isinstance(outputs, dict) and drive_id(outputs.get("masterDriveId")))
        poster_ready = drive_id(source.get("posterDriveId"))
        # A video is not published until HLS and poster exist.
        if kind == "video" and (not has_hls or not poster_ready):
            raise ValueError("Approved Dropbox video must finish HLS and poster first")
        audio_id = source.get("audioDriveId")
        if not drive_id(audio_id):
            raise ValueError("Approved Dropbox recording must have playable audio")
        renditions = outputs.get("renditions") if has_hls else []
        if has_hls:
            if not isinstance(renditions, list) or not renditions:
                raise ValueError("Approved Dropbox HLS renditions are missing")
            for rendition in renditions:
                if not isinstance(rendition, dict) or not (
                    drive_id(rendition.get("playlistDriveId")) and
                    drive_id(rendition.get("mediaDriveId"))
                ):
                    raise ValueError("Approved Dropbox HLS rendition is incomplete")

        fields = {k: reviewed_optional(reviewed, k, limit)
                  for k, limit in REVIEW_TEXT_FIELDS.items()
                  if reviewed.get(k) not in (None, "")}
        reviewed_parsha = reviewed_optional(reviewed, "parsha", 80)
        identified_parshas = parshas_from_title(title)
        # If the title names multiple Parshas, index every one, even if the
        # original review form selected only one primary Parsha.
        if len(identified_parshas) > 1:
            parshas = identified_parshas
        elif reviewed_parsha:
            parshas = [reviewed_parsha]
        else:
            parshas = identified_parshas
        parsha = parshas[0] if parshas else None
        holiday = reviewed_optional(reviewed, "holiday", 80) or match_topic(title, HOLIDAY_ALIASES)
        original_id = source["originalDriveId"]
        generated = {"audioDriveId": audio_id}
        if poster_ready:
            generated["posterDriveId"] = source["posterDriveId"]
        if has_hls:
            generated["hls"] = outputs

        public_item = {
            "id": sid, "library": "series", "speaker": speaker,
            "title": title, "year": year, "parsha": parsha, "parshas": parshas, "holiday": holiday,
            "hidden": False, "hasAudio": True, "hasVideo": kind == "video",
            "audioReady": True, "videoReady": has_hls, "hlsReady": has_hls,
            "previewReady": False, "posterReady": poster_ready,
            "audioFormat": "mp3", "mediaType": kind, "status": "ready",
            "created": source.get("generatedAt"), "sourceModified": source.get("sourceModified"),
            "originalFilename": filename, "originalFormat": original_ext,
            "public": True, **fields,
        }
        private_item = {
            "id": sid, "library": "series", "speaker": speaker,
            "title": title, "year": year, "parsha": parsha, "parshas": parshas, "holiday": holiday,
            "hidden": False, "sourceModified": source.get("sourceModified"),
            "created": source.get("generatedAt"), "originalDriveId": original_id,
            "originalFilename": filename,
            "originalMimeType": mimetypes.guess_type(filename)[0] or "application/octet-stream",
            "videoDriveId": original_id if kind == "video" else None,
            "audioDriveId": original_id if kind == "audio" else None,
            "videoFilename": filename if kind == "video" else None,
            "audioFilename": filename if kind == "audio" else None,
            "videoSourceModified": source.get("sourceModified") if kind == "video" else None,
            "audioSourceModified": source.get("sourceModified") if kind == "audio" else None,
            "generated": generated, "status": "ready", **fields,
        }
        pub["items"].append(public_item)
        priv["items"].append(private_item)
        count += 1

    pub["items"].sort(key=lambda x: (-(int(x.get("year") or 0)),
                                      str(x.get("sourceModified") or ""), x["id"]))
    priv["items"].sort(key=lambda x: (x["speaker"].casefold(), -int(x.get("year") or 0), x["id"]))
    speakers = set(pub.get("speakers") or [])
    speakers.update(x["speaker"] for x in pub["items"] if x.get("speaker"))
    pub["speakers"] = sorted(speakers, key=str.casefold)
    return pub, priv
