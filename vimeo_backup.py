import json
import os
import pickle
import re
import subprocess
import time
import requests
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from tqdm import tqdm

# ==============================
# SETTINGS
# ==============================

BASE_FOLDER = "Vimeo Backup"
RUN_STARTED = time.monotonic()
TOKEN = os.environ["VIMEO_TOKEN"]

# Vimeo showcases are discovered automatically on every backup run.
# This fallback is used only if Vimeo showcase discovery is temporarily unavailable.
FALLBACK_SHOWCASES = [
    {"id": "12369119", "name": "5787 - Rav Zev Smith Shiurim"},
    {"id": "11418895", "name": "Lakewood Chol Hamoed"},
    {"id": "12307671", "name": "5786 Summer Flatbush Shiurim"},
    {"id": "11898343", "name": "5786 Sunday - Rav Zev Smith"},
    {"id": "11898340", "name": "5786 Daily & Chol Hamoed"},
    {"id": "11777050", "name": "5785 Summer Flatbush Shiurim"},
    {"id": "11403516", "name": "5785 Sunday - Rav Zev Smith"},
    {"id": "11403467", "name": "5785 Daily & Chol Hamoed"},
    {"id": "11420419", "name": "Parsha - Rav Elozer Nissen Rubin"},
    {"id": "12330788", "name": "5784 and Older Sunday - Rav Zev Smith Shiurim"},
    {"id": "12330674", "name": "5784 and Older Daily & Chol Hamoed"},
    {"id": "12330572", "name": "5784 and Older Summer Flatbush Shiurim"},
]
SHOWCASE_EXCLUDE_IDS = set()

# False is recommended when Google Drive is the permanent backup.
# It prevents a self-hosted Windows runner from filling its disk.
KEEP_LOCAL_FILES = False

REQUEST_TIMEOUT = 60

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.vimeo.*+json;version=3.4",
}

VIMEO_SESSION = requests.Session()
VIMEO_SESSION.headers.update(HEADERS)


# ==============================
# GOOGLE DRIVE OAUTH
# ==============================

print("Connecting to Google Drive...")

SCOPES = ["https://www.googleapis.com/auth/drive"]
creds = None

if os.path.exists("token.pickle"):
    with open("token.pickle", "rb") as token:
        creds = pickle.load(token)

if not creds:
    raise Exception("token.pickle missing")

if creds.expired and creds.refresh_token:
    creds.refresh(Request())

with open("token.pickle", "wb") as token:
    pickle.dump(creds, token)

drive_service = build("drive", "v3", credentials=creds)
print("Google Drive connected")


# ==============================
# LOCAL FILES
# ==============================

os.makedirs(BASE_FOLDER, exist_ok=True)

local_mp4_count = sum(
    len([f for f in files if f.endswith(".mp4")])
    for _, _, files in os.walk(BASE_FOLDER)
)
print(f"Existing local videos: {local_mp4_count}")

VIDEO_MAP_FILE = os.path.join(BASE_FOLDER, "video-map.json")
AUDIO_MAP_FILE = os.path.join(BASE_FOLDER, "audio-map.json")
DOWNLOAD_MAP_FILE = os.path.join(BASE_FOLDER, "download-map.json")


# ==============================
# GOOGLE DRIVE HELPERS
# ==============================


DRIVE_FOLDER_CACHE = {}


def get_or_create_drive_folder(folder_name, parent_id=None):
    # Cache folder IDs for this run. The old fast script did not repeatedly query
    # Google Drive for the same root/showcase folder for every already-backed video.
    cache_key = (str(parent_id or ""), str(folder_name))
    if cache_key in DRIVE_FOLDER_CACHE:
        return DRIVE_FOLDER_CACHE[cache_key]

    query = (
        f"name='{folder_name}' "
        "and mimeType='application/vnd.google-apps.folder' "
        "and trashed=false"
    )
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = (
        drive_service.files()
        .list(q=query, spaces="drive", fields="files(id,name)")
        .execute()
    )

    if results.get("files"):
        folder_id = results["files"][0]["id"]
        DRIVE_FOLDER_CACHE[cache_key] = folder_id
        return folder_id

    body = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]

    folder = drive_service.files().create(body=body, fields="id").execute()
    folder_id = folder["id"]
    DRIVE_FOLDER_CACHE[cache_key] = folder_id
    print("Created folder:", folder_name)
    return folder_id


def find_file_in_drive(filename, folder_id):
    safe_filename = filename.replace("'", "\\'")
    result = (
        drive_service.files()
        .list(
            q=f"name='{safe_filename}' and '{folder_id}' in parents and trashed=false",
            spaces="drive",
            fields="files(id,name)",
        )
        .execute()
    )
    files = result.get("files", [])
    return files[0]["id"] if files else None


def upload_to_drive(filepath, folder_id):
    filename = os.path.basename(filepath)
    safe_filename = filename.replace("'", "\\'")

    existing = (
        drive_service.files()
        .list(
            q=f"name='{safe_filename}' and '{folder_id}' in parents and trashed=false",
            spaces="drive",
            fields="files(id)",
        )
        .execute()
    )

    media = MediaFileUpload(filepath, resumable=True)

    if existing.get("files"):
        file_id = existing["files"][0]["id"]
        print("Updating:", filename)
        drive_service.files().update(
            fileId=file_id, media_body=media
        ).execute()
        return file_id

    print("Uploading:", filename)
    body = {"name": filename, "parents": [folder_id]}
    request = drive_service.files().create(
        body=body, media_body=media, fields="id"
    )

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Upload {int(status.progress()*100)}%")

    print("Uploaded:", response["id"])
    return response["id"]


# ==============================
# LOAD MAPS & HELPERS
# ==============================

video_map = []
audio_map = []
MAP_LOAD_FAILED = False


def clean_filename(name):
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def load_drive_map(filename):
    root_folder_id = get_or_create_drive_folder("Vimeo Backup")
    maps_folder_id = get_or_create_drive_folder("Maps", root_folder_id)

    results = (
        drive_service.files()
        .list(
            q=f"name='{filename}' and '{maps_folder_id}' in parents and trashed=false",
            spaces="drive",
            fields="files(id,name)",
        )
        .execute()
    )

    files = results.get("files", [])
    if not files:
        print("No Drive map:", filename)
        return []

    try:
        data = (
            drive_service.files()
            .get_media(fileId=files[0]["id"])
            .execute()
        )
        return json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to decode JSON from Drive for {filename}: {e}")
        return None
    except Exception as e:
        print(f"ERROR: Could not load Drive map {filename}: {e}")
        return None


# Sync maps from Drive
print("\nLoading Drive maps...")
drive_video_map = load_drive_map("video-map.json")
if drive_video_map is None:
    MAP_LOAD_FAILED = True
elif drive_video_map:
    video_map = drive_video_map
    print("Drive videos loaded:", len(video_map))

drive_audio_map = load_drive_map("audio-map.json")
if drive_audio_map is None:
    MAP_LOAD_FAILED = True
elif drive_audio_map:
    audio_map = drive_audio_map
    print("Drive audios loaded:", len(audio_map))
    print("Total audios mapped after sync:", len(audio_map))

# O(1) map lookups. With thousands of shiurim, repeatedly scanning the full
# video/audio arrays for every Vimeo item adds unnecessary runtime.
video_map_index = {
    str(item.get("vimeoId")): item
    for item in video_map
    if item.get("vimeoId") is not None
}
audio_map_index = {
    str(item.get("vimeoId")): item
    for item in audio_map
    if item.get("vimeoId") is not None
}

FAST_SKIPS = 0
REPAIRED_ITEMS = 0
NEW_BACKUPS = 0


# ==============================
# VIMEO API HELPERS
# ==============================


def get_showcases():
    """Return every showcase owned by the authenticated Vimeo account."""
    showcases = []
    page = 1
    try:
        while True:
            url = (
                "https://api.vimeo.com/me/albums"
                f"?page={page}&per_page=100&fields=uri,name,total_clips,created_time,modified_time"
            )
            r = VIMEO_SESSION.get(url, timeout=60)
            r.raise_for_status()
            data = r.json()
            for item in data.get("data", []):
                uri = str(item.get("uri") or "")
                match = re.search(r"/albums/(\d+)", uri)
                showcase_id = match.group(1) if match else ""
                name = str(item.get("name") or "").strip()
                if not showcase_id or not name or showcase_id in SHOWCASE_EXCLUDE_IDS:
                    continue
                showcases.append({
                    "id": showcase_id,
                    "name": name,
                    "totalClips": int(item.get("total_clips") or 0),
                })
            if not data.get("paging", {}).get("next"):
                break
            page += 1
        showcases = list({item["id"]: item for item in showcases}.values())
        def sort_key(item):
            year_match = re.search(r"\b(57\d{2})\b", item.get("name", ""))
            year = int(year_match.group(1)) if year_match else 0
            return (-year, item.get("name", "").lower())
        showcases.sort(key=sort_key)
        if not showcases:
            raise RuntimeError("Vimeo returned no showcases")
        print(f"Automatically discovered {len(showcases)} Vimeo showcases")
        return showcases
    except Exception as e:
        print(f"WARNING: automatic showcase discovery failed: {e}")
        print("Using fallback showcase list")
        return [dict(item) for item in FALLBACK_SHOWCASES]


def get_videos(showcase_id):
    videos = []
    page = 1
    while True:
        url = f"https://api.vimeo.com/albums/{showcase_id}/videos?page={page}&per_page=100"
        r = VIMEO_SESSION.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        videos.extend(data["data"])
        if not data["paging"].get("next"):
            break
        page += 1
    return videos


def get_vimeo_download(video_id):
    """
    Returns ONLY real Vimeo download links.
    Never returns /playback/ links.
    """
    try:
        r = VIMEO_SESSION.get(
            f"https://api.vimeo.com/videos/{video_id}", timeout=REQUEST_TIMEOUT
        )
        if r.status_code != 200:
            print("Vimeo API error:", video_id, r.status_code)
            return None

        detail = r.json()
        downloads = detail.get("download", [])
        valid = []

        for item in downloads:
            link = item.get("link")
            if not link:
                continue

            if "/download/" not in link:
                continue

            valid.append(item)

        if not valid:
            print("NO DOWNLOAD LINK:", video_id)
            return None

        best = max(valid, key=lambda x: x.get("width", 0))
        return best["link"]

    except Exception as e:
        print(f"Error fetching Vimeo link for {video_id}: {e}")
        return None


def convert_to_audio(video_path):
    audio_path = os.path.splitext(video_path)[0] + ".mp3"
    if os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
        return audio_path

    print("Creating audio:", audio_path)
    command = [
        "ffmpeg",
        "-i",
        video_path,
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        audio_path,
        "-y",
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0 or not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise RuntimeError(f"FFmpeg failed to create audio for {video_path}")
    return audio_path


def safe_showcase_folder_name(name, showcase_id):
    cleaned = clean_filename(str(name or "")).rstrip(". ")
    if not cleaned:
        cleaned = f"Showcase {showcase_id}"
    return cleaned[:120]


def remove_local_file(path):
    if KEEP_LOCAL_FILES:
        return
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as e:
        print(f"Warning: could not delete local file {path}: {e}")


def upsert_audio_map(video_id, title, audio_drive_id):
    if not audio_drive_id:
        return

    key = str(video_id)
    entry = {
        "vimeoId": video_id,
        "title": title,
        "audioDriveId": audio_drive_id,
    }
    existing_audio = audio_map_index.get(key)
    if existing_audio:
        existing_audio.update(entry)
    else:
        audio_map.append(entry)
        audio_map_index[key] = entry


def download_vimeo_file(vimeo_link, filepath, title):
    if not vimeo_link:
        raise RuntimeError(f"No Vimeo download link available for: {title}")

    print("\n====================")
    print("DOWNLOADING:", title)
    print("====================")

    with requests.get(vimeo_link, stream=True, timeout=REQUEST_TIMEOUT) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(filepath, "wb") as f:
            with tqdm(total=total, unit="B", unit_scale=True) as bar:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        bar.update(len(chunk))

    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        raise RuntimeError(f"Vimeo download produced an empty file: {title}")


# ==============================
# PROCESS VIDEOS
# ==============================


def download_video(video, folder, showcase_id=None):
    global FAST_SKIPS, REPAIRED_ITEMS, NEW_BACKUPS

    video_id = str(video.get("uri") or "").split("/")[-1]
    title = str(video.get("name") or "").strip() or f"Vimeo video {video_id}"

    if not video_id:
        raise RuntimeError("Vimeo video is missing an ID")

    # Original special-case behavior retained.
    if showcase_id == "11777050" and title.startswith("IST-SF7"):
        title = title.replace("5785", "5786")

    existing = video_map_index.get(video_id)
    audio_existing = audio_map_index.get(video_id)

    video_drive_id = existing.get("videoDriveId") if existing else None
    audio_drive_id = (
        (existing.get("audioDriveId") if existing else None)
        or (audio_existing.get("audioDriveId") if audio_existing else None)
    )
    saved_vimeo_link = existing.get("vimeoVideo") if existing else None

    # CRITICAL FAST PATH: do this BEFORE any Google Drive folder/file queries.
    # This restores the ~old behavior for the thousands of videos already backed up.
    if video_drive_id and audio_drive_id:
        if existing and not existing.get("audioDriveId"):
            existing["audioDriveId"] = audio_drive_id
        upsert_audio_map(video_id, title, audio_drive_id)

        # Only call Vimeo for the uncommon case where this old map entry is missing
        # its Vimeo download link. Complete entries require no per-video network calls.
        if existing and not saved_vimeo_link:
            fresh_link = get_vimeo_download(video_id)
            if fresh_link:
                existing["vimeoVideo"] = fresh_link

        FAST_SKIPS += 1
        print(f"Already backed up: {title}")
        return

    # Only incomplete/new items reach Google Drive folder/file lookups.
    created_date = str(video.get("created_time") or "")[:10] or "unknown-date"
    filename = clean_filename(created_date + " - " + title + ".mp4")
    filepath = os.path.join(folder, filename)
    audio_path = os.path.splitext(filepath)[0] + ".mp3"

    root_folder_id = get_or_create_drive_folder("Vimeo Backup")
    drive_folder_id = get_or_create_drive_folder(
        os.path.basename(folder), root_folder_id
    )

    if not video_drive_id:
        video_drive_id = find_file_in_drive(filename, drive_folder_id)
    if not audio_drive_id:
        audio_drive_id = find_file_in_drive(
            os.path.basename(audio_path), drive_folder_id
        )

    # Drive already has both files: repair maps only.
    if video_drive_id and audio_drive_id:
        fresh_link = saved_vimeo_link or get_vimeo_download(video_id)
        entry = {
            "vimeoId": video_id,
            "title": title,
            "videoDriveId": video_drive_id,
            "audioDriveId": audio_drive_id,
            "vimeoVideo": fresh_link,
        }
        if existing:
            existing.update(entry)
        else:
            video_map.append(entry)
            video_map_index[video_id] = entry
        upsert_audio_map(video_id, title, audio_drive_id)
        REPAIRED_ITEMS += 1
        print(f"Found complete backup in Drive: {title}")
        return

    # If either permanent backup is missing, obtain the Vimeo source once and
    # create/upload only the missing Drive file(s).
    vimeo_link = get_vimeo_download(video_id)
    if not vimeo_link:
        raise RuntimeError(f"No Vimeo download link available for: {title}")

    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        download_vimeo_file(vimeo_link, filepath, title)

    if not video_drive_id:
        video_drive_id = upload_to_drive(filepath, drive_folder_id)

    if not audio_drive_id:
        audio_path = convert_to_audio(filepath)
        audio_drive_id = upload_to_drive(audio_path, drive_folder_id)

    entry = {
        "vimeoId": video_id,
        "title": title,
        "videoDriveId": video_drive_id,
        "audioDriveId": audio_drive_id,
        "vimeoVideo": vimeo_link,
    }

    if existing:
        existing.update(entry)
    else:
        video_map.append(entry)
        video_map_index[video_id] = entry

    upsert_audio_map(video_id, title, audio_drive_id)
    NEW_BACKUPS += 1

    remove_local_file(audio_path)
    remove_local_file(filepath)


# ==============================
# RUN SHOWCASES
# ==============================

SHOWCASES = get_showcases()
showcase_failures = []

for showcase in SHOWCASES:
    print("\n====================")
    print(showcase["name"])
    print("====================")

    try:
        folder_name = safe_showcase_folder_name(showcase["name"], showcase["id"])
        folder = os.path.join(BASE_FOLDER, folder_name)
        os.makedirs(folder, exist_ok=True)

        videos = get_videos(showcase["id"])
        print("Videos found:", len(videos))

        for video in videos:
            try:
                download_video(video, folder, showcase_id=showcase["id"])
            except Exception as e:
                print(f"ERROR backing up video in {showcase['name']}: {e}")

    except Exception as e:
        showcase_failures.append((showcase["id"], showcase["name"], str(e)))
        print(f"ERROR reading showcase {showcase['name']} ({showcase['id']}): {e}")


# ==============================
# REPAIR PASS: BACKFILL VIMEO LINKS
# ==============================

print("\n====================")
print("REPAIR PASS: CHECKING VIMEO LINKS")
print("====================")

for item in video_map:
    if not item.get("vimeoVideo"):
        print(f"Refreshing missing Vimeo link for ID {item['vimeoId']} ({item['title']})...")
        link = get_vimeo_download(item["vimeoId"])
        if link:
            item["vimeoVideo"] = link


def dedupe_by_vimeo_id(items):
    deduped = {}
    order = []
    for item in items:
        key = str(item.get("vimeoId") or "").strip()
        if not key:
            continue
        if key not in deduped:
            order.append(key)
            deduped[key] = dict(item)
        else:
            deduped[key].update({k: v for k, v in item.items() if v is not None})
    return [deduped[key] for key in order]


video_map[:] = dedupe_by_vimeo_id(video_map)
audio_map[:] = dedupe_by_vimeo_id(audio_map)


# ==============================
# SAVE LOCAL & GENERATE DOWNLOAD MAP
# ==============================

print("\n====================")
print("SAVING MAP FILES")
print("====================")
print(f"Total videos: {len(video_map)}")

with open(VIDEO_MAP_FILE, "w", encoding="utf-8") as f:
    json.dump(video_map, f, indent=2, ensure_ascii=False)

with open(AUDIO_MAP_FILE, "w", encoding="utf-8") as f:
    json.dump(audio_map, f, indent=2, ensure_ascii=False)

print("\n====================")
print("CREATING DOWNLOAD MAP")
print("====================")

download_map = []
for item in video_map:
    download_map.append(
        {
            "vimeoId": item["vimeoId"],
            "title": item["title"],
            "video": (
                f"https://drive.google.com/uc?export=download&id={item.get('videoDriveId')}"
                if item.get("videoDriveId")
                else None
            ),
            "audio": (
                f"https://drive.google.com/uc?export=download&id={item.get('audioDriveId')}"
                if item.get("audioDriveId")
                else None
            ),
            "vimeoVideo": item.get("vimeoVideo"),
        }
    )

with open(DOWNLOAD_MAP_FILE, "w", encoding="utf-8") as f:
    json.dump(download_map, f, indent=2, ensure_ascii=False)


# ==============================
# UPLOADING MAPS TO GOOGLE DRIVE
# ==============================

print("\n====================")
print("COMPLETE")
print("====================")
print("Updating Google Drive maps...")

if MAP_LOAD_FAILED:
    raise RuntimeError(
        "Existing Drive map files could not be read safely. "
        "Media backups may have succeeded, but map files were NOT overwritten. "
        "Run the backup again after Google Drive access is working."
    )

if showcase_failures:
    print("\nWARNING: Some showcases could not be read:")
    for showcase_id, showcase_name, error in showcase_failures:
        print(f"  - {showcase_id} | {showcase_name}: {error}")
    print("Existing map entries were preserved; successful updates will still be saved.")

root_folder_id = get_or_create_drive_folder("Vimeo Backup")
maps_folder_id = get_or_create_drive_folder("Maps", root_folder_id)

upload_to_drive(VIDEO_MAP_FILE, maps_folder_id)
upload_to_drive(AUDIO_MAP_FILE, maps_folder_id)
upload_to_drive(DOWNLOAD_MAP_FILE, maps_folder_id)

print("\n====================")
print("BACKUP RUN SUMMARY")
print("====================")
print(f"Already backed up (fast skipped): {FAST_SKIPS}")
print(f"Map/Drive repairs: {REPAIRED_ITEMS}")
print(f"New/incomplete backups processed: {NEW_BACKUPS}")
print(f"Total runtime: {(time.monotonic() - RUN_STARTED) / 60:.1f} minutes")
