# HireBridge Africa — Setup Guide

## First-Time Setup (3 Steps)

### Step 1 — Install
```bash
pip install -r requirements.txt
```

### Step 2 — Configure
```bash
cp .env.example .env
```
Open `.env` and set at minimum:
- `SECRET_KEY` — run `python -c "import secrets; print(secrets.token_hex(32))"` to generate one
- `ADMIN_EMAILS` — your email address
- `MAIL_USERNAME` / `MAIL_PASSWORD` — your Gmail + App Password (optional)

### Step 3 — Initialise and Run
```bash
python setup.py   # creates DB and admin account
flask run
```

---

## Admin Access

- URL: `http://localhost:5000/admin`
- Email: whatever you set in `ADMIN_EMAILS` inside `.env`
- Password: randomly generated and printed once by `python setup.py` (or `flask create_admin`) — copy it from the terminal output immediately, it is not stored in plaintext anywhere
- Forgot it later? Use `flask create_admin --email you@example.com --password <new-password>` to reset it, or the "Forgot password?" link on the sign-in page (requires `MAIL_*` settings configured)

---

## Populating Jobs

After logging in as admin:
1. Go to `/admin`
2. Click **Auto-Fetch Jobs**
3. Wait ~30 seconds — jobs import in the background
4. Refresh `/jobs` to see results

The background fetcher also runs automatically every 2 hours while the app is running.

---

## API Keys (optional — adds more African jobs)

Add these to your `.env` file. See `.env.example` for step-by-step instructions on getting each one.

| Key | Source | Free quota |
|-----|--------|-----------|
| `JSEARCH_API_KEY` | Google Jobs (RapidAPI) | 200/month |
| `SCALESERP_API_KEY` | Google Jobs (ScaleSerp) | 100/month |
| `GOOGLE_ALERTS_RSS` | Google Alerts RSS feeds | unlimited |
| `JOOBLE_API_KEY` | Jooble aggregator | varies |
| `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | Adzuna (ZA + NG) | 100/month |
| `REED_API_KEY` | Reed UK | unlimited |
| `FINDWORK_API_KEY` | Findwork | 500/month |

---

## Troubleshooting

**No jobs showing?**
Click Auto-Fetch Jobs in the admin panel. If still empty after 60 seconds, run:
```bash
python -c "from app import app; from app import _do_fetch_jobs; app.app_context().push(); print(_do_fetch_jobs(), 'jobs imported')"
```

**Can't log in?**
```bash
python -c "
from app import app, db
from models import User
with app.app_context():
    u = User.query.filter_by(email='YOUR_EMAIL').first()
    if u: u.set_password('NewPassword123!'); db.session.commit(); print('Password reset')
    else: print('User not found')
"
```

**Database locked?**
Stop Flask (Ctrl+C) before running Python scripts.

**Email not sending?**
Generate a Gmail App Password: Google Account → Security → 2-Step Verification → App Passwords.
Set it as `MAIL_PASSWORD` in `.env`.
