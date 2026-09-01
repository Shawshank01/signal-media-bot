# Signal Media Bot

Download media from links in Signal DMs and groups, then send the media back as Signal attachments.

> [!CAUTION]
> **Privacy & Cloud Hosting Risks**:
>
> - **End-to-End Encryption & Group Decryption**: As an active member of a group or linked device, the bot receives and decrypts all messages in the conversation in memory (RAM).
> - **VPS & Cloud Provider Access**: Running this on a commercial VPS or cloud provider grants the host hypervisor-level access. The provider could theoretically inspect decrypted runtime memory, container logs, and stored cryptographic keys in the `signal_cli_data` volume.
<!---->
> [!NOTE]
> **Recommendations**:
>
> - **Use a Dedicated Number**: Use a dedicated bot phone number instead of linking your personal primary Signal account. Only add this bot as a contact or add it to a group chat where you and your friends never send private messages.
> - **Self-Host at Home**: For absolute privacy, host on local hardware (e.g., Raspberry Pi or home server) where you own the physical machine. Since all traffic is outbound, no router ports need to be exposed.

## Setup

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
```

`BOT_PHONE_NUMBER` is required. Set `BOT_UUID` to enable native Signal `@` mentions ([see how to get it](#obtaining-the-bots-uuid-recommended)). The visible Signal account name can change without affecting native mentions. Change `SIGNAL_API_PORT` or `BOT_PORT` if the default host ports are already in use.

## Signal Account

Use either a new Signal number or link an existing Signal account.

### New Account

Start the Signal API:

```sh
docker compose up -d signal-api
```

Use the registration endpoints from the `signal-cli-rest-api` documentation to submit the captcha, request the SMS code, and verify the code. The gateway does not provide FastAPI's `/openapi.json` endpoint.

### Link Existing Account

Start the Signal API:

```sh
docker compose up -d signal-api
```

Request a pairing QR code on the VPS:

```sh
docker compose run --rm signal-bot sh -c \
 'curl -fsS "http://signal-api:8080/v1/qrcodelink/raw?device_name=signal-media-bot" | python -c "import json, sys; print(json.load(sys.stdin)[\"device_link_uri\"] , end=\"\")" | qrencode -t UTF8 -l H -m 4'
```

Scan the QR code shown in the terminal with Signal under **Settings > Linked devices > Link new device**. Each request creates a new one-time QR code, so scan the latest code without rerunning the command. If the raw endpoint is unavailable, save the normal response and inspect it:

```sh
curl -fsS -o pairing.html "http://127.0.0.1:18080/v1/qrcodelink?device_name=signal-media-bot"
file pairing.html
head -c 500 pairing.html
```

### Obtaining the Bot's UUID (Recommended)

Signal uses UUIDs (ACI) to identify accounts. Set `BOT_UUID` in `.env` to enable native Signal `@` mentions because:

- **Phone Number Privacy**: If the bot account hides its phone number under Signal privacy settings ("Who can see my number: Nobody"), outgoing events and identity envelopes will report the bot via its UUID (`sourceUuid`) with the phone number omitted.
- **Native Group Mentions**: When group members select the bot from Signal's native `@` popup list, Signal generates a structured mention payload containing only the bot's UUID.
- **Self-Message Filtering**: Prevents loopback processing by accurately filtering out messages sent by the bot or its linked primary device.

Confirm that the linked account is registered:

```sh
curl -s http://127.0.0.1:18080/v1/accounts
```

The response should contain the bot's phone number. To query identities, first load the number from `.env` because values in `.env` are not automatically shell variables:

```sh
BOT_PHONE_NUMBER=$(sed -n 's/^BOT_PHONE_NUMBER=//p' .env)
curl -s "http://127.0.0.1:18080/v1/identities/${BOT_PHONE_NUMBER}"
```

Set `BOT_UUID` to the UUID from the identity record whose `number` matches `BOT_PHONE_NUMBER`:

```dotenv
BOT_UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

## Start the Bot

```sh
docker compose up -d --pull always signal-bot
```

Check the bot:

```sh
curl http://127.0.0.1:18000/health
```

Expected health response:

```json
{"status":"ok"}
```

Check logs:

```sh
docker compose logs -f signal-api signal-bot
```

## Using the Bot

In a DM, send any supported media URL.

In a group, use a command or mention:

```text
/dl https://example.com/video
!dl https://x.com/user/status/123
@<the bot's Signal account name> https://www.youtube.com/watch?v=...
```

## Update

```sh
docker compose pull signal-bot
docker compose up -d signal-bot
```

## Credits

- **[signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)** – Dockerized REST/JSON-RPC API gateway for Signal messaging and webhook routing.
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** – Media extraction engine supporting YouTube, TikTok, Instagram, Reddit, and hundreds of other platforms.
- **[FxEmbed](https://github.com/FxEmbed/FxEmbed)** – API engine for Twitter/X media extraction and CDN stream resolution.
- **[FFmpeg](https://ffmpeg.org/)** – Multimedia framework for audio/video stream multiplexing.
