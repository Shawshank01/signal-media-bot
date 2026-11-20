import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("BOT_PHONE_NUMBER", "+15555550100")

import bot


X_URL = "https://x.com/user/status/123"
BSKY_URL = "https://bsky.app/profile/user.example/post/123"
PHOTO_URL = "https://media.example/photo.jpg"
VIDEO_URL = "https://media.example/fallback.mp4"


class SocialRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def send_post(self, url, payload, downloader, option="", api_status=200):
        sent = []
        fetched = []

        def upstream(request):
            if request.method == "POST":
                sent.append(json.loads(request.content))
                return httpx.Response(201)
            fetched.append(str(request.url))
            if request.url.host in {"api.fxtwitter.com", "api.fxbsky.app"}:
                return httpx.Response(api_status, json=payload)
            if str(request.url) == PHOTO_URL:
                return httpx.Response(200, content=b"photo", headers={"content-type": "image/jpeg"})
            if str(request.url) == VIDEO_URL:
                return httpx.Response(200, content=b"fallback", headers={"content-type": "video/mp4"})
            raise AssertionError(f"Unexpected media request {request.url}")

        with tempfile.TemporaryDirectory() as directory:
            config = bot.Settings(cookies_file=None, shared_media_dir=Path(directory))
            message = bot.IncomingMessage(f"{url} {option}".strip(), "+15555550101", None, (), "")
            with patch.object(bot, "download_with_ytdlp", downloader):
                async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                    await bot.process_message(message, config, client)
            self.assertEqual(list(Path(directory).iterdir()), [])
        return sent[-1], fetched

    async def primary_download(self, url, destination, config, options):
        path = destination / ("chosen.m4a" if options.audio_only else "chosen.mp4")
        path.write_bytes(b"primary")
        content_type = "audio/mp4" if options.audio_only else "video/mp4"
        return bot.DownloadResult([bot.DownloadedMedia(path, content_type)], "yt-dlp title")

    async def test_mixed_posts_use_yt_dlp_video_and_fxembed_photos(self):
        posts = {
            X_URL: {"tweet": {"text": "Original caption", "media": {"all": [
                {"type": "video", "url": VIDEO_URL}, {"type": "photo", "url": PHOTO_URL},
            ]}}},
            BSKY_URL: {"status": {"text": "Original caption", "media": {
                "videos": [{"url": VIDEO_URL}], "photos": [{"url": PHOTO_URL}],
            }}},
        }
        for url, payload in posts.items():
            with self.subTest(url=url):
                downloader = AsyncMock(side_effect=self.primary_download)
                sent, fetched = await self.send_post(url, payload, downloader, "720")
                self.assertEqual(downloader.await_count, 1)
                self.assertEqual(downloader.await_args.args[3].max_height, 720)
                self.assertEqual(fetched[1:], [PHOTO_URL])
                self.assertTrue(sent["message"].startswith("Original caption"))
                self.assertEqual([item.split(";", 1)[0] for item in sent["base64_attachments"]],
                                 ["data:video/mp4", "data:image/jpeg"])

    async def test_photo_only_post_skips_yt_dlp(self):
        downloader = AsyncMock()
        payload = {"tweet": {"media": {"photos": [{"url": PHOTO_URL}]}}}
        sent, fetched = await self.send_post(X_URL, payload, downloader)
        downloader.assert_not_awaited()
        self.assertEqual(fetched[1:], [PHOTO_URL])
        self.assertTrue(sent["base64_attachments"][0].startswith("data:image/jpeg;"))

    async def test_plain_video_falls_back_but_resolution_cap_does_not(self):
        payload = {"status": {"media": {"videos": [{"url": VIDEO_URL}]}}}
        downloader = AsyncMock(side_effect=bot.DownloadError("The primary download failed."))
        ordinary, ordinary_fetches = await self.send_post(BSKY_URL, payload, downloader)
        self.assertEqual(ordinary_fetches[1:], [VIDEO_URL])
        self.assertTrue(ordinary["base64_attachments"][0].endswith("ZmFsbGJhY2s="))

        constrained, constrained_fetches = await self.send_post(BSKY_URL, payload, downloader, "720")
        self.assertEqual(len(constrained_fetches), 1)
        self.assertEqual(constrained["message"], "The primary download failed.")

    async def test_audio_only_skips_fxembed_and_video_survives_failed_lookup(self):
        downloader = AsyncMock(side_effect=self.primary_download)
        audio, fetched = await self.send_post(X_URL, {}, downloader, "audio")
        self.assertEqual(fetched, [])
        self.assertTrue(audio["base64_attachments"][0].startswith("data:audio/mp4;"))

        video, fetched = await self.send_post(X_URL, {}, downloader, api_status=503)
        self.assertEqual(len(fetched), 1)
        self.assertTrue(video["base64_attachments"][0].startswith("data:video/mp4;"))


class SocialFormatTests(unittest.TestCase):
    def test_each_clip_gets_its_own_format_and_size_retries(self):
        info = {
            "title": "Two clips",
            "entries": [
                {"id": "clip", "formats": [{"format_id": "one", "ext": "mp4", "height": 720}]},
                {"id": "clip", "formats": [
                    {"format_id": "large", "ext": "mp4", "height": 1080},
                    {"format_id": "small", "ext": "mp4", "height": 720},
                ]},
            ],
        }
        attempts = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download):
                return info

            def process_ie_result(self, entry, download):
                format_id = self.options["format"]
                attempts.append(format_id)
                path = Path(self.options["outtmpl"].replace("%(id)s", entry["id"]).replace("%(ext)s", "mp4"))
                with path.open("wb") as output:
                    output.truncate(1024 ** 2 + 1 if format_id == "large" else 20)

        config = bot.Settings(cookies_file=None, max_file_size_mb=1)
        with tempfile.TemporaryDirectory() as directory, patch.object(bot.yt_dlp, "YoutubeDL", FakeYoutubeDL):
            destination = Path(directory)
            self.assertEqual(bot._download_ytdlp(X_URL, destination, config, bot.DownloadOptions()), "Two clips")
            self.assertEqual(attempts, ["one", "large", "small"])
            self.assertEqual(sorted(path.name for path in destination.iterdir()), ["01-clip.mp4", "02-clip.mp4"])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_extracts_aac_from_a_combined_mp4(self):
        real_downloader = bot.yt_dlp.YoutubeDL
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=16x16:rate=10",
                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.2",
                "-c:v", "mpeg4", "-c:a", "aac", str(source),
            ], check=True, timeout=15)
            encoded = base64.b64encode(source.read_bytes()).decode("ascii")
            info = {"id": "clip", "title": "Combined streams", "formats": [{
                "format_id": "mp4", "ext": "mp4", "url": f"data:video/mp4;base64,{encoded}",
                "vcodec": "mp4v", "acodec": "aac", "height": 16,
            }]}

            def youtube_dl(options):
                instance = real_downloader(options)
                if "format" not in options:
                    instance.extract_info = lambda url, download: info
                return instance

            destination = root / "downloads"
            destination.mkdir()
            config = bot.Settings(cookies_file=None)
            with patch.object(bot.yt_dlp, "YoutubeDL", side_effect=youtube_dl):
                bot._download_ytdlp(BSKY_URL, destination, config, bot.DownloadOptions(audio_only=True))
            self.assertEqual([path.name for path in destination.iterdir()], ["clip.m4a"])
            probe = subprocess.check_output([
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type",
                "-of", "json", str(destination / "clip.m4a"),
            ], timeout=10)
            self.assertEqual(json.loads(probe)["streams"], [{"codec_name": "aac", "codec_type": "audio"}])
