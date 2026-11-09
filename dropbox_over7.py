#!/usr/bin/env python3
"""Manual-only, checkpointed importer for approved Dropbox videos >7-20 GB.

No scheduled execution. Never alters Dropbox or the regular import workflow.
Partial progress is stored beside the private Drive media, *not* in the
published source map. Only a fully uploaded HLS ladder is committed there.
"""
import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from dropbox_import import (
    DropboxLinks, DriveStore, check_drive_headroom, combine_legacy_and_source_map,
    file_id, private_map_name, save_original, sources_from_env, timestamp,
)
from dropbox_large import (
    GIB, large_master_playlist, require_rendition_space,
    space_needed_for_large_source,
)
from dropbox_media_policy import is_excluded_nonlecture
from series_media import make_hls, make_mp3, make_poster, probe, quality_ladder
from series_visibility import ALLOWED_DROPBOX_SOURCES

MIN_BYTES = 7_000_000_000            # Exclusive: regular large importer owns <=7 GB.
MAX_BYTES = 20_000_000_000           # Inclusive, decimal GB.
RESERVE_DRIVE_GIB = 68
PROGRESS_FILE = "oversize-progress.json"
CONFIRM = "IMPORT_OVER_7_GB"
SOURCE_LABELS = {
    "leil-shishi": "Reb Mielech - Leil Shishi",
    "short-word": "Reb Mielech - Short Word for Shabbos",
}
MIN_PHASE_SECONDS = 45 * 60


def candidates(source, files, completed):
    """Only approved, new/revised oversized video files, smallest first."""
    result = []
    for item in files:
        size = int(item["size"])
        if (item["kind"] != "video" or size <= MIN_BYTES or
                size > MAX_BYTES or is_excluded_nonlecture(source, item)):
            continue
        mid = file_id(source["id"], item)
        old = completed.get(mid) or {}
        if (old.get("rev") == item["rev"] and old.get("originalDriveId")
                and old.get("hls", {}).get("masterDriveId")):
            continue
        result.append((size, mid, item))
    return [(mid, item) for _, mid, item in sorted(result)]


def fresh_state(source, mid, item):
    return {
        "version": 1,
        "sourceId": source["id"], "mediaId": mid,
        "sourcePath": item["path"], "rev": item["rev"],
        "size": int(item["size"]), "kind": "video",
        "renditions": [],
    }


def validate_state(state, source, mid, item):
    expected = fresh_state(source, mid, item)
    if (not isinstance(state, dict) or any(state.get(k) != expected[k] for k in
            ("version", "sourceId", "mediaId", "sourcePath", "rev", "size", "kind"))
            or not isinstance(state.get("renditions"), list)):
        raise ValueError("Oversize checkpoint does not match this source revision; refusing overwrite")
    heights = [x.get("height") for x in state["renditions"]]
    if len(heights) != len(set(heights)):
        raise ValueError("Duplicated rendition in oversize checkpoint")


def load_state(store, parent, source, mid, item):
    found = store.find(parent, PROGRESS_FILE)
    if not found:
        return fresh_state(source, mid, item)
    raw = store.api.files().get_media(fileId=found["id"]).execute(num_retries=5)
    state = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    validate_state(state, source, mid, item)
    return state


def save_state(store, parent, state):
    with tempfile.TemporaryDirectory(prefix="dbx-oversize-progress-") as tmp:
        file = Path(tmp) / PROGRESS_FILE
        file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        store.upload(str(file), parent, PROGRESS_FILE, "application/json")


def ensure_disk(size):
    free = shutil.disk_usage(tempfile.gettempdir()).free
    required = space_needed_for_large_source(size)
    print(f"Oversize disk preflight: {free / GIB:.1f} GiB free; "
          f"{required / GIB:.1f} GiB required", flush=True)
    if free < required:
        raise RuntimeError("Insufficient runner disk; use a larger runner, not a higher cap")


def verify_checkpoint_references(store, state):
    """Do not silently trust references to missing/trashed/empty Drive files."""
    ids = [state.get(k) for k in ("originalDriveId", "audioDriveId", "posterDriveId",
                                   "masterDriveId")]
    for rendition in state["renditions"]:
        ids.extend([rendition.get("playlistDriveId"), rendition.get("mediaDriveId")])
    for fid in filter(None, ids):
        info = store.api.files().get(fileId=fid, fields="id,size,trashed").execute(num_retries=5)
        if info.get("trashed") or int(info.get("size") or 0) <= 0:
            raise RuntimeError("Missing or empty checkpointed Google Drive asset")


def is_finished(state):
    qualities = state.get("qualities") or []
    if not qualities or not state.get("originalDriveId") or not state.get("posterDriveId"):
        return False
    if state.get("hasAudio") and not state.get("audioDriveId"):
        return False
    if not state.get("masterDriveId"):
        return False
    by_height = {r.get("height"): r for r in state["renditions"]}
    return (len(by_height) == len(qualities) and
            all(by_height.get(q["height"], {}).get("playlistDriveId") and
                by_height.get(q["height"], {}).get("mediaDriveId") for q in qualities))


def commit_completed(store, maps_parent, mapping, source, mid, item, state):
    if not is_finished(state):
        raise RuntimeError("Refusing to publish incomplete oversized media")
    hls = {
        "masterDriveId": state["masterDriveId"],
        "renditions": sorted(state["renditions"], key=lambda r: r["height"]),
    }
    entry = {
        "id": mid, "sourceId": source["id"], "sourceLabel": source["label"],
        "sourcePath": item["path"], "dropboxFileId": item.get("id"),
        "rev": item["rev"], "sourceModified": item["modified"],
        "size": int(item["size"]), "kind": "video",
        "originalDriveId": state["originalDriveId"],
        "generatedAt": timestamp(), "duration": state["duration"],
        "posterDriveId": state["posterDriveId"], "hls": hls,
    }
    if state.get("audioDriveId"):
        entry["audioDriveId"] = state["audioDriveId"]
    mapping["items"][mid] = entry
    store.write_map(maps_parent, mapping, private_map_name(source["id"]))
    print(f"OVERSIZE COMPLETE | {mid} | committed to private source map", flush=True)


def process_one(dbx, store, source, mid, item, mapping, maps, originals,
                processed, deadline, max_renditions):
    parent = store.folder(store.folder(processed, source["id"]), mid)
    state = load_state(store, parent, source, mid, item)
    verify_checkpoint_references(store, state)
    if is_finished(state):
        commit_completed(store, maps, mapping, source, mid, item, state)
        return "complete"
    ensure_disk(int(item["size"]))
    check_drive_headroom(store, int(item["size"]), RESERVE_DRIVE_GIB)
    if time.monotonic() >= deadline - MIN_PHASE_SECONDS:
        print("OVERSIZE PAUSED | insufficient job time before downloading", flush=True)
        return "paused"
    with tempfile.TemporaryDirectory(prefix="ist-over7-") as tmp:
        src = Path(tmp) / ("source" + (Path(item["name"]).suffix.lower() or ".mp4"))
        print(f"OVERSIZE SOURCE | {mid} | {item['size'] / 1e9:.2f} GB", flush=True)
        dbx.download(source, item["path"], str(src))
        if src.stat().st_size != int(item["size"]):
            raise RuntimeError("Dropbox download size differs from source metadata")
        if not state.get("originalDriveId"):
            state["originalDriveId"] = save_original(store, originals, source["id"], item, str(src))
            save_state(store, parent, state)
        video, has_audio, duration = probe(str(src))
        if not video or duration <= 0:
            raise RuntimeError("Source is not a decodable video with a positive duration")
        qualities = quality_ladder(int(video["width"]), int(video["height"]))
        if state.get("qualities") and state["qualities"] != qualities:
            raise RuntimeError("Video format changed since previous checkpoint")
        state.update({"qualities": qualities, "hasAudio": has_audio, "duration": duration})
        save_state(store, parent, state)
        output = Path(tmp) / "hls"
        output.mkdir()
        def room(stage):
            if time.monotonic() >= deadline - MIN_PHASE_SECONDS:
                print(f"OVERSIZE PAUSED | waiting for next manual run before {stage}", flush=True)
                return False
            return True
        if has_audio and not state.get("audioDriveId"):
            if not room("MP3"):
                return "paused"
            audio = make_mp3(str(src), str(output))
            state["audioDriveId"] = store.upload(audio, parent, "audio.mp3", "audio/mpeg")
            save_state(store, parent, state)
            os.remove(audio)
        if not state.get("posterDriveId"):
            if not room("poster"):
                return "paused"
            poster = make_poster(str(src), str(output), duration)
            state["posterDriveId"] = store.upload(poster, parent, "poster.jpg", "image/jpeg")
            save_state(store, parent, state)
            os.remove(poster)
        completed = {r["height"] for r in state["renditions"]}
        newly_encoded = 0
        for q in qualities:
            if q["height"] in completed:
                continue
            if newly_encoded >= max_renditions or not room(f"{q['height']}p"):
                return "paused"
            require_rendition_space(duration, q)
            print(f"OVERSIZE ENCODING | {mid} | {q['height']}p", flush=True)
            make_hls(str(src), str(output), [q])
            playlist, media = f"{q['height']}p.m3u8", f"{q['height']}p.ts"
            # Upload both files before checkpointing this complete rendition.
            playlist_id = store.upload(str(output / playlist), parent, playlist,
                                       "application/vnd.apple.mpegurl")
            media_id = store.upload(str(output / media), parent, media, "video/mp2t")
            state["renditions"].append({
                "height": q["height"], "width": q["width"],
                "playlist": playlist, "media": media,
                "playlistDriveId": playlist_id, "mediaDriveId": media_id,
            })
            save_state(store, parent, state)
            os.remove(output / playlist)
            os.remove(output / media)
            (output / "master.m3u8").unlink(missing_ok=True)
            newly_encoded += 1
        if not state.get("masterDriveId"):
            if not room("master playlist"):
                return "paused"
            master = output / "master.m3u8"
            master.write_text(large_master_playlist(qualities), encoding="utf-8")
            state["masterDriveId"] = store.upload(str(master), parent, "master.m3u8",
                                                  "application/vnd.apple.mpegurl")
            save_state(store, parent, state)
        verify_checkpoint_references(store, state)
        commit_completed(store, maps, mapping, source, mid, item, state)
        return "complete"


def run(source_key, mode="plan", confirm="", budget_minutes=300,
        max_renditions=4, only_id=""):
    if source_key not in SOURCE_LABELS:
        raise ValueError("Source must be leil-shishi or short-word")
    if mode not in ("plan", "import") or (mode == "import" and confirm != CONFIRM):
        raise ValueError("Import requires exact confirmation phrase")
    if not 45 <= budget_minutes <= 310 or not 1 <= max_renditions <= 4:
        raise ValueError("Unsafe job budget or rendition count")
    label = SOURCE_LABELS[source_key]
    sources = [s for s in sources_from_env() if s["label"] == label]
    if len(sources) != 1 or label not in ALLOWED_DROPBOX_SOURCES:
        raise ValueError("Must match one explicitly approved Dropbox source")
    source = sources[0]
    dbx = DropboxLinks(os.getenv("DROPBOX_APP_KEY"), os.getenv("DROPBOX_APP_SECRET"))
    _, items = dbx.list_tree(source)
    valid = [(file_id(source["id"], item), item) for item in items
             if item["kind"] == "video" and MIN_BYTES < int(item["size"]) <= MAX_BYTES
             and not is_excluded_nonlecture(source, item)]
    print(f"OVERSIZE INVENTORY | {label} | {len(valid)} source videos between 7 and 20 GB", flush=True)
    if mode == "plan":
        for mid, item in sorted(valid, key=lambda x: int(x[1]["size"])):
            print(f"OVERSIZE PLAN | {mid} | {int(item['size']) / 1e9:.2f} GB", flush=True)
        print("Read-only plan. Nothing downloaded, uploaded, or published.", flush=True)
        return "plan"
    store = DriveStore(os.getenv("GOOGLE_TOKEN_JSON"))
    staging = store.folder(None, "IST Dropbox Imports (Private)")
    originals = store.folder(staging, "Originals")
    processed = store.folder(staging, "Processed")
    maps = store.folder(staging, "Maps")
    mapping = combine_legacy_and_source_map(
        store.read_map(maps), store.read_map(maps, private_map_name(source["id"])),
        source["id"])
    pending = candidates(source, items, mapping["items"])
    if only_id:
        pending = [x for x in pending if x[0] == only_id]
    if not pending:
        print("OVERSIZE DONE | no eligible unimported recordings", flush=True)
        return "done"
    mid, item = pending[0]
    result = process_one(dbx, store, source, mid, item, mapping, maps,
                         originals, processed, time.monotonic() + budget_minutes * 60,
                         max_renditions)
    print(f"OVERSIZE RESULT | {result} | {mid} | rerun manually if paused", flush=True)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-key", choices=tuple(SOURCE_LABELS), required=True)
    p.add_argument("--mode", choices=("plan", "import"), default="plan")
    p.add_argument("--confirm", default="")
    p.add_argument("--budget-minutes", type=int, default=300)
    p.add_argument("--max-renditions", type=int, default=4)
    p.add_argument("--only-id", default="")
    args = p.parse_args()
    run(args.source_key, args.mode, args.confirm, args.budget_minutes,
        args.max_renditions, args.only_id)
