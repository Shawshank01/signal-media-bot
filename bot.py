from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("signal-media-bot")

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
X_STATUS_RE = re.compile(r"/(?:[^/]+/)?status/(\d+)", re.IGNORECASE)
BSKY_HOSTS = {"bsky.app", "www.bsky.app", "fxbsky.app", "www.fxbsky.app"}
BSKY_POST_RE = re.compile(r"/profile/([^/]+)/post/([^/?#]+)", re.IGNORECASE)
GROUP_PREFIX = "group."


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )

    bot_phone_number: str
    bot_uuid: str | None = None
    signal_api_url: str = "http://signal-api:8080"
    webhook_path: str = "/webhook/signal"
    trigger_prefixes: str = "/dl,!dl"
    max_file_size_mb: int = 100
    max_urls_per_message: int = 4
    download_timeout_seconds: int = 300
    fxtwitter_api_url: str = "https://api.fxtwitter.com"
    fxbsky_api_url: str = "https://api.fxbsky.app"
    shared_media_dir: Path = Path("/tmp/signal_shared_media")
    cookies_file: Path | None = Path("/app/cookies.txt")
    user_agent: str = "signal-media-bot/1.0"

    @property
    def max_file_size(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def prefixes(self) -> tuple[str, ...]:
        return tuple(
            x.strip().lower() for x in self.trigger_prefixes.split(",") if x.strip()
        )


settings = Settings()


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    sender: str
    group_id: str | None
    mentions: tuple[dict[str, Any], ...]
    quote_text: str


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    content_type: str


def _first_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, dict)), None)
    return None


def unwrap_envelope(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Accept REST webhook envelopes and JSON-RPC notification wrappers."""
    candidates: list[Any] = [payload]
    if isinstance(payload.get("params"), dict):
        candidates.append(payload["params"])
    if isinstance(payload.get("result"), dict):
        candidates.append(payload["result"])
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        envelope = candidate.get("envelope", candidate)
        if isinstance(envelope, dict) and isinstance(envelope.get("dataMessage"), dict):
            return envelope
    return None


def parse_message(payload: dict[str, Any]) -> IncomingMessage | None:
    envelope = unwrap_envelope(payload)
    if not envelope:
        return None
    data = envelope.get("dataMessage") or {}
    sender = str(
        envelope.get("sourceNumber")
        or envelope.get("source")
        or envelope.get("sourceUuid")
        or ""
    )
    if not data.get("message") and not data.get("quote"):
        return None
    group = data.get("groupInfo") or {}
    quote = data.get("quote") or {}
    return IncomingMessage(
        text=str(data.get("message") or ""),
        sender=sender,
        group_id=str(group.get("groupId")) if group.get("groupId") else None,
        mentions=tuple(x for x in data.get("mentions", []) if isinstance(x, dict)),
        quote_text=str(quote.get("text") or ""),
    )


def extract_urls(text: str) -> list[str]:
    found: list[str] = []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(".,!?;:)]}")
        if url not in found:
            found.append(url)
    return found


def is_x_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in X_HOSTS and bool(
        X_STATUS_RE.search(urlparse(url).path)
    )


def is_bsky_url(url: str) -> bool:
    parsed = urlparse(url)
    return bool(
        parsed.hostname
        and parsed.hostname.lower() in BSKY_HOSTS
        and BSKY_POST_RE.search(parsed.path)
    )


def signal_recipient(message: IncomingMessage) -> str:
    if not message.group_id:
        return message.sender
    if message.group_id.startswith(GROUP_PREFIX):
        return message.group_id
    encoded_group_id = base64.b64encode(message.group_id.encode("ascii")).decode(
        "ascii"
    )
    return f"{GROUP_PREFIX}{encoded_group_id}"


def group_triggered(message: IncomingMessage, settings: Settings) -> bool:
    lowered = message.text.lower().strip()
    prefix = any(
        lowered == item or lowered.startswith(item + " ") for item in settings.prefixes
    )
    native_mention = bool(settings.bot_uuid) and any(
        str(item.get("uuid") or "") == settings.bot_uuid for item in message.mentions
    )
    return prefix or native_mention


def message_urls(message: IncomingMessage, settings: Settings) -> list[str]:
    urls = extract_urls(message.text)
    if message.group_id:
        if not group_triggered(message, settings):
            return []
        urls.extend(extract_urls(message.quote_text))
    return list(dict.fromkeys(urls))[: settings.max_urls_per_message]


class DownloadError(Exception):
    pass


class FxTwitterClient:
    def __init__(self, client: httpx.AsyncClient, config: Settings):
        self.client = client
        self.config = config

    async def download(self, url: str, destination: Path) -> list[DownloadedMedia]:
        match = X_STATUS_RE.search(urlparse(url).path)
        if not match:
            raise DownloadError("That X link is not a status post.")
        api_url = f"{self.config.fxtwitter_api_url.rstrip('/')}/{urlparse(url).path.lstrip('/')}"
        response = await self.client.get(api_url)
        response.raise_for_status()
        data = response.json().get("tweet", {})
        media = data.get("media") or {}
        videos = media.get("videos") or []
        photos = media.get("photos") or []
        if videos:
            video = max(videos, key=lambda item: int(item.get("bitrate") or 0))
            media_url = video.get("url")
            if media_url:
                return [
                    await stream_to_file(
                        self.client, media_url, destination / "video.mp4", self.config
                    )
                ]
        if photos:
            output: list[DownloadedMedia] = []
            for index, photo in enumerate(photos):
                media_url = photo.get("url")
                if media_url:
                    output.append(
                        await stream_to_file(
                            self.client,
                            media_url,
                            destination / f"image-{index}.jpg",
                            self.config,
                        )
                    )
            if output:
                return output
        raise DownloadError("No downloadable media was found in that post.")


class FxBlueskyClient:
    def __init__(self, client: httpx.AsyncClient, config: Settings):
        self.client = client
        self.config = config

    async def download(self, url: str, destination: Path) -> list[DownloadedMedia]:
        match = BSKY_POST_RE.search(urlparse(url).path)
        if not match:
            raise DownloadError("That Bluesky link is not a post.")
        handle, rkey = match.groups()
        api_url = f"{self.config.fxbsky_api_url.rstrip('/')}/2/status/{handle}/{rkey}"
        response = await self.client.get(api_url)
        response.raise_for_status()
        data = response.json().get("status", {})
        media = data.get("media") or {}
        videos = media.get("videos") or []
        photos = media.get("photos") or []
        if videos:
            video = videos[0]
            formats = [
                item
                for item in video.get("formats", [])
                if item.get("container") == "mp4"
            ]
            selected = max(
                formats or [video], key=lambda item: int(item.get("bitrate") or 0)
            )
            media_url = selected.get("url") or video.get("url")
            if media_url:
                return [
                    await stream_to_file(
                        self.client, media_url, destination / "video.mp4", self.config
                    )
                ]
        if photos:
            output: list[DownloadedMedia] = []
            for index, photo in enumerate(photos):
                media_url = photo.get("url")
                if media_url:
                    output.append(
                        await stream_to_file(
                            self.client,
                            media_url,
                            destination / f"image-{index}.jpg",
                            self.config,
                        )
                    )
            if output:
                return output
        raise DownloadError("No downloadable media was found in that post.")


async def stream_to_file(
    client: httpx.AsyncClient, url: str, path: Path, config: Settings
) -> DownloadedMedia:
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            length = int(response.headers.get("content-length") or 0)
            if length > config.max_file_size:
                raise DownloadError(
                    f"The media is larger than {config.max_file_size_mb} MB."
                )
            total = 0
            with path.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 256):
                    total += len(chunk)
                    if total > config.max_file_size:
                        raise DownloadError(
                            f"The media is larger than {config.max_file_size_mb} MB."
                        )
                    output.write(chunk)
        content_type = response.headers.get(
            "content-type", "application/octet-stream"
        ).split(";", 1)[0]
        return DownloadedMedia(path, content_type)
    except httpx.HTTPError as exc:
        raise DownloadError("The media could not be downloaded.") from exc


async def download_with_ytdlp(
    url: str, destination: Path, config: Settings
) -> list[DownloadedMedia]:
    def run() -> list[Path]:
        options = {
            "outtmpl": str(destination / "%(id)s.%(ext)s"),
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "max_filesize": config.max_file_size,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "socket_timeout": config.download_timeout_seconds,
        }
        if config.cookies_file and config.cookies_file.is_file():
            options["cookiefile"] = str(config.cookies_file)
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.download([url])
        except (yt_dlp.utils.DownloadError, OSError) as exc:
            raise DownloadError(
                "The link is private, unavailable, or could not be extracted."
            ) from exc
        return [path for path in destination.iterdir() if path.is_file()]

    try:
        paths = await asyncio.wait_for(
            asyncio.to_thread(run), config.download_timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        raise DownloadError("The download timed out.") from exc
    if not paths:
        raise DownloadError("No downloadable media was found at that link.")
    for path in paths:
        if path.stat().st_size > config.max_file_size:
            raise DownloadError(
                f"The media is larger than {config.max_file_size_mb} MB."
            )
    return [
        DownloadedMedia(
            path,
            "video/mp4"
            if path.suffix.lower() == ".mp4"
            else "application/octet-stream",
        )
        for path in paths
    ]


class SignalClient:
    def __init__(self, client: httpx.AsyncClient, config: Settings):
        self.client = client
        self.config = config

    async def send(
        self,
        message: str,
        destination: IncomingMessage,
        media: list[DownloadedMedia] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "number": self.config.bot_phone_number,
            "message": message,
        }
        payload["recipients"] = [signal_recipient(destination)]
        if media:
            payload["base64_attachments"] = await asyncio.gather(
                *(encode_attachment(item) for item in media)
            )
        response = await self.client.post(
            f"{self.config.signal_api_url.rstrip('/')}/v2/send", json=payload
        )
        if response.is_error:
            log.error(
                "Signal API rejected send (%s): %s",
                response.status_code,
                response.text[:1000],
            )
            response.raise_for_status()


async def encode_attachment(media: DownloadedMedia) -> str:
    encoded = await asyncio.to_thread(
        lambda: base64.b64encode(media.path.read_bytes()).decode("ascii")
    )
    return f"data:{media.content_type};filename={media.path.name};base64,{encoded}"


async def process_message(
    message: IncomingMessage, config: Settings, client: httpx.AsyncClient
) -> None:
    urls = message_urls(message, config)
    if not urls:
        return
    signal = SignalClient(client, config)
    fx = FxTwitterClient(client, config)
    bsky = FxBlueskyClient(client, config)
    for url in urls:
        workdir = Path(
            tempfile.mkdtemp(prefix="signal-media-", dir=config.shared_media_dir)
        )
        try:
            await signal.send("Downloading media...", message)
            if is_x_url(url):
                media = await fx.download(url, workdir)
            elif is_bsky_url(url):
                media = await bsky.download(url, workdir)
            else:
                media = await download_with_ytdlp(url, workdir, config)
            await signal.send("", message, media)
        except DownloadError as exc:
            log.info("Download failed for %s: %s", url, exc)
            await send_error(signal, str(exc), message)
        except (httpx.HTTPError, OSError):
            log.exception("Processing failed for %s", url)
            await send_error(signal, "I could not send that media right now.", message)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


async def send_error(signal: SignalClient, text: str, message: IncomingMessage) -> None:
    try:
        await signal.send(text, message)
    except (httpx.HTTPError, OSError):
        log.exception("Could not send error response")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings.shared_media_dir.mkdir(parents=True, exist_ok=True)
    timeout = httpx.Timeout(settings.download_timeout_seconds, connect=15)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": settings.user_agent}
    ) as client:
        app.state.http_client = client
        yield


app = FastAPI(title="Signal Media Downloader", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(settings.webhook_path)
async def webhook(request: Request) -> dict[str, str]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        return {"status": "ignored"}
    message = parse_message(payload)
    if not message or message.sender in {settings.bot_phone_number, settings.bot_uuid}:
        return {"status": "ignored"}
    asyncio.create_task(
        process_message(message, settings, request.app.state.http_client)
    )
    return {"status": "accepted"}
