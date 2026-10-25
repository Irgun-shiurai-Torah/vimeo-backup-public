#!/usr/bin/env python3
"""Read approved Dropbox folder links and stage recordings in Google Drive.

The only Dropbox access is through explicit shared links. No Dropbox account
is joined, copied into, or modified. Media is NOT published to the website;
this importer has its own private Google Drive map and output folders.
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from series_media import make_hls, make_mp3, make_poster, probe, quality_ladder
from dropbox_media_policy import is_excluded_nonlecture
from series_visibility import ALLOWED_DROPBOX_SOURCES

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".mkv", ".avi",
             ".webm", ".wmv", ".mpg", ".mpeg", ".ts"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav",
             ".wma", ".aif", ".aiff"}
DRIVE_FOLDER = "application/vnd.google-apps.folder"
CHUNK = 16 * 1024 * 1024
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def timestamp():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def source_id(url):
    """Ignore disposable Dropbox UI parameters; retain the folder and rlkey."""
    parts = urlsplit(url)
    keys = parse_qs(parts.query)
    canonical = parts.netloc.lower() + parts.path + "?rlkey=" + (keys.get("rlkey") or [""])[0]
    return "dbx-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def file_id(source, item):
    unique = str(item.get("id") or item["path"])
    return "dbx-media-" + hashlib.sha256((source + "\n" + unique).encode("utf-8")).hexdigest()[:24]


def sources_from_env(env=os.environ):
    sources = []
    extra = str(env.get("DROPBOX_SOURCES_JSON") or "").strip()
    if extra:
        # A complete explicit source list supersedes the original single-link
        # test secret so old unrelated recordings aren't unexpectedly imported.
        decoded = json.loads(extra)
        if not isinstance(decoded, list):
            raise ValueError("DROPBOX_SOURCES_JSON must contain an array of source objects")
        sources.extend(decoded)
    else:
        original = str(env.get("DROPBOX_SHARED_LINK") or "").strip()
        if original:
            sources.append({"url": original,
                            "password": str(env.get("DROPBOX_LINK_PASSWORD") or ""),
                            "label": "Initial Dropbox test source"})
    result = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("url"), str):
            raise ValueError("Every Dropbox source needs a url")
        url = source["url"].strip()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in ("dropbox.com", "www.dropbox.com")
                or not parsed.path.startswith(("/scl/fo/", "/sh/"))):
            raise ValueError("Only HTTPS Dropbox shared-folder links are supported")
        password = source.get("password") or ""
        if not isinstance(password, str):
            raise ValueError("Source password must be a string")
        label = source.get("label") or source.get("name") or "Unnamed Dropbox collection"
        if (not isinstance(label, str) or not label.strip() or len(label) > 120
                or any(ord(ch) < 32 for ch in label)):
            raise ValueError("Each source label must be a short, printable name")
        label = label.strip()
        sid = source_id(url)
        if sid in result and result[sid]["password"] != password:
            raise ValueError("Conflicting passwords for the same Dropbox folder link")
        result[sid] = {"id": sid, "url": url, "password": password, "label": label}
    if not result:
        raise ValueError("Set DROPBOX_SHARED_LINK or DROPBOX_SOURCES_JSON")
    return list(result.values())


class DropboxLinks:
    def __init__(self, key, secret):
        if not key or not secret:
            raise ValueError("Set DROPBOX_APP_KEY and DROPBOX_APP_SECRET as GitHub Actions secrets")
        self.key, self.secret = key, secret
        self._token, self._expires = "", 0

    def token(self):
        if self._token and time.time() < self._expires - 180:
            return self._token
        basic = base64.b64encode((self.key + ":" + self.secret).encode("utf-8")).decode("ascii")
        req = Request("https://api.dropboxapi.com/oauth2/token", data=b"grant_type=client_credentials",
                      headers={"Authorization": "Basic " + basic,
                               "Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urlopen(req, timeout=60) as response:
                data = json.load(response)
        except HTTPError as err:
            raise RuntimeError(f"Dropbox app authentication failed (HTTP {err.code}). Check App Key/Secret.") from None
        self._token = data["access_token"]
        self._expires = time.time() + int(data.get("expires_in", 14400))
        return self._token

    def _request(self, route, data, output=None):
        is_download = output is not None
        host = "https://content.dropboxapi.com" if is_download else "https://api.dropboxapi.com"
        for attempt in range(5):
            headers = {"Authorization": "Bearer " + self.token()}
            if is_download:
                headers["Dropbox-API-Arg"] = json.dumps(data, ensure_ascii=True)
                request = Request(host + route, data=b"", headers=headers, method="POST")
            else:
                headers["Content-Type"] = "application/json"
                request = Request(host + route, data=json.dumps(data).encode("utf-8"),
                                  headers=headers, method="POST")
            try:
                with urlopen(request, timeout=180) as response:
                    if is_download:
                        with open(output, "wb") as handle:
                            while True:
                                chunk = response.read(4 * 1024 * 1024)
                                if not chunk:
                                    break
                                handle.write(chunk)
                        return None
                    return json.load(response)
            except (HTTPError, URLError, TimeoutError, ConnectionError) as err:
                code = getattr(err, "code", None)
                if code == 401:
                    self._token = ""
                if attempt == 4 or (code is not None and code not in RETRYABLE and code != 401):
                    raise RuntimeError(f"Dropbox {route} failed ({'HTTP ' + str(code) if code else 'network error'}). "
                                       "Check link expiry, viewing/download permissions, and app scopes.") from None
                retry = getattr(err, "headers", None)
                retry_after = retry.get("Retry-After", "") if retry else ""
                delay = min(45, int(retry_after)) if retry_after.isdigit() else min(30, 2 ** attempt)
                time.sleep(delay)
        raise RuntimeError("Dropbox API retry limit exceeded")

    def list_dir(self, source, path=""):
        shared = {"url": source["url"]}
        if source["password"]:
            shared["password"] = source["password"]
        result = self._request("/2/files/list_folder", {
            "path": path, "recursive": False, "limit": 1000, "shared_link": shared
        })
        while True:
            for entry in result.get("entries", []):
                yield entry
            if not result.get("has_more"):
                return
            result = self._request("/2/files/list_folder/continue", {"cursor": result["cursor"]})

    def list_tree(self, source):
        folders = 0
        queue = [""]
        files = []
        while queue:
            parent = queue.pop(0)
            for entry in self.list_dir(source, parent):
                name = entry.get("name", "")
                if not name or "/" in name or name in (".", ".."):
                    continue
                path = parent + "/" + name
                if entry.get(".tag") == "folder":
                    folders += 1
                    queue.append(path)
                elif entry.get(".tag") == "file":
                    ext = Path(name).suffix.casefold()
                    if ext in VIDEO_EXT | AUDIO_EXT and not name.startswith("._"):
                        files.append({"id": entry.get("id"), "path": path, "name": name,
                                      "size": int(entry.get("size") or 0),
                                      "rev": entry.get("rev") or entry.get("server_modified") or "",
                                      "modified": entry.get("server_modified") or "",
                                      "kind": "video" if ext in VIDEO_EXT else "audio"})
        return folders, files

    def download(self, source, relative_path, destination):
        params = {"url": source["url"], "path": relative_path}
        if source["password"]:
            params["link_password"] = source["password"]
        self._request("/2/sharing/get_shared_link_file", params, output=destination)
        if not os.path.getsize(destination):
            raise RuntimeError("Dropbox returned an empty media file")


class DriveStore:
    def __init__(self, credentials_json):
        if not credentials_json:
            raise ValueError("GOOGLE_TOKEN_JSON GitHub secret is required for import mode")
        info = json.loads(credentials_json)
        scopes = info.get("scopes") or ["https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_authorized_user_info(info, scopes=scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        if not creds.valid:
            raise ValueError("Google Drive OAuth credentials are invalid")
        self.api = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.cache = {}

    def find(self, parent, name):
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and mimeType != '{DRIVE_FOLDER}' and trashed = false"
        if parent:
            query = f"'{parent}' in parents and " + query
        result = self.api.files().list(q=query, spaces="drive", fields="files(id,name,mimeType)",
                                       pageSize=100).execute(num_retries=5)
        return (result.get("files") or [None])[0]

    def folder(self, parent, name):
        cache_key = (parent or "", name)
        if cache_key in self.cache:
            return self.cache[cache_key]
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped}' and mimeType = '{DRIVE_FOLDER}' and trashed = false"
        if parent:
            query = f"'{parent}' in parents and " + query
        result = self.api.files().list(q=query, spaces="drive", fields="files(id)",
                                       pageSize=100).execute(num_retries=5)
        found = result.get("files") or []
        if found:
            folder_id = found[0]["id"]
        else:
            meta = {"name": name, "mimeType": DRIVE_FOLDER}
            if parent:
                meta["parents"] = [parent]
            folder_id = self.api.files().create(body=meta, fields="id").execute(num_retries=5)["id"]
        self.cache[cache_key] = folder_id
        return folder_id

    def read_map(self, maps_parent):
        item = self.find(maps_parent, "dropbox-private-map.json")
        if not item:
            return {"version": 1, "items": {}}
        data = self.api.files().get_media(fileId=item["id"]).execute(num_retries=5)
        parsed = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        if parsed.get("version") != 1 or not isinstance(parsed.get("items"), dict):
            raise ValueError("Unexpected Dropbox map format; refusing to overwrite it")
        return parsed

    def upload(self, path, parent, name=None, mime=None):
        name = name or os.path.basename(path)
        old = self.find(parent, name)
        media = MediaFileUpload(path, mimetype=mime or mimetypes.guess_type(name)[0] or
                                "application/octet-stream", resumable=True, chunksize=CHUNK)
        if old:
            req = self.api.files().update(fileId=old["id"], media_body=media, fields="id")
        else:
            req = self.api.files().create(body={"name": name, "parents": [parent]},
                                          media_body=media, fields="id")
        response = None
        while response is None:
            _, response = req.next_chunk(num_retries=5)
        return response["id"]

    def write_map(self, parent, data):
        with tempfile.TemporaryDirectory(prefix="dbx-map-") as tmp:
            path = os.path.join(tmp, "dropbox-private-map.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            self.upload(path, parent, "dropbox-private-map.json", "application/json")


def save_original(store, original_root, sid, item, local_path):
    parent = store.folder(original_root, sid)
    for component in item["path"].strip("/").split("/")[:-1]:
        parent = store.folder(parent, component)
    return store.upload(local_path, parent, item["name"])


def make_outputs(store, parent, item, src_path, tmp):
    kind = item["kind"]
    if kind == "audio":
        video, has_audio, _ = probe(src_path)
        if not has_audio or video:
            raise RuntimeError("Expected an audio-only recording")
        if Path(item["name"]).suffix.casefold() == ".mp3":
            return {}
        audio = make_mp3(src_path, tmp)
        return {"audioDriveId": store.upload(audio, parent, "audio.mp3", "audio/mpeg")}
    video, has_audio, duration = probe(src_path)
    if not video:
        raise RuntimeError("Expected a decodable video stream")
    out = os.path.join(tmp, "hls")
    os.makedirs(out, exist_ok=True)
    result = {"duration": duration}
    if has_audio:
        audio = make_mp3(src_path, out)
        result["audioDriveId"] = store.upload(audio, parent, "audio.mp3", "audio/mpeg")
    poster = make_poster(src_path, out, duration)
    result["posterDriveId"] = store.upload(poster, parent, "poster.jpg", "image/jpeg")
    qualities = quality_ladder(int(video["width"]), int(video["height"]))
    master = make_hls(src_path, out, qualities)
    renditions = []
    for quality in qualities:
        height = quality["height"]
        playlist = f"{height}p.m3u8"
        media_name = f"{height}p.ts"
        renditions.append({
            "height": height, "width": quality["width"],
            "playlistDriveId": store.upload(os.path.join(out, playlist), parent, playlist,
                                            "application/vnd.apple.mpegurl"),
            "mediaDriveId": store.upload(os.path.join(out, media_name), parent, media_name,
                                         "video/mp2t"),
            "playlist": playlist, "media": media_name
        })
    result["hls"] = {"masterDriveId": store.upload(master, parent, "master.m3u8",
                                                   "application/vnd.apple.mpegurl"),
                     "renditions": renditions}
    return result


def check_disk(size):
    free = shutil.disk_usage(tempfile.gettempdir()).free
    # Source, several HLS resolutions, MP3 and an allowance for the runner.
    required = max(2 * 1024**3, int(size * 2.5) + 1024**3)
    if free < required:
        raise RuntimeError("Not enough runner disk space for this recording; "
                           "use a larger runner or process fewer renditions")


def check_drive_headroom(store, media_size, reserve_gib):
    """Fail closed before an automatic import could exhaust Drive storage.

    Google Drive accounts with no defined storage limit are treated as
    unlimited; for bounded accounts reserve the configured buffer *plus*
    an allowance for the original recording and its HLS renditions.
    """
    if not reserve_gib:
        return
    try:
        data = store.api.about().get(fields="storageQuota(limit,usage)").execute(num_retries=5)
    except Exception as exc:
        raise RuntimeError("Cannot verify Google Drive storage quota; automatic import stopped") from exc
    quota = data.get("storageQuota") or {}
    if not quota.get("limit"):
        print("Drive account has no advertised storage quota limit", flush=True)
        return
    try:
        remaining = int(quota["limit"]) - int(quota.get("usage") or 0)
    except (ValueError, TypeError, OverflowError):
        raise RuntimeError("Cannot interpret Drive quota response; automatic import stopped") from None
    required = reserve_gib * 1024**3 + media_size * 4
    print(f"Drive quota check: {remaining // (1024**3)} GiB remaining; "
          f"{required // (1024**3)} GiB minimum including import allowance.", flush=True)
    if remaining < required:
        raise RuntimeError("Drive storage is below the configured safety reserve; "
                           "automatic import paused without deleting anything")


def run(mode="scan", max_files=1, only_id="", source_label="", min_video_bytes=0,
        max_source_bytes=0, reserve_gib=0):
    sources = sources_from_env()
    if source_label:
        matches = [source for source in sources if source["label"] == source_label]
        if len(matches) != 1:
            raise ValueError("Requested Dropbox source label must match exactly one approved source")
        sources = matches
    if mode == "import":
        denied = [s["label"] for s in sources
                  if s["label"] not in ALLOWED_DROPBOX_SOURCES]
        if denied:
            # No network, downloads, or Google Drive changes for unapproved
            # source links, even if an operator manually starts the workflow.
            raise ValueError("Dropbox import is limited to Gamliel and Biderman. "
                             "Unapproved source(s): " + ", ".join(denied))
    api = DropboxLinks(os.getenv("DROPBOX_APP_KEY"), os.getenv("DROPBOX_APP_SECRET"))
    scanned = []
    folders = 0
    for source in sources:
        count, items = api.list_tree(source)
        folders += count
        excluded = [item for item in items if is_excluded_nonlecture(source, item)]
        items = [item for item in items if not is_excluded_nonlecture(source, item)]
        if excluded:
            print(f"Excluded {len(excluded)} non-lecture file(s) in the "
                  f"Shinyev Yurtziet collection", flush=True)
        scanned.extend((source, item) for item in items)
        print(f"Dropbox source '{source['label']}': {count} folder(s), "
              f"{sum(item['kind'] == 'video' for item in items)} video(s), "
              f"{sum(item['kind'] == 'audio' for item in items)} audio file(s)")
    videos = sum(item["kind"] == "video" for _, item in scanned)
    audios = len(scanned) - videos
    print(f"Dropbox scan succeeded: {len(sources)} shared source(s), {folders} nested folder(s), "
          f"{videos} video(s), {audios} audio file(s).")
    video_candidates = sorted(
        ((s, item) for s, item in scanned if item["kind"] == "video"),
        key=lambda pair: (pair[1]["size"], file_id(pair[0]["id"], pair[1]))
    )
    if video_candidates:
        # Prefer real lecture-sized candidates rather than tiny sample clips.
        sizeable = [pair for pair in video_candidates if pair[1]["size"] >= 50 * 1024 * 1024]
        shortlist = sizeable[:5] if sizeable else video_candidates[:5]
        for candidate_source, candidate_item in shortlist:
            print("Video candidate:", file_id(candidate_source["id"], candidate_item),
                  "size:", candidate_item["size"], "bytes",
                  "path:", candidate_item["path"])
    if mode == "scan":
        print("Read-only scan finished. No media downloaded or Drive files changed.")
        return

    store = DriveStore(os.getenv("GOOGLE_TOKEN_JSON"))
    # Isolated top-level staging, not under a potentially shared Vimeo Backup root.
    # Originals and private source identifiers must not inherit public sharing.
    staging = store.folder(None, "IST Dropbox Imports (Private)")
    originals = store.folder(staging, "Originals")
    processed = store.folder(staging, "Processed")
    maps = store.folder(staging, "Maps")
    record = store.read_map(maps)
    items_by_id = record["items"]
    pending = []
    for source, item in scanned:
        mid = file_id(source["id"], item)
        previous = items_by_id.get(mid) or {}
        if previous.get("rev") == item["rev"] and previous.get("originalDriveId") and (
                item["kind"] == "audio" or previous.get("hls", {}).get("masterDriveId")):
            continue
        pending.append((source, item, mid))
    # Start with a small video for the first test; large recordings can follow.
    pending.sort(key=lambda pair: (pair[1]["kind"] != "video", pair[1]["size"], pair[2]))
    if max_source_bytes:
        before_size_limit = len(pending)
        pending = [entry for entry in pending if entry[1]["size"] <= max_source_bytes]
        deferred = before_size_limit - len(pending)
        if deferred:
            print(f"Deferred {deferred} oversized recordings for a separate "
                  f"high-capacity import (limit {max_source_bytes} bytes).", flush=True)
    if min_video_bytes:
        # A first test should be a real recording, not a tiny/empty clip.
        # Only explicit preview runs use this limit. Production remains
        # capable of importing audio-only and short recordings.
        pending = [entry for entry in pending if
                   entry[1]["kind"] == "video" and entry[1]["size"] >= min_video_bytes]
        if not pending:
            raise RuntimeError("No unimported video meets the sample size minimum")
    if only_id:
        if only_id not in {file_id(s["id"], item) for s, item in scanned}:
            raise ValueError("Requested test file ID is not among the approved Dropbox sources")
        pending = [entry for entry in pending if entry[2] == only_id]
    print(f"Pending for this run: {len(pending)}; "
          f"processing up to {max_files}.")
    converted = 0
    for source, item, mid in pending[:max_files]:
        check_drive_headroom(store, item["size"], reserve_gib)
        check_disk(item["size"])
        with tempfile.TemporaryDirectory(prefix="irgun-dropbox-") as tmp:
            source_name = "source" + (Path(item["name"]).suffix.lower() or ".mp4")
            media = os.path.join(tmp, source_name)
            print(f"Importing {mid} ({item['size']} bytes; {item['kind']}); source: "
                  f"{source['label']}; path: {item['path']}", flush=True)
            api.download(source, item["path"], media)
            if item["size"] and os.path.getsize(media) != item["size"]:
                raise RuntimeError("Dropbox download size did not match folder metadata")
            orig_id = save_original(store, originals, source["id"], item, media)
            dest = store.folder(store.folder(processed, source["id"]), mid)
            generated = make_outputs(store, dest, item, media, tmp)
            if item["kind"] == "audio" and not generated.get("audioDriveId"):
                generated["audioDriveId"] = orig_id
            items_by_id[mid] = {
                "id": mid, "sourceId": source["id"], "sourceLabel": source["label"],
                "sourcePath": item["path"],
                "dropboxFileId": item.get("id"), "rev": item["rev"],
                "sourceModified": item["modified"], "size": item["size"],
                "kind": item["kind"], "originalDriveId": orig_id,
                "generatedAt": timestamp(), **generated
            }
            # Checkpoint after each file; incomplete imports safely retry.
            store.write_map(maps, record)
            converted += 1
            print(f"Saved original and processed outputs to Google Drive: {mid}")
    print(f"Dropbox import completed: {converted} recording(s). "
          "Recordings are staged in Drive, NOT published to the website.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("scan", "import"), default="scan")
    parser.add_argument("--max-files", type=int, default=1)
    parser.add_argument("--only-id", default="", help="Import only this stable media ID")
    parser.add_argument("--source-label", default="", help="Restrict scan and import to one approved source label")
    parser.add_argument("--min-video-bytes", type=int, default=0,
                        help="For sample imports, skip tiny videos and audio-only files")
    parser.add_argument("--max-source-bytes", type=int, default=0,
                        help="For automatic imports, defer oversized recordings for manual review")
    parser.add_argument("--reserve-gib", type=int, default=0,
                        help="Minimum free Drive GiB plus a 4x recording allowance")
    args = parser.parse_args()
    if not 1 <= args.max_files <= 4:
        parser.error("--max-files must be between 1 and 4")
    if not 0 <= args.min_video_bytes <= 1000 * 1024**2:
        parser.error("--min-video-bytes must be between 0 and 1 GiB")
    if not 0 <= args.max_source_bytes <= 200 * 1024**3:
        parser.error("--max-source-bytes must be between 0 and 200 GiB")
    if not 0 <= args.reserve_gib <= 1000:
        parser.error("--reserve-gib must be between 0 and 1000")
    run(mode=args.mode, max_files=args.max_files, only_id=args.only_id,
        source_label=args.source_label, min_video_bytes=args.min_video_bytes,
        max_source_bytes=args.max_source_bytes, reserve_gib=args.reserve_gib)
