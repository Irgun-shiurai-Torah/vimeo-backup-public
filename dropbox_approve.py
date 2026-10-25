#!/usr/bin/env python3
"""Approve exactly one inspected Dropbox recording for the TEST Series catalog.

Never approves an entire source link or silently publishes new imports.
Requires a fully processed recording, reviewed metadata, and matching source
label/path. The existing Series scanner subsequently publishes only the safe
catalog fields; all Drive IDs remain in private Google Drive storage.
"""
import argparse
import copy
import json
import os
import re

from dropbox_import import DRIVE_FOLDER, DriveStore
from dropbox_series_bridge import merge_approved_dropbox, series_id


def approve_record(map_data, media_id, source_label, path, metadata):
    if not isinstance(map_data, dict) or map_data.get("version") != 1:
        raise ValueError("Invalid private Dropbox map")
    if not re.fullmatch(r"dbx-media-[a-f0-9]{24}", media_id):
        raise ValueError("Invalid media ID")
    items = map_data.get("items")
    if not isinstance(items, dict) or media_id not in items:
        raise ValueError("Recording has not been imported into private Google Drive")
    previous = items[media_id]
    if previous.get("sourceLabel") != source_label:
        raise ValueError("Dropbox source label does not match this recording")
    if previous.get("sourcePath") != path:
        raise ValueError("Dropbox source path does not match this recording")
    if previous.get("kind") != "video":
        raise ValueError("This first website test must be a video with audio")
    if previous.get("approved") is True and previous.get("approvedMetadata") != metadata:
        raise ValueError("Recording is already approved with different metadata")
    copied = copy.deepcopy(map_data)
    target = copied["items"][media_id]
    target["approved"] = True
    target["approvedMetadata"] = metadata
    # Validate the full Series schema BEFORE mutating the Google Drive map.
    public, private = merge_approved_dropbox(
        {"version": 1, "library": "series", "items": [], "speakers": []},
        {"version": 1, "library": "series", "items": []},
        {"version": 1, "items": {media_id: target}},
    )
    if len(public["items"]) != 1 or len(private["items"]) != 1:
        raise ValueError("Approved recording did not produce one complete Series entry")
    return copied, series_id(media_id)


def find_folder(store, parent, name):
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = ("name = '" + escaped + "' and mimeType = '" + DRIVE_FOLDER +
             "' and trashed = false")
    if parent:
        query = "'" + parent + "' in parents and " + query
    result = store.api.files().list(
        q=query, spaces="drive", fields="nextPageToken,files(id,name)",
        pageSize=100, supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute(num_retries=5)
    matches = result.get("files") or []
    if len(matches) != 1 or result.get("nextPageToken"):
        raise ValueError("Expected exactly one existing private Drive folder named " + name)
    return matches[0]["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", required=True)
    ap.add_argument("--source-label", required=True)
    ap.add_argument("--path", required=True)
    ap.add_argument("--speaker", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--series", default="")
    ap.add_argument("--parsha", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    store = DriveStore(os.getenv("GOOGLE_TOKEN_JSON"))
    root = find_folder(store, None, "IST Dropbox Imports (Private)")
    folder = find_folder(store, root, "Maps")
    existing = store.read_map(folder)
    metadata = {"speaker": args.speaker, "title": args.title, "year": args.year}
    if args.series:
        metadata["series"] = args.series
    if args.parsha:
        metadata["parsha"] = args.parsha
    proposed, sid = approve_record(
        existing, args.id, args.source_label, args.path, metadata)
    if args.dry_run:
        print("Validated private Dropbox test approval; no changes made:", sid)
        return
    if proposed == existing:
        print("Test recording was already approved:", sid)
        return
    store.write_map(folder, proposed)
    checked = store.read_map(folder)
    if checked.get("items", {}).get(args.id, {}).get("approvedMetadata") != metadata:
        raise RuntimeError("Drive did not persist the approved metadata")
    print("Approved exactly one test recording for Series scanner:", sid)


if __name__ == "__main__":
    main()
