"""Isolated Google Drive access for the staging Series importer."""
import io
import json
import os
import pickle

from google.auth.transport.requests import Request
from series_catalog import speaker_names_from_folder_entries
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

SERIES_ROOT_ID = os.getenv("SERIES_DRIVE_ROOT_ID", "1B5myYj9rsqfoRO8Cjjr6CQOf-q2Q71Ft")
PRIVATE_MAP_NAME = "series-map-private.json"
PUBLIC_MAP_NAME = "series-public-map.json"
FOLDER_MIME = "application/vnd.google-apps.folder"

class SeriesDrive:
    def __init__(self, token_file="token.pickle"):
        with open(token_file, "rb") as handle:
            creds = pickle.load(handle)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds.valid:
            raise RuntimeError("Drive OAuth token is invalid")
        self.api = build("drive", "v3", credentials=creds, cache_discovery=False)

    def children(self, folder_id):
        records, page = [], None
        while True:
            result = self.api.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                pageSize=1000, pageToken=page, supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                fields="nextPageToken,files(id,name,mimeType,size,createdTime,modifiedTime,appProperties)"
            ).execute(num_retries=5)
            records.extend(result.get("files", []))
            page = result.get("nextPageToken")
            if not page:
                return records

    def metadata(self, file_id):
        return self.api.files().get(
            fileId=file_id, fields="id,name,mimeType,parents",
            supportsAllDrives=True
        ).execute(num_retries=5)

    def find(self, folder_id, name):
        return next((x for x in self.children(folder_id) if x["name"] == name), None)

    def folder(self, parent, name):
        existing = self.find(parent, name)
        if existing:
            if existing["mimeType"] != FOLDER_MIME:
                raise RuntimeError(f"Name collision for Drive folder {name}")
            return existing["id"]
        return self.api.files().create(
            body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
            fields="id", supportsAllDrives=True,
        ).execute(num_retries=5)["id"]

    def series_system_folder(self):
        # Unlike Vimeo Backup/Maps, the separate Series root is not shared
        # with anyone holding a link. Contributors receive access only to their
        # own speaker subfolders, never to this underscore-named system folder.
        self.metadata(SERIES_ROOT_ID)  # ensure it exists before writing
        return self.folder(SERIES_ROOT_ID, "_System")

    def backup_maps_folder(self):
        private = self.series_system_folder()
        # One-time migration: the earliest test scan wrote its private map
        # under the legacy Maps location. Move that same file atomically into
        # the restricted Series hierarchy, without copying confidential IDs.
        old_root = self.root_folder_by_name("Vimeo Backup")
        old_maps = self.find(old_root, "Maps")
        legacy = self.find(old_maps["id"], PRIVATE_MAP_NAME) if old_maps else None
        if legacy:
            current = self.find(private, PRIVATE_MAP_NAME)
            if current and current["id"] != legacy["id"]:
                raise RuntimeError("Both old and private Series maps exist; refusing ambiguous migration")
            self.api.files().update(
                fileId=legacy["id"], addParents=private,removeParents=old_maps["id"],
                fields="id,parents", supportsAllDrives=True
            ).execute(num_retries=5)
            print("Moved restricted Series map out of legacy shared backup folder", flush=True)
            if self.find(old_maps["id"], PRIVATE_MAP_NAME):
                raise RuntimeError("Legacy Series map remains visible in shared Maps; stopping")
        return private

    def series_outputs_folder(self):
        return self.folder(self.series_system_folder(), "_Processed")

    def root_folder_by_name(self, name):
        q = "name = '" + name.replace("\\", "\\\\").replace("'", "\\'") + "' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        res = self.api.files().list(q=q, fields="files(id,name,parents)",pageSize=100).execute(num_retries=5)
        matches = res.get("files", [])
        if not matches:
            raise RuntimeError(f"Required Drive backup folder missing: {name}")
        return matches[0]["id"]

    def read_json(self, folder_id, name):
        entry = self.find(folder_id, name)
        if not entry:
            return None
        payload = self.api.files().get_media(fileId=entry["id"]).execute(num_retries=5)
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        result = json.loads(payload.decode("utf-8"))
        if not isinstance(result, dict) or result.get("library") != "series":
            raise RuntimeError(f"Unexpected {name} JSON shape; refusing to overwrite")
        return result

    def write_json(self, folder_id, name, data):
        if data.get("library") != "series":
            raise RuntimeError("Only isolated series catalogs may be written")
        payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=False)
        previous = self.find(folder_id, name)
        if previous:
            self.api.files().update(fileId=previous["id"], media_body=media, fields="id").execute(num_retries=5)
            return previous["id"]
        return self.api.files().create(body={"name": name,"parents":[folder_id]},
                                       media_body=media,fields="id").execute(num_retries=5)["id"]

    def download(self, file_id, path):
        req = self.api.files().get_media(fileId=file_id)
        with open(path, "wb") as out:
            downloader = MediaIoBaseDownload(out, req, chunksize=16*1024*1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=5)
        if os.path.getsize(path) == 0:
            raise RuntimeError("Downloaded source file was empty")

    def upload(self, file_path, folder_id, filename=None, mime="application/octet-stream"):
        filename = filename or os.path.basename(file_path)
        media = MediaFileUpload(file_path, mimetype=mime, chunksize=16*1024*1024,resumable=True)
        old = self.find(folder_id, filename)
        if old:
            request = self.api.files().update(fileId=old["id"], media_body=media, fields="id")
        else:
            request = self.api.files().create(body={"name": filename, "parents":[folder_id]}, media_body=media, fields="id")
        result = None
        while result is None:
            _, result = request.next_chunk(num_retries=5)
        return result["id"]

    def list_speaker_folders(self):
        """List all actual speaker folders, even when they contain no shiurim.

        The public catalog includes names only; no Drive IDs are exposed.
        Underscore-prefixed system and pending folders are always excluded.
        """
        return speaker_names_from_folder_entries(self.children(SERIES_ROOT_ID))

    def list_source_files(self):
        info = self.metadata(SERIES_ROOT_ID)
        if info.get("name", "").lower() != "series" or info.get("mimeType") != FOLDER_MIME:
            raise RuntimeError("SERIES_DRIVE_ROOT_ID does not point to the Series folder")
        records = []
        for speaker in self.children(SERIES_ROOT_ID):
            if speaker.get("mimeType") != FOLDER_MIME or speaker["name"].startswith("_"):
                continue
            for year in self.children(speaker["id"]):
                if year.get("mimeType") != FOLDER_MIME or not year["name"].isdigit():
                    continue
                for file in self.children(year["id"]):
                    if file.get("mimeType") == FOLDER_MIME:
                        continue
                    records.append({"speaker":speaker["name"],"year":year["name"],"file":file})
        return records
