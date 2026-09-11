from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from yt_dlp.utils import DownloadError as YtDlpDownloadError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
)
log = logging.getLogger("signal-media-bot")

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
X_STATUS_RE = re.compile(r"/(?:[^/]+/)?status/(\d+)", re.IGNORECASE)
BSKY_HOSTS = {"bsky.app", "www.bsky.app", "fxbsky.app", "www.fxbsky.app"}
BSKY_POST_RE = re.compile(r"/profile/([^/]+)/post/([^/?#]+)", re.IGNORECASE)
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
INSTAGRAM_RE = re.compile(r"^/(p|reel|reels|tv)/([^/?#]+)", re.IGNORECASE)
TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}
TIKTOK_VIDEO_RE = re.compile(r"^(/@[^/]+/video/\d+)", re.IGNORECASE)
REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com"}
REDDIT_POST_RE = re.compile(r"^(/r/[^/]+/comments/[^/]+(?:/[^/]+)?)", re.IGNORECASE)

TRACKING_QUERY_PARAMS = {
    # Analytics & UTM
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    # Ad click IDs
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "msclkid",
    "yclid",
    # Social platforms tracking
    "igsh",
    "igshid",
    "s",
    "ref_src",
    "ref_url",
    "si",
    "feature",
    "pp",
    "ab_channel",
    "embeds_referring_euri",
    "source_ve_path",
    "_t",
    "_r",
    "_s",
    "is_from_webapp",
    "sender_device",
    "sec_uid",
    "share_app_id",
    "ug_btm",
    "rdt",
    "share_id",
    "ref",
    "referrer",
    "origin",
    "context",
    "spm_id_from",
    "from_source",
    "broadcast_type",
    "mkt_tok",
    "mc_cid",
    "mc_eid",
    "source",
}
TRACKING_PREFIXES = ("utm_", "ga_", "fb_", "wicked", "oly_")
GROUP_PREFIX = "group."
AUDIO_CONTENT_TYPES = {
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".weba": "audio/webm",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", case_sensitive=False
    )

    bot_phone_number: str = ""
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

    @field_validator("bot_phone_number")
    @classmethod
    def validate_bot_phone_number(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("BOT_PHONE_NUMBER environment variable is required")
        return v

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


@dataclass(frozen=True)
class DownloadResult:
    media: list[DownloadedMedia]
    text: str = ""


@dataclass(frozen=True)
class DownloadOptions:
    audio_only: bool = False
    max_height: int | None = None
    bestmini: bool = False
    audio_lang: str | None = None
    audio_track: str | None = None
    info_only: bool = False


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


def download_options(message: IncomingMessage) -> DownloadOptions:
    command_text = URL_RE.sub(" ", message.text.lower())
    tokens = set(
        re.findall(r"\b(audio|bestmini|info|360|480|720|1080)\b", command_text)
    )
    heights = [int(token) for token in tokens if token.isdigit()]
    lang_match = re.search(
        r"\b(?:lang|audio|language)[:=]([a-zA-Z0-9_\-]+)\b", command_text
    )
    track_match = re.search(r"\btrack[:=]([a-zA-Z0-9_\-]+)\b", command_text)

    audio_lang = lang_match.group(1).lower() if lang_match else None
    audio_track = track_match.group(1) if track_match else None

    return DownloadOptions(
        audio_only="audio" in tokens,
        max_height=max(heights) if heights else None,
        bestmini="bestmini" in tokens,
        audio_lang=audio_lang,
        audio_track=audio_track,
        info_only="info" in tokens,
    )


def is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_QUERY_PARAMS or any(
        lowered.startswith(prefix) for prefix in TRACKING_PREFIXES
    )


def clean_source_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url.strip()

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return url.strip()

    hostname = (parsed.hostname or "").lower()

    # 1. X (Twitter)
    if hostname in X_HOSTS:
        match = X_STATUS_RE.search(parsed.path)
        if match:
            return f"https://x.com{match.group(0)}"
        if parsed.path.startswith("/i/spaces/"):
            return f"https://x.com{parsed.path.rstrip('/')}"

    # 2. Bluesky
    if hostname in BSKY_HOSTS:
        match = BSKY_POST_RE.search(parsed.path)
        if match:
            handle, rkey = match.groups()
            return f"https://bsky.app/profile/{handle}/post/{rkey}"

    # 3. YouTube
    if hostname in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        query_dict = dict(parse_qsl(parsed.query, keep_blank_values=False))
        if parsed.path == "/watch" and "v" in query_dict:
            res = f"https://www.youtube.com/watch?v={query_dict['v']}"
            if "t" in query_dict:
                res += f"&t={query_dict['t']}"
            return res
        if parsed.path.startswith(("/shorts/", "/live/", "/embed/")):
            clean_path = parsed.path.rstrip("/")
            return f"https://www.youtube.com{clean_path}"
    elif hostname == "youtu.be":
        video_id = parsed.path.strip("/")
        if video_id:
            query_dict = dict(parse_qsl(parsed.query, keep_blank_values=False))
            res = f"https://youtu.be/{video_id}"
            if "t" in query_dict:
                res += f"?t={query_dict['t']}"
            return res

    # 4. Instagram
    if hostname in INSTAGRAM_HOSTS:
        match = INSTAGRAM_RE.match(parsed.path)
        if match:
            media_type, shortcode = match.groups()
            return f"https://www.instagram.com/{media_type.lower()}/{shortcode}/"

    # 5. TikTok
    if hostname in TIKTOK_HOSTS:
        if hostname in {"vm.tiktok.com", "vt.tiktok.com"}:
            return f"https://{parsed.netloc}{parsed.path.rstrip('/')}/"
        match = TIKTOK_VIDEO_RE.match(parsed.path)
        if match:
            return f"https://www.tiktok.com{match.group(1)}"

    # 6. Reddit
    if hostname in REDDIT_HOSTS:
        match = REDDIT_POST_RE.match(parsed.path)
        if match:
            return f"https://www.reddit.com{match.group(1)}/"

    # 7. General fallback
    qsl = parse_qsl(parsed.query, keep_blank_values=False)
    cleaned_params = [(k, v) for k, v in qsl if not is_tracking_param(k)]
    new_query = urlencode(cleaned_params)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, "")
    )


def format_caption(text: str, url: str) -> str:
    cleaned = text.strip()
    source_url = clean_source_url(url)
    return f"{cleaned}\n\nSource: {source_url}" if cleaned else f"Source: {source_url}"


class DownloadError(Exception):
    pass


def _format_size(
    format_info: Mapping[str, Any], duration: float | None = None
) -> int | None:
    size = format_info.get("filesize") or format_info.get("filesize_approx")
    if size:
        return int(size)
    format_duration = format_info.get("duration") or duration
    bitrate = format_info.get("tbr") or format_info.get("abr")
    if bitrate and format_duration:
        estimated_size = int(float(bitrate) * 1000 / 8 * float(format_duration))
        return int(estimated_size * 1.15)
    return None


def select_ytdlp_format(
    info: Mapping[str, Any], config: Settings, options: DownloadOptions
) -> str:
    raw_formats = info.get("formats") or []
    formats = [item for item in raw_formats if isinstance(item, dict)]
    duration = info.get("duration")

    video = [
        item
        for item in formats
        if item.get("vcodec") not in (None, "none")
        and (
            options.max_height is None
            or int(item.get("height") or 0) <= options.max_height
        )
    ]
    audio = [item for item in formats if item.get("vcodec") in (None, "none")]
    codec_order = ("av01", "avc1", "vp9") if options.bestmini else ("av01", "avc1")
    audio_order = ("opus", "mp4a") if options.bestmini else ("mp4a", "opus")

    def codec_rank(item: Mapping[str, Any], codecs: tuple[str, ...], field: str) -> int:
        codec = str(item.get(field) or "")
        return next(
            (
                index
                for index, preferred in enumerate(codecs)
                if codec.startswith(preferred)
            ),
            len(codecs),
        )

    if options.audio_track:
        matching_audio = [
            item
            for item in audio
            if str(item.get("format_id") or "").lower() == options.audio_track.lower()
        ]
        if not matching_audio:
            available_ids = [
                str(item.get("format_id")) for item in audio if item.get("format_id")
            ]
            raise DownloadError(
                f"Audio track ID '{options.audio_track}' was not found. "
                f"Available track IDs: {', '.join(available_ids[:10])}"
            )
        audio = matching_audio
    elif options.audio_lang:
        req = options.audio_lang.strip().lower()
        matching_audio = [
            item
            for item in audio
            if req == str(item.get("language") or "").lower()
            or str(item.get("language") or "").lower().startswith(req + "-")
            or req in str(item.get("format_note") or "").lower()
            or req in str(item.get("language") or "").lower()
        ]
        if not matching_audio:
            available_langs: list[str] = []
            for item in audio:
                lang = str(item.get("language") or "")
                note = str(item.get("format_note") or "")
                desc = lang
                if note and desc:
                    desc = f"{lang} ({note})"
                elif note:
                    desc = note
                if desc and desc not in available_langs:
                    available_langs.append(desc)
            langs_str = (
                ", ".join(available_langs) if available_langs else "None detected"
            )
            raise DownloadError(
                f"Audio language '{options.audio_lang}' not found at that link. "
                f"Available audio tracks: {langs_str}."
            )
        audio = matching_audio

    def audio_rank(item: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
        note = str(item.get("format_note") or "").lower()
        fid = str(item.get("format_id") or "").lower()
        is_drc = 1 if ("drc" in fid or "drc" in note.split()) else 0
        pref = int(item.get("language_preference") or 0)
        is_default = 1 if ("(default)" in note or "original" in note) else 0
        bitrate = int(item.get("abr") or item.get("tbr") or 0)
        codec = codec_rank(item, audio_order, "acodec")
        return (is_drc, -pref, -is_default, -bitrate, codec)

    audio.sort(key=audio_rank)

    if options.audio_only or (not video and audio):
        selected = next(
            (
                item
                for item in audio
                if (size := _format_size(item, duration)) is not None
                and size <= config.max_file_size
            ),
            None,
        )
        if not selected:
            selected = next(
                (item for item in audio if _format_size(item, duration) is None),
                None,
            )
        if not selected:
            raise DownloadError(
                f"This audio is too large to send within {config.max_file_size_mb} MB."
            )
        return str(selected["format_id"])

    if not video:
        raise DownloadError("No downloadable media was found at that link.")

    video.sort(
        key=lambda item: (
            -int(item.get("height") or 0),
            codec_rank(item, codec_order, "vcodec"),
        )
    )

    custom_audio = bool(options.audio_lang or options.audio_track)
    for video_format in video:
        if video_format.get("acodec") not in (None, "none"):
            if custom_audio:
                continue
            size = _format_size(video_format, duration)
            if size is not None and size <= config.max_file_size:
                return str(video_format["format_id"])
            continue
        video_size = _format_size(video_format, duration)
        if video_size is None:
            continue
        for audio_format in audio:
            audio_size = _format_size(audio_format, duration)
            if (
                audio_size is not None
                and video_size + audio_size <= config.max_file_size
            ):
                return f"{video_format['format_id']}+{audio_format['format_id']}"

    raise DownloadError(
        "This video is too large to send, even at the lowest video quality. "
        "Try `/dl <url> audio` to download only its audio."
    )


class FxTwitterClient:
    def __init__(self, client: httpx.AsyncClient, config: Settings):
        self.client = client
        self.config = config

    async def download(self, url: str, destination: Path) -> DownloadResult:
        match = X_STATUS_RE.search(urlparse(url).path)
        if not match:
            raise DownloadError("That X link is not a status post.")
        status_path = match.group(0).lstrip("/")
        api_url = f"{self.config.fxtwitter_api_url.rstrip('/')}/{status_path}"
        response = await self.client.get(api_url)
        response.raise_for_status()
        data = response.json().get("tweet", {})
        text = str(data.get("text") or "").strip()
        media = data.get("media") or {}

        output: list[DownloadedMedia] = []
        had_video = False

        all_items = media.get("all") or []
        if all_items:
            v_idx = 0
            p_idx = 0
            for item in all_items:
                item_type = str(item.get("type") or "").lower()
                if item_type in ("video", "gif"):
                    had_video = True
                    variants = [
                        v for v in item.get("variants", []) if isinstance(v, dict)
                    ]
                    candidates = sorted(
                        variants or [item],
                        key=lambda v: int(v.get("bitrate") or 0),
                        reverse=True,
                    )
                    filename = (
                        f"video-{v_idx}.mp4" if len(all_items) > 1 else "video.mp4"
                    )
                    v_idx += 1
                    for candidate in candidates:
                        media_url = candidate.get("url") or item.get("url")
                        if not media_url:
                            continue
                        try:
                            downloaded = await stream_to_file(
                                self.client,
                                media_url,
                                destination / filename,
                                self.config,
                            )
                            output.append(downloaded)
                            break
                        except DownloadError as exc:
                            if "too large to send" in str(exc):
                                continue
                            raise
                elif item_type == "photo":
                    media_url = item.get("url")
                    if media_url:
                        filename = f"image-{p_idx}.jpg"
                        p_idx += 1
                        output.append(
                            await stream_to_file(
                                self.client,
                                media_url,
                                destination / filename,
                                self.config,
                            )
                        )
        else:
            videos = media.get("videos") or []
            photos = media.get("photos") or []
            if videos:
                had_video = True
                for v_idx, video in enumerate(videos):
                    variants = [
                        v for v in video.get("variants", []) if isinstance(v, dict)
                    ]
                    candidates = sorted(
                        variants or [video],
                        key=lambda v: int(v.get("bitrate") or 0),
                        reverse=True,
                    )
                    filename = f"video-{v_idx}.mp4" if len(videos) > 1 else "video.mp4"
                    for candidate in candidates:
                        media_url = candidate.get("url") or video.get("url")
                        if not media_url:
                            continue
                        try:
                            downloaded = await stream_to_file(
                                self.client,
                                media_url,
                                destination / filename,
                                self.config,
                            )
                            output.append(downloaded)
                            break
                        except DownloadError as exc:
                            if "too large to send" in str(exc):
                                continue
                            raise
            if photos:
                for p_idx, photo in enumerate(photos):
                    media_url = photo.get("url")
                    if media_url:
                        output.append(
                            await stream_to_file(
                                self.client,
                                media_url,
                                destination / f"image-{p_idx}.jpg",
                                self.config,
                            )
                        )

        if output:
            return DownloadResult(media=output, text=text)
        if had_video:
            raise DownloadError(
                f"This media is too large to send within {self.config.max_file_size_mb} MB. "
                "Try `/dl <url> audio` to download only its audio."
            )
        raise DownloadError("No downloadable media was found in that post.")


class FxBlueskyClient:
    def __init__(self, client: httpx.AsyncClient, config: Settings):
        self.client = client
        self.config = config

    async def download(self, url: str, destination: Path) -> DownloadResult:
        match = BSKY_POST_RE.search(urlparse(url).path)
        if not match:
            raise DownloadError("That Bluesky link is not a post.")
        handle, rkey = match.groups()
        api_url = f"{self.config.fxbsky_api_url.rstrip('/')}/2/status/{handle}/{rkey}"
        response = await self.client.get(api_url)
        response.raise_for_status()
        data = response.json().get("status", {})
        text = str(data.get("text") or "").strip()
        media = data.get("media") or {}
        videos = media.get("videos") or []
        photos = media.get("photos") or []
        output: list[DownloadedMedia] = []
        had_video = False
        if videos:
            had_video = True
            for v_idx, video in enumerate(videos):
                formats = [
                    item
                    for item in video.get("formats", [])
                    if item.get("container") == "mp4"
                ]
                candidates = sorted(
                    formats or [video],
                    key=lambda item: int(item.get("bitrate") or 0),
                    reverse=True,
                )
                filename = f"video-{v_idx}.mp4" if len(videos) > 1 else "video.mp4"
                for candidate in candidates:
                    media_url = candidate.get("url") or video.get("url")
                    if not media_url:
                        continue
                    try:
                        downloaded = await stream_to_file(
                            self.client, media_url, destination / filename, self.config
                        )
                        output.append(downloaded)
                        break
                    except DownloadError as exc:
                        if "too large to send" in str(exc):
                            continue
                        raise
        if photos:
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
            return DownloadResult(media=output, text=text)
        if had_video:
            raise DownloadError(
                f"This media is too large to send within {self.config.max_file_size_mb} MB. "
                "Try `/dl <url> audio` to download only its audio."
            )
        raise DownloadError("No downloadable media was found in that post.")


async def stream_to_file(
    client: httpx.AsyncClient, url: str, path: Path, config: Settings
) -> DownloadedMedia:
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            length = int(response.headers.get("content-length") or 0)
            is_video = path.suffix.lower() == ".mp4"
            suggestion = (
                " Try `/dl <url> audio` to download only its audio." if is_video else ""
            )
            if length > config.max_file_size:
                raise DownloadError(
                    f"This media is too large to send within {config.max_file_size_mb} MB.{suggestion}"
                )
            total = 0
            with path.open("wb") as output:
                async for chunk in response.aiter_bytes(1024 * 256):
                    total += len(chunk)
                    if total > config.max_file_size:
                        path.unlink(missing_ok=True)
                        raise DownloadError(
                            f"This media is too large to send within {config.max_file_size_mb} MB.{suggestion}"
                        )
                    output.write(chunk)
        content_type = response.headers.get(
            "content-type", "application/octet-stream"
        ).split(";", 1)[0]
        return DownloadedMedia(path, content_type)
    except httpx.HTTPError as exc:
        path.unlink(missing_ok=True)
        raise DownloadError("The media could not be downloaded.") from exc
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def download_with_ytdlp(
    url: str,
    destination: Path,
    config: Settings,
    options: DownloadOptions | None = None,
) -> DownloadResult:
    if options is None:
        options = DownloadOptions()

    def run() -> tuple[list[Path], str]:
        ytdlp_options: dict[str, Any] = {
            "outtmpl": str(destination / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "socket_timeout": config.download_timeout_seconds,
            "js_runtimes": {"deno": {}},
            "remote_components": ["ejs:github"],
        }
        temporary_cookie_file: Path | None = None
        if config.cookies_file and config.cookies_file.is_file():
            temporary_cookie_file = destination / ".cookies.txt"
            shutil.copyfile(config.cookies_file, temporary_cookie_file)
            ytdlp_options["cookiefile"] = str(temporary_cookie_file)
        title = ""
        try:
            with yt_dlp.YoutubeDL(cast(Any, ytdlp_options)) as extractor:
                info = extractor.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise DownloadError(
                    "The link is private, unavailable, or could not be extracted."
                )
            format_spec = select_ytdlp_format(info, config, options)
            ytdlp_options["format"] = format_spec
            title = str(info.get("title") or "").strip()

            raw_formats = info.get("formats") or []
            audio_formats = [
                f
                for f in raw_formats
                if isinstance(f, dict)
                and f.get("vcodec") in (None, "none")
                and f.get("acodec") not in (None, "none")
            ]
            distinct_langs = sorted(
                {
                    str(f.get("language"))
                    for f in audio_formats
                    if f.get("language") and str(f.get("language")) != "None"
                }
            )
            if len(distinct_langs) > 1:
                chosen_lang = options.audio_lang or next(
                    (
                        str(f.get("language"))
                        for f in audio_formats
                        if "(default)" in str(f.get("format_note") or "")
                        or int(f.get("language_preference") or 0) > 0
                    ),
                    distinct_langs[0],
                )
                other_langs = [l for l in distinct_langs if l != chosen_lang]
                if other_langs:
                    title += f"\n\n[Audio: {chosen_lang} | Other tracks: {', '.join(other_langs)} (use lang:<code>)]"
                else:
                    title += f"\n\n[Audio: {chosen_lang}]"

            with yt_dlp.YoutubeDL(cast(Any, ytdlp_options)) as downloader:
                downloader.download([url])
        except (YtDlpDownloadError, OSError) as exc:
            raise DownloadError(
                "The link is private, unavailable, or could not be extracted."
            ) from exc
        finally:
            if temporary_cookie_file:
                temporary_cookie_file.unlink(missing_ok=True)
        return (
            [
                path
                for path in destination.iterdir()
                if path.is_file() and path.name != ".cookies.txt"
            ],
            title,
        )

    try:
        paths, title = await asyncio.wait_for(
            asyncio.to_thread(run), config.download_timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        raise DownloadError("The download timed out.") from exc
    if not paths:
        raise DownloadError("No downloadable media was found at that link.")
    for path in paths:
        if path.stat().st_size > config.max_file_size:
            is_audio = options.audio_only or path.suffix.lower() in AUDIO_CONTENT_TYPES
            if is_audio:
                raise DownloadError(
                    f"This audio is too large to send within {config.max_file_size_mb} MB."
                )
            raise DownloadError(
                f"This media is too large to send within {config.max_file_size_mb} MB. "
                "Try `/dl <url> audio` to download only its audio."
            )
    return DownloadResult(
        media=[
            DownloadedMedia(
                path,
                "video/mp4"
                if path.suffix.lower() == ".mp4"
                else AUDIO_CONTENT_TYPES.get(
                    path.suffix.lower(), "application/octet-stream"
                ),
            )
            for path in paths
        ],
        text=title,
    )


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


async def extract_media_info(url: str, config: Settings) -> str:
    def run() -> str:
        temp_dir = Path(tempfile.mkdtemp(prefix="ytdlp-info-"))
        ytdlp_options: dict[str, Any] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": config.download_timeout_seconds,
            "js_runtimes": {"deno": {}},
            "remote_components": ["ejs:github"],
        }
        if config.cookies_file and config.cookies_file.is_file():
            temporary_cookie_file = temp_dir / ".cookies.txt"
            shutil.copyfile(config.cookies_file, temporary_cookie_file)
            ytdlp_options["cookiefile"] = str(temporary_cookie_file)
        try:
            with yt_dlp.YoutubeDL(cast(Any, ytdlp_options)) as extractor:
                info = extractor.extract_info(url, download=False)
        except (YtDlpDownloadError, OSError) as exc:
            raise DownloadError(
                "The link is private, unavailable, or could not be extracted."
            ) from exc
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        if not isinstance(info, dict):
            raise DownloadError(
                "The link is private, unavailable, or could not be extracted."
            )

        title = str(info.get("title") or "Media").strip()
        raw_formats = info.get("formats") or []
        formats = [f for f in raw_formats if isinstance(f, dict)]

        heights = sorted(
            {
                int(f["height"])
                for f in formats
                if f.get("vcodec") not in (None, "none") and f.get("height")
            },
            reverse=True,
        )

        audio_formats = [
            f
            for f in formats
            if f.get("vcodec") in (None, "none")
            and f.get("acodec") not in (None, "none")
        ]
        tracks: dict[str, str] = {}
        for af in audio_formats:
            lang = str(af.get("language") or "")
            note = str(af.get("format_note") or "")
            fid = str(af.get("format_id") or "")
            if not lang and not note:
                continue
            key = lang or fid
            if key not in tracks:
                desc = note if note else f"ID {fid}"
                tracks[key] = desc

        lines = [f"🎬 {title}"]
        if heights:
            lines.append(f"\nResolutions: {', '.join(f'{h}p' for h in heights)}")
        if tracks:
            lines.append("\nAvailable audio tracks:")
            for key, desc in tracks.items():
                lines.append(f"• {key}: {desc}")
            first_lang = next(iter(tracks.keys()))
            lines.append("\nTo download with specific audio:")
            lines.append(f"/dl <url> lang:{first_lang}")
            lines.append(f"/dl <url> audio lang:{first_lang}")
        else:
            lines.append("\nNo separate audio tracks detected.")
        return "\n".join(lines)

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(run), config.download_timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        raise DownloadError("Media inspection timed out.") from exc


async def process_message(
    message: IncomingMessage, config: Settings, client: httpx.AsyncClient
) -> None:
    urls = message_urls(message, config)
    if not urls:
        return
    signal = SignalClient(client, config)
    fx = FxTwitterClient(client, config)
    bsky = FxBlueskyClient(client, config)
    opts = download_options(message)
    for url in urls:
        if opts.info_only:
            try:
                if is_x_url(url) or is_bsky_url(url):
                    await signal.send(
                        "Info command is only supported for video platforms like YouTube.",
                        message,
                    )
                else:
                    info_text = await extract_media_info(url, config)
                    await signal.send(info_text, message)
            except DownloadError as exc:
                log.info("Info extraction failed for %s: %s", url, exc)
                await send_error(signal, str(exc), message)
            except (httpx.HTTPError, OSError):
                log.exception("Info extraction failed for %s", url)
                await send_error(
                    signal, "I could not inspect that media right now.", message
                )
            continue

        workdir = Path(
            tempfile.mkdtemp(prefix="signal-media-", dir=config.shared_media_dir)
        )
        try:
            await signal.send("Downloading media...", message)
            if is_x_url(url):
                result = await fx.download(url, workdir)
            elif is_bsky_url(url):
                result = await bsky.download(url, workdir)
            else:
                result = await download_with_ytdlp(url, workdir, config, opts)
            caption = format_caption(result.text, url)
            await signal.send(caption, message, result.media)
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
