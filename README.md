# Signal Media Bot

Download media links in Signal DMs and groups, then send the media back as Signal attachments.

## VPS Setup

Install Docker and Docker Compose, then download the two required files:

```sh
mkdir signal-media-bot && cd signal-media-bot
curl -fsSLO https://raw.githubusercontent.com/Shawshank01/signal-media-bot/main/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/Shawshank01/signal-media-bot/main/.env.example
cp .env.example .env
```

Edit `.env`:

```dotenv
BOT_PHONE_NUMBER=+
BOT_UUID=
SIGNAL_MEDIA_IMAGE=michifumi/signal-media-bot:latest
BOT_MENTION_NAMES=bot,signalbot
```

`BOT_PHONE_NUMBER` is required. Setting `BOT_UUID` is optional but strongly recommended ([see how to get it](#obtaining-the-bots-uuid-recommended)). Change `SIGNAL_API_PORT` or `BOT_PORT` if the default host ports are already in use.

The host ports are bound to `127.0.0.1` and are not publicly accessible. The containers communicate over Docker's internal network.

## Signal Account

Use either a new Signal number or link an existing Signal account.

### New Account

Start the Signal API:

```sh
docker compose up -d signal-api
```

View the available API endpoints:

```sh
curl -s http://127.0.0.1:18080/openapi.json | less
```

Use the registration endpoints shown in that output to submit the captcha, request the SMS code, and verify the code.

### Link Existing Account

Start the Signal API:

```sh
docker compose up -d signal-api
```

Request a pairing QR code on the VPS:

```sh
curl -o pairing.html "http://127.0.0.1:18080/v1/qrcodelink?device_name=signal-media-bot"
```

Scan the QR code in Signal under **Settings > Linked devices > Link new device**. If the endpoint returns JSON or an image instead of HTML, follow the response format shown by the API.

### Obtaining the Bot's UUID (Recommended)

Signal uses UUIDs (ACI) to identify accounts. Setting `BOT_UUID` in `.env` is **strongly recommended** because:

- **Phone Number Privacy**: If the bot account hides its phone number under Signal privacy settings ("Who can see my number: Nobody"), outgoing events and identity envelopes will report the bot via its UUID (`sourceUuid`) with the phone number omitted.
- **Native Group Mentions**: When group members select the bot from Signal's native `@` popup list, Signal generates a structured mention payload containing only the bot's UUID.
- **Self-Message Filtering**: Prevents loopback processing by accurately filtering out messages sent by the bot or its linked primary device.

Once `signal-api` is registered or linked, retrieve your account's UUID by running:

```sh
curl -s http://127.0.0.1:18080/v1/accounts
```

*(Alternatively: `curl -s http://127.0.0.1:18080/v1/identities/$BOT_PHONE_NUMBER`)*

Copy the `uuid` field from the output and update your `.env` file:

```dotenv
BOT_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

## Start the Bot

```sh
docker compose pull signal-bot signal-api
docker compose up -d
```

Check the bot:

```sh
curl http://127.0.0.1:18000/health
docker compose ps
docker compose logs -f signal-api signal-bot
```

Expected health response:

```json
{"status":"ok"}
```

## Using the Bot

In a DM, send any supported media URL.

In a group, use a command or mention:

```text
/dl https://example.com/video
!dl https://x.com/user/status/123
@signalbot https://www.youtube.com/watch?v=...
```

## Update

```sh
docker compose pull signal-bot
docker compose up -d signal-bot
```

## Credits

- **[signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)** – Dockerized REST/JSON-RPC API gateway for Signal messaging and webhook routing.
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** – Media extraction engine supporting YouTube, TikTok, Instagram, Reddit, and hundreds of other platforms.
- **[FxTwitter / FixTweet](https://github.com/FixTweet/FxTwitter)** – API engine for Twitter/X media extraction and CDN stream resolution.
- **[FFmpeg](https://ffmpeg.org/)** – Multimedia framework for audio/video stream multiplexing.
