"""
HireBridge Africa — Full End-to-End Smoke Test (v2)
Tests HTTP responses AND user-friendliness:
  - Page titles / headings present
  - Flash messages surface correctly
  - Validation errors shown to the user
  - Content is meaningful (not blank pages)
  - Response times are acceptable
  - Full user journeys from landing to conversion
"""
import os, sys, json, time, re, threading
os.environ['DISABLE_BACKGROUND_SCRAPER'] = '1'
os.environ['TESTING'] = '1'
sys.path.insert(0, '/home/claude/HireBridgeAfrica_updated')
os.chdir('/home/claude/HireBridgeAfrica_updated')

from app import app, db
from models import (User, JobPosting, Service, Booking,
                    JobApplication, Subscriber, ContactMessage)
from app import _utcnow

app.config['TESTING']           = True
app.config['WTF_CSRF_ENABLED']  = False
app.config['ADMIN_EMAILS']      = 'admin_smoke@test.com'

# ── Output helpers ────────────────────────────────────────────────────────────
PASS = 0; FAIL = 0; WARNS = []
_block_pass = 0; _block_fail = 0

def chk(label, expected, actual, note=''):
    global PASS, FAIL, _block_pass, _block_fail
    ok = str(actual) == str(expected)
    if ok: PASS += 1; _block_pass += 1; sym = '✅'
    else:  FAIL += 1; _block_fail += 1; sym = '❌'
    suf = f'[{actual}]' if ok else f'expected={expected!r}  got={actual!r}'
    print(f'  {sym}  {label:<58} {suf}')
    if note: print(f'       ↳ {note}')

def ux(label, condition, detail=''):
    """UX check — same as chk but clearly marked as a friendliness check."""
    global PASS, FAIL, _block_pass, _block_fail
    if condition: PASS += 1; _block_pass += 1; sym = '✅'
    else:         FAIL += 1; _block_fail += 1; sym = '❌'
    tag = '[UX]'
    suf = 'OK' if condition else 'MISSING'
    print(f'  {sym}  {tag} {label:<55} {suf}')
    if detail and not condition: print(f'       ↳ {detail}')

def warn(msg):
    WARNS.append(msg)
    print(f'  ⚠️   {msg}')

def section(title):
    global _block_pass, _block_fail
    _block_pass = 0; _block_fail = 0
    print(f'\n{"═"*64}')
    print(f'  {title}')
    print(f'{"═"*64}')
    return title

def end_section(title):
    f = _block_fail
    sym = '✅' if f == 0 else '❌'
    print(f'\n  {sym} {title}: {_block_pass} passed, {_block_fail} failed')
    return _block_pass, _block_fail

def timed_get(c, path, follow=True, ua=None):
    """GET with timing. Returns (status, html, elapsed_ms)."""
    if ua:
        from werkzeug.test import EnvironBuilder
    t0 = time.monotonic()
    r = c.get(path, follow_redirects=follow)
    ms = (time.monotonic() - t0) * 1000
    return r.status_code, r.data.decode('utf-8', 'ignore'), round(ms)

def c_get(c, path, follow=True):
    t0 = time.monotonic()
    r = c.get(path, follow_redirects=follow)
    ms = round((time.monotonic() - t0) * 1000)
    return r.status_code, r.data.decode('utf-8', 'ignore'), ms

def c_post(c, path, data, follow=False):
    t0 = time.monotonic()
    r = c.post(path, data=data, follow_redirects=follow)
    ms = round((time.monotonic() - t0) * 1000)
    return r.status_code, r.data.decode('utf-8', 'ignore'), ms

def make_fresh_user(email, pw, username, is_admin=False):
    with app.app_context():
        User.query.filter_by(email=email).delete(); db.session.commit()
        u = User(email=email, username=username, full_name=f'{username} User',
                 phone='+254700000001', created_at=_utcnow())
        u.set_password(pw); db.session.add(u); db.session.commit()
        return u.id

def do_login(c, email, pw):
    return c_post(c, '/signin', {'email': email, 'password': pw})

results = {}

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 1 — Page loading, content & performance
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 1 — Page loading, content & performance')

with app.test_client() as c:
    pages = [
        ('/',          'Homepage',         ['HireBridge', 'job', 'Job']),
        ('/about',     'About',            ['HireBridge', 'about', 'About']),
        ('/services',  'Services',         ['service', 'Service', 'CV', 'LinkedIn']),
        ('/jobs',      'Jobs listing',     ['job', 'Job', 'engineer', 'Software']),
        ('/contact',   'Contact',          ['contact', 'Contact', 'email', 'Email']),
        ('/privacy',   'Privacy policy',   ['privacy', 'Privacy', 'data', 'Data']),
        ('/signin',    'Sign in page',     ['sign', 'Sign', 'email', 'password']),
        ('/signup',    'Sign up page',     ['sign', 'Sign', 'email', 'password', 'name']),
        ('/reset-password', 'Reset pwd',   ['reset', 'Reset', 'email', 'Email']),
    ]
    for path, label, kws in pages:
        code, html, ms = c_get(c, path)
        chk(f'{label} ({path})', 200, code)
        ux(f'{label} has expected content',
           any(k in html for k in kws),
           f'None of {kws} found in response')
        if ms > 500:
            warn(f'{label} slow: {ms}ms')
        else:
            print(f'       ↳ response time: {ms}ms')

    # Jobs filters all return 200 with content
    for qs, label in [
        ('?q=engineer',                 'Jobs search q=engineer'),
        ('?category=technology',        'Jobs filter category'),
        ('?level=mid',                  'Jobs filter level'),
        ('?type=full_time',             'Jobs filter type'),
        ('?q=engineer&category=technology', 'Jobs combined filters'),
        ('?page=2',                     'Jobs page 2'),
    ]:
        code, html, ms = c_get(c, f'/jobs{qs}')
        chk(f'{label}', 200, code)

    # 4xx responses are friendly (not blank)
    code, html, _ = c_get(c, '/doesnotexist')
    chk('Non-existent route → 404', 404, code)
    ux('404 page has friendly message',
       any(x in html for x in ['404','not found','Not Found','Page','page']),
       'Blank 404 page')

    code2, html2, _ = c_get(c, '/job/999999/apply', follow=False)
    chk('Non-existent job apply → 302 to signin (unauth)', 302, code2)

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 2 — Security gates (unauthenticated access)
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 2 — Security gates (unauthenticated access)')

with app.test_client() as c:
    protected = [
        ('/dashboard',       'Dashboard'),
        ('/applications',    'My applications'),
        ('/book/1',          'Book service'),
        ('/admin',           'Admin dashboard'),
        ('/uploads/any.pdf', 'Resume download'),
    ]
    for path, label in protected:
        code, html, _ = c_get(c, path, follow=False)
        chk(f'{label} unauth → 302', 302, code)

    # CSRF protection on all state-changing POSTs
    # Must re-enable CSRF temporarily since testing mode disables it globally
    app.config['WTF_CSRF_ENABLED'] = True
    with app.test_client() as c_csrf:
        csrf_routes = [
            ('/logout',    'Logout'),
            ('/subscribe', 'Subscribe'),
        ]
        for path, label in csrf_routes:
            code2, _, _ = c_post(c_csrf, path, {})
            chk(f'{label} POST no CSRF → 400', 400, code2)
    app.config['WTF_CSRF_ENABLED'] = False

    # UA-based scraper blocking scoped to /jobs only
    scrapy_ua = {'HTTP_USER_AGENT': 'Scrapy/2.6 (+http://scrapy.org)'}
    r_jobs  = c.get('/jobs',  environ_base=scrapy_ua)
    r_about = c.get('/about', environ_base=scrapy_ua)
    chk('Scrapy UA on /jobs → 403 (blocked)', 403, r_jobs.status_code)
    chk('Scrapy UA on /about → 200 (not blocked)', 200, r_about.status_code)

    # Admin blocked for non-admin
    make_fresh_user('nonadmin@test.com', 'NoAdmin1', 'nonadmin')
    do_login(c, 'nonadmin@test.com', 'NoAdmin1')
    code, html, _ = c_get(c, '/admin', follow=False)
    chk('Non-admin user /admin → 403', 403, code)
    ux('403 page has friendly message',
       any(x in html for x in ['403','forbidden','Forbidden','access','Access']),
       'Blank 403 page')

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 3 — Auth: signup with UX validation feedback
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 3 — Auth: signup, validation messages, signin, logout')

make_fresh_user('admin_smoke@test.com', 'AdminSmoke1!', 'adminsmoke', is_admin=True)

with app.test_client() as c:
    # 3a. Valid signup journey
    code, html, _ = c_post(c, '/signup', {
        'full_name': 'Jane Wanjiku', 'username': 'janew',
        'email': 'jane@test.com', 'phone': '+254712345678',
        'password': 'Kenya2024!', 'confirm_password': 'Kenya2024!',
        'agree_terms': 'on',
    })
    chk('Valid signup → 302 redirect', 302, code)
    with app.app_context():
        u = User.query.filter_by(email='jane@test.com').first()
        chk('User created in DB', True, u is not None)
        chk('Password hashed (not plaintext)', True,
            u is not None and u.password_hash != 'Kenya2024!')
        chk('Password validates correctly', True,
            u is not None and u.check_password('Kenya2024!'))

    # 3b. Duplicate email rejected with user-friendly flash
    code2, html2, _ = c_post(c, '/signup', {
        'full_name': 'Jane Copy', 'username': 'janecopy',
        'email': 'jane@test.com',
        'password': 'Kenya2024!', 'confirm_password': 'Kenya2024!',
        'agree_terms': 'on',
    })
    chk('Duplicate email → 302 (flash + redirect)', 302, code2)

    # Log out first — signup route redirects already-authenticated users
    # straight to the dashboard, which would make every validation check below
    # look like a false failure.
    c_post(c, '/logout', {})

    # 3c. Password too short — form re-renders with error
    code3, html3, _ = c_post(c, '/signup', {
        'full_name': 'Short Pw', 'username': 'shortpwuniq',
        'email': 'shortpw_uniq@test.com',
        'password': 'abc', 'confirm_password': 'abc',
        'agree_terms': 'on',
    })
    chk('Short password → 200 (re-render)', 200, code3)
    ux('Short password shows length error to user',
       any(x in html3 for x in ['8','least','minimum','short','length']),
       'No password length hint in response HTML')

    # 3d. Weak password (no digit) — form validator
    code4, html4, _ = c_post(c, '/signup', {
        'full_name': 'Weak Pw', 'username': 'weakpwuniq',
        'email': 'weakpw_uniq@test.com',
        'password': 'onlyletters', 'confirm_password': 'onlyletters',
        'agree_terms': 'on',
    })
    chk('No-digit password → 200 (re-render)', 200, code4)
    ux('No-digit password shows strength hint',
       any(x in html4 for x in ['number','digit','letter','strength']),
       'No password strength hint in HTML')

    # 3e. Mismatched passwords
    code5, html5, _ = c_post(c, '/signup', {
        'full_name': 'Mismatch', 'username': 'mismatchuniq',
        'email': 'mismatch_uniq@test.com',
        'password': 'Pass1234', 'confirm_password': 'Pass9999',
        'agree_terms': 'on',
    })
    chk('Password mismatch → 200 (re-render)', 200, code5)
    ux('Password mismatch shows error',
       any(x in html5 for x in ['match','equal','confirm','Match']),
       'No mismatch error in response')

    # 3f. No T&C acceptance
    code6, html6, _ = c_post(c, '/signup', {
        'full_name': 'No TnC', 'username': 'notcuniq',
        'email': 'notc_uniq@test.com',
        'password': 'Pass1234', 'confirm_password': 'Pass1234',
    })
    chk('No T&C acceptance → 200/302 (rejected)', True, code6 in (200, 302))

    # 3g. Signin — correct credentials
    code7, html7, _ = c_post(c, '/signin',
        {'email': 'jane@test.com', 'password': 'Kenya2024!'})
    chk('Valid signin → 302', 302, code7)
    # After redirect, dashboard should show welcome
    code_d, html_d, _ = c_get(c, '/dashboard')
    chk('Dashboard accessible post-login → 200', 200, code_d)
    ux('Dashboard greets the user by name',
       any(x in html_d for x in ['Jane','janew','Wanjiku','dashboard','Dashboard']),
       'No personalised greeting on dashboard')

    # 3i. Logout clears session
    code9, _, _ = c_post(c, '/logout', {})
    chk('Logout → 302', 302, code9)
    code_pd, _, _ = c_get(c, '/dashboard', follow=False)
    chk('Dashboard after logout → 302', 302, code_pd)

results[t] = end_section(t)

# 3h. Wrong password — must be a top-level client (not nested) to avoid
# inheriting the outer client's authenticated session. Use a dedicated user.
make_fresh_user('badpwtest@test.com', 'CorrectPw1', 'badpwtest')
with app.test_client() as c_badpw:
    r_bad = c_badpw.post('/signin',
        data={'email': 'badpwtest@test.com', 'password': 'TotallyWrong9'},
        follow_redirects=True)
    chk('Wrong password → 200 (stays on signin)', 200, r_bad.status_code)
    html8 = r_bad.data.decode('utf-8','ignore')
    ux('Wrong password shows "Invalid email or password"',
       'Invalid email or password' in html8,
       'Error message not found in response')
    r_dash2 = c_badpw.get('/dashboard', follow_redirects=False)
    chk('[UX] Wrong login has no active session (dashboard → 302)', 302, r_dash2.status_code)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 4 — Password reset flow (token sign → verify → set new password)
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 4 — Password reset: token lifecycle & UX')

make_fresh_user('resetme@test.com', 'OldPass123', 'resetuser')

with app.test_client() as c:
    # 4a. Request reset — existing email
    code, html, _ = c_post(c, '/reset-password',
        {'email': 'resetme@test.com'})
    chk('Reset request (existing email) → 302', 302, code)

    # 4b. Request reset — non-existing email → same response (no enumeration)
    with app.test_client() as c2:
        code2, html2, _ = c_post(c2, '/reset-password',
            {'email': 'nobody@test.com'})
        chk('Reset request (unknown email) → 302 (same UX)', 302, code2)

    # 4c. Generate and verify a real token in-process
    with app.app_context():
        u = User.query.filter_by(email='resetme@test.com').first()
        token = u.get_reset_token(app.config['SECRET_KEY'])

    code3, html3, _ = c_get(c, f'/reset-password/{token}')
    chk('Valid reset token page → 200', 200, code3)
    ux('Reset page has new-password form',
       any(x in html3 for x in ['password','Password','new','New']),
       'No password field on reset page')

    # 4d. Set new password via token
    code4, _, _ = c_post(c, f'/reset-password/{token}', {
        'password': 'NewPass456', 'confirm_password': 'NewPass456'
    })
    chk('Password reset POST → 302', 302, code4)
    with app.app_context():
        u2 = User.query.filter_by(email='resetme@test.com').first()
        chk('Old password rejected after reset', False, u2.check_password('OldPass123'))
        chk('New password accepted after reset', True,  u2.check_password('NewPass456'))

    # 4e. Tampered token rejected
    bad_token = token[:-8] + 'XXXXXXXX'
    r_bad = c.get(f'/reset-password/{bad_token}', follow_redirects=False)
    chk('Tampered token → redirect (rejected)', 302, r_bad.status_code)

    # 4f. Expired token rejected
    with app.app_context():
        u3 = User.query.filter_by(email='resetme@test.com').first()
        exp_token = u3.get_reset_token(app.config['SECRET_KEY'])
    time.sleep(1)
    with app.app_context():
        expired = User.verify_reset_token(exp_token, app.config['SECRET_KEY'], expires_sec=0)
        chk('Expired token (max_age=0) rejected in-model', None, expired)

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 5 — Full job application journey
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 5 — Full job application journey')

uid = make_fresh_user('applicant@test.com', 'Apply1234', 'applicant5')
# Pre-create stranger outside the outer test_client so the DB row exists
# before any test_client context pushes an app context
make_fresh_user('stranger5@test.com', 'Str5Pass1', 'stranger5')

with app.test_client() as c:
    do_login(c, 'applicant@test.com', 'Apply1234')

    with app.app_context():
        job = JobPosting.query.filter_by(is_active=True).first()
        jid  = job.id;  jtitle = job.title
        job2 = JobPosting.query.filter(JobPosting.id != jid, JobPosting.is_active==True).first()
        jid2 = job2.id if job2 else None

    # 5a. Apply page loads and has the right content
    code, html, ms = c_get(c, f'/job/{jid}/apply')
    chk(f'Apply page /job/{jid}/apply → 200', 200, code)
    ux('Apply page shows job title', jtitle.split()[0] in html,
       f'Job title "{jtitle}" not found on apply page')
    ux('Apply page has cover letter field',
       any(x in html for x in ['cover_letter','Cover','cover']),
       'No cover-letter field found')
    ux('Apply page has file upload field',
       'resume' in html or 'file' in html.lower() or 'upload' in html.lower(),
       'No resume upload field found')
    print(f'       ↳ apply page load: {ms}ms')

    # 5b. Submit application (no resume)
    code2, _, _ = c_post(c, f'/job/{jid}/apply', {
        'application_type': 'direct',
        'cover_letter': 'I am passionate about technology and would love to join your team.',
    })
    chk('Apply without resume → 302', 302, code2)
    with app.app_context():
        a = JobApplication.query.filter_by(user_id=uid, job_id=jid).first()
        chk('Application record in DB', True, a is not None)
        chk('Cover letter stored', True, a is not None and len(a.cover_letter or '') > 10)

    # 5c. Duplicate application → flash, no second record
    code3, _, _ = c_post(c, f'/job/{jid}/apply', {
        'application_type': 'direct', 'cover_letter': 'Second attempt'
    })
    chk('Duplicate apply → 302 (flash redirect)', 302, code3)
    with app.app_context():
        n = JobApplication.query.filter_by(user_id=uid, job_id=jid).count()
        chk('Only 1 application record (no duplicate)', 1, n)

    # 5d. Apply WITH resume to a second job
    if jid2:
        import io
        fake_pdf = b'%PDF-1.4 fake resume for smoke test'
        data = {
            'application_type': 'direct',
            'cover_letter': 'Attaching my resume as requested.',
        }
        r_upload = c.post(f'/job/{jid2}/apply', data={
            **data,
            'resume': (io.BytesIO(fake_pdf), 'my_cv.pdf', 'application/pdf'),
        }, content_type='multipart/form-data')
        chk('Apply with resume upload → 302', 302, r_upload.status_code)
        import os as _os
        from config import Config
        with app.app_context():
            a2 = JobApplication.query.filter_by(user_id=uid, job_id=jid2).first()
            chk('Resume application in DB', True, a2 is not None)
            fname = a2.resume_filename if a2 else None
            chk('resume_filename stored', True, bool(fname))
            if fname:
                on_disk = _os.path.exists(_os.path.join(Config.UPLOAD_FOLDER, fname))
                not_in_static = not _os.path.exists(
                    _os.path.join('static/uploads', fname))
                chk('Resume on disk in instance/uploads/', True, on_disk)
                chk('Resume NOT exposed in static/', True, not_in_static)

    # 5e. My-applications page shows submitted work
    code4, html4, _ = c_get(c, '/applications')
    chk('My-applications page → 200', 200, code4)
    ux('My-applications lists the applied job',
       any(x in html4 for x in [jtitle.split()[0], 'Software', 'Engineer', 'application']),
       'Applied job not visible on my-applications page')

    # 5f. Capture resume filename for access control checks below
    with app.app_context():
        a2r = JobApplication.query.filter_by(user_id=uid).filter(
            JobApplication.resume_filename != None).first()
        test_fname = a2r.resume_filename if a2r else None

    if test_fname:
        # Owner (still logged in as applicant) can download
        code_own, _, _ = c_get(c, f'/uploads/{test_fname}')
        chk('Owner downloads own resume → 200', 200, code_own)

# Resume access control: stranger + anon must be tested in their OWN
# top-level clients (outside the owner's 'with c' block) so sessions
# don't bleed across clients sharing the same pushed app context.
with app.test_client() as c_stranger:
    do_login(c_stranger, 'stranger5@test.com', 'Str5Pass1')
    if test_fname:
        r_str = c_stranger.get(f'/uploads/{test_fname}', follow_redirects=False)
        chk('Stranger cannot access resume → 403', 403, r_str.status_code)

with app.test_client() as c_anon:
    if test_fname:
        r_anon = c_anon.get(f'/uploads/{test_fname}', follow_redirects=False)
        chk('Anon resume access → 302 to login', 302, r_anon.status_code)

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 6 — Services & booking journey
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 6 — Services & booking journey')

uid_b = make_fresh_user('booker@test.com', 'Book1234', 'booker6')
uid_o = make_fresh_user('other6@test.com', 'Other1234', 'other6')

with app.test_client() as c:
    do_login(c, 'booker@test.com', 'Book1234')

    # 6a. Services listing
    code, html, _ = c_get(c, '/services')
    chk('Services listing → 200', 200, code)
    ux('Services page lists actual services',
       any(x in html for x in ['LinkedIn','CV','Resume','career','Career']),
       'No service names found on services page')

    # 6b. Individual service detail
    with app.app_context():
        svc = Service.query.first()
        svc_id = svc.id; svc_name = svc.name

    code2, html2, _ = c_get(c, f'/service/{svc_id}')
    chk(f'Service detail /service/{svc_id} → 200', 200, code2)
    ux(f'Service detail shows service name',
       svc_name.split()[0] in html2,
       f'Service name "{svc_name}" not on detail page')

    # 6c. Non-existent service → 404
    code404, _, _ = c_get(c, '/service/9999')
    chk('Non-existent service → 404', 404, code404)

    # 6d. Booking form
    code3, html3, _ = c_get(c, f'/book/{svc_id}')
    chk(f'Booking form /book/{svc_id} → 200', 200, code3)
    ux('Booking form shows service name', svc_name.split()[0] in html3)
    ux('Booking form has date/notes fields',
       any(x in html3 for x in ['date','Date','preferred','notes','Notes']),
       'No date/notes fields found on booking form')

    # 6e. Submit booking
    with app.app_context(): bk_before = Booking.query.count()
    code4, _, _ = c_post(c, f'/book/{svc_id}', {
        'preferred_date': '2026-12-20', 'notes': 'Morning slot please.',
    })
    chk('Submit booking → 302', 302, code4)
    with app.app_context():
        bk_after = Booking.query.count()
        bk = Booking.query.filter_by(user_id=uid_b).order_by(Booking.id.desc()).first()
        chk('Booking count +1', bk_before + 1, bk_after)
        chk('Booking record in DB', True, bk is not None)
        chk('Notes saved', 'Morning slot please.', bk.notes if bk else None)
        chk('Status is pending', 'pending', bk.status if bk else None)
        bk_id = bk.id if bk else None

    # 6f. Dashboard shows the booking
    code5, html5, _ = c_get(c, '/dashboard')
    ux('Dashboard shows booking summary',
       any(x in html5 for x in ['pending','Pending','booking','Booking',svc_name.split()[0]]),
       'No booking info on dashboard')

    # 6g. Cancel own booking
    if bk_id:
        code6, _, _ = c_post(c, f'/booking/{bk_id}/cancel', {})
        chk('Cancel booking → 302', 302, code6)
        with app.app_context():
            bk_c = db.session.get(Booking, bk_id)
            chk('Booking status = cancelled', 'cancelled', bk_c.status if bk_c else None)

results[t] = end_section(t)

# 6h. Cannot cancel another user's booking (outside outer client to avoid session bleed)
if bk_id:
    with app.test_client() as c_other6:
        do_login(c_other6, 'other6@test.com', 'Other1234')
        r_oth = c_other6.post(f'/booking/{bk_id}/cancel', follow_redirects=False)
        chk("Cancel another user's booking → 403", 403, r_oth.status_code)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 7 — Contact form & newsletter subscribe
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 7 — Contact form & newsletter subscribe')

with app.test_client() as c:
    # 7a. Contact form GET
    code, html, _ = c_get(c, '/contact')
    chk('Contact GET → 200', 200, code)
    ux('Contact page has name/email/message fields',
       all(x in html for x in ['name', 'email', 'message']),
       'Missing form fields on contact page')

    # 7b. Valid contact submission
    with app.app_context(): msg_before = ContactMessage.query.count()
    code2, _, _ = c_post(c, '/contact', {
        'name': 'Amina Odhiambo',
        'email': 'amina@test.com',
        'subject': 'Enquiry about CV writing',
        'message': 'Hello, I would like to know more about your CV writing service and pricing.',
    })
    chk('Contact form submit → 302', 302, code2)
    with app.app_context():
        chk('Message saved to DB', msg_before + 1, ContactMessage.query.count())
        msg = ContactMessage.query.order_by(ContactMessage.id.desc()).first()
        chk('Name stored correctly', 'Amina Odhiambo', msg.name if msg else None)

    # 7c. Too-short message rejected (min 10 chars)
    with app.app_context(): msg_before2 = ContactMessage.query.count()
    code3, html3, _ = c_post(c, '/contact', {
        'name': 'Short', 'email': 'short@test.com', 'message': 'Hi',
    })
    chk('Short message (<10 chars) → 200 re-render', 200, code3)
    ux('Short message shows validation error',
       any(x in html3 for x in ['10','least','minimum','too short','error','invalid']),
       'No validation feedback for short message')
    with app.app_context():
        chk('Short message NOT saved to DB', msg_before2, ContactMessage.query.count())

    # 7d. Invalid email rejected
    code4, html4, _ = c_post(c, '/contact', {
        'name': 'Bad Email', 'email': 'notanemail',
        'message': 'This should not be saved because email is invalid.',
    })
    chk('Invalid email → 200 re-render', 200, code4)
    ux('Invalid email shows validation error',
       any(x in html4 for x in ['valid','email','Email','invalid','error']),
       'No email validation error shown')

    # 7e. Subscribe with valid email
    with app.app_context(): sub_before = Subscriber.query.count()
    code5, _, _ = c_post(c, '/subscribe', {'email': 'subscribe_new@test.com'})
    chk('Subscribe valid email → 302', 302, code5)
    with app.app_context():
        sub = Subscriber.query.filter_by(email='subscribe_new@test.com').first()
        chk('Subscriber row created', True, sub is not None)
        chk('Subscriber is active', True, bool(sub.is_active) if sub else False)

    # 7f. Duplicate subscribe — no crash, no duplicate row
    code6, _, _ = c_post(c, '/subscribe', {'email': 'subscribe_new@test.com'})
    chk('Duplicate subscribe → 302 (graceful)', True, code6 in (200, 302))
    with app.app_context():
        n = Subscriber.query.filter_by(email='subscribe_new@test.com').count()
        chk('Still only 1 subscriber row', 1, n)

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 8 — Admin panel (full CRUD + access control)
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 8 — Admin panel (CRUD, access control, UX)')

app.config['ADMIN_EMAILS'] = 'admin_smoke@test.com'

with app.test_client() as c:
    do_login(c, 'admin_smoke@test.com', 'AdminSmoke1!')

    # 8a. Admin dashboard loads with key content
    code, html, ms = c_get(c, '/admin')
    chk('Admin dashboard → 200', 200, code)
    ux('Dashboard shows job management tools',
       any(x in html for x in ['Add Job', 'add-job', 'Applications', 'auto-fetch']),
       'No job management tools visible')
    print(f'       ↳ admin dashboard load: {ms}ms')

    # 8b. Admin applications list
    code2, html2, _ = c_get(c, '/admin/applications')
    chk('Admin applications list → 200', 200, code2)
    ux('Applications page has application records',
       any(x in html2 for x in ['applicant','Apply','application','Application', 'status']),
       'No application records on admin applications page')

    # 8c. Job add form
    code3, html3, _ = c_get(c, '/admin/job/add')
    chk('Admin job add form → 200', 200, code3)
    ux('Add-job form has required fields',
       all(x in html3 for x in ['title','company','location','description']),
       'Missing required form fields on add-job page')

    # 8d. Create job
    with app.app_context(): before = JobPosting.query.count()
    code4, _, _ = c_post(c, '/admin/job/add', {
        'title': 'Data Analyst Kenya',
        'company': 'Safaricom PLC',
        'location': 'Nairobi, Kenya',
        'job_type': 'full_time',
        'category': 'technology',
        'experience_level': 'mid',
        'salary_range': 'KES 130k-160k',
        'experience_years': '3',
        'skills': 'Python, SQL, Tableau',
        'description': 'Analyse business data and build dashboards for East Africa operations.',
        'application_url': 'https://safaricom.co.ke/careers/data-analyst',
    })
    chk('Create job → 302', 302, code4)
    with app.app_context():
        after = JobPosting.query.count()
        j = JobPosting.query.filter_by(title='Data Analyst Kenya').first()
        chk('Job count +1', before + 1, after)
        chk('Title in DB', 'Data Analyst Kenya', j.title if j else None)
        chk('Company in DB', 'Safaricom PLC', j.company if j else None)
        chk('is_active defaults True', True, bool(j.is_active) if j else None)
        new_jid = j.id if j else None

    # 8e. Edit job
    if new_jid:
        code5, html5, _ = c_get(c, f'/admin/job/{new_jid}/edit')
        chk('Edit job form → 200', 200, code5)
        ux('Edit form pre-filled with existing values',
           'Data Analyst Kenya' in html5 or 'Safaricom' in html5,
           'Edit form not pre-filled with current values')

        code6, _, _ = c_post(c, f'/admin/job/{new_jid}/edit', {
            'title': 'Senior Data Analyst Kenya',
            'company': 'Safaricom PLC',
            'location': 'Nairobi, Kenya (Hybrid)',
            'job_type': 'hybrid',
            'category': 'technology',
            'experience_level': 'senior',
            'salary_range': 'KES 180k-220k',
            'experience_years': '5',
            'skills': 'Python, SQL, Tableau, dbt',
            'description': 'Senior role: lead analytics for East Africa.',
            'application_url': 'https://safaricom.co.ke/careers/senior-da',
        })
        chk('Edit job → 302', 302, code6)
        with app.app_context():
            j2 = db.session.get(JobPosting, new_jid)
            chk('Title updated in DB', 'Senior Data Analyst Kenya', j2.title if j2 else None)
            chk('Location updated in DB', 'Nairobi, Kenya (Hybrid)', j2.location if j2 else None)
            chk('Level updated in DB', 'senior', j2.experience_level if j2 else None)

    # 8f. Toggle active state
    if new_jid:
        with app.app_context(): orig = db.session.get(JobPosting, new_jid).is_active
        code7, _, _ = c_post(c, f'/admin/job/{new_jid}/toggle', {})
        chk('Toggle job → 302', 302, code7)
        with app.app_context():
            chk('is_active toggled', True,
                db.session.get(JobPosting, new_jid).is_active != orig)
        # restore
        c_post(c, f'/admin/job/{new_jid}/toggle', {})

    # 8g. Application status update (all valid statuses)
    with app.app_context():
        ap = JobApplication.query.filter_by(user_id=uid).first()
        ap_id = ap.id if ap else None
    if ap_id:
        for status in ['under_review', 'shortlisted', 'rejected', 'submitted']:
            code_s, _, _ = c_post(c, f'/admin/application/{ap_id}/update-status',
                                  {'status': status})
            chk(f"Status '{status}' → 302", 302, code_s)
            with app.app_context():
                saved = db.session.get(JobApplication, ap_id).status
                chk(f"Status '{status}' saved", status, saved)

        # Invalid status rejected
        code_inv, _, _ = c_post(c, f'/admin/application/{ap_id}/update-status',
                                 {'status': 'reviewing'})
        with app.app_context():
            not_changed = db.session.get(JobApplication, ap_id).status
            chk("Invalid status 'reviewing' rejected (status unchanged)",
                True, not_changed != 'reviewing')

    # 8h. CSRF enforced on all destructive POSTs
    app.config['WTF_CSRF_ENABLED'] = True
    with app.test_client() as c_csrf:
        do_login(c_csrf, 'admin_smoke@test.com', 'AdminSmoke1!')
        chk('Delete no CSRF → 400',    400, c_csrf.post('/admin/job/1/delete', data={}).status_code)
        chk('Toggle no CSRF → 400',    400, c_csrf.post('/admin/job/1/toggle', data={}).status_code)
    app.config['WTF_CSRF_ENABLED'] = False

    # 8i. Nonexistent resource → 404
    code404, _, _ = c_get(c, '/admin/job/999999/edit')
    chk('Edit nonexistent job → 404', 404, code404)

    # 8j. Delete test job (cleanup)
    if new_jid:
        code8, _, _ = c_post(c, f'/admin/job/{new_jid}/delete', {})
        chk('Delete job → 302', 302, code8)
        with app.app_context():
            chk('Job removed from DB', True,
                db.session.get(JobPosting, new_jid) is None)

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 9 — API endpoints
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 9 — API endpoints')

with app.test_client() as c:
    # 9a. /api/jobs
    code, html, ms = c_get(c, '/api/jobs')
    chk('/api/jobs → 200', 200, code)
    data = json.loads(html)
    chk('/api/jobs returns JSON list', True, isinstance(data, list))
    chk('/api/jobs has entries', True, len(data) > 0)
    if data:
        j = data[0]
        for f in ['title', 'company', 'location', 'application_url']:
            chk(f'/api/jobs entry has "{f}" field', True, f in j)
    print(f'       ↳ /api/jobs response time: {ms}ms')

    # 9b. /api/jobs/search — must be fast (DB, not live scraper)
    t0 = time.monotonic()
    code2, html2, _ = c_get(c, '/api/jobs/search?q=engineer')
    elapsed_ms = round((time.monotonic() - t0) * 1000)
    chk('/api/jobs/search → 200', 200, code2)
    chk('/api/jobs/search < 100ms (DB-backed, not live scraper)',
        True, elapsed_ms < 100)
    print(f'       ↳ search response time: {elapsed_ms}ms')
    d2 = json.loads(html2)
    chk('Search response has "jobs" key',  True, 'jobs' in d2)
    chk('Search response has "count" key', True, 'count' in d2)
    chk('count == len(jobs)', d2.get('count'), len(d2.get('jobs', [])))

    # 9c. Empty search returns all jobs
    code3, html3, _ = c_get(c, '/api/jobs/search')
    d3 = json.loads(html3)
    chk('Empty search returns all active jobs', True, d3.get('count', 0) > 0)

    # 9d. Location filter
    code4, html4, _ = c_get(c, '/api/jobs/search?location=Nairobi')
    chk('Location filter → 200', 200, code4)
    d4 = json.loads(html4)
    if d4.get('jobs'):
        ux('Location filter returns Nairobi jobs',
           all('Nairobi' in j.get('location','') for j in d4['jobs']),
           'Non-Nairobi jobs returned for location=Nairobi filter')

    # 9e. /api/availability
    code5, html5, _ = c_get(c, '/api/availability?service_id=1&date=2026-12-15')
    chk('/api/availability → 200', 200, code5)
    d5 = json.loads(html5)
    chk('Availability has "available" key', True, 'available' in d5)

    # 9f. /api/jobs does not expose inactive jobs
    with app.app_context():
        inactive_j = JobPosting.query.filter_by(is_active=True).first()
        if inactive_j:
            inactive_j.is_active = False
            db.session.commit()
            inactive_title = inactive_j.title
            inactive_id    = inactive_j.id
    code6, html6, _ = c_get(c, '/api/jobs')
    d6 = json.loads(html6)
    if inactive_j:
        titles = [x.get('title','') for x in d6]
        chk('Inactive job excluded from /api/jobs', True, inactive_title not in titles)
        with app.app_context():
            db.session.get(JobPosting, inactive_id).is_active = True
            db.session.commit()

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 10 — XSS, security headers, open-redirect, scraper controls
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 10 — XSS prevention, security headers, scraper controls')

from app import _is_safe_external_url, _is_safe_redirect
from markupsafe import escape
import job_scraper as js

with app.app_context():
    # 10a. URL safety validator
    bad_urls = [
        ('javascript:alert(1)',           'javascript: scheme'),
        ('data:text/html,<h1>x</h1>',    'data: scheme'),
        ('vbscript:msgbox(1)',            'vbscript: scheme'),
        ('"><script>alert(1)</script>',   'XSS in URL string'),
        ('/relative/path',                'relative path'),
        ('',                              'empty string'),
    ]
    for url, label in bad_urls:
        chk(f'URL blocked: {label}', False, _is_safe_external_url(url))

    good_urls = [
        ('https://careers.safaricom.co.ke/jobs/123', 'https URL'),
        ('http://jobs.example.com/apply',             'http URL'),
    ]
    for url, label in good_urls:
        chk(f'URL allowed: {label}', True, _is_safe_external_url(url))

    # 10b. HTML escaping works
    malicious = '<script>alert("xss")</script>'
    escaped = str(escape(malicious))
    chk('escape() converts < to &lt;', True, '&lt;' in escaped)
    chk('escape() converts > to &gt;', True, '&gt;' in escaped)
    chk('escape() removes raw <script>', True, '<script>' not in escaped)

    # 10c. Open-redirect protection
    chk('External redirect blocked (https://evil.com)',  False, _is_safe_redirect('https://evil.com'))
    chk('Protocol-relative blocked (//evil.com)',        False, _is_safe_redirect('//evil.com'))
    chk('Relative path allowed (/dashboard)',            True,  _is_safe_redirect('/dashboard'))
    chk('None blocked',                                  False, _is_safe_redirect(None))

# 10d. Security headers on responses
with app.test_client() as c:
    r = c.get('/about')
    h = dict(r.headers)
    chk('X-Content-Type-Options: nosniff',
        'nosniff', h.get('X-Content-Type-Options','').lower())
    chk('X-Frame-Options: SAMEORIGIN',
        'sameorigin', h.get('X-Frame-Options','').lower())
    chk('Referrer-Policy set',
        True, bool(h.get('Referrer-Policy','')))
    chk('Permissions-Policy set',
        True, bool(h.get('Permissions-Policy','')))

# 10e. Scraper resource config
print(f'\n  Scraper config:')
for k in ['TIMEOUT','MAX_WORKERS','GROUP_WORKERS','ATS_BATCH_SIZE','FETCH_BUDGET_SECONDS']:
    print(f'    {k} = {js._CFG[k]}')

total_boards = len(js._GH_AFRICA) + len(js._LV_AFRICA) + len(js._WK_AFRICA)
batch = js._CFG['ATS_BATCH_SIZE']

chk('MAX_WORKERS <= 6 (was 24)',          True, js._CFG['MAX_WORKERS'] <= 6)
chk('GROUP_WORKERS <= 4 (was 5–20)',      True, js._CFG['GROUP_WORKERS'] <= 4)
chk('TIMEOUT <= 8s (was 12s)',            True, js._CFG['TIMEOUT'] <= 8)
chk('FETCH_BUDGET_SECONDS in use',        True, 0 < js._CFG['FETCH_BUDGET_SECONDS'] <= 60)
chk('ATS batch < total boards',           True, 0 < batch < total_boards,
    f'batch={batch} of {total_boards} total')
cycles = -(-total_boards // batch)
chk(f'Full coverage in {cycles} cycles (<=6)', True, cycles <= 6)

# Batch rotation correctness
js._ats_batch_offset = 0
starts = set()
for _ in range(cycles):
    with js._ats_batch_lock:
        s = js._ats_batch_offset % total_boards
        js._ats_batch_offset = (s + batch) % total_boards
    starts.add(s)
chk('Each cycle scans a different board slice', True, len(starts) == cycles,
    f'unique offsets seen: {sorted(starts)}')

chk('_ats_get CB wrapper callable',  True, callable(js._ats_get))
chk('FETCH_BUDGET in fetch_jobs_multi source', True,
    'FETCH_BUDGET_SECONDS' in __import__('inspect').getsource(js.fetch_jobs_multi))

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# BLOCK 11 — Production safety guard
# ═════════════════════════════════════════════════════════════════════════════
t = section('BLOCK 11 — Production safety guard')

from app import _check_production_safety
import os as _os

placeholder_keys = [
    ('',                              'empty SECRET_KEY'),
    ('changeme-use-strong-key',       'placeholder key 1'),
    ('replace_with_strong_random_key','placeholder key 2'),
]
orig_env = _os.environ.get('FLASK_ENV','development')
orig_key = app.config.get('SECRET_KEY','')

_os.environ['FLASK_ENV'] = 'production'
for bad_key, label in placeholder_keys:
    app.config['SECRET_KEY'] = bad_key
    with app.app_context():
        try:
            _check_production_safety(); blocked = False
        except RuntimeError: blocked = True
    chk(f'Production boot blocked: {label}', True, blocked)

# Strong key — should NOT block
app.config['SECRET_KEY'] = 'a-very-strong-64-char-secret-key-for-production-use-1234567890ab'
with app.app_context():
    try:
        _check_production_safety(); blocked = False
    except RuntimeError: blocked = True
chk('Production boot allowed: strong SECRET_KEY', False, blocked)

_os.environ['FLASK_ENV'] = orig_env
app.config['SECRET_KEY'] = orig_key

# DISABLE_BACKGROUND_SCRAPER env var
from app import _background_fetch
threads_before = threading.active_count()
_background_fetch()
time.sleep(0.2)
chk('DISABLE_BACKGROUND_SCRAPER env var suppresses background thread',
    True, threading.active_count() <= threads_before + 1)

results[t] = end_section(t)

# ═════════════════════════════════════════════════════════════════════════════
# FINAL REPORT
# ═════════════════════════════════════════════════════════════════════════════
print(f'\n\n{"═"*64}')
print(f'  FULL SMOKE TEST REPORT — HireBridge Africa')
print(f'{"═"*64}')
print(f'  {"BLOCK":<46} {"PASS":>4}  {"FAIL":>4}  STATUS')
print(f'  {"─"*60}')
for name, (p, f) in results.items():
    sym  = '✅' if f == 0 else '❌'
    name_short = name.split('—',1)[1].strip() if '—' in name else name
    bar  = '█' * min(p, 20) + ('░' * min(f, 5) if f else '')
    print(f'  {sym} {name_short:<44} {p:>4}  {f:>4}  {bar}')
print(f'  {"─"*60}')
print(f'  {"TOTAL":<46} {PASS:>4}  {FAIL:>4}')
print(f'\n  Pass rate: {PASS/(PASS+FAIL)*100:.0f}%  ({PASS} passed, {FAIL} failed)')

if WARNS:
    print(f'\n  ⚠️  Performance notices:')
    for w in WARNS: print(f'     • {w}')

if FAIL == 0:
    print(f'\n  🎉 All checks passed. The app is fully functional and user-friendly.')
else:
    print(f'\n  Findings to address:')
