import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("BOT_PHONE_NUMBER", "+15555550100")

import bot


class SizeFallbackTests(unittest.TestCase):
    def test_retries_next_combination_when_merged_file_exceeds_limit(self):
        mib = 1024 * 1024
        info = {
            "title": "Test video",
            "formats": [
                {"format_id": "398", "height": 720, "vcodec": "av01", "acodec": "none", "filesize": 78743077},
                {"format_id": "397", "height": 480, "vcodec": "av01", "acodec": "none", "filesize": 44829787},
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 25631491},
            ],
        }
        attempts = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = dict(options)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download):
                return info

            def download(self, urls):
                spec = self.options["format"]
                attempts.append(spec)
                output = Path(self.options["outtmpl"].replace("%(id)s", "video").replace("%(ext)s", "mp4"))
                with output.open("wb") as file:
                    file.truncate(100 * mib + 1 if spec == "398+140" else 70 * mib)

        config = bot.Settings(bot_phone_number="+15555550100", cookies_file=None)
        with tempfile.TemporaryDirectory() as directory, patch.object(bot.yt_dlp, "YoutubeDL", FakeYoutubeDL):
            destination = Path(directory)
            title = bot._download_ytdlp("https://example.com/video", destination, config, bot.DownloadOptions())
            self.assertEqual(["398+140", "397+140"], attempts)
            self.assertLessEqual((destination / "video.mp4").stat().st_size, config.max_file_size)
            self.assertEqual("Test video", title)

    def run_scenario(self, formats, sizes, options=None):
        attempts = []
        info = {"title": "Test", "formats": formats}

        class FakeYoutubeDL:
            def __init__(self, settings):
                self.settings = dict(settings)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download):
                return info

            def download(self, urls):
                spec = self.settings["format"]
                attempts.append(spec)
                suffix = "m4a" if options and options.audio_only else "mp4"
                output = Path(self.settings["outtmpl"].replace("%(id)s", "video").replace("%(ext)s", suffix))
                with output.open("wb") as file:
                    file.truncate(sizes[spec])

        config = bot.Settings(bot_phone_number="+15555550100", cookies_file=None)
        with tempfile.TemporaryDirectory() as directory, patch.object(bot.yt_dlp, "YoutubeDL", FakeYoutubeDL):
            try:
                destination = Path(directory)
                bot._download_ytdlp("https://example.com/video", destination, config, options or bot.DownloadOptions())
                suffix = "m4a" if options and options.audio_only else "mp4"
                return attempts, (destination / f"video.{suffix}").stat().st_size, None
            except bot.DownloadError as exc:
                return attempts, None, str(exc)

    def test_retries_audio_only_when_actual_size_exceeds_estimate(self):
        mib = 1024 * 1024
        formats = [
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 90 * mib, "abr": 128},
            {"format_id": "139", "vcodec": "none", "acodec": "mp4a", "filesize": 40 * mib, "abr": 64},
        ]
        attempts, size, error = self.run_scenario(formats, {"140": 101 * mib, "139": 40 * mib}, bot.DownloadOptions(audio_only=True))
        self.assertEqual(["140", "139"], attempts)
        self.assertEqual(40 * mib, size)
        self.assertIsNone(error)

    def test_exhausted_formats_report_error_without_repeating(self):
        mib = 1024 * 1024
        formats = [
            {"format_id": "18", "height": 360, "vcodec": "avc1", "acodec": "mp4a", "filesize": 99 * mib},
        ]
        attempts, size, error = self.run_scenario(formats, {"18": 101 * mib})
        self.assertEqual(["18"], attempts)
        self.assertIsNone(size)
        self.assertIn("This media is too large", error or "")

    def test_retry_keeps_higher_resolution_with_a_smaller_audio_track(self):
        mib = 1024 * 1024
        formats = [
            {"format_id": "720", "height": 720, "vcodec": "av01", "acodec": "none", "filesize": 75 * mib},
            {"format_id": "480", "height": 480, "vcodec": "av01", "acodec": "none", "filesize": 40 * mib},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 24 * mib, "abr": 128},
            {"format_id": "139", "vcodec": "none", "acodec": "mp4a", "filesize": 9 * mib, "abr": 48},
        ]
        attempts, size, error = self.run_scenario(formats, {"720+140": 101 * mib, "720+139": 85 * mib})
        self.assertEqual(["720+140", "720+139"], attempts)
        self.assertEqual(85 * mib, size)
        self.assertIsNone(error)

    def test_retry_preserves_requested_audio_track_and_resolution(self):
        mib = 1024 * 1024
        formats = [
            {"format_id": "720", "height": 720, "vcodec": "av01", "acodec": "none", "filesize": 60 * mib},
            {"format_id": "480", "height": 480, "vcodec": "av01", "acodec": "none", "filesize": 50 * mib},
            {"format_id": "360", "height": 360, "vcodec": "av01", "acodec": "none", "filesize": 40 * mib},
            {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 20 * mib},
            {"format_id": "139", "vcodec": "none", "acodec": "mp4a", "filesize": 10 * mib},
        ]
        options = bot.DownloadOptions(max_height=480, audio_track="140")
        attempts, size, error = self.run_scenario(formats, {"480+140": 101 * mib, "360+140": 60 * mib}, options)
        self.assertEqual(["480+140", "360+140"], attempts)
        self.assertEqual(60 * mib, size)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
