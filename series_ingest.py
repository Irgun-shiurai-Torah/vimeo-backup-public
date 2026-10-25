"""Scan the dedicated Series root and produce a safe staging catalog.

The full map with Drive file IDs is saved ONLY in private Google Drive storage.
The public JSON in this repository contains no Drive file IDs or OAuth credentials.
"""
import json
import os
import pathlib
import sys

from series_catalog import build_maps
from series_drive import SeriesDrive, PRIVATE_MAP_NAME

PUBLIC_OUTPUT = pathlib.Path("series-public-map.json")
ALLOW_EMPTY = os.getenv("SERIES_ALLOW_EMPTY", "0") == "1"

def main():
    drive = SeriesDrive()
    files = drive.list_source_files()
    maps_folder = drive.backup_maps_folder()
    previous = drive.read_json(maps_folder, PRIVATE_MAP_NAME)
    pub, private = build_maps(files, previous_private=previous)
    pub["speakers"] = drive.list_speaker_folders()
    prior_count = len((previous or {}).get("items", []))
    if prior_count and not len(pub["items"]) and not ALLOW_EMPTY:
        raise RuntimeError("Scan unexpectedly empty after previous catalog; refusing to erase it")
    for item in private["items"]:
        if item.get("duplicateInputCount"):
            print(f"WARNING: {item['title']} has {item['duplicateInputCount']} extra same-format file(s)")
    # A malformed partial response should never replace a known good private map.
    ids = [x["id"] for x in private["items"]]
    if len(ids) != len(set(ids)):
        raise RuntimeError("Duplicate catalog IDs detected; refusing to publish")
    private_map_id = drive.write_json(maps_folder, PRIVATE_MAP_NAME, private)
    # Public preview map is safe to store in a public GitHub repository:
    # it includes display metadata and internal API paths, never Drive IDs.
    PUBLIC_OUTPUT.write_text(json.dumps(pub,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("Series root scan completed")
    print(f"Scanned files: {len(files)}, speaker folders: {len(pub['speakers'])}, grouped shiurim: {len(pub['items'])}")
    print(f"Audio: {sum(1 for x in pub['items'] if x['hasAudio'])}, video: {sum(1 for x in pub['items'] if x['hasVideo'])}")
    print(f"Private Drive map updated (ID deliberately not printed); public staging catalog: {PUBLIC_OUTPUT}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
