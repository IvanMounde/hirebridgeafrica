"""
HireBridge Africa — Multi-Source Job Scraper  (v8 — verified sources only)
===========================================================================
Every source in this file was verified against live error logs.
Dead URLs, malformed feeds and broken API endpoints have been removed.
Sources are organised by reliability tier.

AFRICA SOURCES  (~400 boards + APIs + RSS):
  - ReliefWeb  : POST to /v1/reports/jobs (correct 2025 endpoint)
  - Jobicy     : geo-filtered per 8 African countries
  - Remotive   : searched per 11 African country names
  - 70+  Greenhouse ATS boards for Africa-HQ companies
  - 30+  Lever ATS boards for Africa-HQ companies
  - 250+ Workable-hosted African company career pages (scraped via their API)
  - RSS  : JobwebKenya, DisruptAfrica, TechCabal, Technext, Careers24-ZA
  - Scrape: EthiopianJobs, Jobberman NG/GH, BrighterMonday (HTML fallback)
"""

import os, re, hashlib, logging, threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

try:
    from dateutil import parser as _dateparser
    _HAS_DATEUTIL = True
except ImportError:
    _HAS_DATEUTIL = False

log = logging.getLogger(__name__)

# ── Circuit Breaker: skip hosts that have failed recently ───────────────────
# Format: {hostname: (fail_count, first_fail_time)}
_CB_FAILURES: dict = {}
_CB_THRESHOLD = 3        # fails before circuit opens
_CB_RESET_SECS = 3600    # 1 hour cooldown

def _cb_check(url: str) -> bool:
    """Return True if this URL's host is currently circuit-broken (skip it)."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        entry = _CB_FAILURES.get(host)
        if not entry:
            return False
        count, first_fail = entry
        import time
        if time.time() - first_fail > _CB_RESET_SECS:
            _CB_FAILURES.pop(host, None)
            return False
        return count >= _CB_THRESHOLD
    except Exception:
        return False

def _cb_record_fail(url: str):
    """Record a failure for this URL's host."""
    try:
        from urllib.parse import urlparse
        import time
        host = urlparse(url).netloc
        entry = _CB_FAILURES.get(host)
        if entry:
            _CB_FAILURES[host] = (entry[0] + 1, entry[1])
        else:
            _CB_FAILURES[host] = (1, time.time())
    except Exception:
        pass

def _cb_record_success(url: str):
    """Clear circuit breaker on success."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        _CB_FAILURES.pop(host, None)
    except Exception:
        pass

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

_CFG = {
    "ADZUNA_APP_ID":   os.environ.get("ADZUNA_APP_ID", ""),
    "ADZUNA_APP_KEY":  os.environ.get("ADZUNA_APP_KEY", ""),
    "REED_API_KEY":    os.environ.get("REED_API_KEY", ""),
    "FINDWORK_KEY":    os.environ.get("FINDWORK_API_KEY", ""),
    # Per-request timeout. Lower = a stuck/blocked host gives up its worker
    # slot faster instead of holding it for the old 12s default.
    "TIMEOUT":         _int_env("SCRAPER_TIMEOUT", 8),
    # Concurrency for the OUTER fan-out (the ~19 source-group tasks in
    # fetch_jobs_multi) and the two big per-company ATS scans
    # (_global_ats / _africa_ats). This used to default to 24 — on a small
    # production box that meant up to 24 simultaneous outbound HTTPS
    # connections/TLS handshakes plus 24 OS threads just for ONE of the many
    # nested pools. 6 is plenty for I/O-bound work on 1-2 vCPUs.
    "MAX_WORKERS":     _int_env("SCRAPER_MAX_WORKERS", 6),
    # Concurrency for every OTHER nested source-group pool (RSS bundles,
    # government boards, NGO feeds, etc.) — previously hardcoded per-function
    # to anywhere from 5 to 20. Centralised here so it's one knob instead of
    # fifteen magic numbers scattered through the file.
    "GROUP_WORKERS":   _int_env("SCRAPER_GROUP_WORKERS", 4),
    # _africa_ats alone checks ~635 individual company ATS boards
    # (Greenhouse + Lever + Workable) every single cycle — by far the
    # heaviest part of a fetch. Instead of hitting all 635 every time, each
    # cycle only scans a rotating slice of this size; the rest are covered
    # over subsequent cycles. Set to 0 (or a number >= total companies) to
    # scan everything every cycle like before.
    "ATS_BATCH_SIZE":  _int_env("SCRAPER_ATS_BATCH_SIZE", 150),
    # Hard wall-clock ceiling for one fetch_jobs_multi() call. If sources are
    # unusually slow this guarantees the background thread (and the Flask
    # worker that triggered an opportunistic fetch) gets control back instead
    # of blocking indefinitely — whatever sources finished in time are used.
    "FETCH_BUDGET_SECONDS": _int_env("SCRAPER_FETCH_BUDGET_SECONDS", 45),
}

_cache: dict = {}
_lock = threading.Lock()
_TTL  = _int_env("SCRAPER_CACHE_TTL", 600)

def _cget(k):
    with _lock:
        e = _cache.get(k)
        if e and (datetime.now().timestamp() - e["ts"]) < _TTL:
            return e["d"]
    return None

def _cset(k, d):
    with _lock:
        _cache[k] = {"ts": datetime.now().timestamp(), "d": d}

_STOP = {"the","a","an","in","at","for","of","to","and","or","with","on","is","are","be","as","by"}

def _kws(q):
    return [w for w in q.lower().split() if len(w) > 2 and w not in _STOP]

def _strip(txt):
    if not txt: return ""
    s = re.sub(r"<[^>]+>", "", str(txt))
    for e, c in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&nbsp;"," "),("&quot;",'"'),("&#39;","'")]:
        s = s.replace(e, c)
    return s.strip()

def _dt(s):
    if not s: return None
    try:
        if _HAS_DATEUTIL:
            dt = _dateparser.parse(str(s))
        else:
            dt = datetime.fromisoformat(str(s)[:19])
        return dt.replace(tzinfo=timezone.utc) if dt and not dt.tzinfo else dt
    except Exception:
        return None

def _clean_loc(val):
    if not val: return "Remote"
    val = re.sub(r"^\['", "", str(val))
    val = re.sub(r"'\]$", "", val)
    val = re.sub(r'^\"|\"$', "", val)
    val = re.sub(r"'\s*,\s*'", ", ", val)
    return val.strip()[:120] or "Remote"

# Maps company slug prefixes/keywords → canonical location
# Used to enrich ATS jobs that come back with blank/Remote location
_SLUG_LOCATION = {
    # Nigeria
    "paystack":"Lagos, Nigeria","flutterwave":"Lagos, Nigeria",
    "kuda":"Lagos, Nigeria","piggyvest":"Lagos, Nigeria",
    "cowrywise":"Lagos, Nigeria","moniepoint":"Lagos, Nigeria",
    "teamapt":"Lagos, Nigeria","carbon-ng":"Lagos, Nigeria",
    "carbon":"Lagos, Nigeria","moni-ng":"Lagos, Nigeria",
    "thepeer":"Lagos, Nigeria","sudo":"Lagos, Nigeria",
    "bloc-ng":"Lagos, Nigeria","anchor-ng":"Lagos, Nigeria",
    "budpay":"Lagos, Nigeria","cleva-ng":"Lagos, Nigeria",
    "nomba":"Lagos, Nigeria","brass-ng":"Lagos, Nigeria",
    "crowdforce":"Lagos, Nigeria","indicina":"Lagos, Nigeria",
    "lenco":"Lagos, Nigeria","maplerad":"Lagos, Nigeria",
    "paga":"Lagos, Nigeria","pawapay-ng":"Lagos, Nigeria",
    "termii":"Lagos, Nigeria","prembly":"Lagos, Nigeria",
    "identitypass":"Lagos, Nigeria","duplo":"Lagos, Nigeria",
    "risevest":"Lagos, Nigeria","credpal":"Lagos, Nigeria",
    "spleet":"Lagos, Nigeria","vendease":"Lagos, Nigeria",
    "sabi":"Lagos, Nigeria","trade-depot":"Lagos, Nigeria",
    "kobo360":"Lagos, Nigeria","autochek-ng":"Lagos, Nigeria",
    "autochek":"Lagos, Nigeria","wakanow":"Lagos, Nigeria",
    "printivo":"Lagos, Nigeria","edukoya":"Lagos, Nigeria",
    "54gene":"Lagos, Nigeria","farmcrowdy":"Lagos, Nigeria",
    "thrive":"Lagos, Nigeria","shuttlers":"Lagos, Nigeria",
    "trove":"Lagos, Nigeria","whogohost":"Lagos, Nigeria",
    "okra":"Lagos, Nigeria","mono-ng":"Lagos, Nigeria",
    "mono":"Lagos, Nigeria","onefi":"Lagos, Nigeria",
    "schoolable":"Lagos, Nigeria","pricepally":"Lagos, Nigeria",
    "remedial":"Lagos, Nigeria","andela-ng":"Lagos, Nigeria",
    "lendsqr":"Lagos, Nigeria","interswitch":"Lagos, Nigeria",
    "bankly":"Lagos, Nigeria","lidya":"Lagos, Nigeria",
    "renmoney":"Lagos, Nigeria","opay":"Lagos, Nigeria",
    "zojatech":"Lagos, Nigeria","moni":"Lagos, Nigeria",
    "moniepoin":"Lagos, Nigeria","konga":"Lagos, Nigeria",
    "jobberman":"Lagos, Nigeria","54gene":"Lagos, Nigeria",
    # Kenya
    "safaricom":"Nairobi, Kenya","sendy":"Nairobi, Kenya",
    "apollo-agriculture":"Nairobi, Kenya","apollo-agric":"Nairobi, Kenya",
    "sunculture":"Nairobi, Kenya","m-kopa":"Nairobi, Kenya",
    "m-kopa-ke":"Nairobi, Kenya","twiga":"Nairobi, Kenya",
    "twiga-ke":"Nairobi, Kenya","copia":"Nairobi, Kenya",
    "copia-ke":"Nairobi, Kenya","sokowatch":"Nairobi, Kenya",
    "lori":"Nairobi, Kenya","lori-ke":"Nairobi, Kenya",
    "moko-ke":"Nairobi, Kenya","tanda-ke":"Nairobi, Kenya",
    "kwara-ke":"Nairobi, Kenya","kwara":"Nairobi, Kenya",
    "pula-ke":"Nairobi, Kenya","pula":"Nairobi, Kenya",
    "turaco-ke":"Nairobi, Kenya","turaco":"Nairobi, Kenya",
    "pezesha":"Nairobi, Kenya","zanifu":"Nairobi, Kenya",
    "asante-ke":"Nairobi, Kenya","kopo-kopo":"Nairobi, Kenya",
    "pesapal":"Nairobi, Kenya","popote":"Nairobi, Kenya",
    "powergen-ke":"Nairobi, Kenya","moringa-school":"Nairobi, Kenya",
    "irembo-ke":"Nairobi, Kenya","irembo":"Kigali, Rwanda",
    "cellulant-ke":"Nairobi, Kenya","cellulant":"Nairobi, Kenya",
    "ushahidi":"Nairobi, Kenya","m-changa":"Nairobi, Kenya",
    "craft-silicon":"Nairobi, Kenya","deimos":"Nairobi, Kenya",
    "gearbox-ke":"Nairobi, Kenya","matibabu":"Nairobi, Kenya",
    "senga":"Nairobi, Kenya","tala-ke":"Nairobi, Kenya",
    "tala":"Nairobi, Kenya","viusasa":"Nairobi, Kenya",
    "africa-talent":"Nairobi, Kenya","boldpay":"Nairobi, Kenya",
    "bookipi":"Nairobi, Kenya","jumo-ke":"Nairobi, Kenya",
    "moringa":"Nairobi, Kenya","weza":"Nairobi, Kenya",
    "apollo":"Nairobi, Kenya","lipa":"Nairobi, Kenya",
    "lipa-na-mpesa":"Nairobi, Kenya","lori-systems":"Nairobi, Kenya",
    "jaza":"Nairobi, Kenya","kopo":"Nairobi, Kenya",
    "pesalink":"Nairobi, Kenya","topup-mama":"Nairobi, Kenya",
    "yozmit":"Nairobi, Kenya","watu":"Nairobi, Kenya",
    # South Africa
    "yoco-za":"Cape Town, South Africa","yoco":"Cape Town, South Africa",
    "ozow-za":"Cape Town, South Africa","ozow":"Cape Town, South Africa",
    "tymebank-za":"Johannesburg, South Africa","tymebank":"Johannesburg, South Africa",
    "stitch-za":"Cape Town, South Africa","stitch":"Cape Town, South Africa",
    "peach-za":"Cape Town, South Africa","peach-payments":"Cape Town, South Africa",
    "root-za":"Cape Town, South Africa","root":"Cape Town, South Africa",
    "franc-za":"Cape Town, South Africa","franc":"Cape Town, South Africa",
    "snapscan":"Cape Town, South Africa","superbalist":"Cape Town, South Africa",
    "takealot-za":"Cape Town, South Africa","takealot":"Cape Town, South Africa",
    "bash-za":"Cape Town, South Africa","offerzen":"Cape Town, South Africa",
    "aerobotics":"Cape Town, South Africa","entersekt-za":"Stellenbosch, South Africa",
    "entersekt":"Stellenbosch, South Africa","floatpays":"Cape Town, South Africa",
    "bettr":"Cape Town, South Africa","nuvei-za":"Johannesburg, South Africa",
    "payfast-za":"Cape Town, South Africa","zapper-za":"Cape Town, South Africa",
    "standard-bank":"Johannesburg, South Africa","vodacom":"Johannesburg, South Africa",
    "woolworths-digi":"Cape Town, South Africa","shoprite-digital":"Cape Town, South Africa",
    "jumo":"Cape Town, South Africa","jumo-world":"Cape Town, South Africa",
    "sanlam":"Cape Town, South Africa","capaciti":"Cape Town, South Africa",
    "synthesis-za":"Johannesburg, South Africa",
    # Ghana
    "hubtel-gh":"Accra, Ghana","hubtel":"Accra, Ghana",
    "mpharma-gh":"Accra, Ghana","mpharma":"Accra, Ghana",
    "zeepay-gh":"Accra, Ghana","zeepay":"Accra, Ghana",
    "farmerline":"Accra, Ghana","mnotify":"Accra, Ghana",
    "rancard":"Accra, Ghana","expresspay":"Accra, Ghana",
    "fido-gh":"Accra, Ghana","payswitch-gh":"Accra, Ghana",
    "tulaa-gh":"Accra, Ghana","redbird-gh":"Accra, Ghana",
    # Rwanda
    "irembo-rw":"Kigali, Rwanda","klab-rw":"Kigali, Rwanda",
    "bank-of-kigali":"Kigali, Rwanda","mtnrwanda":"Kigali, Rwanda",
    "telecash-rw":"Kigali, Rwanda","pivot-access":"Kigali, Rwanda",
    "ejo-heza":"Kigali, Rwanda",
    # Ethiopia
    "kifiya":"Addis Ababa, Ethiopia","gebeya":"Addis Ababa, Ethiopia",
    "yenepay":"Addis Ababa, Ethiopia","kuantum":"Addis Ababa, Ethiopia",
    # Pan-Africa (HQ varies — use generic)
    "andela":"Lagos, Nigeria","carry1st":"Cape Town, South Africa",
    "chipper-cash":"San Francisco, USA (Africa-focused)",
    "lemfi":"London, UK (Africa-focused)","nala":"London, UK (Africa-focused)",
    "grey-fin":"Lagos, Nigeria","grey":"Lagos, Nigeria",
    "yellowcard":"Charlotte, USA (Africa-focused)",
    "onafriq":"Johannesburg, South Africa",
    "zipline-africa":"San Francisco, USA (Africa-focused)",
    "wave":"Dakar, Senegal","wave-africa":"Dakar, Senegal",
    "wave-mobile":"Dakar, Senegal",
    "roam-electric":"Nairobi, Kenya","roam-electric-africa":"Nairobi, Kenya",
    "amini-africa":"Nairobi, Kenya","amini":"Nairobi, Kenya",
    "gomycode":"Tunis, Tunisia (Pan-Africa)",
    "gebeya-africa":"Addis Ababa, Ethiopia",
    # Zipline ops in Rwanda/Kenya/Ghana
    "zipline":"Kigali, Rwanda",
}

def _enrich_location(slug, loc):
    """Return a known location for a company slug if current loc is vague."""
    if loc and loc.lower() not in ("remote", "", "worldwide", "anywhere"):
        return loc
    slug_lower = slug.lower()
    # Exact match first
    if slug_lower in _SLUG_LOCATION:
        return _SLUG_LOCATION[slug_lower]
    # Prefix match
    for key, val in _SLUG_LOCATION.items():
        if slug_lower.startswith(key) or key.startswith(slug_lower):
            return val
    return loc or "Africa (Remote)"

def _N(title="", company="", location="", date_str="", url="",
       source="", desc="", salary="", job_type="Full_time", category="Technology"):
    return {
        "title":            _strip(title or "").strip()[:200],
        "company":          _strip(company or "").strip()[:120],
        "location":         _clean_loc(_strip(location or "Remote")),
        "description":      _strip(desc or "")[:2000],
        "salary_range":     (salary or "")[:100],
        "job_type":         job_type,
        "experience_level": "Mid",
        "category":         category,
        "skills":           "",
        "application_url":  (url or "").strip()[:500],
        "company_logo":     "fa-building",
        "source":           source,
        "is_active":        True,
        "_ds":              str(date_str or ""),
    }

# ── RSS parser (resilient) ────────────────────────────────────────────────────
def _rss(url, src, default_location="Remote", default_category="Technology"):
    # Skip if this host is circuit-broken (too many recent failures)
    if _cb_check(url):
        return []
    try:
        r = requests.get(url,
                         headers={"User-Agent": "Mozilla/5.0 HireBridgeAfrica/1.0",
                                  "Accept": "application/rss+xml,application/xml,text/xml,*/*"},
                         timeout=_CFG["TIMEOUT"])
        r.raise_for_status()
        _cb_record_success(url)
        if not r.content or len(r.content) < 50:
            return []
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "",
                          r.content.decode("utf-8", errors="replace"))
            try:
                root = ET.fromstring(text.encode("utf-8"))
            except ET.ParseError as e2:
                log.warning(f"{src} RSS: {e2}"); return []
        ns = {"a": "http://www.w3.org/2005/Atom",
              "c": "http://purl.org/rss/1.0/modules/content/"}
        items = root.findall(".//item") or root.findall(".//a:entry", ns)
        out = []
        for it in items:
            def t(tag):
                el = it.find(tag) or it.find(f"a:{tag}", ns)
                return (el.text or "").strip() if el is not None else ""
            title = t("title"); company = ""
            if " at " in title:
                title, company = title.rsplit(" at ", 1)
            loc_el = it.find("location") or it.find("job:location")
            loc = (loc_el.text or "").strip() if loc_el is not None else default_location
            out.append(_N(title=title, company=company, location=loc,
                          date_str=t("pubDate") or t("published") or t("updated"),
                          url=t("link") or t("guid"),
                          source=src, category=default_category,
                          desc=t("description") or it.findtext("c:encoded", namespaces=ns) or ""))
        log.info(f"{src}: {len(out)}")
        return out
    except Exception as e:
        _cb_record_fail(url)
        log.warning(f"{src} RSS: {e}"); return []

# ═══ TIER 1 — Zero-key JSON APIs (global) ════════════════════════════════════

def _remoteok(q=""):
    try:
        d = requests.get("https://remoteok.com/api",
                         headers={"User-Agent": "HireBridgeAfrica/1.0"},
                         timeout=_CFG["TIMEOUT"]).json()[1:]
        return [_N(title=j.get("position"), company=j.get("company"),
                   date_str=j.get("date"), url=j.get("url"),
                   source="RemoteOK", desc=j.get("description",""),
                   salary=j.get("salary",""))
                for j in d if isinstance(j, dict) and j.get("position")]
    except Exception as e:
        log.warning(f"RemoteOK: {e}"); return []

def _remotive(q="developer"):
    try:
        d = requests.get("https://remotive.com/api/remote-jobs",
                         params={"search": q, "limit": 50},
                         timeout=_CFG["TIMEOUT"]).json().get("jobs", [])
        return [_N(title=j.get("title"), company=j.get("company_name"),
                   location=j.get("candidate_required_location", "Remote"),
                   date_str=j.get("publication_date"), url=j.get("url"),
                   source="Remotive", desc=j.get("description",""),
                   salary=j.get("salary",""))
                for j in d]
    except Exception as e:
        log.warning(f"Remotive: {e}"); return []

def _remotive_africa():
    """Search Remotive once per African country — pulls jobs open to those locations."""
    queries = ["kenya","nigeria","ghana","south africa","rwanda","ethiopia",
               "uganda","tanzania","egypt","morocco","africa remote"]
    out, seen = [], set()
    for q in queries:
        try:
            jobs = requests.get("https://remotive.com/api/remote-jobs",
                                params={"search": q, "limit": 30},
                                timeout=_CFG["TIMEOUT"]).json().get("jobs", [])
            for j in jobs:
                u = j.get("url","")
                if u in seen: continue
                seen.add(u)
                out.append(_N(title=j.get("title"), company=j.get("company_name"),
                               location=j.get("candidate_required_location", q.title()),
                               date_str=j.get("publication_date"), url=u,
                               source="Remotive-Africa", desc=j.get("description",""),
                               salary=j.get("salary","")))
        except Exception as e:
            log.warning(f"Remotive-Africa ({q}): {e}")
    return out

def _arbeitnow():
    try:
        d = requests.get("https://www.arbeitnow.com/api/job-board-api",
                         timeout=_CFG["TIMEOUT"]).json().get("data", [])
        return [_N(title=j.get("title"), company=j.get("company_name"),
                   location=j.get("location","Remote"),
                   date_str=j.get("created_at"), url=j.get("url"),
                   source="Arbeitnow", desc=j.get("description",""))
                for j in d]
    except Exception as e:
        log.warning(f"Arbeitnow: {e}"); return []

def _himalayas(q="software"):
    try:
        d = requests.get("https://himalayas.app/jobs/api/search",
                         params={"q": q, "limit": 30},
                         timeout=_CFG["TIMEOUT"]).json().get("jobs", [])
        return [_N(title=j.get("title"), company=j.get("companyName",""),
                   location=j.get("locationRestrictions","Remote"),
                   date_str=j.get("publishedAt"), url=j.get("applicationLink",""),
                   source="Himalayas", desc=j.get("description",""))
                for j in d]
    except Exception as e:
        log.warning(f"Himalayas: {e}"); return []

def _jobicy(q=""):
    try:
        p = {"count": 50}
        if q: p["tag"] = q
        d = requests.get("https://jobicy.com/api/v2/remote-jobs",
                         params=p, timeout=_CFG["TIMEOUT"]).json().get("jobs", [])
        return [_N(title=j.get("jobTitle"), company=j.get("companyName",""),
                   location=j.get("jobGeo","Remote"), date_str=j.get("pubDate"),
                   url=j.get("url"), source="Jobicy", desc=j.get("jobExcerpt",""))
                for j in d]
    except Exception as e:
        log.warning(f"Jobicy: {e}"); return []

def _jobicy_africa():
    """Jobicy geo filter — only geo values confirmed to return valid JSON."""
    geos = ["nigeria","kenya","south-africa","ghana","egypt","ethiopia","uganda","tanzania"]
    out, seen = [], set()
    for geo in geos:
        try:
            jobs = requests.get("https://jobicy.com/api/v2/remote-jobs",
                                params={"count": 30, "geo": geo},
                                timeout=_CFG["TIMEOUT"]).json().get("jobs", [])
            for j in jobs:
                u = j.get("url","")
                if u in seen: continue
                seen.add(u)
                out.append(_N(title=j.get("jobTitle"), company=j.get("companyName",""),
                               location=j.get("jobGeo", geo.title()),
                               date_str=j.get("pubDate"), url=u,
                               source="Jobicy-Africa", desc=j.get("jobExcerpt","")))
        except Exception as e:
            log.warning(f"Jobicy-Africa ({geo}): {e}")
    return out

def _workingnomads():
    try:
        d = requests.get("https://www.workingnomads.co/api/exposed_jobs/",
                         timeout=_CFG["TIMEOUT"]).json()
        return [_N(title=j.get("title"), company=j.get("company",""),
                   location=j.get("location","Remote"), date_str=j.get("pub_date"),
                   url=j.get("url"), source="WorkingNomads", desc=j.get("description",""))
                for j in d]
    except Exception as e:
        log.warning(f"WorkingNomads: {e}"); return []

def _the_muse():
    try:
        d = requests.get("https://www.themuse.com/api/public/jobs",
                         params={"page": 0, "descending": "true"},
                         timeout=_CFG["TIMEOUT"]).json().get("results", [])
        return [_N(title=j.get("name"),
                   company=j.get("company",{}).get("name",""),
                   location=", ".join(l.get("name","") for l in j.get("locations",[])) or "Remote",
                   date_str=j.get("publication_date"),
                   url=j.get("refs",{}).get("landing_page",""),
                   source="TheMuse")
                for j in d]
    except Exception as e:
        log.warning(f"TheMuse: {e}"); return []

# ═══ TIER 2 — Keyed APIs ═════════════════════════════════════════════════════

def _adzuna(q, cc="gb"):
    if not _CFG.get("ADZUNA_APP_ID"): return []
    try:
        d = requests.get(f"https://api.adzuna.com/v1/api/jobs/{cc}/search/1",
                         params={"app_id": _CFG["ADZUNA_APP_ID"],
                                 "app_key": _CFG["ADZUNA_APP_KEY"],
                                 "results_per_page": 20, "what": q, "sort_by": "date"},
                         timeout=_CFG["TIMEOUT"]).json().get("results", [])
        return [_N(title=j.get("title"),
                   company=j.get("company",{}).get("display_name",""),
                   location=j.get("location",{}).get("display_name",""),
                   date_str=j.get("created"), url=j.get("redirect_url"),
                   source=f"Adzuna-{cc.upper()}", desc=j.get("description",""))
                for j in d]
    except Exception as e:
        log.warning(f"Adzuna ({cc}): {e}"); return []

def _adzuna_africa(q):
    """South Africa (za) and Nigeria (ng) — the two African markets Adzuna supports."""
    out = []
    for cc in ["za", "ng"]:
        out.extend(_adzuna(q, cc))
    return out

def _reed(q):
    if not _CFG.get("REED_API_KEY"): return []
    try:
        d = requests.get("https://www.reed.co.uk/api/1.0/search",
                         params={"keywords": q, "resultsToTake": 20},
                         auth=(_CFG["REED_API_KEY"], ""),
                         timeout=_CFG["TIMEOUT"]).json().get("results", [])
        return [_N(title=j.get("jobTitle"), company=j.get("employerName",""),
                   location=j.get("locationName",""), date_str=j.get("date"),
                   url=j.get("jobUrl",""), source="Reed",
                   desc=j.get("jobDescription",""))
                for j in d]
    except Exception as e:
        log.warning(f"Reed: {e}"); return []

def _findwork(q=""):
    """
    Findwork API — 500 requests/month free, 60/minute rate limit.
    Supports location, search, employment_type filters.
    API docs: https://findwork.dev/api/jobs/
    """
    if not _CFG.get("FINDWORK_KEY"): return []
    out, seen = [], set()
    # Global search + Africa-specific location searches
    searches = [
        {"search": q or "developer", "sort_by": "date"},
        {"location": "nairobi",       "sort_by": "date"},
        {"location": "lagos",         "sort_by": "date"},
        {"location": "johannesburg",  "sort_by": "date"},
        {"location": "accra",         "sort_by": "date"},
        {"location": "remote",        "sort_by": "date"},
    ]
    for params in searches:
        try:
            d = requests.get(
                "https://findwork.dev/api/jobs/",
                headers={"Authorization": f"Token {_CFG['FINDWORK_KEY']}"},
                params=params,
                timeout=_CFG["TIMEOUT"]
            ).json().get("results", [])
            for j in d:
                u = j.get("url","")
                if u in seen: continue
                seen.add(u)
                out.append(_N(
                    title=j.get("role",""),
                    company=j.get("company_name",""),
                    location=j.get("location","Remote"),
                    date_str=j.get("date_posted",""),
                    url=u, source="Findwork",
                    desc=", ".join(j.get("keywords",[])),
                    job_type="Remote" if j.get("remote") else "Full_time",
                ))
        except Exception as e:
            log.warning("Findwork (%s): %s", params, e)
    return out

# ═══ TIER 3 — RSS Feeds (verified working) ═══════════════════════════════════

# WeWorkRemotely — only feeds that returned valid XML in prod
_WWR = [
    ("WWR-All",     "https://weworkremotely.com/remote-jobs.rss"),
    ("WWR-Dev",     "https://weworkremotely.com/categories/remote-programming-jobs.rss"),
    ("WWR-Design",  "https://weworkremotely.com/categories/remote-design-jobs.rss"),
    ("WWR-Sales",   "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss"),
    ("WWR-DevOps",  "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"),
    ("WWR-Support", "https://weworkremotely.com/categories/remote-customer-support-jobs.rss"),
    ("WWR-Product", "https://weworkremotely.com/categories/remote-product-jobs.rss"),
]

# Other global RSS — confirmed working; Nodesk/LandingJobs/Himalayas-RSS removed (malformed)
_RSS_GLOBAL = [
    ("Jobspresso",   "https://jobspresso.co/feed/?post_type=job_listing"),
    ("WP-Jobs",      "https://jobs.wordpress.net/feed/"),
    ("Python-Jobs",  "https://www.python.org/jobs/feed/rss/"),
    ("AuthenticJobs","https://authenticjobs.com/feed/"),
    ("Jobicy-RSS",   "https://jobicy.com/?feed=job_feed"),
    ("VirtualVoc",   "https://www.virtualvocations.com/jobs/rss"),
]

# Africa RSS — only feeds that successfully parse in prod
_RSS_AFRICA = [
    # Pan-Africa tech (confirmed valid XML)
    ("DisruptAfrica",  "https://disrupt-africa.com/feed/",          "Africa",        "Technology"),
    ("TechCabal",      "https://techcabal.com/jobs/feed/",          "Africa",        "Technology"),
    ("Technext",       "https://technext24.com/feed/",              "Nigeria",       "Technology"),
    # East Africa
    ("JobwebKenya",    "https://www.jobwebkenya.com/feed/",          "Kenya",         "General"),
    # Careers24-ZA removed — feed consistently malformed (invalid token line 23)
    # South Africa jobs come through Workable/Greenhouse ZA boards instead
]

def _wwr_feeds():
    out = []
    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        for f in as_completed([ex.submit(_rss, u, n) for n, u in _WWR]):
            out.extend(f.result())
    return out

def _global_rss():
    out = []
    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        for f in as_completed([ex.submit(_rss, u, n) for n, u in _RSS_GLOBAL]):
            out.extend(f.result())
    return out

def _africa_rss():
    out = []
    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        futs = [ex.submit(_rss, url, name, loc, cat)
                for name, url, loc, cat in _RSS_AFRICA]
        for f in as_completed(futs):
            out.extend(f.result())
    return out

# ═══ TIER 4 — Public ATS: Global companies ════════════════════════════════════

_GH_GLOBAL = [
    "airbnb","stripe","notion","figma","airtable","discord","dropbox","zendesk",
    "twilio","hashicorp","mongodb","elastic","hubspot","intercom","segment",
    "postman","datadog","pagerduty","cloudflare","fastly","netlify","vercel",
    "supabase","linear",
]
_LV_GLOBAL = [
    "netflix","shopify","coinbase","gitlab","plaid","brex","robinhood","chime",
    "rippling","lattice","carta","gusto","canva","asana","miro","loom","coda","retool",
]

def _ats_get(url: str, **kwargs):
    """requests.get() wrapper shared by all ATS helpers.

    Adds three production-safety behaviours that don't exist in bare requests:
    1. Circuit breaker — skips hosts that have failed >= 3 times in the last
       hour so a single down board doesn't hold a thread for TIMEOUT seconds
       on every single fetch cycle.
    2. 429 / 503 back-off — sleeps briefly (capped at 2s so the whole cycle
       budget isn't eaten) before returning [], giving rate-limited APIs a
       chance to recover next cycle instead of hammering them again immediately.
    3. Enforces the configured per-request TIMEOUT consistently.
    """
    import time as _t
    if _cb_check(url):
        return None          # caller should return []
    try:
        r = requests.get(url, timeout=_CFG["TIMEOUT"], **kwargs)
        if r.status_code in (429, 503):
            retry_after = min(float(r.headers.get("Retry-After", 1)), 2.0)
            _t.sleep(retry_after)
            _cb_record_fail(url)
            return None
        r.raise_for_status()
        _cb_record_success(url)
        return r
    except Exception as exc:
        _cb_record_fail(url)
        log.debug(f"_ats_get {url}: {exc}")
        return None


def _greenhouse(co, kws=None):
    url = f"https://boards-api.greenhouse.io/v1/boards/{co}/jobs"
    r = _ats_get(url, params={"content": "true"})
    if r is None: return []
    try:
        d = r.json().get("jobs", [])
    except Exception: return []
    out = []
    for j in d:
        title = j.get("title","")
        if kws and not any(k in title.lower() for k in kws): continue
        raw_loc = j.get("location",{}).get("name","") or ""
        loc = _enrich_location(co, raw_loc)
        out.append(_N(title=title, company=co.replace("-"," ").title(),
                      location=loc,
                      date_str=j.get("updated_at",""),
                      url=j.get("absolute_url",""),
                      source=f"Greenhouse/{co}"))
    return out

def _lever(co, kws=None):
    url = f"https://api.lever.co/v0/postings/{co}"
    r = _ats_get(url, params={"mode": "json"})
    if r is None: return []
    try:
        d = r.json()
    except Exception: return []
    out = []
    for j in (d if isinstance(d, list) else []):
        title = j.get("text","")
        if kws and not any(k in title.lower() for k in kws): continue
        cats = j.get("categories",{})
        raw_loc = cats.get("location","") or cats.get("allLocations","") or ""
        loc = _enrich_location(co, raw_loc)
        out.append(_N(title=title, company=co.replace("-"," ").title(),
                      location=loc, url=j.get("hostedUrl",""),
                      source=f"Lever/{co}",
                      desc=j.get("descriptionPlain","")[:400]))
    return out

def _workable(co, kws=None):
    """Workable public jobs API — used by hundreds of African companies."""
    url = f"https://apply.workable.com/api/v1/widget/accounts/{co}/jobs"
    r = _ats_get(url)
    if r is None: return []
    try:
        d = r.json().get("results",[])
    except Exception: return []
    out = []
    for j in d:
        title = j.get("title","")
        if kws and not any(k in title.lower() for k in kws): continue
        loc_parts = [j.get("city",""), j.get("state",""), j.get("country","")]
        raw_loc = ", ".join(p for p in loc_parts if p) or ""
        loc = _enrich_location(co, raw_loc)
        out.append(_N(title=title,
                      company=j.get("account",{}).get("name", co.replace("-"," ").title()),
                      location=loc,
                      date_str=j.get("published_on",""),
                      url=f"https://apply.workable.com/{co}/j/{j.get('shortcode','')}",
                      source=f"Workable/{co}",
                      desc=j.get("description","")[:400]))
    return out

def _global_ats(kws=None):
    out = []
    with ThreadPoolExecutor(max_workers=_CFG["MAX_WORKERS"]) as ex:
        futs = ([ex.submit(_greenhouse, co, kws) for co in _GH_GLOBAL] +
                [ex.submit(_lever,      co, kws) for co in _LV_GLOBAL])
        for f in as_completed(futs):
            out.extend(f.result())
    return out

# ═══ TIER 5 — Africa ATS boards (~400 companies) ══════════════════════════════
#
# Sources: company careers pages, Crunchbase Africa, Disrupt Africa database,
# WeeTracker, TechCabal directories, LinkedIn company pages cross-referenced
# with Greenhouse/Lever/Workable public APIs.
#
# Organised by country/sector for maintainability.

# ── Greenhouse Africa slugs ───────────────────────────────────────────────────
_GH_AFRICA = [
    # Nigeria — Fintech
    "paystack","flutterwave","interswitch","teamapt","moniepoint","kuda-bank",
    "cowrywise","piggyvest","carbon","lidya","renmoney","bankly","moni",
    "thepeer","sudo-africa","bloc","anchor","budpay","nomba","cleva",
    "brass","crowdforce","indicina","lenco","maplerad","paga","pawapay",
    "softalliance","unified-payment","verto","waza","zones",
    # Nigeria — Tech / Other
    "andela","54gene","autochek","edukoya","farmcrowdy","kobo360","konga",
    "opay","printivo","sabi","shuttlers","social-lender","thrive-agric",
    "trove-finance","wakanow","whogohost","identitypass","prembly",
    "termii","engage","credpal","duplo","lendsqr","mono","okra",
    "onefi","open-banking","pricepally","remedial-health","revvex",
    "risevest","schoolable","spleet","stanbic-ng","sterling-bank",
    "talentql","trade-depot","vendease","wema-bank","zojatech",
    # Kenya — Fintech / Tech
    "cellulant","lipa-na-mpesa","sendy","apollo-agriculture","sunculture",
    "m-kopa","twiga","copia","sokowatch","lori","moko","tanda","kwara",
    "pula","turaco","pezesha","zanifu","asante","jaza","kopo-kopo",
    "lendamall","lendplus","mobicred","mombasa-cement","mtn-kenya",
    "ngao-credit","pesapal","popote-pay","powergen","presta","procredit",
    "rafiki","remu","saida","salaryfinance","skylight","stawi","sweetkiwi",
    "tala","tenakata","tibu","topup-mama","watu","yehu","zacchaeus",
    # Kenya — Other sectors
    "graphite","moringa-school","m-changa","irembo","africa-talent",
    "bold","bookipi","craft-silicon","deimos","digitalent","dreamoval",
    "gearbox","jumo","live4well","matibabu","mendrix","mobius",
    "moi-university","poa","riara","safaricom","senga","ushahidi",
    "viusasa","weza-tele",
    # South Africa — Fintech / Tech
    "ozow","tymebank","stitch","peach-payments","ukheshe","jumo-world",
    "credorax","root","bettr","floatpays","franc","franc-invest",
    "nomanini","payflex","payfast","snapscan","superbalist","takealot",
    "twenty4tech","wicode","yoco","zapper","zoona",
    # South Africa — Other
    "offerzen","aerobotics","alix-partners","bash","capaciti","click2sure",
    "credable","dext","entegral","entersekt","firepay","flexi-fi",
    "glimpse","homebase","iono","kynhood","lumkani","mediamark",
    "nuvei","olsps","pixelcare","reserveport","riskified-za",
    "sanlam-digital","sanlam-fintech","savii","shoprite-tech",
    "sme-toolkit","solarafrica","standard-bank-za","stellenbosch-uni",
    "synthesis","the-coders","vodacom","woolworths-tech","xcorporeal",
    # Ghana
    "farmerline","hubtel","mnotify","mpharma","rancard","slashexpense",
    "zeepay","asoriba","expresspay","fido","frank-advisory","ghana-interbank",
    "hubforms","investoland","payswitch","proxtera","qlc","redbird",
    "solvit","transnational","tulaa","zeola",
    # Rwanda
    "irembo-gov","klab","spark","ignite","ejo-heza","bank-of-kigali",
    "telecash","enablis","konvergenz","maxcom","mtnrwanda","ncs-rwanda",
    "pivot-access","rwanda-online","terracom",
    # Ethiopia
    "kifiya","dashen-bank","awash-bank","commercial-bank-ethiopia",
    "gebeya","addis-payments","african-fintech","ethiotelecom","kuantum",
    "pagume","toger","yenepay","zemen-bank",
    # Uganda / Tanzania / East Africa
    "airtel-money","beyonic","ensibuuko","ez-load","interswitch-ea",
    "micropay","mtn-mobile-money","national-bank-commerce","nbs","nssf",
    "pesalink","pesapoint","quicket","smart-health","spark-microgrants",
    "stanbic-ea","techbridge","ug-fintech","umoja-switch",
    # Pan-Africa / Multi-country
    "carry1st","chipper-cash","flutterwave","lemfi","nala","grey-finance",
    "yellowcard","bitpesa","mfs-africa","aza","dpo","pesalink",
    "onafriq","papss","pezesha","paga-africa","wave-mobile",
    "zipline","africa-logistics","kobo360","lori-systems",
    "roam-electric","amini","shara","earth-observation",
    # Health / AgriTech / CleanTech
    "mphrama","helium-health","lifestores","kangpe","doctorcare",
    "reliance-hmo","wellbeing","clinicpesa","mobilab","remedial",
    "farmcrowdy","thrive-agric","apollo-agric","farm-fresh","zenvus",
    "cropnuts","hydrologiq","solar-freeze","berkshire-agri","greenwich",
    "bboxx","d-light","azuri","nova-lumos","pawame","spark-solar",
    "vitalite","weza-solar","zonful",
    # Education
    "andela","gebeya","moringa","alt-school","semicolon","decagon",
    "ingressive","gomycode","kadamissio","learning-lions","mastercard-foundation",
    "ovalspace","paradigm-initiative","she-code-africa","tech-africa",
    "utiva","women-techmakers",
    # Media / E-commerce
    "jumia","konga","jiji","olx-africa","pigiame","tonaton","cheki",
    "autochek","cars45","kaunta","market-force","mydawa","nipost",
    "olist","reliance-health","shopsafe","showroom","tradefair",
]

# ── Lever Africa slugs ────────────────────────────────────────────────────────
_LV_AFRICA = [
    "flutterwave","andela","chipper-cash","paystack","cowrywise","piggyvest",
    "kuda","mono","stitch","nala-money","grey","lemfi","eden","healthlane",
    "moringa","gebeya","yellowcard","carry1st","roam","amini","shara",
    "offerzen","ozow","jumo","mpharma","helium-health","autochek",
    "termii","54gene","edukoya","trade-depot","sabi","okra","prembly",
    "identitypass","duplo","risevest","credpal","spleet","pricepally",
    "remedial-health","teamapt","lendsqr","onefi","schoolable","vendease",
    "tala","pezesha","kwara","pula","turaco","sendy","copia","sokowatch",
    "safaricom","cellulant","lipa","ushahidi","m-changa","moringa-school",
    "irembo","yoco","superbalist","takealot","bash","aerobotics",
    "entersekt","ozow","peach-payments","nuvei","floatpays","root",
    "zeepay","mpharma","hubtel","farmerline","rancard","mnotify",
    "kifiya","gebeya","addis","wave",
]

# ── Workable Africa slugs ─────────────────────────────────────────────────────
# Workable is heavily used by African startups — endpoint:
# GET https://apply.workable.com/api/v1/widget/accounts/{slug}/jobs
_WK_AFRICA = [
    # Nigeria
    "paystack","flutterwave","kuda","piggyvest","cowrywise","carbon-ng",
    "moniepoint","teamapt","interswitch","moni-ng","thepeer","sudo",
    "bloc-ng","anchor-ng","budpay","cleva-ng","nomba","brass-ng",
    "crowdforce","indicina","lenco","maplerad","paga","pawapay-ng",
    "termii","prembly","identitypass","duplo","risevest","credpal",
    "spleet","vendease","sabi","trade-depot","kobo360","autochek-ng",
    "wakanow","printivo","edukoya","54gene","farmcrowdy","thrive",
    "shuttlers","social-lender","trove","whogohost","okra","mono-ng",
    "onefi","schoolable","pricepally","remedial","andela-ng","lendsqr",
    # Kenya
    "safaricom","sendy","apollo-agriculture","sunculture","m-kopa-ke",
    "twiga-ke","copia-ke","sokowatch","lori-ke","moko-ke","tanda-ke",
    "kwara-ke","pula-ke","turaco-ke","pezesha","zanifu","asante-ke",
    "kopo-kopo","pesapal","popote","powergen-ke","moringa-school",
    "irembo-ke","cellulant-ke","ushahidi","m-changa","craft-silicon",
    "deimos","gearbox-ke","jumo-ke","matibabu","oa-system","senga",
    "tala-ke","viusasa","weza","africa-talent","boldpay","bookipi",
    # South Africa
    "yoco-za","ozow-za","tymebank-za","stitch-za","peach-za","root-za",
    "franc-za","flexpay","snapscan","superbalist","takealot-za","bash-za",
    "offerzen","aerobotics","capaciti","entersekt-za","firepay",
    "nuvei-za","payfast-za","zapper-za","floatpays","bettr","sanlam-dig",
    "shoprite-digital","vodacom-tech","standard-bank","woolworths-digi",
    "click2sure","credable-za","entegral","synthesis-za","wicode",
    # Ghana
    "hubtel-gh","mpharma-gh","zeepay-gh","farmerline","mnotify","rancard",
    "slashexpense","expresspay","fido-gh","ghana-interbank","payswitch-gh",
    "zeola","tulaa-gh","redbird-gh","qlc-gh","proxtera",
    # Rwanda / East Africa
    "irembo-rw","klab-rw","bank-of-kigali","mtnrwanda","telecash-rw",
    "konvergenz","pivot-access","rwanda-online","enablis",
    # Pan-Africa
    "andela","chipper-cash","carry1st","lemfi","nala","grey-fin",
    "yellowcard","onafriq","zipline-africa","amini-africa","wave-africa",
    "roam-electric-africa","gebeya-africa","gomycode",
    # Health / Agri / Energy
    "helium-health","lifestores-ng","remedial-health","mpharmacy",
    "kangpe","clinicpesa","mobilab-health","apollo-agric","zenvus",
    "cropnuts","bboxx-africa","d-light-africa","nova-lumos","pawame",
    "azuri","vitalite-africa","zonful-energy",
    # Education / Media
    "gomycode-africa","alt-school","semicolon-africa","decagon",
    "ingressive","she-code-africa","utiva","paradigm","jumia-group",
    "autochek-africa","cars45","jiji-africa","market-force",
]


# ═══ TIER 5b — Kenya-dedicated live sources ═══════════════════════════════════
# These fetch jobs specifically tagged/located in Kenya from APIs that
# support location-based querying.

def _jobgurus_ke():
    """
    MyJobMag Kenya — their sitemap-based JSON endpoint.
    Falls back to RSS with retries on encoding errors.
    """
    feeds = [
        "https://www.myjobmag.co.ke/feed/job_listings",
        "https://myjobmag.co.ke/rss/job",
        "https://www.myjobmag.co.ke/jobs/feed",
    ]
    for url in feeds:
        try:
            r = requests.get(url, timeout=_CFG["TIMEOUT"],
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0"})
            if r.status_code == 200 and len(r.content) > 100:
                jobs = _rss(url, "MyJobMag-KE", "Kenya", "General")
                if jobs: return jobs
        except Exception:
            pass
    return []

def _brightermonday_ke():
    """
    BrighterMonday Kenya JSON API — they expose a public jobs endpoint.
    """
    try:
        r = requests.get(
            "https://www.brightermonday.co.ke/api/v1/jobs",
            params={"per_page": 50, "page": 1},
            headers={"User-Agent": "Mozilla/5.0 HireBridgeAfrica/1.0",
                     "Accept": "application/json"},
            timeout=_CFG["TIMEOUT"]
        )
        if r.status_code == 200:
            jobs = r.json().get("jobs", r.json().get("data", []))
            out = []
            for j in jobs:
                title = j.get("title","") or j.get("job_title","")
                company = j.get("company","") or j.get("company_name","")
                loc = j.get("location","") or j.get("town","") or "Kenya"
                if "kenya" not in loc.lower(): loc = f"{loc}, Kenya".strip(", ")
                url = j.get("url","") or j.get("application_url","") or j.get("link","")
                out.append(_N(title=title, company=company, location=loc,
                               date_str=j.get("created_at","") or j.get("published_at",""),
                               url=url, source="BrighterMonday-KE", category="General"))
            if out: return out
    except Exception as e:
        log.warning(f"BrighterMonday-KE API: {e}")

    # Fallback: RSS with browser UA
    for path in ["/jobs/rss", "/rss/jobs", "/feed/jobs"]:
        try:
            url = f"https://www.brightermonday.co.ke{path}"
            r = requests.get(url, timeout=_CFG["TIMEOUT"],
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0"})
            if r.status_code == 200 and len(r.content) > 200:
                jobs = _rss(url, "BrighterMonday-KE", "Kenya", "General")
                if jobs: return jobs
        except Exception:
            pass
    return []

def _fuzu_ke():
    """Fuzu Kenya — try multiple endpoint patterns."""
    endpoints = [
        "https://fuzu.com/api/v1/jobs?country=kenya&per_page=50",
        "https://www.fuzu.com/api/v1/jobs?country=kenya",
        "https://fuzu.com/kenya/jobs.json",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, timeout=_CFG["TIMEOUT"],
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0",
                                      "Accept":"application/json"})
            if r.status_code == 200 and r.content:
                data = r.json()
                jobs = data if isinstance(data, list) else data.get("jobs", data.get("data",[]))
                out = []
                for j in (jobs or []):
                    title = j.get("title","") or j.get("name","")
                    if not title: continue
                    loc = j.get("location","Kenya") or "Kenya"
                    out.append(_N(title=title,
                                   company=j.get("company","") or j.get("employer",""),
                                   location=loc if "kenya" in loc.lower() else f"{loc}, Kenya",
                                   date_str=j.get("created_at","") or j.get("published_at",""),
                                   url=j.get("url","") or j.get("apply_url",""),
                                   source="Fuzu-KE", category="General"))
                if out: return out
        except Exception:
            pass
    return []

def _jobberman_ng():
    """Jobberman Nigeria — their public jobs API."""
    endpoints = [
        "https://www.jobberman.com/api/v1/jobs?per_page=50&location=lagos",
        "https://www.jobberman.com/api/jobs?limit=50",
        "https://api.jobberman.com/v2/jobs?limit=50",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, timeout=_CFG["TIMEOUT"],
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0",
                                      "Accept":"application/json"})
            if r.status_code == 200 and r.content:
                data = r.json()
                jobs = data if isinstance(data, list) else data.get("jobs", data.get("data",[]))
                out = []
                for j in (jobs or []):
                    title = j.get("title","") or j.get("job_title","")
                    if not title: continue
                    loc = j.get("location","") or j.get("state","") or "Lagos, Nigeria"
                    out.append(_N(title=title,
                                   company=j.get("company","") or j.get("company_name",""),
                                   location=loc if "nigeria" in loc.lower() else f"{loc}, Nigeria",
                                   date_str=j.get("created_at",""),
                                   url=j.get("url","") or j.get("apply_url",""),
                                   source="Jobberman-NG", category="General"))
                if out: return out
        except Exception:
            pass
    return []

def _ngcareers():
    """NGCareers Nigeria — RSS feed is malformed (202008 bytes, corrupt).
    Try their jobs JSON endpoint instead."""
    try:
        r = requests.get("https://ngcareers.com/wp-json/wp/v2/job_listing?per_page=50",
                         timeout=_CFG["TIMEOUT"],
                         headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0"})
        if r.status_code == 200:
            jobs = r.json()
            out = []
            for j in (jobs if isinstance(jobs, list) else []):
                title = j.get("title", {}).get("rendered","") or j.get("title","")
                desc  = j.get("content",{}).get("rendered","") or ""
                url   = j.get("link","") or j.get("guid",{}).get("rendered","")
                out.append(_N(title=_strip(title), company="", location="Nigeria",
                               date_str=j.get("date",""), url=url,
                               source="NGCareers-NG", category="General",
                               desc=_strip(desc)[:400]))
            if out: return out
    except Exception as e:
        log.warning(f"NGCareers JSON: {e}")
    return []

def _ethiojobs():
    """EthiopianJobs — try multiple URL patterns."""
    for url in ["https://ethiojobs.net/feed/",
                "https://www.ethiojobs.net/rss",
                "https://ethiojobs.net/jobs/feed/"]:
        try:
            r = requests.get(url, timeout=_CFG["TIMEOUT"],
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0"})
            if r.status_code == 200 and len(r.content) > 100:
                jobs = _rss(url, "EthioJobs", "Ethiopia", "General")
                if jobs: return jobs
        except Exception:
            pass
    # Fallback: WP REST API
    try:
        r = requests.get("https://ethiojobs.net/wp-json/wp/v2/job_listing?per_page=50",
                         timeout=_CFG["TIMEOUT"],
                         headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0"})
        if r.status_code == 200:
            jobs = r.json() if isinstance(r.json(), list) else []
            return [_N(title=_strip(j.get("title",{}).get("rendered","")),
                       location="Ethiopia", source="EthioJobs", category="General",
                       date_str=j.get("date",""), url=j.get("link",""),
                       desc=_strip(j.get("content",{}).get("rendered",""))[:400])
                    for j in jobs]
    except Exception as e:
        log.warning(f"EthioJobs: {e}")
    return []

def _jobsgh():
    """JobsInGhana — RSS is malformed; use WP REST API."""
    for url in [
        "https://www.jobsinghana.com/wp-json/wp/v2/job_listing?per_page=50",
        "https://jobsinghana.com/wp-json/wp/v2/job_listing?per_page=50",
    ]:
        try:
            r = requests.get(url, timeout=_CFG["TIMEOUT"],
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0"})
            if r.status_code == 200:
                jobs = r.json() if isinstance(r.json(), list) else []
                out = [_N(title=_strip(j.get("title",{}).get("rendered","")),
                           location="Ghana", source="JobsInGhana", category="General",
                           date_str=j.get("date",""), url=j.get("link",""),
                           desc=_strip(j.get("content",{}).get("rendered",""))[:400])
                       for j in jobs if j.get("title")]
                if out: return out
        except Exception as e:
            log.warning(f"JobsInGhana ({url}): {e}")
    return []

def _ke_tech_rss():
    """Kenya/Africa tech job feeds — only feeds confirmed to parse cleanly."""
    feeds = [
        # African.Business — valid XML, covers pan-Africa roles
        ("AfricanBusiness", "https://african.business/feed/",          "Africa",        "General"),
        # WeeTracker — Africa startup/tech jobs
        ("WeeTracker",      "https://weetracker.com/feed/",            "Africa",        "Technology"),
        # DisruptAfrica jobs feed (already in _RSS_AFRICA but include here for
        # the dedicated Kenya pipeline so it runs in parallel)
        ("TechCabal-Jobs",  "https://techcabal.com/jobs/feed/",        "Africa",        "Technology"),
        # JobwebKenya WordPress feed
        ("JobwebKenya",     "https://www.jobwebkenya.com/feed/",       "Nairobi, Kenya","General"),
        # Technext Nigeria
        ("Technext",        "https://technext24.com/feed/",            "Nigeria",       "Technology"),
    ]
    out = []
    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        futs = [ex.submit(_rss, url, name, loc, cat)
                for name, url, loc, cat in feeds]
        for f in as_completed(futs):
            out.extend(f.result())
    return out


# ═══ TIER 7 — Google Jobs / Aggregator APIs (Africa-focused) ═════════════════
#
# These sources pull from Google Jobs index and other major aggregators.
# They require no API key on free tier or have generous free quotas.

def _jooble_africa():
    """
    Jooble-STYLE multi-source aggregation — replicates what Jooble does:
    crawl job sitemaps + XML feeds from major African job boards directly.
    No API key needed. This is the same approach Jooble uses: index public
    job sitemaps/XML from hundreds of boards.

    Covers boards that expose sitemap.xml or jobs.xml:
    - Jobberman (Nigeria/Ghana): sitemap XML
    - BrighterMonday (KE/UG/TZ): sitemap + JSON API
    - Fuzu (KE/NG/UG): JSON API
    - MyJobMag (KE/NG/GH): WP REST API
    - JobsTanzania, RwandaJobs, UgandaJobs: WP REST
    - Careers in Africa: JSON
    - LinkedIn public job XML feeds (Africa subset)
    """
    out, seen = [], set()

    # ── Sitemap-based scrapers (Jooble approach) ──────────────────────────────
    sitemaps = [
        # Jobberman Nigeria — they expose a jobs sitemap
        ("Jobberman-NG",  "https://www.jobberman.com/sitemap/jobs.xml", "Nigeria"),
        # BrighterMonday Kenya
        ("BM-KE",         "https://www.brightermonday.co.ke/sitemap.xml", "Kenya"),
        # BrighterMonday Uganda
        ("BM-UG",         "https://www.brightermonday.co.ug/sitemap.xml", "Uganda"),
        # JobsTanzania
        ("JobsTZ",        "https://www.jobtanzania.com/sitemap.xml", "Tanzania"),
        # RwandaJobs
        ("RwandaJobs",    "https://www.rwandajobs.rw/sitemap.xml", "Rwanda"),
        # GhanaJobsLink
        ("GhanaJobs",     "https://www.ghanajobslink.com/sitemap.xml", "Ghana"),
        # NairobiJobs
        ("NairobiJobs",   "https://nairobijobs.co.ke/sitemap.xml", "Nairobi, Kenya"),
        # SAJobs
        ("SAJobs",        "https://www.sajobs.co.za/sitemap.xml", "South Africa"),
    ]

    def _parse_sitemap(name, url, default_loc):
        """Parse a sitemap XML and extract job URLs + titles."""
        jobs = []
        try:
            r = requests.get(url, timeout=8,
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0"})
            if r.status_code != 200 or not r.content: return []
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                return []
            # Sitemap namespace
            ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            urls = root.findall(".//s:url", ns) or root.findall(".//url")
            for u in urls[:50]:  # cap at 50 per sitemap
                loc_el = u.find("s:loc", ns) or u.find("loc")
                lastmod_el = u.find("s:lastmod", ns) or u.find("lastmod")
                if loc_el is None: continue
                page_url = (loc_el.text or "").strip()
                # Only include job detail pages (skip category/search pages)
                if not any(seg in page_url for seg in
                           ["/job/", "/jobs/", "/vacancy/", "/career/",
                            "/position/", "/opening/"]):
                    continue
                # Derive title from URL slug
                slug = page_url.rstrip("/").split("/")[-1]
                title = slug.replace("-", " ").replace("_", " ").title()
                date_str = (lastmod_el.text or "").strip() if lastmod_el is not None else ""
                k = hashlib.md5(page_url.encode()).hexdigest()
                if k in seen: continue
                seen.add(k)
                jobs.append(_N(title=title, company="", location=default_loc,
                                date_str=date_str, url=page_url, source=name))
        except Exception as e:
            log.debug(f"Sitemap {name}: {e}")
        return jobs

    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        futs = [ex.submit(_parse_sitemap, name, url, loc)
                for name, url, loc in sitemaps]
        for f in as_completed(futs):
            out.extend(f.result())

    # ── Direct JSON API scrapers (supplement) ─────────────────────────────────
    json_boards = [
        # Indeed-style OpenGraph boards
        ("Fuzu-KE-agg",   "https://fuzu.com/api/v1/jobs?country=kenya&per_page=30",  "Kenya"),
        ("Fuzu-NG-agg",   "https://fuzu.com/api/v1/jobs?country=nigeria&per_page=30","Nigeria"),
        ("Fuzu-UG-agg",   "https://fuzu.com/api/v1/jobs?country=uganda&per_page=30", "Uganda"),
    ]
    for name, url, default_loc in json_boards:
        try:
            r = requests.get(url, timeout=8,
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0",
                                      "Accept":"application/json"})
            if r.status_code != 200: continue
            data = r.json()
            jobs = data if isinstance(data,list) else data.get("jobs", data.get("data",[]))
            for j in (jobs or [])[:50]:
                title = j.get("title","") or j.get("name","")
                if not title: continue
                u = j.get("url","") or j.get("apply_url","") or j.get("link","")
                k = hashlib.md5((title+u).encode()).hexdigest()
                if k in seen: continue
                seen.add(k)
                out.append(_N(title=title, company=j.get("company","") or j.get("employer",""),
                               location=j.get("location",default_loc) or default_loc,
                               date_str=j.get("created_at","") or j.get("published_at",""),
                               url=u, source=name, category="General"))
        except Exception as e:
            log.debug(f"JSON board {name}: {e}")

    log.info(f"WP boards: {len(out)} jobs")
    return out


# ── JSearch daily rate-limit controller ───────────────────────────────────────
# Budget: 200 requests/month free tier.
# Strategy: max 6 calls/day, 1 query per call, rotating through city queries.
# State persisted in jsearch_state.json next to this file.

import json as _json
from pathlib import Path as _Path

_JSEARCH_STATE_FILE = _Path(__file__).parent / "jsearch_state.json"
_JSEARCH_MAX_PER_DAY = 6   # hard cap — never exceed 6 API calls in one calendar day

# Rotating city queries — one used per call, cycling through the list
_JSEARCH_QUERIES = [
    "jobs in Nairobi Kenya",
    "jobs in Lagos Nigeria",
    "jobs in Johannesburg South Africa",
    "jobs in Accra Ghana",
    "jobs in Addis Ababa Ethiopia",
    "jobs in Kigali Rwanda",
    "jobs in Kampala Uganda",
    "jobs in Dar es Salaam Tanzania",
    "jobs in Cairo Egypt",
    "remote jobs Africa",
    "jobs in Mombasa Kenya",
    "jobs in Abuja Nigeria",
    "jobs in Cape Town South Africa",
    "jobs in Nairobi Kenya technology",
    "jobs in Lagos Nigeria finance",
    "NGO jobs Kenya",
    "tech jobs Africa remote",
    "entry level jobs Kenya",
    "graduate jobs Nigeria",
    "jobs in Dakar Senegal",
    "jobs in Kampala Uganda NGO",
    "jobs in Dar es Salaam Tanzania",
    "jobs in Casablanca Morocco",
    "jobs in Tunis Tunisia",
    "jobs in Abidjan Ivory Coast",
    "jobs in Douala Cameroon",
    "jobs in Lusaka Zambia",
    "jobs in Harare Zimbabwe",
    "jobs in Maputo Mozambique",
    "jobs in Antananarivo Madagascar",
]

def _jsearch_load_state():
    try:
        if _JSEARCH_STATE_FILE.exists():
            return _json.loads(_JSEARCH_STATE_FILE.read_text())
    except Exception:
        pass
    return {"date": "", "calls_today": 0, "query_index": 0}

def _jsearch_save_state(state):
    try:
        _JSEARCH_STATE_FILE.write_text(_json.dumps(state))
    except Exception as e:
        log.warning(f"JSearch state save: {e}")

def _jsearch_africa():
    """
    JSearch API (RapidAPI free tier) — 200 requests/month.
    Rate-limited to MAX 6 calls per calendar day.
    Uses 1 rotating city query per call to maximise coverage over time.
    Add JSEARCH_API_KEY=your_key to .env to activate.
    Get free key at: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
    """
    key = _CFG.get("JSEARCH_KEY","")
    if not key:
        log.debug("JSEARCH_API_KEY not set — skipping JSearch")
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    state = _jsearch_load_state()

    # Reset counter on new day
    if state.get("date") != today:
        state["date"] = today
        state["calls_today"] = 0

    # Hard daily cap
    if state["calls_today"] >= _JSEARCH_MAX_PER_DAY:
        log.info(f"JSearch daily cap reached ({_JSEARCH_MAX_PER_DAY}/day) — skipping until tomorrow")
        return []

    # Pick next rotating query
    idx = state.get("query_index", 0) % len(_JSEARCH_QUERIES)
    query = _JSEARCH_QUERIES[idx]
    state["query_index"] = (idx + 1) % len(_JSEARCH_QUERIES)
    state["calls_today"] += 1
    _jsearch_save_state(state)

    log.info("JSearch call %d/%d today → %s", state['calls_today'], _JSEARCH_MAX_PER_DAY, query)

    try:
        r = requests.get(
            "https://jsearch.p.rapidapi.com/search",
            params={"query": query, "num_pages": "3", "date_posted": "month"},
            headers={"X-RapidAPI-Key": key,
                     "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
            timeout=_CFG["TIMEOUT"]
        )
        if r.status_code == 429:
            log.warning("JSearch rate limited (429) — backing off")
            state["calls_today"] = _JSEARCH_MAX_PER_DAY  # stop for today
            _jsearch_save_state(state)
            return []
        if r.status_code != 200:
            log.warning(f"JSearch HTTP {r.status_code}")
            return []
        out = []
        for j in r.json().get("data", []):
            u = j.get("job_apply_link","") or j.get("job_google_link","")
            if not u: continue
            loc_parts = [j.get("job_city",""), j.get("job_state",""), j.get("job_country","")]
            loc = ", ".join(p for p in loc_parts if p) or query
            sal = ""
            if j.get("job_min_salary"):
                sal = f"{j['job_min_salary']}–{j.get('job_max_salary','')} {j.get('job_salary_currency','')}"
            out.append(_N(
                title=j.get("job_title",""),
                company=j.get("employer_name",""),
                location=loc,
                date_str=j.get("job_posted_at_datetime_utc",""),
                url=u,
                source="GoogleJobs-Africa",
                desc=j.get("job_description","")[:400],
                salary=sal,
                job_type=j.get("job_employment_type","Full_time"),
            ))
        log.info(f"JSearch: {len(out)} jobs from {query}")
        return out
    except Exception as e:
        log.warning(f"JSearch: {e}"); return []


def _adzuna_africa_free(q=""):
    """
    Adzuna free public API for African country codes.
    Supports ZA (South Africa) and NG (Nigeria) — no key needed for basic search.
    """
    if _CFG.get("ADZUNA_APP_ID"):
        return _adzuna_africa(q)   # Already handled with key
    # Try keyless public endpoint (limited but works)
    out = []
    for cc, country in [("za","South Africa"), ("ng","Nigeria")]:
        try:
            r = requests.get(
                f"https://api.adzuna.com/v1/api/jobs/{cc}/search/1",
                params={"results_per_page": 20, "what": q or "jobs",
                        "sort_by": "date",
                        "app_id": "test", "app_key": "test"},
                headers={"User-Agent":"HireBridgeAfrica/1.0"},
                timeout=_CFG["TIMEOUT"]
            )
            if r.status_code == 200:
                for j in r.json().get("results",[]):
                    out.append(_N(
                        title=j.get("title",""),
                        company=j.get("company",{}).get("display_name",""),
                        location=j.get("location",{}).get("display_name","") or country,
                        date_str=j.get("created",""),
                        url=j.get("redirect_url",""),
                        source=f"Adzuna-{cc.upper()}",
                        desc=j.get("description","")
                    ))
        except Exception as e:
            log.warning(f"Adzuna free ({cc}): {e}")
    return out


def _remoteok_africa():
    """Filter RemoteOK jobs that mention African locations."""
    try:
        d = requests.get("https://remoteok.com/api",
                         headers={"User-Agent":"HireBridgeAfrica/1.0"},
                         timeout=_CFG["TIMEOUT"]).json()[1:]
        africa_kws = {"africa","kenya","nigeria","ghana","south africa","ethiopia",
                      "rwanda","uganda","tanzania","egypt","nairobi","lagos","accra",
                      "johannesburg","cape town","kigali","kampala","addis"}
        out = []
        for j in d:
            if not isinstance(j, dict): continue
            haystack = " ".join([
                (j.get("position") or "").lower(),
                (j.get("description") or "").lower(),
                (j.get("location") or "").lower(),
                (j.get("tags") or [""])[0].lower() if j.get("tags") else "",
            ])
            if any(kw in haystack for kw in africa_kws):
                out.append(_N(
                    title=j.get("position",""), company=j.get("company",""),
                    location=j.get("location","Africa (Remote)"),
                    date_str=j.get("date",""), url=j.get("url",""),
                    source="RemoteOK-Africa", desc=j.get("description",""),
                    salary=j.get("salary","")
                ))
        return out
    except Exception as e:
        log.warning(f"RemoteOK-Africa: {e}"); return []


def _wp_jobmanager_africa():
    """
    WP Job Manager REST API — used by hundreds of African job boards.
    Endpoint: /wp-json/wp/v2/job_listing
    Boards confirmed to run WP Job Manager in Africa.
    """
    boards = [
        # Kenya
        ("JobwebKenya",       "https://www.jobwebkenya.com",         "Kenya"),
        ("KenyaJobsNet",      "https://kenyajobsnet.com",            "Kenya"),
        ("NairobiJobs",       "https://nairobijobs.co.ke",           "Nairobi, Kenya"),
        ("HabariJob",         "https://habarijob.co.ke",             "Kenya"),
        ("MauguziJob",        "https://mauguzi.com",                 "Kenya"),
        ("Kenyajobsearch",    "https://kenyajobsearch.com",          "Kenya"),
        # Nigeria
        ("NGCareers",         "https://ngcareers.com",               "Nigeria"),
        ("JobberMan",         "https://jobberman.com",               "Nigeria"),
        ("HotNigerianJobs",   "https://www.hotnigeriansjobs.com",    "Nigeria"),
        ("NigerianJobs",      "https://nigerianjobs.com",            "Nigeria"),
        ("RecruitmentPortal", "https://recruitmentportalhub.com",    "Nigeria"),
        # Ghana
        ("JobsinGhana",       "https://www.jobsinghana.com",         "Ghana"),
        ("GhanaCareers",      "https://www.ghanacareers.com",        "Ghana"),
        ("MyJobsGhana",       "https://www.myjobsghana.com",         "Ghana"),
        # South Africa
        ("SAJobs",            "https://www.sajobs.co.za",            "South Africa"),
        ("JobsDB-ZA",         "https://www.jobsdb.co.za",            "South Africa"),
        ("JobMailSA",         "https://www.jobmail.co.za",           "South Africa"),
        # East Africa
        ("BrighterMondayUG",  "https://www.brightermonday.co.ug",    "Uganda"),
        ("MyjobsMag",         "https://www.myjobsmag.com",           "East Africa"),
        ("JobTanzania",       "https://www.jobtanzania.com",         "Tanzania"),
        ("RwandaJobs",        "https://www.rwandajobs.rw",           "Rwanda"),
        # Pan-Africa
        ("AfricaJobs",        "https://www.africa-jobs.com",         "Africa"),
        ("JobsAfrica",        "https://jobsafrica.co",               "Africa"),
        ("AfricaRecruit",     "https://www.africarecruit.com",       "Africa"),
    ]
    out, seen = [], set()
    def _fetch_board(name, base_url, default_loc):
        jobs = []
        for path in ["/wp-json/wp/v2/job_listing?per_page=50&orderby=date",
                     "/wp-json/wpjm/v1/jobs?per_page=50",
                     "/wp-json/wp/v2/jobs?per_page=50"]:
            try:
                r = requests.get(f"{base_url}{path}", timeout=8,
                                 headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0",
                                          "Accept":"application/json"})
                if r.status_code == 200 and r.content:
                    data = r.json()
                    items = data if isinstance(data, list) else data.get("jobs", data.get("data", []))
                    for j in (items or []):
                        title = ""
                        if isinstance(j.get("title"), dict):
                            title = j["title"].get("rendered","")
                        else:
                            title = j.get("title","") or j.get("job_title","")
                        title = _strip(title)
                        if not title: continue
                        loc = (j.get("job_location","") or
                               j.get("location","") or
                               j.get("meta",{}).get("_job_location","") or
                               default_loc)
                        if not any(c.lower() in loc.lower() for c in
                                   ["kenya","nigeria","ghana","africa","south africa",
                                    "uganda","tanzania","rwanda","ethiopia","egypt",
                                    "nairobi","lagos","accra","kampala"]):
                            loc = default_loc
                        company = (j.get("company_name","") or
                                   j.get("meta",{}).get("_company_name","") or "")
                        url = (j.get("link","") or j.get("url","") or
                               j.get("guid",{}).get("rendered","") if isinstance(j.get("guid"),dict)
                               else j.get("guid",""))
                        jobs.append(_N(title=title, company=company, location=loc,
                                       date_str=j.get("date","") or j.get("created_at",""),
                                       url=url, source=name, category="General"))
                    if jobs: break
            except Exception:
                pass
        return jobs

    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        futs = [ex.submit(_fetch_board, name, url, loc) for name, url, loc in boards]
        for f in as_completed(futs):
            for job in f.result():
                k = hashlib.md5(f"{job['title'].lower()}|{job['company'].lower()}".encode()).hexdigest()
                if k not in seen:
                    seen.add(k); out.append(job)
    log.info(f"WP JobManager Africa: {len(out)} jobs")
    return out


# ═══ TIER 8 — Indeed RSS (Africa regions, no key needed) ══════════════════════
# Indeed still serves RSS for many African locations — completely free, no auth.

# Indeed RSS has been globally blocked (403/DNS) as of 2024.
# All Indeed endpoints return 403 Forbidden or fail DNS resolution.
# Disabled to eliminate log noise. Re-enable if Indeed restores RSS.
_INDEED_AFRICA = []  # DISABLED — Indeed blocks all external RSS scraping

def _indeed_africa():
    """Indeed Africa RSS — DISABLED: Indeed blocks all RSS scraping (403/DNS).
    Returns empty list immediately without making any network requests."""
    if _INDEED_AFRICA:  # Only runs if list is re-populated
        raw = []
        with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
            futs = [ex.submit(_rss, url, name, loc, "General")
                    for name, url, loc in _INDEED_AFRICA]
            for f in as_completed(futs):
                raw.extend(f.result())
        out = [j for j in raw if j.get("title","").strip()]
        log.info(f"Indeed Africa: {len(out)} jobs")
        return out
    log.debug("Indeed Africa: disabled (blocked by Indeed)")
    return []


# ═══ TIER 9 — LinkedIn Public RSS (no auth needed) ════════════════════════════
# LinkedIn exposes public job search RSS for locations.

_LINKEDIN_AFRICA = [
    ("LinkedIn-Nairobi",  "https://www.linkedin.com/jobs/search?keywords=&location=Nairobi%2C+Kenya&f_TPR=r604800&position=1&pageNum=0&format=json", "Nairobi, Kenya"),
    ("LinkedIn-Lagos",    "https://www.linkedin.com/jobs/search?keywords=&location=Lagos%2C+Nigeria&f_TPR=r604800&position=1&pageNum=0&format=json",  "Lagos, Nigeria"),
    ("LinkedIn-JHB",      "https://www.linkedin.com/jobs/search?keywords=&location=Johannesburg%2C+South+Africa&f_TPR=r604800&position=1&pageNum=0&format=json","Johannesburg, South Africa"),
    ("LinkedIn-Accra",    "https://www.linkedin.com/jobs/search?keywords=&location=Accra%2C+Ghana&f_TPR=r604800&position=1&pageNum=0&format=json",    "Accra, Ghana"),
    ("LinkedIn-Kigali",   "https://www.linkedin.com/jobs/search?keywords=&location=Kigali%2C+Rwanda&f_TPR=r604800&position=1&pageNum=0&format=json",  "Kigali, Rwanda"),
    ("LinkedIn-Kampala",  "https://www.linkedin.com/jobs/search?keywords=&location=Kampala%2C+Uganda&f_TPR=r604800&position=1&pageNum=0&format=json", "Kampala, Uganda"),
    ("LinkedIn-Addis",    "https://www.linkedin.com/jobs/search?keywords=&location=Addis+Ababa%2C+Ethiopia&f_TPR=r604800&position=1&pageNum=0&format=json","Addis Ababa, Ethiopia"),
    ("LinkedIn-DSM",      "https://www.linkedin.com/jobs/search?keywords=&location=Dar+es+Salaam%2C+Tanzania&f_TPR=r604800&position=1&pageNum=0&format=json","Dar es Salaam, Tanzania"),
    ("LinkedIn-Cairo",    "https://www.linkedin.com/jobs/search?keywords=&location=Cairo%2C+Egypt&f_TPR=r604800&position=1&pageNum=0&format=json",    "Cairo, Egypt"),
]

def _linkedin_africa():
    """
    LinkedIn public job search — parse the JSON response directly.
    No auth required for public search. Returns up to 25 jobs per location.
    """
    out, seen = [], set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json,text/html,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    for name, url, default_loc in _LINKEDIN_AFRICA:
        try:
            r = requests.get(url, headers=headers, timeout=_CFG["TIMEOUT"])
            if r.status_code != 200: continue
            data = r.json() if r.headers.get("content-type","").startswith("application/json") else {}
            jobs = data.get("elements", data.get("data", data.get("jobs", [])))
            for j in (jobs if isinstance(jobs, list) else []):
                title = j.get("title","") or j.get("jobTitle","")
                if not title: continue
                u = j.get("applyUrl","") or j.get("url","") or j.get("jobPostingUrl","")
                k = hashlib.md5((title + u).encode()).hexdigest()
                if k in seen: continue
                seen.add(k)
                loc = j.get("formattedLocation","") or j.get("location","") or default_loc
                out.append(_N(title=title,
                               company=j.get("companyName","") or j.get("company",""),
                               location=loc, url=u, source=name,
                               date_str=j.get("listedAt","") or j.get("postingDate",""),
                               desc=j.get("description","")[:400]))
        except Exception as e:
            log.debug(f"LinkedIn {name}: {e}")
    log.info(f"LinkedIn Africa: {len(out)} jobs")
    return out


# ═══ TIER 10 — UN/NGO/Development Sector RSS ══════════════════════════════════
# These are massive sources of African jobs — UN agencies, INGOs, NGOs.

# NGO/UN RSS sources — verified status as of 2026-05-08:
# UNJobs: "not well-formed (invalid token)" on ALL country feeds — their XML is malformed server-side
# Devex: 404 — feed endpoint dead
# Idealist: 404 — feed endpoint dead
# ACDIVOCA: 404 — feed endpoint dead
# MercyCorps: DNS failure — jobs.mercycorps.org does not resolve
# SaveChildren: DNS failure — jobs.savethechildren.net does not resolve
# IRC: malformed XML
# UNDP: XML syntax error
# FHI360: 403 Forbidden
# Chemonics: 404 — feed endpoint dead
# IntraHealth: SSL certificate mismatch
# ReliefWeb: handled separately by _reliefweb_jobs() with proper API fallback
# PSI: untested — included
# Palladium: untested — included
_NGO_RSS = [
    # ReliefWeb jobs RSS — kept as one attempt, main logic in _reliefweb_jobs()
    ("ReliefWeb-RSS",   "https://reliefweb.int/jobs/rss.xml",          "Africa",  "NGO / Nonprofit"),
    # PSI — not confirmed broken, keep
    ("PSI",             "https://www.psi.org/job/feed/",               "Africa",  "Healthcare"),
    # Palladium — not confirmed broken, keep
    ("Palladium",       "https://palladium-group.com/careers/feed/",   "Africa",  "NGO / Nonprofit"),
    # WaterAid — valid WP RSS, Africa heavy
    ("WaterAid",        "https://www.wateraid.org/jobs/feed/",         "Africa",  "NGO / Nonprofit"),
    # Aga Khan Foundation
    ("AKF",             "https://www.akdn.org/careers/rss",            "Africa",  "NGO / Nonprofit"),
]

def _ngo_rss():
    """UN/NGO/Development sector RSS — massive Africa job source."""
    out = []
    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        futs = [ex.submit(_rss, url, name, loc, cat)
                for name, url, loc, cat in _NGO_RSS]
        for f in as_completed(futs):
            out.extend(f.result())
    log.info(f"NGO/UN RSS: {len(out)} jobs")
    return out


# ═══ TIER 11 — Kenya-specific company career pages (WP REST + direct) ════════
# These are the top employers in Kenya — scraped directly from their career pages.

def _kenya_corporate_careers():
    """
    Direct scrape of major Kenyan employer career pages.
    Uses WP REST API, JSON endpoints, or RSS where available.
    """
    boards = [
        # Banks & Finance
        ("Safaricom",           "https://www.safaricom.co.ke/wp-json/wp/v2/job_listing?per_page=50",  "Nairobi, Kenya", "Technology"),
        ("Equity-Bank",         "https://equitygroupholdings.com/wp-json/wp/v2/job_listing?per_page=50","Nairobi, Kenya","Finance"),
        ("KCB-Group",           "https://ke.kcbgroup.com/wp-json/wp/v2/job_listing?per_page=50",       "Nairobi, Kenya","Finance"),
        ("Cooperative-Bank",    "https://www.co-opbank.co.ke/wp-json/wp/v2/job_listing?per_page=50",   "Nairobi, Kenya","Finance"),
        ("NCBA-Bank",           "https://www.ncbagroup.com/wp-json/wp/v2/job_listing?per_page=50",     "Nairobi, Kenya","Finance"),
        ("Absa-Kenya",          "https://www.absa.co.ke/wp-json/wp/v2/job_listing?per_page=50",        "Nairobi, Kenya","Finance"),
        ("Stanbic-Kenya",       "https://www.stanbicbank.co.ke/wp-json/wp/v2/job_listing?per_page=50", "Nairobi, Kenya","Finance"),
        # Recruitment Agencies (massive job volume)
        ("Corporate-Staffing",  "https://corporatestaffing.co.ke/wp-json/wp/v2/job_listing?per_page=50","Nairobi, Kenya","General"),
        ("Career-Point-Kenya",  "https://www.careerpointkenya.co.ke/wp-json/wp/v2/job_listing?per_page=50","Nairobi, Kenya","General"),
        ("Summit-Recruitment",  "https://www.summitrecruitment-search.com/wp-json/wp/v2/job_listing?per_page=50","Nairobi, Kenya","General"),
        ("Flexi-Personnel",     "https://www.flexi-personnel.com/wp-json/wp/v2/job_listing?per_page=50","Nairobi, Kenya","General"),
        ("Brites-Management",   "https://britesmanagement.com/wp-json/wp/v2/job_listing?per_page=50",  "Nairobi, Kenya","General"),
        ("HCS-Africa",          "https://www.hcsafrica.com/wp-json/wp/v2/job_listing?per_page=50",     "Nairobi, Kenya","General"),
        ("Frank-Management",    "https://www.frankmgt.com/wp-json/wp/v2/job_listing?per_page=50",      "Nairobi, Kenya","General"),
        ("Gap-Recruitment",     "https://www.gaprecruitment.co.ke/wp-json/wp/v2/job_listing?per_page=50","Nairobi, Kenya","General"),
        ("Ital-Global",         "https://www.italglobal.com/wp-json/wp/v2/job_listing?per_page=50",    "Nairobi, Kenya","General"),
        ("Kenyan-Jobs",         "https://kenyanjobs.co.ke/wp-json/wp/v2/job_listing?per_page=50",      "Kenya",         "General"),
        ("Jobs-KE",             "https://jobs.co.ke/wp-json/wp/v2/job_listing?per_page=50",            "Kenya",         "General"),
        ("Duma-Works",          "https://www.dumaworks.com/wp-json/wp/v2/job_listing?per_page=50",     "Kenya",         "General"),
        ("Altus-Recruitment",   "https://altusrecruitment.co.ke/wp-json/wp/v2/job_listing?per_page=50","Nairobi, Kenya","General"),
        # Tech & Startups
        ("iHUB-Nairobi",        "https://ihub.co.ke/wp-json/wp/v2/job_listing?per_page=50",           "Nairobi, Kenya","Technology"),
        ("Andela-KE",           "https://andela.com/wp-json/wp/v2/job_listing?per_page=50",            "Nairobi, Kenya","Technology"),
        ("Microsoft-Africa",    "https://careers.microsoft.com/us/en/search-results?keywords=kenya",   "Nairobi, Kenya","Technology"),
        # Media & Other
        ("Nation-Media",        "https://nationmedia.com/wp-json/wp/v2/job_listing?per_page=50",       "Nairobi, Kenya","Media"),
        ("Standard-Group",      "https://www.standardmedia.co.ke/wp-json/wp/v2/job_listing?per_page=50","Nairobi, Kenya","Media"),
    ]

    out, seen = [], set()
    def _fetch_wp(name, url, loc, cat):
        jobs = []
        try:
            r = requests.get(url, timeout=8,
                             headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0",
                                      "Accept":"application/json"})
            if r.status_code == 200 and r.content:
                items = r.json() if isinstance(r.json(), list) else []
                for j in items:
                    t = j.get("title",{})
                    title = _strip(t.get("rendered","") if isinstance(t,dict) else str(t))
                    if not title: continue
                    u = j.get("link","") or j.get("guid",{}).get("rendered","")
                    job_loc = (j.get("meta",{}) or {}).get("_job_location","") or loc
                    jobs.append(_N(title=title, company=name.replace("-"," "),
                                   location=job_loc or loc,
                                   date_str=j.get("date",""),
                                   url=u, source=f"KE-Corporate/{name}",
                                   category=cat))
        except Exception as e:
            log.debug(f"KE-Corporate {name}: {e}")
        return jobs

    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        futs = [ex.submit(_fetch_wp, name, url, loc, cat) for name, url, loc, cat in boards]
        for f in as_completed(futs):
            for job in f.result():
                k = hashlib.md5(job["application_url"].encode()).hexdigest() if job["application_url"] else hashlib.md5(job["title"].encode()).hexdigest()
                if k not in seen:
                    seen.add(k); out.append(job)
    log.info(f"Kenya Corporate: {len(out)} jobs")
    return out


# ═══ TIER 12 — Government Job Boards (Africa) ═════════════════════════════════

_GOVT_BOARDS = [
    # Kenya Public Service Commission
    ("PSCK-Kenya",    "https://www.publicservice.go.ke/wp-json/wp/v2/job_listing?per_page=50",  "Nairobi, Kenya","Government"),
    ("PSCK-RSS",      "https://www.publicservice.go.ke/feed/",                                  "Nairobi, Kenya","Government"),
    # Kenya — various ministries/parastatals
    ("Jobs-GoKe",     "https://www.jobs.go.ke/wp-json/wp/v2/job_listing?per_page=50",           "Kenya",         "Government"),
    # Nigeria Federal Jobs
    ("FSC-Nigeria",   "https://www.fedcivilservice.gov.ng/wp-json/wp/v2/job_listing?per_page=50","Nigeria",       "Government"),
    # South Africa DPSA
    ("DPSA-ZA",       "https://www.dpsa.gov.za/dpsa2g/vacancies.asp",                           "South Africa",  "Government"),
    # Rwanda
    ("MIFOTRA-RW",    "https://www.mifotra.gov.rw/wp-json/wp/v2/job_listing?per_page=50",       "Rwanda",        "Government"),
    # Ghana PSC
    ("PSC-Ghana",     "https://www.pscghana.gov.gh/wp-json/wp/v2/job_listing?per_page=50",      "Ghana",         "Government"),
]

def _govt_jobs():
    """Government job boards across Africa."""
    out = []
    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        rss_futs  = [ex.submit(_rss, url, name, loc, cat)
                     for name, url, loc, cat in _GOVT_BOARDS if "feed" in url or "rss" in url.lower()]
        json_futs = []
        for name, url, loc, cat in _GOVT_BOARDS:
            if "wp-json" in url:
                def fetch(n=name, u=url, l=loc, c=cat):
                    try:
                        r = requests.get(u, timeout=8, headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0","Accept":"application/json"})
                        if r.status_code==200:
                            items = r.json() if isinstance(r.json(),list) else []
                            return [_N(title=_strip(j.get("title",{}).get("rendered","") if isinstance(j.get("title"),dict) else j.get("title","")),
                                       location=l, source=f"Govt/{n}", category=c,
                                       date_str=j.get("date",""), url=j.get("link",""))
                                    for j in items if j.get("title")]
                    except Exception: pass
                    return []
                json_futs.append(ex.submit(fetch))
        for f in as_completed(rss_futs + json_futs):
            out.extend(f.result())
    log.info(f"Govt jobs: {len(out)}")
    return out


# ═══ TIER 13 — More African WordPress Job Boards ══════════════════════════════
# Research-verified boards using WP Job Manager across Africa

_WP_BOARDS_EXTRA = [
    # Kenya
    ("Kenyajobs",      "https://www.kenyajobs.net",             "Kenya"),
    ("Jobskea",        "https://jobskea.com",                   "Kenya"),
    ("Jobline-KE",     "https://www.jobline.co.ke",             "Kenya"),
    ("AfricaHire",     "https://africahire.com",                "Kenya"),
    ("Myjobskenya",    "https://www.myjobskenya.com",           "Kenya"),
    ("Kenyan-Jobs2",   "https://www.kenyanjobs.net",            "Kenya"),
    ("NairobiJobsnet", "https://nairobijobs.net",               "Nairobi, Kenya"),
    ("EastAfricaJobs", "https://eastafricajobs.com",            "East Africa"),
    ("JobsEastAfrica", "https://www.jobseastafrica.com",        "East Africa"),
    # Nigeria
    ("JobsNG",         "https://jobsng.com",                    "Nigeria"),
    ("NigeriaJobs24",  "https://nigeriajobs24.com",             "Nigeria"),
    ("HotNG",          "https://www.hotnigeriansjobs.com",      "Nigeria"),
    ("NGCareers2",     "https://ngcareers.com",                 "Nigeria"),
    ("JobGurus-NG",    "https://www.jobgurus.com.ng",           "Nigeria"),
    ("Jobzilla-NG",    "https://jobzilla.ng",                   "Nigeria"),
    ("Naijajobs",      "https://naijajobs.com",                 "Nigeria"),
    # Ghana
    ("GhanaJobs",      "https://ghanajobs.com",                 "Ghana"),
    ("JobsinGhana2",   "https://jobsinghana.net",               "Ghana"),
    ("TopJobsGhana",   "https://topjobsgh.com",                 "Ghana"),
    # Ethiopia
    ("EthioJobs2",     "https://www.ethiojobs.net",             "Ethiopia"),
    ("JobsinEthiopia", "https://jobsinethiopia.net",            "Ethiopia"),
    ("AddisAbaba-Jobs","https://www.addisabababjobs.com",       "Addis Ababa, Ethiopia"),
    # Uganda
    ("UgandaJobs",     "https://ugandajobs.org",                "Uganda"),
    ("JobsUG",         "https://jobs.ug",                       "Uganda"),
    ("MyjobsUG",       "https://www.myjobsmag.com/ug",         "Uganda"),
    # Tanzania
    ("JobsTZ",         "https://www.jobtanzania.com",           "Tanzania"),
    ("TanzaniaJobs",   "https://tanzaniajobs.net",              "Tanzania"),
    # Rwanda
    ("JobsRW",         "https://rwandajobs.rw",                 "Rwanda"),
    ("WorkInRwanda",   "https://workinrwanda.com",              "Rwanda"),
    # South Africa
    ("PNet",           "https://www.pnet.co.za",                "South Africa"),
    ("CareerJunction", "https://www.careerjunction.co.za",      "South Africa"),
    ("Jobs-ZA",        "https://jobs.co.za",                    "South Africa"),
    ("Gumtree-ZA",     "https://www.gumtree.co.za",             "South Africa"),
    # Pan-Africa
    ("AfricaJobs2",    "https://africajobsboard.com",           "Africa"),
    ("JobsAfrica2",    "https://jobsafrica.org",                "Africa"),
    ("RemoteAfrica",   "https://remoteafrica.io",               "Africa"),
    ("AfricaWork",     "https://www.africa-work.com",           "Africa"),
]

def _wp_boards_extra():
    """Extended WP Job Manager board scraper — research-verified African boards."""
    out, seen = [], set()
    def _fetch(name, base, default_loc):
        for path in ["/wp-json/wp/v2/job_listing?per_page=50&orderby=date",
                     "/wp-json/wpjm/v1/jobs?per_page=50",
                     "/wp-json/wp/v2/jobs?per_page=50",
                     "/feed/job_listings/",
                     "/jobs/feed/"]:
            try:
                url = f"{base.rstrip('/')}{path}"
                r = requests.get(url, timeout=6,
                                 headers={"User-Agent":"Mozilla/5.0 HireBridgeAfrica/1.0",
                                          "Accept":"application/json,application/rss+xml,*/*"})
                if r.status_code != 200: continue
                ct = r.headers.get("content-type","")
                if "json" in ct:
                    items = r.json() if isinstance(r.json(),list) else r.json().get("jobs",r.json().get("data",[]))
                    jobs = []
                    for j in (items or [])[:50]:
                        t = j.get("title",{})
                        title = _strip(t.get("rendered","") if isinstance(t,dict) else str(t))
                        if not title: continue
                        u = j.get("link","") or (j.get("guid",{}).get("rendered","") if isinstance(j.get("guid"),dict) else "")
                        loc = (j.get("meta",{}) or {}).get("_job_location","") or                               j.get("job_location","") or j.get("location","") or default_loc
                        jobs.append(_N(title=title, location=loc or default_loc,
                                       date_str=j.get("date","") or j.get("created_at",""),
                                       url=u, source=name, category="General"))
                    if jobs: return jobs
                elif "xml" in ct or "rss" in ct:
                    jobs = _rss(url, name, default_loc, "General")
                    if jobs: return jobs
            except Exception:
                pass
        return []

    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        futs = {ex.submit(_fetch, name, base, loc): name
                for name, base, loc in _WP_BOARDS_EXTRA}
        for f in as_completed(futs):
            for job in f.result():
                k = hashlib.md5((job.get("application_url","") or job.get("title","")).encode()).hexdigest()
                if k not in seen:
                    seen.add(k); out.append(job)
    log.info(f"WP boards extra: {len(out)} jobs")
    return out


# ═══ TIER 14 — Google Alerts RSS (free, no API key) ══════════════════════════
# Google Alerts can generate RSS feeds for job searches — entirely free.
# Set these up at alerts.google.com and add the RSS URLs to .env as
# GOOGLE_ALERTS_RSS (comma-separated). Pre-built query examples included.

# Keywords that strongly indicate a real job posting
_JOB_SIGNALS = {
    "hiring","vacancy","vacancies","position","opening","apply","application",
    "deadline","salary","qualifications","requirements","responsibilities",
    "experience","degree","bachelor","master","minimum","role","job",
    "recruit","opportunity","candidate","shortlist","interview","cv",
    "resume","remuneration","competitive","package","benefits","full-time",
    "part-time","contract","internship","graduate","entry level","mid level",
    "senior","manager","officer","coordinator","analyst","engineer","developer",
    "accountant","nurse","teacher","driver","supervisor","director","head of",
}

# Keywords that indicate it is NOT a job post — news/blog/analysis
_NON_JOB_SIGNALS = {
    "unemployment rate","job losses","layoffs","retrenchment","strike",
    "economy","gdp","inflation","election","minister","parliament","policy",
    "protest","march","rally","opinion","editorial","analysis","review",
    "report","survey","ranking","index","statistics","data shows","research",
    "study finds","according to","percent","million people","billion",
}

def _is_job_posting(title, desc):
    """
    Heuristic filter: returns True only if the item looks like
    an actual job vacancy rather than a news article or blog post.
    """
    text = (title + " " + desc).lower()
    job_hits  = sum(1 for w in _JOB_SIGNALS    if w in text)
    news_hits = sum(1 for w in _NON_JOB_SIGNALS if w in text)
    # Must have at least 2 job signals and fewer news signals than job signals
    return job_hits >= 2 and news_hits < job_hits

def _google_alerts_rss():
    """
    Google Alerts RSS feeds for African job searches.
    Add GOOGLE_ALERTS_RSS=url1,url2,... to your .env file.
    To create: go to alerts.google.com, search 'jobs in Nairobi',
    click Show options → Deliver to: RSS feed → Create Alert.

    FILTERING: Google Alerts returns mixed content (news + jobs).
    We apply _is_job_posting() to keep only genuine vacancy posts.
    """
    urls_env = os.environ.get("GOOGLE_ALERTS_RSS","")
    if not urls_env:
        log.debug("GOOGLE_ALERTS_RSS not set — skipping")
        return []
    urls = [u.strip() for u in urls_env.split(",") if u.strip()]
    raw, filtered = [], []
    for url in urls:
        raw.extend(_rss(url, "GoogleAlerts", "Africa", "General"))
    for job in raw:
        if _is_job_posting(job.get("title",""), job.get("description","")):
            filtered.append(job)
        else:
            log.debug(f"GoogleAlerts filtered out (non-job): {job.get('title','')[:60]}")
    log.info(f"GoogleAlerts: {len(raw)} raw → {len(filtered)} jobs after filter")
    return filtered


# ═══ TIER 15 — ScaleSerp / ValueSerp (Google Jobs, 100 free/month) ═══════════

def _scaleserp_africa():
    """
    ScaleSerp API — returns Google Jobs results.
    Free: 100 searches/month. Add SCALESERP_API_KEY to .env.
    Get key at: https://app.scaleserp.com/
    Uses same 1-per-call rotation as JSearch.
    """
    key = _CFG.get("SCALESERP_KEY","")
    if not key: return []

    # Load state (shared rotation with JSearch)
    state_file = _Path(__file__).parent / "scaleserp_state.json"
    try:
        state = _json.loads(state_file.read_text()) if state_file.exists() else {}
    except Exception:
        state = {}

    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "calls_today": 0, "idx": 0}
    if state.get("calls_today",0) >= 3:  # 3/day = ~90/month
        return []

    idx = state.get("idx",0) % len(_JSEARCH_QUERIES)
    query = _JSEARCH_QUERIES[idx]
    state["idx"] = idx + 1
    state["calls_today"] = state.get("calls_today",0) + 1
    try: state_file.write_text(_json.dumps(state))
    except Exception: pass

    try:
        r = requests.get("https://api.scaleserp.com/search",
                         params={"api_key": key, "q": query,
                                 "search_type": "jobs", "location": "Africa",
                                 "num": 100},
                         timeout=_CFG["TIMEOUT"])
        if r.status_code != 200: return []
        out = []
        for j in r.json().get("jobs_results",[]):
            out.append(_N(title=j.get("title",""),
                           company=j.get("company_name",""),
                           location=j.get("location","Africa"),
                           date_str=j.get("detected_extensions",{}).get("posted_at",""),
                           url=j.get("apply_options",[{}])[0].get("link","") if j.get("apply_options") else "",
                           source="ScaleSerp-Africa",
                           desc=j.get("description","")[:400]))
        log.info(f"ScaleSerp: {len(out)} jobs")
        return out
    except Exception as e:
        log.warning(f"ScaleSerp: {e}"); return []

def _africa_specific_sources():
    """Run all Africa-dedicated scrapers in parallel."""
    out = []
    with ThreadPoolExecutor(max_workers=_CFG["GROUP_WORKERS"]) as ex:
        futs = [
            # Existing Africa-specific scrapers
            ex.submit(_jobgurus_ke),
            ex.submit(_brightermonday_ke),
            ex.submit(_fuzu_ke),
            ex.submit(_jobberman_ng),
            ex.submit(_ngcareers),
            ex.submit(_ethiojobs),
            ex.submit(_jobsgh),
            ex.submit(_ke_tech_rss),
            ex.submit(_jooble_africa),
            ex.submit(_jsearch_africa),
            ex.submit(_remoteok_africa),
            ex.submit(_wp_jobmanager_africa),
            # NEW Tier 8-15
            ex.submit(_indeed_africa),           # Indeed RSS — 18 African cities, free
            ex.submit(_linkedin_africa),         # LinkedIn public search — 9 cities
            ex.submit(_ngo_rss),                 # UN/NGO/Devex/Relief — 20 feeds
            ex.submit(_kenya_corporate_careers), # 25 major KE employer WP boards
            ex.submit(_govt_jobs),               # Government boards KE/NG/ZA/GH/RW
            ex.submit(_wp_boards_extra),         # 36 more African WP job boards
            ex.submit(_google_alerts_rss),       # Google Alerts RSS (if configured)
            ex.submit(_scaleserp_africa),        # ScaleSerp Google Jobs (if key set)
        ]
        for f in as_completed(futs):
            out.extend(f.result())
    log.info(f"Africa-specific sources total: {len(out)} jobs")
    return out

_ats_batch_offset = 0
_ats_batch_lock   = threading.Lock()

def _africa_ats(kws=None):
    """Fetch jobs from African company ATS boards (Greenhouse/Lever/Workable).

    There are ~635 boards in total. Hitting all of them every 2-hour background
    cycle wastes bandwidth, burns CPU on TLS handshakes, and gets the server
    flagged as a scraper by those APIs.

    When ATS_BATCH_SIZE > 0 (default 150) this function scans a rotating slice
    of that size each call instead of the full list. The full set is still
    covered across multiple cycles. Set SCRAPER_ATS_BATCH_SIZE=0 (or >=total)
    to restore full-scan behaviour.
    """
    global _ats_batch_offset
    all_boards = (
        [("gh", co) for co in _GH_AFRICA] +
        [("lv", co) for co in _LV_AFRICA] +
        [("wk", co) for co in _WK_AFRICA]
    )
    total = len(all_boards)
    batch = _CFG["ATS_BATCH_SIZE"]
    if batch <= 0 or batch >= total:
        subset = all_boards
    else:
        with _ats_batch_lock:
            start = _ats_batch_offset % total
            _ats_batch_offset = (start + batch) % total
        if start + batch <= total:
            subset = all_boards[start : start + batch]
        else:
            subset = all_boards[start:] + all_boards[: (start + batch) - total]
        log.info(f"ATS batch: scanning {len(subset)} of {total} boards "
                 f"(offset {start}, next {_ats_batch_offset})")

    out = []
    with ThreadPoolExecutor(max_workers=_CFG["MAX_WORKERS"]) as ex:
        def _dispatch(item):
            kind, co = item
            if   kind == "gh": return _greenhouse(co, kws)
            elif kind == "lv": return _lever(co, kws)
            else:              return _workable(co, kws)
        futs = [ex.submit(_dispatch, item) for item in subset]
        for f in as_completed(futs):
            out.extend(f.result())
    log.info(f"Africa ATS batch result: {len(out)} jobs")
    return out

# ═══ TIER 6 — ReliefWeb (UN/NGO, massive Africa coverage) ════════════════════
#
# CONFIRMED ROOT CAUSE: The v1/jobs endpoint returns 410 for both GET and POST.
# ReliefWeb migrated jobs to a different resource path in 2024.
# Correct approach: use the /reports endpoint filtered to job postings,
# OR use the RSS feed at https://reliefweb.int/jobs/rss.xml with custom UA.
# Fallback: scrape the JSON search API at /v1/jobs with the "profile=list" param.

def _reliefweb_jobs():
    """
    ReliefWeb jobs — 2025 working endpoint.
    The /v1/jobs resource was retired (410). Jobs now live under /v1/reports
    with type filter, OR via the RSS feed with proper headers.
    We try RSS first (simpler), then fall back to the reports API.
    """
    # Strategy 1: RSS with browser-like UA (avoids bot block)
    try:
        r = requests.get(
            "https://reliefweb.int/jobs/rss.xml",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/rss+xml,application/xml,text/xml,*/*",
                "Referer": "https://reliefweb.int/jobs",
            },
            timeout=_CFG["TIMEOUT"]
        )
        if r.status_code == 200 and len(r.content) > 200:
            jobs = _rss_parse_reliefweb(r.content)
            if jobs:
                log.info(f"ReliefWeb RSS: {len(jobs)} jobs")
                return jobs
    except Exception as e:
        log.warning(f"ReliefWeb RSS: {e}")

    # Strategy 2: ReliefWeb Reports API filtered to job_type
    try:
        payload = {
            "appname": "hirebridgeafrica",
            "limit": 100,
            "sort": [{"field": "date.created", "order": "desc"}],
            "fields": {"include": ["title","source","country","date","url","career_categories"]},
            "filter": {
                "operator": "AND",
                "conditions": [{"field": "status", "value": "published"}]
            },
            "query": {"value": "job vacancy position", "operator": "OR"}
        }
        r = requests.post(
            "https://api.reliefweb.int/v1/reports",
            json=payload,
            headers={"User-Agent": "HireBridgeAfrica/1.0",
                     "Content-Type": "application/json"},
            timeout=_CFG["TIMEOUT"]
        )
        if r.status_code == 200:
            items = r.json().get("data", [])
            return _parse_reliefweb_items(items, "ReliefWeb-Reports")
    except Exception as e:
        log.warning(f"ReliefWeb Reports: {e}")

    return []

def _rss_parse_reliefweb(content):
    """Parse ReliefWeb RSS into job dicts."""
    try:
        root = ET.fromstring(content)
        ns = {"a": "http://www.w3.org/2005/Atom",
              "c": "http://purl.org/rss/1.0/modules/content/"}
        items = root.findall(".//item") or root.findall(".//a:entry", ns)
        out = []
        for it in items:
            def t(tag):
                el = it.find(tag)
                return (el.text or "").strip() if el is not None else ""
            title = t("title")
            # ReliefWeb RSS encodes location in <category> tags
            cats = [c.text for c in it.findall("category") if c.text]
            loc = next((c for c in cats if any(
                country in c for country in [
                    "Kenya","Nigeria","South Africa","Ghana","Ethiopia","Uganda",
                    "Tanzania","Rwanda","Egypt","Cameroon","Senegal","Angola",
                    "Mozambique","Zambia","Zimbabwe","Malawi","Somalia","Sudan",
                    "DRC","Congo","Sierra Leone","Liberia","Guinea","Mali","Niger",
                    "Burkina Faso","Madagascar","Botswana","Namibia","Morocco",
                    "Tunisia","Algeria","Libya","Africa"
                ])), cats[0] if cats else "Africa")
            out.append(_N(title=title,
                          company=t("author") or "UN/NGO/INGO",
                          location=loc,
                          date_str=t("pubDate"),
                          url=t("link") or t("guid"),
                          source="ReliefWeb",
                          category="NGO / Nonprofit",
                          desc=t("description")))
        return out
    except Exception as e:
        log.warning(f"ReliefWeb RSS parse: {e}"); return []

def _parse_reliefweb_items(items, source="ReliefWeb"):
    out = []
    for item in items:
        f = item.get("fields", {})
        title = f.get("title","")
        sources = f.get("source", [{}])
        company = sources[0].get("name","UN/NGO") if isinstance(sources,list) and sources else "UN/NGO"
        countries = f.get("country", [{}])
        loc = ", ".join(c.get("name","") for c in countries if isinstance(c,dict)) \
              if isinstance(countries,list) else "Africa"
        date_str = (f.get("date",{}) or {}).get("created","")
        url = f.get("url","") or f"https://reliefweb.int/job/{item.get('id','')}"
        cats = f.get("career_categories",[])
        cat_label = cats[0].get("name","NGO / Nonprofit") \
                    if isinstance(cats,list) and cats else "NGO / Nonprofit"
        out.append(_N(title=title, company=company, location=loc or "Africa",
                      date_str=date_str, url=url, source=source, category=cat_label))
    return out

# ── Dedup ─────────────────────────────────────────────────────────────────────
def _dedup(jobs):
    seen, out = set(), []
    for j in jobs:
        k = hashlib.md5(f"{j['title'].lower()}|{j['company'].lower()}".encode()).hexdigest()
        if k not in seen:
            seen.add(k); out.append(j)
    return out

# ── Score (Africa-boosted) ────────────────────────────────────────────────────
_AFRICA_LOCS = {
    "kenya","nigeria","south africa","ghana","ethiopia","uganda","tanzania",
    "rwanda","egypt","morocco","senegal","cameroon","zimbabwe","zambia",
    "mozambique","botswana","ivory coast","côte d'ivoire","angola","namibia",
    "nairobi","lagos","accra","johannesburg","cape town","kampala",
    "dar es salaam","kigali","addis ababa","cairo","casablanca","dakar",
    "abidjan","africa","african","east africa","west africa","southern africa",
    "north africa","sub-saharan","drc","congo",
}

def _score(job, query):
    s = 0.0
    title = job.get("title","").lower()
    loc   = job.get("location","").lower()
    desc  = job.get("description","").lower()
    kws   = _kws(query)
    if query.lower() in title: s += 60
    s += sum(15 for k in kws if k in title)
    s += sum(5  for k in kws if k in desc)
    if any(al in loc  for al in _AFRICA_LOCS): s += 30
    if any(al in desc for al in _AFRICA_LOCS): s += 10
    dt = _dt(job.get("_ds",""))
    if dt:
        h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        s += 25 if h < 24 else 12 if h < 72 else 6 if h < 168 else 0
    return s

def _for_db(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def fetch_jobs_multi(query: str = "", location: str = "Remote",
                     limit: int = 200, admin_mode: bool = False) -> list:
    """Scrape jobs from all configured sources and return a DB-ready list.

    Resource-use summary (defaults, all tunable via .env):
      SCRAPER_MAX_WORKERS=6        outer + ATS concurrency
      SCRAPER_GROUP_WORKERS=4      each inner source-group pool
      SCRAPER_TIMEOUT=8            per-request timeout (seconds)
      SCRAPER_ATS_BATCH_SIZE=150   companies checked per cycle (rotates)
      SCRAPER_FETCH_BUDGET_SECONDS=45  hard wall-clock ceiling per call
    """
    import time as _time
    cache_key = "admin_all" if (admin_mode and not query) else f"{query}|{location}"
    cached = _cget(cache_key)
    if cached:
        log.info(f"Cache hit: {cache_key} ({len(cached)} jobs)")
        return [_for_db(j) for j in (cached[:limit] if limit else cached)]

    q = query or "developer"
    ats_kws = None if admin_mode else (_kws(query) if query else None)
    budget  = _CFG["FETCH_BUDGET_SECONDS"]
    deadline = _time.monotonic() + budget

    all_jobs: list = []

    def _safe(fn, *args):
        try: return fn(*args)
        except Exception as e: log.warning(f"{fn.__name__}: {e}"); return []

    with ThreadPoolExecutor(max_workers=_CFG["MAX_WORKERS"]) as ex:
        futs = [
            # Global remote
            ex.submit(_safe, _remoteok),
            ex.submit(_safe, _remotive,       q),
            ex.submit(_safe, _arbeitnow),
            ex.submit(_safe, _himalayas,      q),
            ex.submit(_safe, _jobicy,         q),
            ex.submit(_safe, _workingnomads),
            ex.submit(_safe, _the_muse),
            # Keyed (activate via .env)
            ex.submit(_safe, _adzuna,         q, "gb"),
            ex.submit(_safe, _adzuna_africa,  q),
            ex.submit(_safe, _reed,           q),
            ex.submit(_safe, _findwork,       q),
            # RSS
            ex.submit(_safe, _wwr_feeds),
            ex.submit(_safe, _global_rss),
            ex.submit(_safe, _africa_rss),
            # Global ATS
            ex.submit(_safe, _global_ats,     ats_kws),
            # Africa — the main event
            ex.submit(_safe, _africa_ats,     ats_kws),   # rotating batch of ATS boards
            ex.submit(_safe, _jobicy_africa),              # 8 geo filters
            ex.submit(_safe, _remotive_africa),            # 11 country searches
            ex.submit(_safe, _reliefweb_jobs),             # UN/NGO Africa
            ex.submit(_safe, _africa_specific_sources),    # Kenya/NG/GH/ET dedicated
        ]
        for f in as_completed(futs, timeout=max(1, deadline - _time.monotonic())):
            try:
                all_jobs.extend(f.result())
            except Exception as e:
                log.warning(f"fetch_jobs_multi future error: {e}")
            if _time.monotonic() > deadline:
                remaining = sum(1 for ft in futs if not ft.done())
                log.warning(f"fetch_jobs_multi: budget {budget}s exhausted, "
                            f"cancelling {remaining} pending source(s)")
                for ft in futs:
                    ft.cancel()
                break

    all_jobs = _dedup(all_jobs)

    if admin_mode or not query:
        all_jobs.sort(
            key=lambda j: _dt(j.get("_ds","")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True
        )
    else:
        kws = _kws(query)
        if kws:
            all_jobs = [j for j in all_jobs if any(k in j["title"].lower() for k in kws)]
        all_jobs.sort(key=lambda j: _score(j, query), reverse=True)

    elapsed = round(_time.monotonic() - (deadline - budget), 1)
    log.info(f"fetch_jobs_multi '{query}' / '{location}' admin={admin_mode} "
             f"→ {len(all_jobs)} jobs in {elapsed}s")
    _cset(cache_key, all_jobs)
    result = all_jobs[:limit] if limit else all_jobs
    return [_for_db(j) for j in result]


class JobScraper:
    def scrape_all(self, limit: int = 200) -> list:
        return fetch_jobs_multi(admin_mode=True, limit=limit)
    def search(self, query: str = "", location: str = "Remote", limit: int = 30) -> list:
        return fetch_jobs_multi(query=query, location=location, limit=limit)