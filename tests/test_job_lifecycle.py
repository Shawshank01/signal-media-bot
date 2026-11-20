import asyncio
import fcntl
import json
import os
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

import httpx

os.environ.setdefault("BOT_PHONE_NUMBER", "+15555550100")

import bot


class JobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_bounds_pending_messages_and_processes_them_in_order(self):
        started = asyncio.Event()
        release = asyncio.Event()
        processed = []

        async def process(message, config, client):
            processed.append(message.text)
            started.set()
            await release.wait()

        async def post(client, text):
            return await client.post(bot.settings.webhook_path, json={
                "envelope": {
                    "sourceNumber": "+15555550101",
                    "dataMessage": {"message": text},
                },
            })

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(bot.settings, "shared_media_dir", Path(directory)),
            patch.object(bot, "process_message", process),
        ):
            async with bot.lifespan(bot.app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=bot.app), base_url="http://bot.test"
            ) as client:
                urls = [f"https://example.test/{index}" for index in range(10)]
                self.assertEqual((await post(client, urls[0])).json(), {"status": "accepted"})
                await asyncio.wait_for(started.wait(), 2)
                for url in urls[1:9]:
                    self.assertEqual((await post(client, url)).json(), {"status": "accepted"})
                self.assertEqual((await post(client, "ordinary text")).json(), {"status": "ignored"})
                self.assertEqual((await post(client, urls[9])).status_code, 503)
                self.assertEqual(processed, urls[:1])

                release.set()
                await asyncio.wait_for(bot.app.state.messages.join(), 2)
                self.assertEqual(processed, urls[:9])
                self.assertEqual((await post(client, urls[9])).json(), {"status": "accepted"})
                await asyncio.wait_for(bot.app.state.messages.join(), 2)
                self.assertEqual(processed, urls)

    async def test_real_worker_downloads_and_inspects_offline_media(self):
        url = "data:video/mp4;base64,bWVkaWE="
        config = bot.Settings(cookies_file=None, download_timeout_seconds=15)
        with tempfile.TemporaryDirectory() as directory:
            info = await bot.extract_media_info(url, config)
            self.assertIn("No separate audio tracks detected.", info)
            result = await bot.download_with_ytdlp(url, Path(directory), config)
            self.assertEqual(len(result.media), 1)
            self.assertEqual(result.media[0].content_type, "video/mp4")
            self.assertEqual(result.media[0].path.read_bytes(), b"media")

    async def test_download_timeout_stops_worker_group_and_cleans_partial_files(self):
        await self.check_worker_cleanup(cancel=False)

    async def test_download_cancellation_stops_worker_group_and_cleans_partial_files(self):
        await self.check_worker_cleanup(cancel=True)

    async def check_worker_cleanup(self, cancel):
        messages = []

        def handle_send(request):
            messages.append(json.loads(request.content)["message"])
            return httpx.Response(201)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worker = root / "worker.py"
            worker.write_text(dedent("""
                import fcntl
                import json
                from pathlib import Path
                import subprocess
                import sys
                import time

                root = Path(__file__).parent
                if sys.argv[1] == "child":
                    with (root / "child.lock").open("w") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        (root / "started").touch()
                        time.sleep(30)
                else:
                    job = json.load(sys.stdin)
                    (Path(job["destination"]) / "partial.mp4").touch()
                    subprocess.run([sys.executable, __file__, "child"], check=True)
            """))
            media_dir = root / "media"
            media_dir.mkdir()
            config = bot.Settings(
                cookies_file=None,
                shared_media_dir=media_dir,
                download_timeout_seconds=10 if cancel else 2,
            )
            message = bot.IncomingMessage("https://example.test/video", "+15555550101", None, (), "")
            with patch.object(bot, "__file__", str(worker)):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle_send)) as client:
                    job = asyncio.create_task(bot.process_message(message, config, client))
                    if cancel:
                        try:
                            async with asyncio.timeout(5):
                                while not (root / "started").exists():
                                    await asyncio.sleep(0.01)
                        finally:
                            job.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await job
                    else:
                        await asyncio.wait_for(job, 5)
                        self.assertIn("The download timed out.", messages)

            self.assertTrue((root / "started").exists())
            self.assertEqual(list(media_dir.iterdir()), [])
            # The child holds the lock until the process exits.
            with (root / "child.lock").open("r+") as lock:
                async with asyncio.timeout(2):
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            await asyncio.sleep(0.01)
