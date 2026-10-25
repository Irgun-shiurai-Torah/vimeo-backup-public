import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

from series_catalog import build_maps
from series_media import make_hls, make_mp3, make_poster, make_preview_mp4, probe, quality_ladder


def source(name="Lecture.wav", file_id="audio-original", modified="2026-10-25T00:00:00Z"):
    return {"speaker":"Rabbi A","year":"5787","file":{
        "id":file_id,"name":name,"modifiedTime":modified,"createdTime":modified,
        "mimeType":"audio/wav"}}


class SeriesMediaTests(unittest.TestCase):
    def test_audio_only_conversion_survives_catalog_rescan(self):
        files=[source()]
        public,private=build_maps(files)
        self.assertFalse(public["items"][0]["audioReady"])
        private["items"][0]["generated"]={"audioDriveId":"generated-mp3"}
        after,again=build_maps(files,private)
        self.assertTrue(after["items"][0]["audioReady"])
        self.assertEqual(after["items"][0]["audioFormat"],"mp3")
        self.assertEqual(again["items"][0]["generated"]["audioDriveId"],"generated-mp3")
        changed,_=build_maps([source(modified="2026-10-26T00:00:00Z")],private)
        self.assertFalse(changed["items"][0]["audioReady"])

    def test_audio_published_before_hls_encoding(self):
        from unittest.mock import patch
        from series_media import encode_video
        sequence=[]
        class FakeDrive:
            def folder(self,parent,name):return "staging-output"
            def download(self,fid,path):
                pathlib.Path(path).write_bytes(b"fake-video")
            def upload(self,path,parent,name,mime):
                sequence.append("upload:"+name)
                return "id-"+name
        item={"id":"series-"+"a"*24,"videoDriveId":"source-video","videoFilename":"lesson.MTS",
              "audioDriveId":None,"generated":{}}
        def audio_created(info):
            self.assertIn("audioDriveId",info)
            sequence.append("audio-published")
        def encode_hls(_source,_destination,_quality):
            self.assertIn("audio-published",sequence)
            sequence.append("hls-encoded")
            return "/tmp/master.m3u8"
        with patch("series_media.probe",return_value=({"width":640,"height":360},True,3)), \
             patch("series_media.make_mp3",return_value="/tmp/audio.mp3"), \
             patch("series_media.make_poster",return_value="/tmp/poster.jpg"), \
             patch("series_media.make_preview_mp4",return_value="/tmp/preview.mp4"), \
             patch("series_media.make_hls",side_effect=encode_hls):
            result=encode_video(FakeDrive(),item,"output-root",audio_created)
        self.assertLess(sequence.index("audio-published"),sequence.index("upload:preview.mp4"))
        self.assertLess(sequence.index("upload:preview.mp4"),sequence.index("hls-encoded"))
        self.assertEqual(result["audioDriveId"],"id-audio.mp3")
        self.assertIn("masterDriveId",result["hls"])

    def test_missing_mp3_is_queued_even_when_hls_is_ready(self):
        from series_media import needs_series_work
        item={"id":"series-"+"a"*24,"videoDriveId":"source-mts",
              "audioDriveId":None,"generated":{"hls":{"masterDriveId":"master-id"},
              "posterDriveId":"poster-id"}}
        self.assertEqual(needs_series_work(item),(True,True))
        item["generated"]["audioDriveId"]="repaired-audio-id"
        self.assertEqual(needs_series_work(item),(False,True))
        del item["generated"]["audioDriveId"]
        item["generated"]["audioNoTrack"]=True
        self.assertEqual(needs_series_work(item),(False,True))

    def test_repair_does_not_reencode_video(self):
        from unittest.mock import patch
        from series_media import repair_video_audio
        class FakeDrive:
            def folder(self,parent,name):return "outputs"
            def download(self,fid,path):pathlib.Path(path).write_bytes(b"original")
            def upload(self,path,parent,name,mime):
                self.last=(name,mime)
                return "repaired-mp3-id"
        old_hls={"masterDriveId":"old-master-id","renditions":[{"height":720}]}
        item={"id":"series-"+"b"*24,"videoDriveId":"source-mts",
          "videoFilename":"lecture.mts","generated":{"hls":old_hls,"posterDriveId":"old-poster"}}
        fake=FakeDrive()
        with patch("series_media.probe",return_value=({"width":1920,"height":1080},True,30)), \
             patch("series_media.make_mp3",return_value="/tmp/audio.mp3"), \
             patch("series_media.make_hls",side_effect=AssertionError("HLS must not be re-encoded")):
            result=repair_video_audio(fake,item,"outputs")
        self.assertEqual(result["audioDriveId"],"repaired-mp3-id")
        self.assertEqual(result["hls"],old_hls)
        self.assertEqual(fake.last,("audio.mp3","audio/mpeg"))

    def test_only_browser_supported_original_video_skips_early_preview(self):
        from series_media import is_native_web_video
        h264={"codec_name":"h264","audioCodec":"aac"}
        self.assertTrue(is_native_web_video("shiur.mp4",h264,True))
        self.assertFalse(is_native_web_video("shiur.mts",h264,True))
        self.assertFalse(is_native_web_video("shiur.mov",h264,True))
        self.assertFalse(is_native_web_video("shiur.mp4",{"codec_name":"hevc","audioCodec":"aac"},True))
        self.assertFalse(is_native_web_video("shiur.mp4",{"codec_name":"h264","audioCodec":"ac3"},True))
        self.assertTrue(is_native_web_video("shiur.webm",{"codec_name":"vp9","audioCodec":"opus"},True))
        self.assertFalse(is_native_web_video("shiur.webm",{"codec_name":"av1","audioCodec":"aac"},True))

    def test_old_preview_does_not_mark_video_ready_before_hls(self):
        files=[source("Shabbos MTS shiur.mts",file_id="camera-source")]
        public,private=build_maps(files)
        self.assertFalse(public["items"][0]["videoReady"])
        private["items"][0]["generated"]={"previewMp4DriveId":"early-mp4"}
        updated,retained=build_maps(files,private)
        self.assertFalse(updated["items"][0]["videoReady"])
        self.assertFalse(updated["items"][0]["hlsReady"])
        self.assertEqual(updated["items"][0]["status"],"processing")
        self.assertEqual(retained["items"][0]["generated"]["previewMp4DriveId"],"early-mp4")

    def test_poster_and_preview_flags_never_publish_drive_ids(self):
        files=[source("Shiur.mts",file_id="source-video")]
        _,private=build_maps(files)
        private["items"][0]["generated"]={
            "posterDriveId":"secret-drive-poster-id",
            "previewMp4DriveId":"secret-drive-preview-id"
        }
        public,_=build_maps(files,private)
        self.assertTrue(public["items"][0]["posterReady"])
        self.assertTrue(public["items"][0]["previewReady"])
        self.assertFalse(public["items"][0]["videoReady"])
        self.assertNotIn("secret-drive-poster-id",str(public))
        self.assertNotIn("secret-drive-preview-id",str(public))

    def test_uploaded_parsha_metadata_is_preserved(self):
        file=source("Thursday Evening shiur.mp4",file_id="source")
        file["file"]["appProperties"]={"irgunSeriesParsha":"Lech-Lecha","irgunSeriesHoliday":""}
        public,_=build_maps([file])
        self.assertEqual(public["items"][0]["parsha"],"Lech-Lecha")
        self.assertFalse(public["items"][0]["videoReady"])

    def test_ladder_never_upscales(self):
        self.assertEqual([r["height"] for r in quality_ladder(1920,1080)],[360,480,720,1080])
        self.assertEqual([r["height"] for r in quality_ladder(1280,720)],[360,480,720])
        self.assertEqual([r["height"] for r in quality_ladder(426,240)],[240])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),"FFmpeg not installed")
    def test_real_ffmpeg_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            video=os.path.join(folder,"source.mp4")
            subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y",
              "-f","lavfi","-i","color=c=black:s=640x360:r=20:d=3",
              "-f","lavfi","-i","sine=frequency=440:sample_rate=48000:duration=3",
              "-shortest","-c:v","libx264","-c:a","aac","-pix_fmt","yuv420p",video],check=True)
            stream,audio,duration=probe(video)
            self.assertTrue(stream)
            self.assertTrue(audio)
            self.assertGreater(duration,2)
            out=os.path.join(folder,"hls")
            pathlib.Path(out).mkdir()
            qualities=quality_ladder(stream["width"],stream["height"])
            master=make_hls(video,out,qualities)
            self.assertTrue(pathlib.Path(master).exists())
            self.assertTrue(pathlib.Path(out,"360p.ts").exists())
            self.assertIn("#EXT-X-BYTERANGE",pathlib.Path(out,"360p.m3u8").read_text())
            self.assertGreater(pathlib.Path(make_mp3(video,out)).stat().st_size,500)
            self.assertGreater(pathlib.Path(make_poster(video,out,duration)).stat().st_size,300)
            preview=pathlib.Path(make_preview_mp4(video,out,640,360))
            self.assertTrue(preview.exists())
            self.assertLess(preview.stat().st_size,1_000_000,"Short Series fallback must stay compact")

if __name__=="__main__":
    unittest.main()
