# HireBridge Africa — 100% Free Deployment Guide

This guide gets your app live on the internet for **£0 forever** using:

| Layer | Provider | Free limit |
|---|---|---|
| App hosting | **Render** | 750 hrs/month (enough for 1 service) |
| Database | **Neon** | 0.5 GB PostgreSQL, no expiry |
| File storage | Local `instance/uploads/` | Included |
| SSL certificate | Render automatic | Included |
| Custom domain | Render | Included |

---

## Step 1 — Set up a free PostgreSQL database (Neon)

Render's own free Postgres expires after 90 days. Neon is free forever.

1. Go to **[neon.tech](https://neon.tech)** → Sign up (GitHub login works)
2. Create a new project → name it `hirebridgeafrica`
3. Copy the **Connection string** — it looks like:
   ```
   postgresql://user:password@ep-xyz.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
4. Save it — you'll need it in Step 3.

---

## Step 1.5 — Set up free permanent CV storage (Cloudinary)

Render's free tier wipes any files saved to disk every time you redeploy. To
keep uploaded CVs permanently, this app supports Cloudinary out of the box —
you just need to create a free account and set one environment variable.

1. Go to **[cloudinary.com](https://cloudinary.com)** → Sign up free (no credit card, 25 GB free forever)
2. On your Cloudinary dashboard, find the box labeled **"API Environment variable"** — it looks like:
   ```
   CLOUDINARY_URL=cloudinary://123456789012345:AbCdEfGhIjKlMnOpQrStUvWxYz@your_cloud_name
   ```
3. Copy the whole line — you'll paste it into Render's environment variables in Step 3.

That's it. The app automatically detects `CLOUDINARY_URL` and switches to permanent storage — no code changes needed. If you skip this step, the app still works, but uploaded CVs will disappear on your next `git push`.

**How it stays secure:** CVs are uploaded to Cloudinary as "authenticated" files, meaning they are NOT publicly accessible by URL. When an applicant or admin requests a CV through the app, the server checks that they're allowed to see it, then generates a temporary signed link that expires after 60 seconds. Nobody can guess or share a working CV link.

---

## Step 2 — Push your code to GitHub

```bash
# One-time setup
git init
git add .
git commit -m "Initial commit — HireBridge Africa"

# Create a repo on github.com then:
git remote add origin https://github.com/YOUR_USERNAME/hirebridgeafrica.git
git push -u origin main
```

**Important:** verify `.env` is in `.gitignore` before pushing:
```bash
git status   # .env should NOT appear in the list
```

---

## Step 3 — Deploy on Render

1. Go to **[dashboard.render.com](https://dashboard.render.com)** → **New → Web Service**
2. Connect your GitHub account → select your `hirebridgeafrica` repo
3. Render detects `render.yaml` automatically — review the settings
4. Click **"Advanced"** → **"Add Environment Variable"** and set:

   | Key | Value |
   |---|---|
   | `SECRET_KEY` | Click "Generate" — Render creates a strong random key |
   | `DATABASE_URL` | Paste the Neon connection string from Step 1 |
   | `ADMIN_EMAILS` | Your email address |
   | `MAIL_USERNAME` | Your Gmail address |
   | `MAIL_PASSWORD` | Your [Gmail App Password](https://support.google.com/accounts/answer/185833) |
   | `FLASK_ENV` | `production` |
   | `CLOUDINARY_URL` | The line you copied from Cloudinary in Step 1.5 (optional but recommended) |

5. Click **"Create Web Service"**

Render will:
- Install dependencies (`pip install -r requirements.txt`)
- Run database migrations (`flask db upgrade`)
- Start the app with gunicorn

First deploy takes ~3 minutes. After that, every `git push` triggers an automatic redeploy.

---

## Step 4 — First-run setup

Once deployed, open the Render dashboard → **Shell** tab and run:

```bash
flask create_admin --email YOUR_EMAIL@example.com
```

This prints a randomly generated admin password. Save it — it won't be shown again.

Then visit `https://your-app.onrender.com/admin` and log in.

---

## Understanding the free-tier limits

### Render free tier
- **Spins down after 15 minutes of inactivity.** The first visitor after inactivity waits 20–60 seconds for the app to wake up. All subsequent requests are fast.
- **750 free hours/month** = enough for one always-on service.
- **512 MB RAM** — the app is tuned to stay well under this.

### Keeping the app "warm" (optional)
If the cold-start delay bothers you, use a free uptime monitor to ping the app every 10 minutes:

- **[UptimeRobot](https://uptimerobot.com)** — free, monitors every 5 minutes
- **[cron-job.org](https://cron-job.org)** — free cron jobs, can hit a health endpoint

Add a health endpoint to your app (already included at `/api/jobs`).

### Neon free tier
- 0.5 GB storage
- Scales to zero when idle (similar to Render — first query after idle is ~1s slower)
- No expiry — free forever

---

## Alternative: Koyeb (faster free tier)

Koyeb's free tier sleeps less aggressively than Render.

1. Go to **[koyeb.com](https://koyeb.com)** → Sign up
2. New App → GitHub → select your repo
3. Set:
   - **Run command:** `gunicorn app:app --config gunicorn.conf.py`
   - **Port:** `10000`
4. Add the same environment variables as in Step 3
5. Deploy

Koyeb has slightly more CPU per free instance than Render, which helps if your job scraper is active.

---

## Performance on the free tier

These optimisations are already applied in this build:

| Optimisation | Saving |
|---|---|
| Gzip compression (Flask-Compress) | ~70% smaller HTML/JSON responses |
| WhiteNoise static file serving | Static files bypass Flask entirely |
| 1-year browser cache for static assets | Repeat visitors load instantly |
| Images compressed + WebP versions | Images 70–97% smaller |
| ATS scraper batch rotation (150/cycle) | 76% fewer outbound requests per cycle |
| Background scraper every 4h | Half the CPU of the old 2h cycle |
| Per-request timeout 8s | Workers freed faster on slow sources |
| `pool_pre_ping=True` | No "lost connection" errors on Neon/Supabase |

### Expected performance on free tier
- Page loads: **200–400ms** (warm), 20–60s (cold start after idle)
- Concurrent users: **8–16** without degradation
- Monthly capacity: **500–2,000 unique visitors** comfortably

---

## Upgrading later (when you outgrow free)

When you need more capacity, upgrading is simple:

| What you need | Option | Cost |
|---|---|---|
| No cold starts | Render Starter | $7/month |
| More DB storage | Neon Pro | $19/month |
| Global CDN | Cloudflare (free tier) | $0 |
| More workers | Set `WEB_CONCURRENCY=2` | Free (if RAM allows) |

---

## Troubleshooting

**"Application error" on first visit**
- Check Render logs: Dashboard → your service → Logs
- Most common cause: missing environment variable

**Database connection errors**
- Confirm `DATABASE_URL` starts with `postgresql://` not `postgres://`
- The app fixes this automatically, but double-check your Neon connection string

**Admin password forgotten**
- Render Shell: `flask create_admin --email your@email.com --password newpassword`

**Job board is empty**
- Log in as admin → `/admin` → click "Auto-Fetch Jobs"
- Takes 30–60 seconds on first run

**Scraper taking too long**
- Set `SCRAPER_FETCH_BUDGET_SECONDS=30` in Render environment variables
- Set `SCRAPER_ATS_BATCH_SIZE=100` to scan fewer companies per cycle

**Build fails on `Pillow` with `KeyError: '__version__'` (or similar wheel-build error)**
- This means Render used a newer Python version than this app was tested on.
- Render no longer reads `runtime.txt` for version pinning. This repo now ships a
  `.python-version` file (and a `PYTHON_VERSION` env var in `render.yaml`) set to
  `3.11.9` — confirm both are present and that `PYTHON_VERSION=3.11.9` is set in
  your Render service's environment variables, then trigger "Manual Deploy" →
  "Clear build cache & deploy".

---

## Architecture diagram

```
 User browser
     │
     ▼
 Render (gunicorn + Flask)
     │  WhiteNoise serves static files directly
     │  Flask-Compress gzips dynamic responses
     │
     ├──► Neon PostgreSQL (jobs, users, bookings)
     │
     ├──► Cloudinary (resumes — permanent, if CLOUDINARY_URL is set)
     │       Falls back to instance/uploads/ on Render's local disk if
     │       CLOUDINARY_URL isn't configured (lost on every redeploy).
     │
     └──► External job APIs (RemoteOK, Jobicy, ATS boards…)
              Background thread, every 4 hours
```

### Resume storage — already handled
This build already supports Cloudinary for permanent CV storage (see Step 1.5 above). If you set `CLOUDINARY_URL`, uploaded resumes survive redeploys automatically — no extra code needed. If you skip it, resumes still work, they're just stored on Render's local disk and will be lost on the next `git push`.
