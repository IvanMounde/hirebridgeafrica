#!/usr/bin/env python3
"""
HireBridge Africa — Database Setup Script
Run once on fresh install: python setup.py
"""
import os
import sys
import secrets

# Load .env before importing app
from dotenv import load_dotenv
load_dotenv()

from app import app, db
from models import User, Service, JobPosting, init_services


def setup_database():
    print("🔧 Setting up HireBridge Africa database...")

    with app.app_context():
        print("  ⚙️  Creating database tables...")
        db.create_all()

        print("  📋 Initialising services...")
        init_services()

        # ── Default admin account ───────────────────────────────
        admin_email = os.environ.get("ADMIN_EMAILS", "hirebridge@gmail.com").split(",")[0].strip()
        admin = User.query.filter_by(email=admin_email).first()
        if not admin:
            admin_password = secrets.token_urlsafe(12)
            admin = User(
                username="admin",
                email=admin_email,
                full_name="HireBridge Admin",
                phone="+254742390793",
            )
            admin.set_password(admin_password)
            db.session.add(admin)
            db.session.commit()
            print(f"    ✅ Admin created — email: {admin_email}")
            print(f"    🔑 Password: {admin_password}")
            print("    ⚠️  This password is randomly generated and shown only once — copy it now.")
        else:
            print(f"    ℹ️  Admin ({admin_email}) already exists")

        # ── Extra admins from ADMIN_EMAILS ──────────────────────
        all_admins = [e.strip() for e in os.environ.get("ADMIN_EMAILS","").split(",") if e.strip()]
        for extra_email in all_admins[1:]:
            if not User.query.filter_by(email=extra_email).first():
                extra_password = secrets.token_urlsafe(12)
                u = User(username=extra_email.split("@")[0],
                         email=extra_email, full_name="Admin",
                         phone="+254742390793")
                u.set_password(extra_password)
                db.session.add(u)
                db.session.commit()
                print(f"    ✅ Extra admin created — {extra_email}")
                print(f"    🔑 Password: {extra_password}")

        print("\n✨ Setup complete!")
        print("\n📌 Next steps:")
        print("   1. Edit .env with your real values (email, secret key, API keys)")
        print("   2. Run: flask run")
        print(f"   3. Login at http://localhost:5000/signin with: {admin_email}")
        print("   4. Save the generated password somewhere safe — it won't be shown again")
        print("      (use `flask create_admin --email ... --password ...` to set your own)")
        print("   5. Go to /admin → Auto-Fetch Jobs to populate the job board")


if __name__ == "__main__":
    setup_database()
