"""Disk-efficient import support for Dropbox recordings above the normal 2.2 GB cap.

Only used by the separate oversized job. Its job has a hard 7 GB source cap,
one file at a time, and retains every standard quality in the HLS ladder.
"""
import os
import shutil
import tempfile
from pathlib import Path

from series_media import (
    AUDIO_BITRATE, make_hls, make_mp3, make_poster, probe, quality_ladder,
)

NORMAL_LIMIT = 2_200_000_000
FIVE_GB = 5_000_000_000
SEVEN_GB = 7_000_000_000
GIB = 1024 ** 3
MIN_HEADROOM = 2 * GIB


class InsufficientRunnerDisk(RuntimeError):
    """The current free runner cannot safely accommodate this recording."""


def size_buckets(pending):
    """Count *unimported* recordings in the 2.2 / 5 / 7 GB size tiers."""
    sizes = [int(row[1]["size"]) for row in pending]
    return {
        "normalUpTo2_2GB": sum(n <= NORMAL_LIMIT for n in sizes),
        "oversize2_2To5GB": sum(NORMAL_LIMIT < n <= FIVE_GB for n in sizes),
        "oversize5To7GB": sum(FIVE_GB < n <= SEVEN_GB for n in sizes),
        "above5GB": sum(n > FIVE_GB for n in sizes),
        "above7GB": sum(n > SEVEN_GB for n in sizes),
        "largestGB": round(max(sizes, default=0) / 1_000_000_000, 2),
    }


def space_needed_for_large_source(size):
    """Source download + one HLS rendition + conservative runner headroom.

    The video is encoded/uploaded one resolution at a time; the standard
    importer instead stores all generated quality files simultaneously.
    """
    return int(size) + max(3 * GIB, int(size * 0.55)) + MIN_HEADROOM


def require_large_source_space(size):
    free = shutil.disk_usage(tempfile.gettempdir()).free
    needed = space_needed_for_large_source(size)
    print(f"Large Dropbox disk preflight: {free / GIB:.1f} GiB free; "
          f"{needed / GIB:.1f} GiB needed for {size / 1e9:.2f} GB input",
          flush=True)
    if free < needed:
        raise InsufficientRunnerDisk(
            "Runner has insufficient free space for this large source. "
            "Try another runner with more available disk, not a higher file cap."
        )


def require_rendition_space(duration, quality):
    """Conservatively estimate temporary output size before starting FFmpeg."""
    free = shutil.disk_usage(tempfile.gettempdir()).free
    estimated = int(
        max(0.0, duration)
        * ((quality["maxrate"] + AUDIO_BITRATE) * 1000 / 8)
        * 1.55
    )
    needed = estimated + MIN_HEADROOM
    if free < needed:
        raise InsufficientRunnerDisk(
            f"Not enough space for {quality['height']}p conversion: "
            f"{free / GIB:.1f} GiB free, {needed / GIB:.1f} GiB required"
        )


def large_master_playlist(qualities):
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for q in qualities:
        avg = (q["bitrate"] + AUDIO_BITRATE) * 1000
        peak = int((q["maxrate"] + AUDIO_BITRATE) * 1000 * 1.10)
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={peak},AVERAGE-BANDWIDTH={avg},"
            f"RESOLUTION={q['width']}x{q['height']}"
        )
        lines.append(f"{q['height']}p.m3u8")
    return "\n".join(lines) + "\n"


def make_large_outputs(store, parent, item, source_path, temp, progress=None):
    """Build and upload one quality at a time to keep peak disk use bounded.

    Every rendition retains the existing format, bitrate, and filenames.
    The public/Drive map is written by the caller only once all outputs exist.
    """
    def phase(message):
        print("Large Dropbox: " + message, flush=True)
        if progress:
            progress.update(message)

    kind = item["kind"]
    video, has_audio, duration = probe(source_path)
    if kind == "audio":
        if video or not has_audio:
            raise RuntimeError("Expected audio-only recording")
        if Path(item["name"]).suffix.casefold() == ".mp3":
            return {}
        phase("Converting large audio to MP3")
        mp3 = make_mp3(source_path, temp)
        return {"audioDriveId": store.upload(mp3, parent, "audio.mp3", "audio/mpeg")}

    if not video:
        raise RuntimeError("Expected decodable video")
    output = Path(temp) / "hls"
    output.mkdir(exist_ok=True)
    result = {"duration": duration}

    if has_audio:
        phase("Encoding and uploading MP3")
        audio = make_mp3(source_path, str(output))
        result["audioDriveId"] = store.upload(audio, parent, "audio.mp3", "audio/mpeg")
        os.remove(audio)

    phase("Creating and uploading poster")
    poster = make_poster(source_path, str(output), duration)
    result["posterDriveId"] = store.upload(poster, parent, "poster.jpg", "image/jpeg")
    os.remove(poster)

    qualities = quality_ladder(int(video["width"]), int(video["height"]))
    renditions = []
    for index, q in enumerate(qualities, 1):
        require_rendition_space(duration, q)
        height = q["height"]
        phase(f"Encoding {height}p quality ({index}/{len(qualities)})")
        make_hls(source_path, str(output), [q])
        playlist, media = f"{height}p.m3u8", f"{height}p.ts"
        phase(f"Uploading {height}p video to private Drive ({index}/{len(qualities)})")
        playlist_id = store.upload(str(output / playlist), parent, playlist,
                                   "application/vnd.apple.mpegurl")
        media_id = store.upload(str(output / media), parent, media, "video/mp2t")
        renditions.append({
            "height": height, "width": q["width"],
            "playlistDriveId": playlist_id,
            "mediaDriveId": media_id,
            "playlist": playlist, "media": media,
        })
        # Delete this rendition immediately after uploading to reclaim disk.
        os.remove(output / playlist)
        os.remove(output / media)

    phase("Saving final multi-quality HLS playlist")
    master = output / "master.m3u8"
    master.write_text(large_master_playlist(qualities), encoding="utf-8")
    result["hls"] = {
        "masterDriveId": store.upload(str(master), parent, "master.m3u8",
                                       "application/vnd.apple.mpegurl"),
        "renditions": renditions,
    }
    return result
