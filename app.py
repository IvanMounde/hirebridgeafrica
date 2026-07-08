"""
HireBridge Africa - Main Flask Application v2
"""
import os, re, time
import secrets as _secrets
import click
from datetime import datetime, timezone, date as date_type
from typing import Any, Optional, Tuple

from flask import (Flask, render_template, redirect, url_for, flash, request,
                   abort, jsonify, Response, send_from_directory)
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from flask_mail import Mail, Message as MailMessage
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_compress import Compress
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from whitenoise import WhiteNoise
from markupsafe import Markup, escape

# Cloudinary is optional — only used if CLOUDINARY_URL is set in the
# environment. Without it, resumes fall back to local disk storage
# (fine for local dev, but wiped on every Render free-tier redeploy).
try:
    import cloudinary
    import cloudinary.uploader
    import cloudinary.utils
    _CLOUDINARY_AVAILABLE = True
except ImportError:
    _CLOUDINARY_AVAILABLE = False

from config import Config
from models import (db, User, Service, Booking, ContactMessage, JobPosting,
                    JobApplication, Subscriber, UserConsent,
                    VALID_APPLICATION_STATUSES, init_services, _utcnow)
from forms import (SignUpForm, SignInForm, BookingForm, ContactForm, JobApplicationForm,
                   RequestResetForm, ResetPasswordForm)
from job_scraper import JobScraper, fetch_jobs_multi

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config.from_object(Config)
Config.configure_logging()

# Middleware stack (order matters — outermost wrapper runs first)
# 1. ProxyFix: read X-Forwarded-* headers set by Render/Koyeb load balancer
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
# 2. WhiteNoise: serve static files directly from the WSGI layer without
#    hitting Flask at all — much faster and works on Render's free tier
#    without a separate CDN. Falls through to Flask for dynamic routes.
app.wsgi_app = WhiteNoise(app.wsgi_app, root="static/", prefix="static",
                          max_age=31_536_000, autorefresh=False)

db.init_app(app)
csrf = CSRFProtect(app)
mail = Mail(app)
# Flask-Compress: gzip/brotli responses — reduces HTML/JSON transfer by ~70%
compress = Compress(app)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri=app.config.get("RATELIMIT_STORAGE_URI", "memory://"))

# ── Cloudinary (persistent resume storage) ───────────────────────────────────
# CLOUDINARY_URL looks like: cloudinary://<api_key>:<api_secret>@<cloud_name>
# Reading it here (rather than passing config= explicitly) lets the SDK parse
# and configure itself from the single env var Cloudinary gives you on signup.
_CLOUDINARY_ENABLED = _CLOUDINARY_AVAILABLE and bool(os.environ.get("CLOUDINARY_URL"))
if _CLOUDINARY_ENABLED:
    cloudinary.config(secure=True)
    app.logger.info("Cloudinary configured — resumes will be stored persistently.")
else:
    app.logger.info("Cloudinary not configured — resumes stored on local disk "
                    "(will NOT survive a Render redeploy).")

login_manager = LoginManager(app)
login_manager.login_view = "signin"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "warning"

# ── Production safety check ──────────────────────────────────────────────────
# Refuse to boot in production with the well-known default admin password or
# a placeholder SECRET_KEY — both are printed in this repo's source/README,
# so leaving them in place on a live server is a backdoor.
_DEFAULT_ADMIN_PASSWORD_HASH_MARKER = "Admin@HireBridge2025!"

def _check_production_safety():
    if os.environ.get("FLASK_ENV") != "production":
        return
    if app.config.get("SECRET_KEY") in (None, "", "changeme-use-strong-key", "replace_with_strong_random_key"):
        raise RuntimeError(
            "Refusing to start: SECRET_KEY is unset or still a placeholder. "
            "Set a strong, random SECRET_KEY in your environment before running in production."
        )
    with app.app_context():
        try:
            admin_emails = [e.strip() for e in app.config.get("ADMIN_EMAILS", "").split(",") if e.strip()]
            for email in admin_emails:
                user = User.query.filter_by(email=email).first()
                if user and user.check_password(_DEFAULT_ADMIN_PASSWORD_HASH_MARKER):
                    raise RuntimeError(
                        f"Refusing to start: admin account '{email}' still uses the default "
                        f"seed password. Run `flask create_admin --email {email} --password <new-strong-password>` "
                        f"to change it before running in production."
                    )
        except RuntimeError:
            raise
        except Exception:
            # DB not migrated yet, etc — don't block startup on this check failing to run
            pass

# Run this on every import (covers `flask run`, gunicorn, etc — not just
# `python app.py`), not just the __main__ block below.
_check_production_safety()

# ── Anti-scraping headers ─────────────────────────────────────────────────────
# Scoped to human-facing job-listing pages only — broad blocking by UA also
# breaks uptime monitors, payment-provider webhooks, and API consumers who
# legitimately use curl/requests, so we don't apply it globally.
_SCRAPE_PROTECTED_PREFIXES = ("/jobs",)
_SCRAPE_BLOCK_AGENTS = ("scrapy", "wget", "go-http", "libwww")

@app.after_request
def add_security_headers(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
    ua = request.headers.get("User-Agent", "").lower()
    if (request.path.startswith(_SCRAPE_PROTECTED_PREFIXES)
            and any(b in ua for b in _SCRAPE_BLOCK_AGENTS)):
        return Response("Access denied", status=403)
    return response

@app.errorhandler(413)
def request_too_large(error: Any) -> Tuple[str, int]:
    flash("That file is too large — please upload a resume under 5 MB.", "danger")
    return redirect(request.referrer or url_for("jobs")), 413

@app.route("/robots.txt")
def robots_txt():
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

# ── User loader ───────────────────────────────────────────────────────────────
@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    return db.session.get(User, int(user_id))

# ── Template helpers ──────────────────────────────────────────────────────────
def _safe_days_ago(dt: Optional[datetime]) -> int:
    if not dt:
        return 0
    now = _utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (now - dt).days)

app.jinja_env.globals["days_ago"] = _safe_days_ago

import re as _re
def _strip_html(value: str) -> str:
    """Remove HTML tags from a string."""
    if not value:
        return ""
    clean = _re.sub(r'<[^>]+>', '', str(value))
    # Decode common HTML entities
    clean = clean.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
                 .replace('&nbsp;', ' ').replace('&quot;', '"').replace('&#39;', "'")
    return clean.strip()

app.jinja_env.filters["strip_html"] = _strip_html

def _clean_location(val):
    """Remove list brackets/quotes from location strings like ['Philippines']"""
    import re
    if not val:
        return val
    # Remove list brackets and quotes: ['Philippines'] -> Philippines
    val = re.sub(r"^\['", "", val)
    val = re.sub(r"'\]$", "", val)
    val = re.sub(r"^\[\"", "", val)
    val = re.sub(r"\"\]$", "", val)
    # Also handle multiple values: ['US', 'UK'] -> US, UK
    val = re.sub(r"', '", ", ", val)
    val = re.sub(r"\"', \"", ", ", val)
    return val.strip()

app.jinja_env.filters["clean_location"] = _clean_location

@app.context_processor
def inject_datetime():
    return {"now": _utcnow(), "current_year": _utcnow().year}

def _is_safe_redirect(url: Optional[str]) -> bool:
    if not url:
        return False
    from urllib.parse import urlparse
    parsed = urlparse(url)
    return not parsed.netloc and not parsed.scheme

def _is_safe_external_url(url: Optional[str]) -> bool:
    """True only for well-formed http(s) links — used before rendering any
    URL sourced from third-party scraper feeds or admin input into HTML."""
    if not url:
        return False
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in {"pdf", "doc", "docx"}

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("signin"))
        admin_emails = [e.strip() for e in app.config.get("ADMIN_EMAILS", "").split(",")]
        if current_user.email not in admin_emails:
            abort(403)
        return f(*args, **kwargs)
    return decorated

def _record_consent(user_id, email, consent_type, version="1.0"):
    try:
        c = UserConsent(
            user_id=user_id, email=email, consent_type=consent_type,
            version=version,
            ip_address=request.remote_addr,
            user_agent=request.headers.get("User-Agent", "")[:500]
        )
        db.session.add(c)
        db.session.commit()
    except Exception:
        db.session.rollback()

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index() -> str:
    services = Service.query.filter_by(category="individual").limit(6).all()
    return render_template("index.html", services=services)


# ── Background job prefetch (runs once on startup, then every 2 hours) ───────
import threading as _threading

# A single process-wide lock + cooldown timestamp. Without this, every visitor
# hitting /jobs while the DB is sparsely populated spawns its own scrape
# thread — multiple overlapping fetches then hit the same external rate
# limits concurrently and can race on the dedup check-then-insert, producing
# duplicate JobPosting rows. `_fetch_lock` ensures only one fetch runs at a
# time; `_FETCH_COOLDOWN_SECONDS` stops the opportunistic "page looks empty"
# trigger from firing more than once per cooldown window regardless of traffic.
_fetch_lock = _threading.Lock()
_last_fetch_at: Optional[float] = None
_FETCH_COOLDOWN_SECONDS = 600  # 10 minutes


def _try_fetch_jobs(force: bool = False) -> int:
    """Run a job fetch if no other fetch is in progress and the cooldown has
    elapsed. Returns the number of new jobs imported, or 0 if skipped."""
    import time
    global _last_fetch_at
    if not force and _last_fetch_at is not None and (time.time() - _last_fetch_at) < _FETCH_COOLDOWN_SECONDS:
        return 0
    if not _fetch_lock.acquire(blocking=False):
        return 0  # a fetch is already running
    try:
        count = _do_fetch_jobs()
        _last_fetch_at = time.time()
        return count
    finally:
        _fetch_lock.release()


def _background_fetch():
    """Import jobs into DB in a background thread — never blocks page loads."""
    if os.environ.get("DISABLE_BACKGROUND_SCRAPER", "").lower() in ("1", "true", "yes"):
        import logging
        logging.getLogger(__name__).info("Background scraper disabled via DISABLE_BACKGROUND_SCRAPER env var")
        return
    def _run():
        import time
        # 30s startup delay so DB migrations and initial request handling
        # settle before the scraper starts consuming connections/threads.
        time.sleep(30)
        while True:
            try:
                with app.app_context():
                    _try_fetch_jobs(force=True)
            except Exception as exc:
                db.session.rollback()
                app.logger.error("Background fetch error: %s", exc)
            # ATS board listings change at most a few times a day.
            # Default 4h — tune via SCRAPER_INTERVAL_SECONDS in .env.
            interval = int(os.environ.get("SCRAPER_INTERVAL_SECONDS", 14400))
            app.logger.info("Background fetch: sleeping %ds until next cycle", interval)
            time.sleep(interval)

    t = _threading.Thread(target=_run, daemon=True)
    t.start()

def _do_fetch_jobs():
    """Actually insert scraped jobs into DB. Only call via _try_fetch_jobs,
    which guards against concurrent/overlapping runs."""
    import time as _t
    from job_scraper import fetch_jobs_multi as _fetch
    t0 = _t.monotonic()
    valid_cols = set(JobPosting.__table__.columns.keys())
    # Belt-and-suspenders: cap every string field to its actual DB column
    # length before insert. This is what actually prevents a repeat of the
    # salary_range bug (or any future field) from rolling back an entire
    # batch — a single oversized value used to make Postgres reject the
    # whole multi-hundred-row transaction, silently zeroing out the import.
    col_max_len = {
        c.name: c.type.length
        for c in JobPosting.__table__.columns
        if hasattr(c.type, "length") and c.type.length
    }
    raw = _fetch(admin_mode=True, limit=0)
    to_insert = []
    for j in raw:
        if not j.get("title") or not j.get("application_url"):
            continue
        # Dedup by application_url first (cheapest), then title+company
        exists = JobPosting.query.filter_by(
            application_url=j["application_url"]
        ).first()
        if not exists:
            exists = JobPosting.query.filter(
                JobPosting.title == j["title"],
                JobPosting.company == j.get("company", ""),
            ).first()
        if exists:
            continue
        data = {k: v for k, v in j.items() if k in valid_cols}
        for key, maxlen in col_max_len.items():
            v = data.get(key)
            if isinstance(v, str) and len(v) > maxlen:
                data[key] = v[:maxlen]
        to_insert.append(data)

    count = 0
    if to_insert:
        try:
            # Fast path: insert everything in one transaction.
            for data in to_insert:
                db.session.add(JobPosting(**data))
            db.session.commit()
            count = len(to_insert)
        except Exception as exc:
            # Something still slipped past the length guard (e.g. a bad
            # enum-like value, encoding issue, etc). Roll back and retry
            # row-by-row so we salvage every job except the actual offender,
            # instead of losing the whole batch like before.
            db.session.rollback()
            app.logger.warning(
                "Batch job commit failed (%s) — retrying %d rows individually",
                exc, len(to_insert),
            )
            for data in to_insert:
                try:
                    db.session.add(JobPosting(**data))
                    db.session.commit()
                    count += 1
                except Exception as row_exc:
                    db.session.rollback()
                    app.logger.warning(
                        "Skipped one job (title=%r) — %s",
                        data.get("title", "?"), row_exc,
                    )
    elapsed = round(_t.monotonic() - t0, 1)
    app.logger.info("Background fetch done: %d new jobs in %.1fs", count, elapsed)
    return count

# Start background fetch once app is ready
with app.app_context():
    pass   # ensure context exists
_background_fetch()



# ── Filter value normaliser ────────────────────────────────────────────────────
# Stored values vary (Full_time, full-time, Full Time) — normalise both sides.
def _norm(v):
    return v.lower().replace("_", " ").replace("-", " ").strip()

def _apply_filters(db_q, query, loc, cat, level, jtype):
    """Apply all filters using AND logic with broad ilike matching."""
    from sqlalchemy import func, or_

    if query:
        db_q = db_q.filter(
            JobPosting.title.ilike(f"%{query}%") |
            JobPosting.company.ilike(f"%{query}%") |
            JobPosting.description.ilike(f"%{query}%")
        )

    if loc:
        # Match location OR description (catches "open to Kenya candidates" in desc)
        db_q = db_q.filter(
            JobPosting.location.ilike(f"%{loc}%") |
            JobPosting.description.ilike(f"%{loc}%")
        )

    if cat and cat != "all":
        # Match category name OR slug variant
        cat_norm = cat.replace("-", " ").replace("_", " ")
        db_q = db_q.filter(
            JobPosting.category.ilike(f"%{cat_norm}%") |
            JobPosting.category.ilike(f"%{cat}%")
        )

    if level and level != "all":
        level_norm = _norm(level)
        # Map common aliases: entry→junior, senior→senior/lead/staff
        level_variants = {
            "entry":     ["entry", "junior", "graduate", "intern", "associate"],
            "mid":       ["mid", "middle", "intermediate"],
            "senior":    ["senior", "lead", "staff", "principal", "sr"],
            "executive": ["executive", "director", "vp", "c-level", "head", "chief", "president"],
            "internship":["intern", "trainee", "attachment", "placement"],
        }.get(level_norm, [level_norm])
        from sqlalchemy import or_
        db_q = db_q.filter(or_(
            *[JobPosting.experience_level.ilike(f"%{v}%") for v in level_variants]
        ))

    if jtype and jtype != "all":
        jtype_norm = _norm(jtype)
        # Map frontend values to stored variants
        type_variants = {
            "full time":  ["full_time", "full-time", "fulltime", "permanent", "full time"],
            "part time":  ["part_time", "part-time", "parttime", "part time"],
            "remote":     ["remote", "distributed", "work from home", "wfh"],
            "hybrid":     ["hybrid", "flexible", "partially remote"],
            "contract":   ["contract", "contractor", "freelance", "temporary", "temp"],
            "freelance":  ["freelance", "contract", "self-employed", "gig"],
            "internship": ["intern", "internship", "trainee", "attachment"],
            "onsite":     ["onsite", "on-site", "on site", "office"],
        }.get(jtype_norm, [jtype_norm])
        from sqlalchemy import or_
        db_q = db_q.filter(or_(
            *[JobPosting.job_type.ilike(f"%{v}%") for v in type_variants]
        ))

    return db_q


@app.route("/jobs")
def jobs() -> str:
    query    = request.args.get("q", "").strip()
    loc      = request.args.get("location", "").strip()
    cat      = request.args.get("category", "all")
    level    = request.args.get("level", "all")
    jtype    = request.args.get("type", "all")
    page     = max(1, request.args.get("page", 1, type=int))
    per_page = 20

    db_q = JobPosting.query.filter_by(is_active=True)
    db_q = _apply_filters(db_q, query, loc, cat, level, jtype)
    db_q = db_q.order_by(JobPosting.posted_date.desc())

    pagination = db_q.paginate(page=page, per_page=per_page, error_out=False)

    # If DB has very few results AND this is the first page, opportunistically
    # trigger a fetch so the user isn't staring at an empty page. Guarded by
    # _try_fetch_jobs's lock + cooldown so concurrent visitors don't each spin
    # up their own overlapping scrape thread.
    if pagination.total < 30 and page == 1:
        import threading
        def _quick_fetch():
            try:
                with app.app_context():
                    _try_fetch_jobs()
            except Exception:
                db.session.rollback()
        threading.Thread(target=_quick_fetch, daemon=True).start()

    return render_template("jobs.html", jobs=pagination.items, pagination=pagination,
                           query=query, location=loc, cat=cat, level=level, jtype=jtype)


@app.route("/about")
def about() -> str:
    return render_template("about.html")


@app.route("/signup", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def signup() -> str | Response:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    form = SignUpForm()
    if form.validate_on_submit():
        # Require T&C acceptance
        if not request.form.get("agree_terms"):
            flash("You must accept the Terms of Use and Privacy Policy to continue.", "danger")
            return render_template("auth/signup.html", form=form)
        if User.query.filter_by(email=form.email.data.lower()).first():
            flash("Email already registered. Please sign in.", "warning")
            return redirect(url_for("signin"))
        if User.query.filter_by(username=form.username.data).first():
            flash("Username already taken. Please choose another.", "warning")
            return render_template("auth/signup.html", form=form)
        user = User(
            email=form.email.data.lower(),
            username=form.username.data,
            full_name=form.full_name.data,
            phone=form.phone.data,
            terms_accepted=True,
            terms_accepted_at=_utcnow(),
            privacy_accepted=True,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        # Record consent
        _record_consent(user.id, user.email, "terms")
        _record_consent(user.id, user.email, "privacy")
        if request.form.get("agree_marketing"):
            _record_consent(user.id, user.email, "marketing")
        login_user(user)
        flash("Welcome to HireBridge Africa! Your account has been created.", "success")
        next_page = request.args.get("next")
        return redirect(next_page if _is_safe_redirect(next_page) else url_for("dashboard"))
    return render_template("auth/signup.html", form=form)


@app.route("/signin", methods=["GET", "POST"])
@limiter.limit("10 per 5 minutes")
def signin() -> str | Response:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    form = SignInForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            flash(f"Welcome back, {user.full_name or user.username}!", "success")
            next_page = request.args.get("next")
            return redirect(next_page if _is_safe_redirect(next_page) else url_for("dashboard"))
        flash("Invalid email or password.", "danger")
    return render_template("auth/signin.html", form=form)


@app.route("/logout", methods=["GET", "POST"])
@login_required
def logout() -> Response:
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("index"))


@app.route("/reset-password", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def request_reset() -> str | Response:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    form = RequestResetForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        if user:
            token = user.get_reset_token(app.config["SECRET_KEY"])
            reset_url = url_for("reset_password", token=token, _external=True)
            try:
                msg = MailMessage("Reset your HireBridge Africa password", recipients=[user.email])
                msg.body = (
                    f"Hi {user.full_name or user.username},\n\n"
                    f"We received a request to reset your password. Click the link below "
                    f"to choose a new one. This link expires in 30 minutes.\n\n"
                    f"{reset_url}\n\n"
                    f"If you didn't request this, you can safely ignore this email.\n\n"
                    f"HireBridge Africa Team"
                )
                mail.send(msg)
            except Exception as exc:
                app.logger.error("Password reset email failed: %s", exc)
        # Always show the same message whether or not the email exists,
        # so this endpoint can't be used to enumerate registered accounts.
        flash("If that email is registered, a password reset link has been sent.", "info")
        return redirect(url_for("signin"))
    return render_template("auth/request_reset.html", form=form)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def reset_password(token: str) -> str | Response:
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    user = User.verify_reset_token(token, app.config["SECRET_KEY"])
    if not user:
        flash("That reset link is invalid or has expired. Please request a new one.", "danger")
        return redirect(url_for("request_reset"))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        db.session.commit()
        flash("Your password has been updated. Please sign in.", "success")
        return redirect(url_for("signin"))
    return render_template("auth/reset_password.html", form=form, token=token)


@app.route("/services")
def services() -> str:
    individual = Service.query.filter_by(category="individual").all()
    corporate = Service.query.filter_by(category="corporate").all()
    return render_template("services.html", individual_services=individual,
                           corporate_services=corporate)


@app.route("/service/<int:service_id>")
def service_detail(service_id: int) -> str:
    service = db.session.get(Service, service_id) or abort(404)
    return render_template("service_detail.html", service=service)


@app.route("/book/<int:service_id>", methods=["GET", "POST"])
@login_required
def book_service(service_id: int) -> str | Response:
    service = db.session.get(Service, service_id) or abort(404)
    form = BookingForm()
    if form.validate_on_submit():
        booking = Booking(
            user_id=current_user.id,
            service_id=service.id,
            preferred_date=form.preferred_date.data,
            notes=form.notes.data,
        )
        db.session.add(booking)
        db.session.commit()
        try:
            _send_booking_confirmation(current_user, service, booking)
        except Exception:
            pass
        flash(f"Booking confirmed for {service.name}! We will contact you shortly.", "success")
        return redirect(url_for("dashboard"))
    return render_template("booking.html", service=service, form=form)


@app.route("/dashboard")
@login_required
def dashboard() -> str | Response:
    admin_emails = [e.strip() for e in app.config.get("ADMIN_EMAILS", "").split(",")]
    if current_user.email in admin_emails:
        return redirect(url_for("admin_dashboard"))
    bookings = Booking.query.filter_by(user_id=current_user.id)\
                            .order_by(Booking.booking_date.desc()).all()
    applications = JobApplication.query.filter_by(user_id=current_user.id)\
                                       .order_by(JobApplication.applied_at.desc()).all()
    return render_template("dashboard/user_dashboard.html",
                           bookings=bookings, applications=applications)


@app.route("/admin")
@admin_required
def admin_dashboard() -> str:
    total_users = User.query.count()
    total_bookings = Booking.query.count()
    pending_bookings = Booking.query.filter_by(status="pending").count()
    total_jobs = JobPosting.query.count()
    active_jobs = JobPosting.query.filter_by(is_active=True).count()
    total_applications = JobApplication.query.count()
    recent_bookings = Booking.query.order_by(Booking.booking_date.desc()).limit(10).all()
    recent_apps = JobApplication.query.order_by(JobApplication.applied_at.desc()).limit(10).all()
    subscribers = Subscriber.query.filter_by(is_active=True).count()
    return render_template("dashboard/admin_dashboard.html",
                           total_users=total_users, total_bookings=total_bookings,
                           pending_bookings=pending_bookings, total_jobs=total_jobs,
                           active_jobs=active_jobs, total_applications=total_applications,
                           recent_bookings=recent_bookings, recent_apps=recent_apps,
                           subscribers=subscribers)


@app.route("/admin/job/add", methods=["GET", "POST"])
@admin_required
def admin_add_job() -> str | Response:
    if request.method == "POST":
        job = JobPosting(
            title=request.form.get("title", "").strip(),
            company=request.form.get("company", "").strip(),
            location=request.form.get("location", "").strip(),
            job_type=request.form.get("job_type", "onsite"),
            category=request.form.get("category", "general"),
            experience_level=request.form.get("experience_level", "mid"),
            description=request.form.get("description", "").strip(),
            salary_range=request.form.get("salary_range", "").strip(),
            experience_years=request.form.get("experience_years", "").strip(),
            skills=request.form.get("skills", "").strip(),
            application_url=request.form.get("application_url", "").strip(),
            source="manual",
        )
        db.session.add(job)
        db.session.commit()
        flash("Job posted successfully!", "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("dashboard/admin_add_job.html")


@app.route("/admin/job/<int:job_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_job(job_id: int) -> str | Response:
    job = db.session.get(JobPosting, job_id) or abort(404)
    if request.method == "POST":
        job.title = request.form.get("title", job.title).strip()
        job.company = request.form.get("company", job.company).strip()
        job.location = request.form.get("location", job.location).strip()
        job.job_type = request.form.get("job_type", job.job_type)
        job.category = request.form.get("category", job.category)
        job.experience_level = request.form.get("experience_level", job.experience_level)
        job.description = request.form.get("description", job.description).strip()
        job.salary_range = request.form.get("salary_range", "").strip()
        job.experience_years = request.form.get("experience_years", "").strip()
        job.skills = request.form.get("skills", "").strip()
        job.application_url = request.form.get("application_url", "").strip()
        db.session.commit()
        flash("Job updated successfully!", "success")
        return redirect(url_for("admin_dashboard"))
    return render_template("dashboard/admin_edit_job.html", job=job)


@app.route("/admin/job/<int:job_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_job(job_id: int) -> Response:
    job = db.session.get(JobPosting, job_id) or abort(404)
    job.is_active = not job.is_active
    db.session.commit()
    status = "activated" if job.is_active else "deactivated"
    flash(f"Job {status}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/job/<int:job_id>/delete", methods=["POST"])
@admin_required
def admin_delete_job(job_id: int) -> Response:
    job = db.session.get(JobPosting, job_id) or abort(404)
    db.session.delete(job)
    db.session.commit()
    flash("Job deleted.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/jobs/auto-fetch", methods=["POST"])
@admin_required
def admin_auto_fetch_jobs() -> Response:
    # IMPORTANT: don't call auto_fetch_jobs() directly here. A full scrape
    # can take 30-90s, and Render's free tier (fractional shared CPU) treats
    # a request that blocks that long as a dead worker and returns 502 Bad
    # Gateway before this function even gets to respond — the fetch was
    # actually still running server-side when that happened. Instead, kick
    # it off in a background thread (same pattern as the scheduled scraper)
    # and return immediately so the request completes instantly.
    import threading

    def _run():
        try:
            with app.app_context():
                count = _try_fetch_jobs(force=True)
                app.logger.info("Manual auto-fetch (admin) completed: %d new jobs", count)
        except Exception as exc:
            db.session.rollback()
            app.logger.error("Manual auto-fetch (admin) failed: %s", exc)

    threading.Thread(target=_run, daemon=True).start()
    flash("Fetching jobs in the background — this can take a minute. "
          "Refresh this page shortly to see the new count.", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/booking/<int:booking_id>/cancel", methods=["POST"])
@login_required
def cancel_booking(booking_id: int) -> Response:
    booking = db.session.get(Booking, booking_id) or abort(404)
    if booking.user_id != current_user.id:
        abort(403)
    booking.status = "cancelled"
    db.session.commit()
    flash("Booking cancelled successfully.", "info")
    return redirect(url_for("dashboard"))


@app.route("/contact", methods=["GET", "POST"])
@limiter.limit("10 per hour")
def contact() -> str | Response:
    form = ContactForm()
    if form.validate_on_submit():
        msg = ContactMessage(
            name=form.name.data, email=form.email.data.lower(),
            subject=form.subject.data, message=form.message.data,
        )
        db.session.add(msg)
        db.session.commit()
        flash("Message sent! We'll get back to you within 24 hours.", "success")
        return redirect(url_for("contact"))
    return render_template("contact.html", form=form)


@app.route("/job/<int:job_id>/apply", methods=["GET", "POST"])
@login_required
def apply_for_job(job_id: int) -> str | Response:
    job = db.session.get(JobPosting, job_id) or abort(404)
    if not job.is_active:
        flash("This job is no longer accepting applications.", "warning")
        return redirect(url_for("jobs"))

    existing = JobApplication.query.filter_by(
        user_id=current_user.id, job_id=job_id
    ).first()
    if existing:
        flash("You have already applied for this position.", "info")
        return redirect(url_for("my_applications"))

    form = JobApplicationForm()
    if form.validate_on_submit():
        application_type = request.form.get("application_type", "direct")
        resume_filename = None
        if "resume" in request.files:
            file = request.files["resume"]
            if file and file.filename and _allowed_file(file.filename):
                # Random token in the filename (rather than a guessable
                # user_id prefix) so resume URLs can't be enumerated even
                # by an authorized viewer who only knows other user IDs.
                safe_name = secure_filename(file.filename)
                token = f"{current_user.id}_{_secrets.token_hex(8)}_{safe_name}"

                if _CLOUDINARY_ENABLED:
                    # Upload to Cloudinary as an "authenticated" resource —
                    # this means the file is NOT publicly reachable by its
                    # Cloudinary URL alone. We generate a short-lived signed
                    # URL on demand in serve_resume(), after our own
                    # ownership check passes. The DB stores a "cloudinary:"
                    # prefixed public_id, not a filename, so serve_resume()
                    # knows which storage backend to use.
                    try:
                        result = cloudinary.uploader.upload(
                            file,
                            resource_type="raw",
                            type="authenticated",
                            public_id=f"hirebridge_resumes/{token}",
                            overwrite=False,
                        )
                        resume_filename = f"cloudinary:{result['public_id']}"
                    except Exception as exc:
                        app.logger.error("Cloudinary upload failed, falling back to local disk: %s", exc)
                        resume_filename = None  # fall through to local save below

                if not resume_filename:
                    # Local disk fallback — used automatically if Cloudinary
                    # isn't configured, or if the upload above failed.
                    resume_filename = token
                    upload_folder = app.config.get("UPLOAD_FOLDER",
                                                   os.path.join(app.instance_path, "uploads"))
                    os.makedirs(upload_folder, exist_ok=True)
                    file.save(os.path.join(upload_folder, resume_filename))

        application = JobApplication(
            user_id=current_user.id,
            job_id=job_id,
            application_type=application_type,
            cover_letter=form.cover_letter.data,
            resume_filename=resume_filename,
        )
        db.session.add(application)
        db.session.commit()

        if application_type == "hirebridge":
            flash("HireBridge Africa will apply on your behalf! Our team will contact you within 24 hours.", "success")
        else:
            flash("Application submitted! Good luck with your application.", "success")
            if job.application_url and _is_safe_external_url(job.application_url):
                # job.application_url may originate from third-party scraper
                # feeds, so it's untrusted — escape it even though this flash
                # is rendered with |safe in base.html.
                safe_url = escape(job.application_url)
                flash(Markup(f'Also apply directly at: <a href="{safe_url}" target="_blank" '
                              f'rel="noopener noreferrer nofollow">the company website</a>'), "info")
        return redirect(url_for("my_applications"))
    return render_template("job_apply.html", job=job, form=form)


@app.route("/uploads/<path:filename>")
@login_required
def serve_resume(filename: str) -> Response:
    """Serve an applicant's resume — restricted to the resume's owner or an admin.

    Uploaded resumes are deliberately stored outside static/ so Flask's static
    file handler can never hand them out to an unauthenticated visitor; this
    route enforces ownership before streaming the file, regardless of which
    storage backend (local disk or Cloudinary) the file actually lives in.
    """
    application = JobApplication.query.filter_by(resume_filename=filename).first()
    if not application:
        abort(404)
    admin_emails = [e.strip() for e in app.config.get("ADMIN_EMAILS", "").split(",")]
    if application.user_id != current_user.id and current_user.email not in admin_emails:
        abort(403)

    if filename.startswith("cloudinary:"):
        if not _CLOUDINARY_ENABLED:
            # File was uploaded when Cloudinary was configured, but it isn't
            # right now (e.g. env var removed) — fail clearly rather than 404.
            abort(503, description="Resume storage is temporarily unavailable.")
        public_id = filename.split("cloudinary:", 1)[1]
        # Generate a signed URL valid for 60 seconds — long enough for the
        # browser to fetch it, short enough that it can't be shared/reused.
        signed_url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type="raw",
            type="authenticated",
            sign_url=True,
            expires_at=int(time.time()) + 60,
        )
        return redirect(signed_url)

    upload_folder = app.config.get("UPLOAD_FOLDER", os.path.join(app.instance_path, "uploads"))
    return send_from_directory(upload_folder, filename, as_attachment=True)


@app.route("/applications")
@login_required
def my_applications() -> str:
    applications = JobApplication.query.filter_by(user_id=current_user.id)\
                                       .order_by(JobApplication.applied_at.desc()).all()
    return render_template("user_applications.html", applications=applications)


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
@login_required
def withdraw_application(app_id: int) -> Response:
    app_obj = db.session.get(JobApplication, app_id) or abort(404)
    if app_obj.user_id != current_user.id:
        abort(403)
    if app_obj.status not in ("submitted", "under_review"):
        flash("This application cannot be withdrawn.", "warning")
        return redirect(url_for("my_applications"))
    app_obj.status = "withdrawn"
    db.session.commit()
    flash("Application withdrawn.", "info")
    return redirect(url_for("my_applications"))


@app.route("/admin/applications")
@admin_required
def admin_applications() -> str:
    status_filter = request.args.get("status", "all")
    q = JobApplication.query.join(User).join(JobPosting)
    if status_filter != "all":
        q = q.filter(JobApplication.status == status_filter)
    applications = q.order_by(JobApplication.applied_at.desc()).all()
    return render_template("admin_applications.html", applications=applications,
                           status_filter=status_filter)


@app.route("/admin/application/<int:app_id>/update-status", methods=["POST"])
@admin_required
def update_application_status(app_id: int) -> Response:
    app_obj = db.session.get(JobApplication, app_id) or abort(404)
    new_status = request.form.get("status", "")
    if new_status not in VALID_APPLICATION_STATUSES:
        flash("Invalid status.", "danger")
        return redirect(url_for("admin_applications"))
    app_obj.status = new_status
    db.session.commit()
    flash(f"Application status updated to {new_status}.", "success")
    return redirect(url_for("admin_applications"))


@app.route("/admin/application/<int:app_id>/forward", methods=["POST"])
@admin_required
def forward_application(app_id: int) -> Response:
    app_obj = db.session.get(JobApplication, app_id) or abort(404)
    recipient = request.form.get("recipient_email", "").strip()
    if not recipient:
        flash("Recipient email is required.", "danger")
        return redirect(url_for("admin_applications"))
    try:
        msg = MailMessage(
            f"Job Application – {app_obj.job.title} @ {app_obj.job.company}",
            recipients=[recipient],
        )
        msg.body = (
            f"Applicant: {app_obj.user.full_name}\n"
            f"Email: {app_obj.user.email}\n"
            f"Phone: {app_obj.user.phone or 'N/A'}\n\n"
            f"Position: {app_obj.job.title}\n"
            f"Company: {app_obj.job.company}\n\n"
            f"Cover Letter:\n{app_obj.cover_letter or 'N/A'}\n\n"
            f"Applied via HireBridge Africa"
        )
        mail.send(msg)
        app_obj.status = "under_review"
        db.session.commit()
        flash("Application forwarded successfully.", "success")
    except Exception as exc:
        app.logger.error("Forward email failed: %s", exc)
        flash("Email send failed. Check mail settings.", "danger")
    return redirect(url_for("admin_applications"))


# ── Newsletter ─────────────────────────────────────────────────────────────────
@app.route("/subscribe", methods=["POST"])
@limiter.limit("10 per hour")
def subscribe():
    email = request.form.get("email", "").strip().lower()
    name  = request.form.get("name", "").strip()
    if not email or "@" not in email:
        flash("Please enter a valid email address.", "danger")
        return redirect(request.referrer or url_for("index"))
    existing = Subscriber.query.filter_by(email=email).first()
    if existing:
        if not existing.is_active:
            existing.is_active = True
            db.session.commit()
            flash("You've been re-subscribed to our newsletter!", "success")
        else:
            flash("You're already subscribed!", "info")
    else:
        sub = Subscriber(email=email, full_name=name, source="website")
        db.session.add(sub)
        db.session.commit()
        flash("Thank you for subscribing to HireBridge Africa updates!", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/unsubscribe")
def unsubscribe():
    email = request.args.get("email", "").strip().lower()
    sub = Subscriber.query.filter_by(email=email).first()
    if sub:
        sub.is_active = False
        db.session.commit()
        flash("You have been unsubscribed.", "info")
    return redirect(url_for("index"))


# ── Legal pages ────────────────────────────────────────────────────────────────
@app.route("/terms")
def terms():
    return render_template("legal/terms.html")

@app.route("/privacy")
def privacy():
    return render_template("legal/privacy.html")

@app.route("/cookies")
def cookies():
    return render_template("legal/cookies.html")


# ── JSON APIs ──────────────────────────────────────────────────────────────────
@app.route("/api/services")
def api_services() -> Response:
    services = Service.query.all()
    return jsonify([{"id": s.id, "name": s.name, "price": s.price,
                     "category": s.category, "tag": s.tag} for s in services])


@app.route("/api/jobs")
def api_jobs() -> Response:
    jobs = JobPosting.query.filter_by(is_active=True)\
                           .order_by(JobPosting.posted_date.desc()).limit(50).all()
    return jsonify([j.to_dict() for j in jobs])


@app.route("/api/jobs/search")
def api_jobs_search() -> Response:
    """Search jobs. Always queries the local DB (fast).
    The live scraper is only invoked for background imports, not here."""
    q   = request.args.get("q", "").strip()
    loc = request.args.get("location", "").strip()
    query = JobPosting.query.filter_by(is_active=True)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                JobPosting.title.ilike(like),
                JobPosting.company.ilike(like),
                JobPosting.description.ilike(like),
            )
        )
    if loc:
        query = query.filter(JobPosting.location.ilike(f"%{loc}%"))
    jobs = query.order_by(JobPosting.posted_date.desc()).limit(30).all()
    return jsonify({"jobs": [j.to_dict() for j in jobs], "count": len(jobs)})


@app.route("/api/availability")
def check_availability() -> Response:
    service_id = request.args.get("service_id", type=int)
    date_str   = request.args.get("date", "")
    if not service_id or not date_str:
        return jsonify({"error": "Missing parameters"}), 400
    try:
        booking_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    count = Booking.query.filter_by(service_id=service_id,
                                    preferred_date=booking_date).count()
    return jsonify({"available": count < 3, "slots_remaining": max(0, 3 - count)})


# ── Admin booking forward ──────────────────────────────────────────────────────
@app.route("/admin/booking/<int:booking_id>/forward", methods=["POST"])
@admin_required
def forward_booking(booking_id: int) -> Response:
    booking = db.session.get(Booking, booking_id) or abort(404)
    recipient = request.form.get("recipient_email", "").strip()
    if not recipient:
        flash("Recipient email is required.", "danger")
        return redirect(url_for("admin_dashboard"))
    try:
        msg = MailMessage(
            f"Service Booking – {booking.service.name}",
            recipients=[recipient],
        )
        preferred = booking.preferred_date.strftime("%B %d, %Y") if booking.preferred_date else "TBD"
        msg.body = (
            f"Client: {booking.user.full_name}\n"
            f"Email: {booking.user.email}\n"
            f"Phone: {booking.user.phone or 'N/A'}\n\n"
            f"Service: {booking.service.name}\n"
            f"Preferred Date: {preferred}\n"
            f"Notes: {booking.notes or 'None'}\n\n"
            f"Forwarded from HireBridge Africa"
        )
        mail.send(msg)
        booking.status = "confirmed"
        db.session.commit()
        flash("Booking forwarded successfully.", "success")
    except Exception as exc:
        app.logger.error("Forward booking email failed: %s", exc)
        flash("Email send failed.", "danger")
    return redirect(url_for("admin_dashboard"))


# ── Helpers ────────────────────────────────────────────────────────────────────
def _send_booking_confirmation(user: User, service: Service, booking: Booking) -> None:
    try:
        msg = MailMessage(
            "Booking Confirmation – HireBridge Africa",
            recipients=[user.email],
        )
        preferred = booking.preferred_date.strftime("%B %d, %Y") if booking.preferred_date else "Not specified"
        price_str = f"KES {service.price:,}" if service.price else "Contact us for pricing"
        msg.body = (
            f"Dear {user.full_name or user.username},\n\n"
            f"Thank you for booking with HireBridge Africa.\n\n"
            f"Service: {service.name}\n"
            f"Price: {price_str}\n"
            f"Preferred Date: {preferred}\n\n"
            f"IMPORTANT DISCLAIMER: HireBridge Africa provides career support services. "
            f"Booking a service does not guarantee employment or job placement.\n\n"
            f"A member of our team will contact you shortly.\n\n"
            f"Warm regards,\nHireBridge Africa Team\n"
            f"hirebridge@gmail.com | +254742390793"
        )
        mail.send(msg)
    except Exception as exc:
        app.logger.error("Booking confirmation email failed: %s", exc)


def auto_fetch_jobs() -> int:
    """Admin-triggered bulk import. Bypasses the cooldown (it's an explicit
    action) but still respects the lock so it can't run concurrently with
    the background fetch thread."""
    try:
        return _try_fetch_jobs(force=True)
    except Exception as exc:
        app.logger.error("Auto-fetch jobs failed: %s", exc)
        db.session.rollback()
        return 0


# ── Error handlers ─────────────────────────────────────────────────────────────
@app.errorhandler(403)
def forbidden(error: Any) -> Tuple[str, int]:
    return render_template("403.html"), 403

@app.errorhandler(404)
def not_found(error: Any) -> Tuple[str, int]:
    return render_template("404.html"), 404

@app.errorhandler(500)
def internal_error(error: Any) -> Tuple[str, int]:
    db.session.rollback()
    return render_template("500.html"), 500


# ── CLI ────────────────────────────────────────────────────────────────────────
@app.cli.command()
def init_db() -> None:
    db.create_all()
    init_services()
    _seed_default_admin()
    print("Database initialised!")
    print("Run `flask create_admin --email YOU@example.com --password YOURPASS` any time to change the admin login.")


@app.cli.command("create_admin")
@click.option("--email", default=None, help="Admin email address")
@click.option("--username", default=None, help="Admin username")
@click.option("--password", default=None, help="Admin password (random and printed once if omitted)")
def create_admin(email: Optional[str], username: Optional[str], password: Optional[str]) -> None:
    """Create or reset the admin user account."""
    email = email or app.config.get("ADMIN_EMAILS", "hirebridge@gmail.com").split(",")[0].strip()
    username = username or "admin"
    generated = password is None
    password = password or _secrets.token_urlsafe(12)

    existing = User.query.filter_by(email=email).first()
    if existing:
        existing.set_password(password)
        db.session.commit()
        print(f"✅  Admin password reset  →  email: {email}  |  password: {password}")
    else:
        user = User(
            email=email,
            username=username,
            full_name="Site Administrator",
            phone="",
            created_at=_utcnow(),
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        print(f"✅  Admin created  →  email: {email}  |  username: {username}  |  password: {password}")
    if generated:
        print("   ⚠ This password was randomly generated — copy it now, it won't be shown again.")
    print(f"   Make sure {email} is listed in ADMIN_EMAILS in config.py or .env")


def _seed_default_admin() -> None:
    """Silently create the default admin account if it doesn't exist, with a
    freshly generated random password rather than a hardcoded value — a
    fixed default would otherwise be sitting in source control/README forever."""
    try:
        admin_email = app.config.get("ADMIN_EMAILS", "hirebridge@gmail.com").split(",")[0].strip()
        if not User.query.filter_by(email=admin_email).first():
            password = _secrets.token_urlsafe(12)
            user = User(
                email=admin_email,
                username="admin",
                full_name="Site Administrator",
                phone="",
                created_at=_utcnow(),
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            app.logger.info("Default admin account created: %s", admin_email)
            print(f"✅  First-run admin account created  →  email: {admin_email}  |  password: {password}")
            print("   ⚠ Copy this password now — it is not stored anywhere in plaintext and won't be shown again.")
            print(f"   To change it later: flask create_admin --email {admin_email} --password <new-password>")
    except Exception as exc:
        db.session.rollback()
        app.logger.warning("Could not seed admin: %s", exc)


# ── Auto-provision database tables ───────────────────────────────────────────
# Runs on every boot — under gunicorn (production) AND under `python app.py`
# (local dev). Render's free tier has no Shell access, so this is what
# guarantees tables exist even if a build-time `flask init-db` didn't take.
# Fully idempotent: create_all/init_services/_seed_default_admin all check
# before writing, and the whole thing is wrapped so a transient DB hiccup at
# boot (e.g. a cold Neon instance waking up) never crashes the app.
with app.app_context():
    try:
        db.create_all()
        init_services()
        _seed_default_admin()
    except Exception as _exc:
        app.logger.error("Startup DB provisioning failed: %s", _exc)


if __name__ == "__main__":
    with app.app_context():
        _check_production_safety()
    app.run(debug=os.environ.get("FLASK_ENV") == "development", port=5000)