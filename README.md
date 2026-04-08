# FamDoc

Telegram bot and **Telegram Mini App** for sharing and organizing family documents in vaults. One process runs **aiogram** (long polling) and **FastAPI** (Mini App UI, APIs, payment webhooks) together.

## Features

- Vaults, documents, previews, and family invites via Telegram and the web Mini App
- Optional **S3-compatible** storage (e.g. Cloudflare R2) with optional **AES-256-GCM** encryption for blobs
- **SQLite** metadata and entitlements (`FAMDOC_DATA_DIR`, default `./data`)
- Billing: **Telegram Stars** / provider token, or **PayTech.uz** (Payme / Click) with HTTPS webhooks

## Requirements

- **Python 3.12+** (3.12 matches the production `Dockerfile`)
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)

## Local setup

```bash
cd FamDoc
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`: set at least `TELEGRAM_BOT_TOKEN`. For the Mini App over the public internet, set `WEBAPP_PUBLIC_URL` to an **HTTPS** base URL (tunnel such as ngrok or Cloudflare, or your deployed host). See comments in [`.env.example`](.env.example).

## Run

```bash
python -m bot.main
```

The HTTP server listens on `WEBAPP_HOST` / `WEBAPP_PORT` (defaults `0.0.0.0:8080`). Telegram updates use **polling** (not a Telegram webhook URL).

## Project layout

| Path | Role |
| --- | --- |
| `bot/` | Bot handlers, DB, PayTech integration, FastAPI app factory |
| `webapp/` | Mini App static assets (HTML/JS/CSS) served by FastAPI |
| `data/` | Default SQLite DB and local blob folder when using `FAMDOC_STORAGE=local` |
| `deploy/` | Docker entrypoint, example Caddy config |
| `Dockerfile`, `docker-compose.yml`, `render.yaml` | Container and hosting blueprints |

## Production

FamDoc must stay **always on** (polling + HTTPS for Mini App and PayTech). See **[DEPLOY.md](DEPLOY.md)** for:

- **Docker Compose** on a VPS (e.g. Oracle Cloud Always Free)
- **Render** (`render.yaml`, paid instance + persistent disk recommended)

## Security

- Never commit real `.env` values or push tokens to a public repo.
- Rotate any secret that has been exposed.
