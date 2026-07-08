# HireBridge Africa

Pan-African career services platform — Flask + SQLAlchemy + 1,200+ source job scraper.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — set SECRET_KEY, your email, and any API keys you have

# 3. Initialise database + create admin account
python setup.py

# 4. Run
flask run
```

Open http://localhost:5000 — admin panel at http://localhost:5000/admin

---

## Job Scraper Sources (1,200+ boards)

### Always-on (no key needed)
| Source | Coverage |
|--------|----------|
| RemoteOK, Remotive, Arbeitnow, Himalayas, Jobicy, WorkingNomads | Global remote |
| WeWorkRemotely RSS (7 category feeds) | Global remote |
| Python.org, WordPress Jobs, Jobspresso, AuthenticJobs | Global tech |
| **Indeed RSS** — 18 African cities | Kenya, Nigeria, South Africa, Ghana, Ethiopia, Uganda, Tanzania, Rwanda, Egypt, Morocco |
| **LinkedIn public search** — 9 African cities | Pan-Africa |
| **UNJobs RSS** — per-country feeds | Kenya, Nigeria, Ethiopia, Uganda, Tanzania, Rwanda, Ghana, South Africa |
| **Devex, ReliefWeb, UNDP, IRC, Mercy Corps, Save the Children, FHI360** | Pan-Africa NGO/Development |
| **Greenhouse ATS** — 359 African company boards | Pan-Africa |
| **Lever ATS** — 82 African company boards | Pan-Africa |
| **Workable ATS** — 849 African company boards | Pan-Africa |
| **WP Job Manager** — 60+ African boards | Kenya, Nigeria, Ghana, Ethiopia, Uganda, Tanzania, Rwanda, South Africa |
| **Kenya Corporate careers** — 25 major employers | Safaricom, Equity Bank, KCB, Corporate Staffing, Summit Recruitment, Brites Management, etc. |
| **Government boards** | Kenya PSC, Nigeria FSC, South Africa DPSA, Rwanda MIFOTRA, Ghana PSC |
| JobwebKenya, DisruptAfrica, TechCabal, Technext | East Africa / Nigeria tech |
| BrighterMonday KE, Fuzu KE, Jobberman NG | East & West Africa |
| Sitemap scrapers — Jobberman, BrighterMonday, NairobiJobs, SAJobs | Africa |

### Activated by API key (all free tiers)
| Key in .env | Source | Free quota |
|-------------|--------|-----------|
| `JSEARCH_API_KEY` | Google Jobs via RapidAPI | 200/month (6/day enforced) |
| `SCALESERP_API_KEY` | Google Jobs via ScaleSerp | 100/month (3/day enforced) |
| `JOOBLE_API_KEY` | Jooble aggregator | varies |
| `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | Adzuna South Africa + Nigeria | 100/month |
| `REED_API_KEY` | Reed UK (Africa-open roles) | unlimited |
| `FINDWORK_API_KEY` | Findwork — searches Nairobi, Lagos, Johannesburg, Accra, Remote | 500/month |
| `GOOGLE_ALERTS_RSS` | Google Alerts RSS for African cities | unlimited |

---

## Environment Variables

See `.env.example` for full documentation of every variable including step-by-step instructions for obtaining each API key.

---

## Admin Panel

- **URL**: `/admin`
- **Auto-Fetch**: Triggers background scrape of all 1,200+ sources
- **Background fetch**: Runs automatically every 2 hours after startup
- **Manual jobs**: Add/edit/delete individual job postings
- **Applications**: View and manage all job applications

---

## Deployment Checklist

- [ ] Set a strong `SECRET_KEY` in `.env` (the app now **refuses to start** with `FLASK_ENV=production` if this is still a placeholder)
- [ ] Run `flask create_admin --email you@example.com` to set the admin login — first run otherwise generates and prints a random admin password once (it is never stored in plaintext, so save it immediately)
- [ ] Set `FLASK_ENV=production` in `.env`
- [ ] Switch `DATABASE_URL` to PostgreSQL for production (SQLite is fine for dev, but the background job-fetch thread and admin writes can contend under concurrent load)
- [ ] Set up HTTPS (nginx + certbot recommended)
- [ ] Configure email (`MAIL_PASSWORD`) — now also required for the "forgot password" flow, not just booking/application notifications
- [ ] Add at least `JSEARCH_API_KEY` and `GOOGLE_ALERTS_RSS` for Africa job volume
- [ ] Set `ADMIN_EMAILS` to your real email address
- [ ] Resumes are stored under `instance/uploads/` (outside `static/`) and served only via the authenticated `/uploads/<filename>` route — don't symlink or copy them into `static/`
- [ ] If you're fronting this with a load balancer/reverse proxy, set `RATELIMIT_STORAGE_URI` to a shared store (e.g. `redis://...`) instead of the default in-memory limiter, or rate limits will be per-process instead of global

---

## Tech Stack

- **Backend**: Flask 3.0, SQLAlchemy, Flask-Login, Flask-Mail, Flask-WTF
- **Database**: SQLite (dev) / PostgreSQL (prod)
- **Scraper**: Async ThreadPoolExecutor, 61 source functions, 10-min cache
- **Frontend**: Vanilla JS, custom CSS, mobile-first responsive design
- **Server**: Gunicorn (production)
