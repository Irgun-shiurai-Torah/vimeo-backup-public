"""Series-only catalog builder for Irgun test website.

This module has no Google Drive dependencies and never reads the city's catalog.
It groups mixed audio/video uploads into one lecture, infers taxonomy from
filenames, and produces a separate public-safe and private media map.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone

VIDEO_EXTENSIONS = {".mp4", ".mts", ".m2ts", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".mpg", ".mpeg", ".ts", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
AUDIO_PRIORITY = {ext: pos for pos, ext in enumerate([".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".aiff", ".aif", ".wma"])}
VIDEO_PRIORITY = {ext: pos for pos, ext in enumerate([".mp4", ".mov", ".m4v", ".webm", ".mkv", ".mts", ".m2ts", ".avi", ".ts", ".mpeg", ".mpg", ".wmv"])}
FOLDER_MIME = "application/vnd.google-apps.folder"
# Full Review & Publish metadata is stored in the original recording's
# Google Drive description. This remains separate from all city catalog data.
UPLOAD_METADATA_MARKER = "\n\n[IST_SERIES_METADATA_V1]\n"
UPLOAD_FIELD_LIMITS = {
    "titleEnglish": 350, "titleHebrew": 350, "speakerEnglish": 160,
    "speakerHebrew": 160, "rabbiAlternate": 160, "lecturerAlternate": 160,
    "topic": 260, "topicEnglish": 260, "topicHebrew": 260,
    "category": 160, "program": 200, "series": 200, "language": 80,
    "organization": 160, "location": 160, "shiurCode": 100,
    "lectureDate": 20,
}


def read_review_metadata(description):
    """Read only known public display fields; reject malformed metadata."""
    raw = str(description or "")
    if UPLOAD_METADATA_MARKER not in raw:
        return raw[:2400], {}, False
    human, encoded = raw.rsplit(UPLOAD_METADATA_MARKER, 1)
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError):
        return raw[:2400], {}, False
    if not isinstance(payload, dict):
        return raw[:2400], {}, False
    fields = {
        key: str(payload[key]).strip()[:limit]
        for key, limit in UPLOAD_FIELD_LIMITS.items()
        if isinstance(payload.get(key), str)
    }
    if "lectureDate" in fields and fields["lectureDate"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["lectureDate"]):
        fields.pop("lectureDate")
    return human.strip()[:2400], fields, payload.get("hidden") is True


# All names resolve to one canonical English ID. Hebrew aliases cover typical
# Orthodox Hebrew/Yiddish titles; only explicit aliases are used to avoid
# classifying unrelated recordings by an unreliable upload date.
PARSHA_ALIASES = {
    "Bereshit": ["בראשית", "bereshit", "bereishis", "bereishit"],
    "Noach": ["נח", "noach", "noah"],
    "Lech-Lecha": ["לך לך", "לך-לך", "lech lecha", "lech-lecha", "lechlecha", "lekh lekha"],
    "Vayera": ["וירא", "vayera", "vayeira"],
    "Chayei Sara": ["חיי שרה", "chayei sara", "chayei sarah"],
    "Toldot": ["תולדות", "toldot", "toldos"],
    "Vayetzei": ["ויצא", "vayetzei", "vayeitzei"],
    "Vayishlach": ["וישלח", "vayishlach"],
    "Vayeshev": ["וישב", "vayeshev", "vayeishev"],
    "Miketz": ["מקץ", "miketz", "mikeitz"],
    "Vayigash": ["ויגש", "vayigash"],
    "Vayechi": ["ויחי", "vayechi"],
    "Shemot": ["שמות", "shemot", "shmos", "shemos"],
    "Vaera": ["וארא", "vaera", "vaeira"],
    "Bo": ["פרשת בא", "פרשת בו", "parshas bo", "parashat bo"],
    "Beshalach": ["בשלח", "beshalach"],
    "Yitro": ["יתרו", "yitro", "yisro"],
    "Mishpatim": ["משפטים", "mishpatim"],
    "Terumah": ["תרומה", "terumah", "trumah"],
    "Tetzaveh": ["תצוה", "תצווה", "tetzaveh"],
    "Ki Tisa": ["כי תשא", "ki tisa", "ki sisa"],
    "Vayakhel": ["ויקהל", "vayakhel"],
    "Pekudei": ["פקודי", "pekudei"],
    "Vayikra": ["ויקרא", "vayikra"],
    "Tzav": ["פרשת צו", "parshas tzav", "parashat tzav"],
    "Shmini": ["שמיני", "shmini", "shemini"],
    "Tazria": ["תזריע", "tazria"],
    "Metzora": ["מצורע", "metzora"],
    "Achrei Mot": ["אחרי מות", "achrei mot", "acharei mos"],
    "Kedoshim": ["קדושים", "kedoshim", "kedoshim"],
    "Emor": ["אמור", "emor"],
    "Behar": ["בהר", "behar"],
    "Bechukotai": ["בחקותי", "בחוקותי", "bechukotai", "bechukosai"],
    "Bamidbar": ["במדבר", "bamidbar"],
    "Nasso": ["נשא", "nasso", "naso"],
    "Beha'alotcha": ["בהעלותך", "behaalotcha", "beha'alotcha"],
    "Sh'lach": ["שלח", "shlach", "shelach"],
    "Korach": ["קרח", "korach"],
    "Chukat": ["חקת", "חוקת", "chukat", "chukas"],
    "Balak": ["בלק", "balak"],
    "Pinchas": ["פינחס", "פנחס", "pinchas"],
    "Matot": ["מטות", "matot", "matos"],
    "Masei": ["מסעי", "masei", "masey"],
    "Devarim": ["דברים", "devarim", "devarim"],
    "Vaetchanan": ["ואתחנן", "vaetchanan", "vaeschanan"],
    "Eikev": ["עקב", "eikev", "eikev"],
    "Re'eh": ["ראה", "re'eh", "reeh"],
    "Shoftim": ["שופטים", "shoftim"],
    "Ki Teitzei": ["כי תצא", "ki teitzei", "ki seitzei"],
    "Ki Tavo": ["כי תבוא", "ki tavo", "ki sovo"],
    "Nitzavim": ["נצבים", "ניצבים", "nitzavim"],
    "Vayeilech": ["וילך", "vayeilech"],
    "Ha'Azinu": ["האזינו", "haazinu"],
    "Vezot Haberakhah": ["וזאת הברכה", "vezot haberakhah"],
}
HOLIDAY_ALIASES = {
    "Rosh Hashanah": ["ראש השנה", "rosh hashanah", "rosh hashana"],
    "Yom Kippur": ["יום כיפור", "יום הכיפורים", "yom kippur"],
    "Sukkot": ["סוכות", "sukkot", "succos", "sukkos"],
    "Shmini Atzeret": ["שמיני עצרת", "shmini atzeret"],
    "Simchat Torah": ["שמחת תורה", "simchat torah", "simchas torah"],
    "Chanukah": ["חנוכה", "חנכה", "chanukah", "chanuka", "hanukkah"],
    "Tu BiShvat": ["טו בשבט", 'ט"ו בשבט', "tu bishvat", "tu bshvat"],
    "Purim": ["פורים", "purim"],
    "Pesach": ["פסח", "pesach", "passover"],
    "Shavuot": ["שבועות", "shavuos", "shavuot"],
    "Tisha B'Av": ["תשעה באב", "תשעה באב", "tisha bav", "tisha b'av"],
    "Lag BaOmer": ["לג בעומר", "ל״ג בעומר", "lag baomer"],
    "Three Weeks": ["בין המצרים", "three weeks"],
    "Elul": ["אלול", "elul"],
}

def clean_name(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf").strip()

def stem(filename):
    return os.path.splitext(clean_name(filename))[0].strip()

def clean_key(value):
    val = clean_name(value).casefold()
    val = re.sub(r"\s*[\[(](?:audio|video|original|אודיו|וידאו)[\])]\s*$", "", val)
    val = re.sub(r"(?:\s+|[-_])(?:audio|video|original|אודיו|וידאו)$", "", val)
    val = re.sub(r"[\u05f3\u05f4'’\"־\-_.,:;()\[\]{}]+", " ", val)
    return re.sub(r"\s+", " ", val).strip()

def extension(filename):
    return os.path.splitext(clean_name(filename))[1].lower()

def match_topic(title, aliases):
    normalized = " " + clean_key(title) + " "
    # longest alias first avoids 'Sukkot' before Shmini Atzeret-like matches
    options = sorted(((clean_key(alias), key) for key, names in aliases.items() for alias in names),
                     key=lambda pair: len(pair[0]), reverse=True)
    for alias, key in options:
        if alias and (" " + alias + " ") in normalized:
            return key
        # Hebrew titles commonly attach one/two prepositions directly to the
        # holiday name, e.g. לפורים or בחנוכה; require a full-token match.
        if alias and " " not in alias and re.fullmatch(r"[\u0590-\u05ff]+", alias):
            if re.search(r"(?<!\S)[בלמהוכש]{1,2}" + re.escape(alias) + r"(?!\S)", normalized):
                return key
    return None

def lecture_id(speaker, year, key):
    raw = f"irgun-series-v1:{clean_key(speaker)}:{year}:{key}"
    return "series-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

def pick_media(files, priority):
    return min(files, key=lambda x: (priority.get(extension(x.get("name")), 999),
                                    str(x.get("createdTime") or ""), str(x.get("id") or ""))) if files else None

def choose_speaker_photo(records):
    """Choose a safe portrait image by filename preference, never an SVG."""
    allowed={"image/jpeg","image/png","image/webp"}
    priority={"speaker":0,"portrait":1,"profile":2,"photo":3,"headshot":4}
    photos=[]
    for file in records:
        name=str(file.get("name") or "")
        mime=str(file.get("mimeType") or "")
        if not name or name.startswith("_") or mime not in allowed or not file.get("id"):
            continue
        try:
            if file.get("size") and int(file["size"])>25*1024*1024:continue
        except (ValueError,TypeError):
            continue
        stem=name.rsplit(".",1)[0].casefold().strip()
        photos.append((priority.get(stem,10),name.casefold(),file))
    if not photos:return None
    chosen=min(photos,key=lambda x:(x[0],x[1]))[2]
    return {"id":chosen["id"],"mimeType":chosen["mimeType"]}


def speaker_names_from_folder_entries(records):
    """Public-safe speaker names from Drive (including empty folders)."""
    folder_mime = "application/vnd.google-apps.folder"
    return sorted(
        {item["name"] for item in records
         if item.get("mimeType") == folder_mime
         and item.get("name") and not item["name"].startswith("_")},
        key=str.casefold,
    )


def build_maps(source_files, previous_private=None, generated_at=None):
    """Return (public catalog, private catalog). No private Drive IDs in public."""
    now = generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    previous = previous_private.get("items", []) if isinstance(previous_private, dict) else []
    prior_by_file = {}
    for item in previous:
        for fid in (item.get("videoDriveId"), item.get("audioDriveId"), item.get("originalDriveId")):
            if fid: prior_by_file[str(fid)] = item
    grouped = {}
    for row in source_files:
        file = row["file"]
        ext = extension(file.get("name", ""))
        if ext not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS: continue
        speaker, year = clean_name(row["speaker"]), clean_name(row["year"])
        if not re.fullmatch(r"5\d{3}", year): continue
        grouping = (speaker.casefold(), year, clean_key(stem(file.get("name"))))
        grouped.setdefault(grouping, {"speaker": speaker, "year": int(year), "audio": [], "video": []})
        grouped[grouping]["video" if ext in VIDEO_EXTENSIONS else "audio"].append(file)
    priv, public, used_ids = [], [], set()
    for key, g in sorted(grouped.items(), key=lambda p: p[0]):
        video, audio = pick_media(g["video"], VIDEO_PRIORITY), pick_media(g["audio"], AUDIO_PRIORITY)
        main = video or audio
        fileids = [str(x.get("id")) for x in (video, audio) if x]
        old = next((prior_by_file[fid] for fid in fileids if fid in prior_by_file), None)
        uid = str(old.get("id")) if old else lecture_id(g["speaker"], g["year"], key[2])
        if uid in used_ids:  # file paired in an earlier group after a rename
            uid = lecture_id(g["speaker"], g["year"], key[2])
        used_ids.add(uid)
        title = stem(main["name"]).replace("_", " ")
        modified = str(main.get("modifiedTime") or main.get("createdTime") or "")
        # If two formats share a title, either source can carry metadata.
        meta_source = next((f for f in (main, video, audio) if f and
                            UPLOAD_METADATA_MARKER in str(f.get("description") or "")), main)
        description, extra_metadata, hidden = read_review_metadata(meta_source.get("description"))
        props = main.get("appProperties") or {}
        meta_props = meta_source.get("appProperties") or {}
        parsha = clean_name(meta_props.get("irgunSeriesParsha") or props.get("irgunSeriesParsha")) or match_topic(title, PARSHA_ALIASES)
        holiday = clean_name(meta_props.get("irgunSeriesHoliday") or props.get("irgunSeriesHoliday")) or match_topic(title, HOLIDAY_ALIASES)
        old_video = bool(old and video and old.get("videoDriveId") == video.get("id")
                         and old.get("videoSourceModified") == video.get("modifiedTime"))
        old_audio = bool(old and audio and old.get("audioDriveId") == audio.get("id")
                         and old.get("audioSourceModified") == audio.get("modifiedTime"))
        generated = (old.get("generated", {}) if old and (old_video or (not video and old_audio)) else {})
        has_audio = bool(audio or generated.get("audioDriveId"))
        hls = generated.get("hls", {}) if isinstance(generated.get("hls", {}), dict) else {}
        # Video is available only when HLS is ready. Preserve source originals for download.
        is_video_playable = bool(video and hls.get("masterDriveId"))
        is_audio_playable = bool(audio and extension(audio["name"]) in {".mp3", ".m4a", ".aac", ".ogg", ".opus"} or generated.get("audioDriveId"))
        private = {
            "id": uid, "library": "series", "speaker": g["speaker"], "year": g["year"],
            "title": title, "parsha": parsha, "holiday": holiday,
            "description": description, "hidden": hidden, **extra_metadata,
            "videoDriveId": str(video["id"]) if video else None,
            "audioDriveId": str(audio["id"]) if audio else None,
            "videoSourceModified": str(video.get("modifiedTime") or "") if video else None,
            "audioSourceModified": str(audio.get("modifiedTime") or "") if audio else None,
            "originalDriveId": str(main["id"]), "originalFilename": clean_name(main["name"]),
            "originalMimeType": str(main.get("mimeType") or "application/octet-stream"),
            "videoFilename": clean_name(video["name"]) if video else None,
            "audioFilename": clean_name(audio["name"]) if audio else None,
            "sourceModified": modified, "created": main.get("createdTime"),
            "generated": generated, "status": "ready" if is_audio_playable or is_video_playable else "processing",
            "duplicateInputCount": max(0, len(g["audio"])-1) + max(0,len(g["video"])-1),
        }
        priv.append(private)
        safe = {
            "id": uid, "library": "series", "speaker": g["speaker"], "year": g["year"],
            "title": title, "parsha": parsha, "holiday": holiday,
            "description": description, "hidden": hidden, **extra_metadata,
            "hasAudio": has_audio, "hasVideo": bool(video),
            "audioReady": is_audio_playable, "videoReady": is_video_playable,
            "hlsReady": bool(hls.get("masterDriveId")),
            "previewReady": bool(generated.get("previewMp4DriveId")),
            "posterReady": bool(generated.get("posterDriveId")),
            "audioFormat": extension(audio["name"]).lstrip(".") if audio and extension(audio["name"]) in {".mp3",".m4a",".aac",".ogg",".opus"} else ("mp3" if generated.get("audioDriveId") else None),
            "mediaType": "video" if video else "audio",
            "status": private["status"],
            "created": main.get("createdTime"), "sourceModified": modified,
            "originalFilename": clean_name(main["name"]),
            "originalFormat": extension(main["name"]).lstrip("."),
            "audioPlaybackUrl": f"/api/series/media/{uid}/audio" if is_audio_playable else None,
            "videoPlaybackUrl": f"/api/series/media/{uid}/video" if is_video_playable else None,
            "originalDownloadUrl": f"/api/series/media/{uid}/original",
            "audioDownloadUrl": f"/api/series/media/{uid}/audio?download=1" if is_audio_playable else None,
            "public": True,
        }
        public.append(safe)
    public.sort(key=lambda x: (-x["year"], x.get("sourceModified") or "", x["id"]), reverse=False)
    # Sort years from most recent to oldest; within a year newest source first.
    public.sort(key=lambda x: x.get("sourceModified") or "", reverse=True)
    public.sort(key=lambda x: x["year"], reverse=True)
    priv.sort(key=lambda x: (x["speaker"], -x["year"], x["id"]))
    return (
        {"version": 1, "library": "series", "mode": "staging", "generatedAt": now, "items": public},
        {"version": 1, "library": "series", "generatedAt": now, "items": priv},
    )
