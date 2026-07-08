"""
Gunicorn configuration for HireBridge Africa.
Tuned for Render / Koyeb free tier (512 MB RAM, shared vCPU).
"""
import multiprocessing, os

# ── Workers ────────────────────────────────────────────────────────────────────
# Free-tier boxes have 0.1–0.5 vCPU and 512 MB RAM.
# 2 gthread workers with 4 threads each = 8 concurrent requests max,
# which is more than enough for free-tier traffic, while keeping RSS under 300 MB.
# The background job-scraper thread also lives here, so we deliberately keep
# workers=1 in production to avoid multiple scrapers running simultaneously.
workers     = int(os.environ.get("WEB_CONCURRENCY", 1))
threads     = int(os.environ.get("GUNICORN_THREADS", 4))
worker_class = "gthread"

# ── Timeouts ───────────────────────────────────────────────────────────────────
# 120s matches Render's own request timeout. The background scraper runs in a
# daemon thread — it won't hold up normal request handling.
timeout          = int(os.environ.get("GUNICORN_TIMEOUT", 120))
keepalive        = 5
graceful_timeout = 30

# ── Binding ────────────────────────────────────────────────────────────────────
# Render injects PORT at runtime; default to 10000 for local parity.
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

# ── Logging ────────────────────────────────────────────────────────────────────
accesslog  = "-"   # stdout → captured by Render's log viewer
errorlog   = "-"
loglevel   = os.environ.get("LOG_LEVEL", "info")
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Startup ────────────────────────────────────────────────────────────────────
preload_app = True   # Load app once before forking — saves RAM and catches
                     # startup errors early before the first request arrives.

def on_starting(server):
    server.log.info("HireBridge Africa starting up (workers=%d, threads=%d)",
                    workers, threads)
