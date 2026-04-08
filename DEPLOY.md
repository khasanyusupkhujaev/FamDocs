# Running FamDoc on a server (24/7)

Your app is **one long-lived process**: Telegram long-polling plus a Mini App HTTP server on port **8080**. Closing the laptop stops that process, so you need a machine that stays on.

**Why not Render’s _free_ web tier for this bot?** Free instances **spin down** after idle time, and they **cannot use a persistent disk**. That breaks long‑running Telegram polling and **wipes SQLite** on restarts. For FamDoc on Render, use a **paid instance** (`starter` or higher) **plus** a **persistent disk** mounted at `/data` (see `render.yaml`).

**Why not Fly.io as “free”?** New Fly organizations are **pay-as-you-go** (credit card required). Small always-on machines are typically a few dollars per month, not $0.

**Oracle Cloud “Always Free”** remains a realistic **$0** VPS option. **Docker** is the same image everywhere.

---

## 1. Oracle Cloud Infrastructure (recommended $0 tier)

1. Create an account on [Oracle Cloud](https://www.oracle.com/cloud/free/) and provision an **Ampere A1** (ARM) instance (Always Free eligible: up to 4 OCPUs / 24 GB RAM total across shapes you choose). Ubuntu 22.04+ is fine.
2. Open inbound **TCP 22** (SSH). For HTTPS you will use **443** (and **80** for ACME) if you terminate TLS on the VM, or no inbound ports if you use **Cloudflare Tunnel**.
3. On the instance, install Docker ([Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)) and Docker Compose plugin.
4. Copy the project (or clone your repo) onto the server. Create `.env` on the server from `.env.example` — **set `WEBAPP_PUBLIC_URL` to your final HTTPS URL** (same origin PayTech webhooks use).
5. From the project directory:

   ```bash
   docker compose up -d --build
   ```

   SQLite and any **local** blob cache live in the Docker volume `famdoc_data` mounted at `/data` inside the container (`FAMDOC_DATA_DIR`).

6. **HTTPS (pick one):**
   - **Own domain + Caddy**: Point DNS A/AAAA to the VM’s public IP. Install [Caddy](https://caddyserver.com/) on the host, use `deploy/Caddyfile.example` as a template, and proxy to `127.0.0.1:8080` (already bound by `docker-compose.yml`).
   - **No open ports / no domain on Oracle**: Run [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) (`cloudflared`) on the VM and map a hostname to `http://127.0.0.1:8080`. Use the tunnel’s **https** URL as `WEBAPP_PUBLIC_URL` and restart the stack.

7. **Telegram / PayTech:** Update `WEBAPP_PUBLIC_URL` to the public HTTPS base URL. Restart: `docker compose up -d`. In PayTech / Payme / Click dashboards, webhook URLs must be `https://<that-host>/payments/payme/webhook` and `.../click/webhook` as in `.env.example`.

8. **Backups:** Find the volume name with `docker volume ls` (often `<project>_famdoc_data`). Then e.g. `docker run --rm -v <that_volume_name>:/data -v $(pwd):/backup alpine tar czf /backup/famdoc-data.tgz -C /data .`, or snapshot the VM disk in Oracle.

---

## 2. Render

1. Push this repo to GitHub (or GitLab/Bitbucket) and sign up at [Render](https://render.com).
2. In the Render dashboard, choose **New → Blueprint** and connect the repo, or place `render.yaml` at the repo root and apply the blueprint.
3. When prompted, set **environment variables** (minimum `TELEGRAM_BOT_TOKEN` and `WEBAPP_PUBLIC_URL`). Copy the rest from your local `.env` (S3/R2, `FAMDOC_FILE_MASTER_KEY`, PayTech keys, etc.).
4. After the first deploy, Render gives you a URL like `https://famdoc.onrender.com`. Set **`WEBAPP_PUBLIC_URL`** to that exact HTTPS origin (no trailing slash required) and **redeploy** so the Mini App menu and webhooks match your public host.
5. The blueprint attaches a **persistent disk** at **`/data`**, which matches **`FAMDOC_DATA_DIR`** in the Docker image. SQLite and local file blobs (if `FAMDOC_STORAGE=local`) live there.
6. **Cost:** The **free** tier is **not** suitable for this app (see above). **`starter` + disk** is billed per Render’s [pricing](https://render.com/pricing); check current rates before enabling.

**Manual setup (no Blueprint):** New **Web Service** → connect repo → **Docker** → root directory `.` → add the same env vars → **Advanced → Add disk** → mount path **`/data`**, size as needed → create. Render injects **`PORT`**; **`deploy/entrypoint.sh`** maps it to **`WEBAPP_PORT`** so uvicorn listens correctly.

---

## 3. Moving data from your laptop

If you used local SQLite under `./data/famdoc.db`:

1. Stop the bot locally.
2. Copy `data/` (or at least `famdoc.db` and `data/files/` if `FAMDOC_STORAGE=local`) to the server.
3. For Docker volume restore, copy into the volume or bind-mount `./data:/data` **once** for migration, then switch back to a named volume if you prefer.

If blobs are already on **R2/S3** (`FAMDOC_STORAGE=s3`), you mainly need **`famdoc.db`** and the same env keys.

---

## 4. Other hosts

The same **`Dockerfile`** and **`docker-compose.yml`** work on **Hetzner**, **DigitalOcean**, **Linode**, a home server, etc. You always need:

- **Always-on** process (no sleep).
- **HTTPS** URL for the Mini App and PayTech webhooks.
- **Persistent disk** for SQLite (Docker volume or bind mount).

---

## 5. Build / run without Compose

```bash
docker build -t famdoc .
docker run -d --restart unless-stopped \
  --env-file .env \
  -e FAMDOC_DATA_DIR=/data \
  -v famdoc_data:/data \
  -p 127.0.0.1:8080:8080 \
  famdoc
```

---

## 6. Secrets

Never commit `.env`. If `.env.example` ever contained real tokens, **rotate** them in Telegram, PayTech, R2, etc.
