"""Encode video and audio in the separate test Series library.

Reuses the same HLS ladder, FFmpeg codecs and single-file byte-range approach
as the existing Vimeo HLS backup, but writes exclusively to Vimeo Backup /
Series-HLS / series-id. Never edits the regular HLS map or city folders.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

from series_catalog import extension
from series_drive import SeriesDrive, PRIVATE_MAP_NAME

LADDER = [(360,550,700,1100),(480,900,1200,1800),
          (720,1600,2200,3200),(1080,2800,4000,5600)]
SEGMENT_SECONDS=6
AUDIO_BITRATE=96
PRESET=os.getenv("SERIES_HLS_PRESET","superfast")
KNOWN_AUDIO_EXTENSIONS={".mp3",".m4a",".aac",".ogg",".opus",".flac",".wav",".wma",".aif",".aiff"}

def run(*args):
    # Report encoder progress every 30 seconds instead of appearing frozen
    # during long camera-origin MTS conversions.
    if args and args[0]=="ffmpeg":
        args=(args[0],"-stats_period","30","-progress","pipe:1",*args[1:])
    subprocess.run(args,check=True)

def probe(path):
    result=subprocess.run(["ffprobe","-v","error","-show_entries",
        "format=duration:stream=codec_type,width,height","-of","json",path],
        check=True,capture_output=True,text=True)
    data=json.loads(result.stdout)
    streams=data.get("streams",[])
    video=next((s for s in streams if s.get("codec_type")=="video"),None)
    audio=any(s.get("codec_type")=="audio" for s in streams)
    duration=max(0,float(data.get("format",{}).get("duration") or 0))
    return video,audio,duration

def even(value):
    n=max(2,int(round(value)))
    return n if n%2==0 else n-1

def quality_ladder(width,height):
    options=[{"height":h,"width":even(width*h/height),"bitrate":b,"maxrate":m,"buf":buf}
             for h,b,m,buf in LADDER if h<=height]
    return options or [{"height":even(height),"width":even(width),
                       "bitrate":450,"maxrate":600,"buf":900}]

def make_mp3(input_file,out_dir):
    destination=os.path.join(out_dir,"audio.mp3")
    run("ffmpeg","-hide_banner","-loglevel","error","-y","-i",input_file,
        "-vn","-c:a","libmp3lame","-q:a","4",destination)
    if os.path.getsize(destination)<128:
        raise RuntimeError("Extracted audio output was empty")
    return destination

def make_poster(input_file,out_dir,duration):
    destination=os.path.join(out_dir,"poster.jpg")
    run("ffmpeg","-hide_banner","-loglevel","error","-y","-ss",
        str(min(10,max(0,duration/2))),"-i",input_file,
        "-frames:v","1","-vf","scale=640:-2",destination)
    if os.path.getsize(destination)<128:
        raise RuntimeError("Poster output was empty")
    return destination

def make_preview_mp4(src, output_dir, width, height, has_audio=True):
    """Fast, full-length 480p MP4 for MTS/MOV uploads before expensive HLS."""
    os.makedirs(output_dir, exist_ok=True)
    preview=os.path.join(output_dir,"preview.mp4")
    target_height=even(min(480, int(height)))
    print(f"Encoding Series early-playback MP4 at {target_height}p",flush=True)
    args=["ffmpeg","-hide_banner","-loglevel","error","-y","-i",src,
          "-map","0:v:0","-map","0:a:0?","-vf",f"scale=-2:{target_height}",
          "-c:v","libx264","-preset","ultrafast","-crf","29",
          "-pix_fmt","yuv420p","-c:a","aac","-b:a","96k",
          "-movflags","+faststart",preview]
    run(*args)
    if os.path.getsize(preview)<1024:
        raise RuntimeError("Playable MP4 conversion produced an empty file")
    return preview


def make_hls(src,destination,qualities):
    """Same H.264/AAC byterange HLS approach as hls_backup.py."""
    os.makedirs(destination,exist_ok=True)
    n=len(qualities)
    splits="".join(f"[v{i}]" for i in range(n))
    filters=[f"[0:v:0]split={n}{splits}"]+[
        f"[v{i}]scale=-2:{q['height']}[vo{i}]" for i,q in enumerate(qualities)]
    args=["ffmpeg","-hide_banner","-loglevel","error","-y","-i",src,
          "-filter_complex",";".join(filters)]
    for i,q in enumerate(qualities):
        h=q["height"]
        args += ["-map",f"[vo{i}]","-map","0:a:0?","-c:v","libx264",
          "-preset",PRESET,"-profile:v","main","-pix_fmt","yuv420p",
          "-b:v",f"{q['bitrate']}k","-maxrate",f"{q['maxrate']}k",
          "-bufsize",f"{q['buf']}k","-sc_threshold","0",
          "-force_key_frames",f"expr:gte(t,n_forced*{SEGMENT_SECONDS})",
          "-c:a","aac","-b:a",f"{AUDIO_BITRATE}k","-ac","2","-ar","48000",
          "-hls_time",str(SEGMENT_SECONDS),"-hls_playlist_type","vod",
          "-hls_flags","single_file+independent_segments","-hls_segment_type","mpegts",
          "-hls_segment_filename",os.path.join(destination,f"{h}p.ts"),
          os.path.join(destination,f"{h}p.m3u8")]
    run(*args)
    for quality in qualities:
        path=os.path.join(destination,f"{quality['height']}p.m3u8")
        if "#EXT-X-BYTERANGE" not in pathlib.Path(path).read_text():
            raise RuntimeError("Expected HLS byte-range playlist was missing")
    master=os.path.join(destination,"master.m3u8")
    lines=["#EXTM3U","#EXT-X-VERSION:6","#EXT-X-INDEPENDENT-SEGMENTS"]
    for q in qualities:
        avg=(q["bitrate"]+AUDIO_BITRATE)*1000
        peak=int((q["maxrate"]+AUDIO_BITRATE)*1000*1.10)
        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={peak},AVERAGE-BANDWIDTH={avg},RESOLUTION={q['width']}x{q['height']}")
        lines.append(f"{q['height']}p.m3u8")
    pathlib.Path(master).write_text("\n".join(lines)+"\n",encoding="utf-8")
    return master

def encode_video(drive,item,outputs_parent,on_audio_ready=None):
    video_id=item.get("videoDriveId")
    if not video_id:
        raise RuntimeError("Video source ID missing")
    item_id=item["id"]
    if not item_id.startswith("series-"):
        raise RuntimeError("Refusing to process non-Series media")
    destination=drive.folder(outputs_parent,item_id)
    with tempfile.TemporaryDirectory(prefix="irgun-series-video-") as directory:
        suffix=extension(item.get("videoFilename") or "") or ".mp4"
        original=os.path.join(directory,"source"+suffix)
        drive.download(video_id,original)
        video,has_audio,duration=probe(original)
        if not video:
            raise RuntimeError("Uploaded file has no decodable video stream")
        out=os.path.join(directory,"hls")
        os.makedirs(out,exist_ok=True)
        interim=dict(item.get("generated") or {})
        # Publish MP3 before beginning slow HLS transcoding, particularly for
        # large camera-origin MTS recordings. The current original stays intact.
        if not item.get("audioDriveId") and has_audio and not interim.get("audioDriveId"):
            interim["audioDriveId"]=drive.upload(
                make_mp3(original,out),destination,"audio.mp3","audio/mpeg"
            )
            if on_audio_ready:
                on_audio_ready(dict(interim))
        # Browsers can stream MP4/WebM directly; MTS and MOV require a
        # browser-compatible preview before the full HLS ladder is generated.
        if suffix.lower() not in {".mp4",".webm"} and not interim.get("previewMp4DriveId"):
            interim["previewMp4DriveId"]=drive.upload(
                make_preview_mp4(original,out,int(video["width"]),int(video["height"]),has_audio),
                destination,"preview.mp4","video/mp4"
            )
            if on_audio_ready:
                on_audio_ready(dict(interim))
        poster_id=interim.get("posterDriveId") or drive.upload(
            make_poster(original,out,duration),destination,"poster.jpg","image/jpeg"
        )
        qualities=quality_ladder(int(video["width"]),int(video["height"]))
        print(f"Encoding Series HLS ladder: {len(qualities)} qualities; duration={duration:.1f}s",flush=True)
        master=make_hls(original,out,qualities)
        renditions=[]
        for q in qualities:
            h=q["height"]
            pl=f"{h}p.m3u8"; media=f"{h}p.ts"
            renditions.append({
                "height":h,"width":q["width"],"bitrateKbps":q["bitrate"],
                "playlist":pl,"playlistDriveId":drive.upload(os.path.join(out,pl),destination,pl,"application/vnd.apple.mpegurl"),
                "media":media,"mediaDriveId":drive.upload(os.path.join(out,media),destination,media,"video/mp2t")
            })
        return {**interim,
            "generatedAt":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
            "posterDriveId":poster_id,
            "hls":{"masterDriveId":drive.upload(master,destination,"master.m3u8","application/vnd.apple.mpegurl"),
                   "renditions":renditions,"duration":duration}
        }

def repair_video_audio(drive, item, outputs_parent, on_audio_ready=None):
    """Recover a missing MP3 even when HLS was completed in an earlier run.

    This is intentionally audio-only: do not re-encode the already published
    360p/480p/720p/1080p ladder or change the source MTS/MP4.
    """
    source=item.get("videoDriveId")
    if not source:
        raise RuntimeError("Cannot repair audio without the original video")
    output=drive.folder(outputs_parent,item["id"])
    with tempfile.TemporaryDirectory(prefix="irgun-series-repair-mp3-") as temp:
        suffix=extension(item.get("videoFilename") or "") or ".mp4"
        original=os.path.join(temp,"source"+suffix)
        drive.download(source,original)
        _,has_audio,_=probe(original)
        generated=dict(item.get("generated") or {})
        if not has_audio:
            generated["audioNoTrack"]=True
            print("No audio track in Series video:",item["id"],flush=True)
            return generated
        print("Repairing missing Series MP3 without regenerating HLS:",item["id"],flush=True)
        generated["audioDriveId"]=drive.upload(make_mp3(original,temp),output,"audio.mp3","audio/mpeg")
        generated.pop("audioNoTrack",None)
        if on_audio_ready:
            on_audio_ready(dict(generated))
        return generated


def encode_audio_only(drive,item,outputs_parent):
    audio_id=item.get("audioDriveId")
    if not audio_id or item.get("videoDriveId"):
        raise RuntimeError("Not an audio-only upload")
    suffix=extension(item.get("audioFilename") or "")
    if suffix not in KNOWN_AUDIO_EXTENSIONS:
        raise RuntimeError("Unrecognized audio format")
    folder=drive.folder(outputs_parent,item["id"])
    with tempfile.TemporaryDirectory(prefix="irgun-series-audio-") as directory:
        src=os.path.join(directory,"source"+suffix)
        drive.download(audio_id,src)
        _,has_audio,_=probe(src)
        if not has_audio:
            raise RuntimeError("Uploaded audio file has no decodable audio track")
        dest=make_mp3(src,directory)
        return {"generatedAt":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
                "audioDriveId":drive.upload(dest,folder,"audio.mp3","audio/mpeg")}

def refresh_public_catalog():
    """Preserve an audio-ready map even while HLS transcoding continues."""
    import sys
    subprocess.run([sys.executable, "-u", "series_ingest.py"], check=True)
    if os.getenv("GITHUB_ACTIONS","").lower()!="true":
        return
    run("git","config","user.name","Irgun Series Converter")
    run("git","config","user.email","actions@users.noreply.github.com")
    run("git","add","series-public-map.json")
    changed=subprocess.run(["git","diff","--cached","--quiet"])
    if changed.returncode==1:
        run("git","commit","-m","Publish Series audio while HLS encoding continues [skip ci]")
        run("git","push","origin","HEAD:main")
        print("Published audio-ready Series catalog before HLS",flush=True)
    elif changed.returncode:
        raise RuntimeError("Unable to inspect pending Series map changes")


def main(limit=1):
    limit=max(1,min(int(limit),4))
    drive=SeriesDrive()
    maps=drive.backup_maps_folder()
    private=drive.read_json(maps,PRIVATE_MAP_NAME)
    if not private or not private.get("items"):
        print("No Series private catalog yet; nothing to encode")
        return 0
    output_root=None
    count=0; errors=[]
    for item in private["items"]:
        if count>=limit:break
        generated=item.get("generated") or {}
        video=bool(item.get("videoDriveId"))
        if video:
            hls_complete=bool(generated.get("hls",{}).get("masterDriveId") and
                              generated.get("posterDriveId"))
            # Deleting the old independently uploaded audio must not prevent
            # regeneration from the original camera video.
            missing_mp3=not item.get("audioDriveId") and not generated.get("audioDriveId") \
                and not generated.get("audioNoTrack")
            pending=not hls_complete or missing_mp3
        else:
            ext=extension(item.get("audioFilename") or "")
            pending=bool(item.get("audioDriveId") and ext!=".mp3" and
                         not generated.get("audioDriveId"))
        if not pending: continue
        try:
            if output_root is None:output_root=drive.series_outputs_folder()
            print("Processing Series",item["id"],"format",extension(item.get("videoFilename") or item.get("audioFilename") or ""),flush=True)
            def on_audio_ready(partial):
                item["generated"]=partial
                drive.write_json(maps,PRIVATE_MAP_NAME,private)
                refresh_public_catalog()
            fresh=(encode_video(drive,item,output_root,on_audio_ready) if not hls_complete else
                   repair_video_audio(drive,item,output_root,on_audio_ready)) if video else encode_audio_only(drive,item,output_root)
            item["generated"]=fresh
            # Persist after every successful lecture; don't lose work if the
            # next recording fails or GitHub runner is interrupted.
            drive.write_json(maps,PRIVATE_MAP_NAME,private)
            print("Completed Series conversion:",item["id"],flush=True)
            count+=1
        except Exception as error:
            errors.append(f"{item['id']}: {error}")
            print("ERROR:",item["id"],str(error),flush=True)
    print(f"Series conversions completed: {count}; failed: {len(errors)}")
    if errors:
        raise RuntimeError("; ".join(errors))
    return 0

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--limit",type=int,default=1)
    args=parser.parse_args()
    raise SystemExit(main(args.limit))
