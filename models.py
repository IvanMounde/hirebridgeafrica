"""
HireBridge Africa - Database Models (v2)
SQLAlchemy 2.0 mapped_column style with timezone-aware datetimes.
Includes: User, Service, Booking, ContactMessage, JobPosting, JobApplication,
          Newsletter, Subscriber, UserConsent
"""
from typing import Optional
import re as _re
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, Text, DateTime, Date, ForeignKey, Boolean

db = SQLAlchemy()


def _strip_html_tags(value: str) -> str:
    """Remove HTML tags and decode common entities."""
    if not value:
        return ""
    clean = _re.sub(r'<[^>]+>', '', str(value))
    clean = clean.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
                 .replace('&nbsp;', ' ').replace('&quot;', '"').replace('&#39;', "'")
    return clean.strip()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# User
# ─────────────────────────────────────────────────────────────────────────────
class User(UserMixin, db.Model):  # type: ignore
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(150))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Terms acceptance
    terms_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    terms_accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    privacy_accepted: Mapped[bool] = mapped_column(Boolean, default=False)

    bookings: Mapped[list["Booking"]] = relationship("Booking", back_populates="user")
    job_applications: Mapped[list["JobApplication"]] = relationship(
        "JobApplication", back_populates="user"
    )
    consents: Mapped[list["UserConsent"]] = relationship("UserConsent", back_populates="user")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def get_reset_token(self, secret_key: str) -> str:
        """Generate a signed password-reset token (expiry enforced on verify)."""
        from itsdangerous import URLSafeTimedSerializer
        s = URLSafeTimedSerializer(secret_key)
        return s.dumps({"user_id": self.id}, salt="password-reset")

    @staticmethod
    def verify_reset_token(token: str, secret_key: str, expires_sec: int = 1800) -> Optional["User"]:
        from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
        s = URLSafeTimedSerializer(secret_key)
        try:
            data = s.loads(token, salt="password-reset", max_age=expires_sec)
        except (BadSignature, SignatureExpired):
            return None
        return db.session.get(User, data.get("user_id"))

    def __repr__(self) -> str:
        return f"<User {self.username}>"


# ─────────────────────────────────────────────────────────────────────────────
# Service
# ─────────────────────────────────────────────────────────────────────────────
class Service(db.Model):  # type: ignore
    __tablename__ = "service"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Optional[int]] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(20), default="individual")
    tag: Mapped[Optional[str]] = mapped_column(String(50))
    details: Mapped[Optional[str]] = mapped_column(Text)

    bookings: Mapped[list["Booking"]] = relationship("Booking", back_populates="service")

    def __repr__(self) -> str:
        return f"<Service {self.name}>"


# ─────────────────────────────────────────────────────────────────────────────
# Booking
# ─────────────────────────────────────────────────────────────────────────────
class Booking(db.Model):  # type: ignore
    __tablename__ = "booking"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("service.id"), nullable=False)
    booking_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    preferred_date: Mapped[Optional[datetime]] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    notes: Mapped[Optional[str]] = mapped_column(Text)
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid")

    user: Mapped["User"] = relationship("User", back_populates="bookings")
    service: Mapped["Service"] = relationship("Service", back_populates="bookings")

    def __repr__(self) -> str:
        return f"<Booking {self.id} - {self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# ContactMessage
# ─────────────────────────────────────────────────────────────────────────────
class ContactMessage(db.Model):  # type: ignore
    __tablename__ = "contact_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    def __repr__(self) -> str:
        return f"<ContactMessage from {self.name}>"


# ─────────────────────────────────────────────────────────────────────────────
# JobPosting
# ─────────────────────────────────────────────────────────────────────────────
class JobPosting(db.Model):  # type: ignore
    __tablename__ = "job_posting"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    company: Mapped[str] = mapped_column(String(150), nullable=False)
    location: Mapped[str] = mapped_column(String(150), nullable=False)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    experience_level: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    salary_range: Mapped[Optional[str]] = mapped_column(String(100))
    experience_years: Mapped[Optional[str]] = mapped_column(String(50))
    skills: Mapped[Optional[str]] = mapped_column(Text)
    company_logo: Mapped[Optional[str]] = mapped_column(String(50))
    application_url: Mapped[Optional[str]] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(50), default="manual")
    posted_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    applications: Mapped[list["JobApplication"]] = relationship(
        "JobApplication", back_populates="job", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<JobPosting {self.title} at {self.company}>"

    def to_dict(self) -> dict:
        now = _utcnow()
        posted = self.posted_date
        if posted and posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return {
            "id": self.id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "job_type": self.job_type,
            "category": self.category,
            "experience_level": self.experience_level,
            "description": _strip_html_tags(self.description),
            "salary_range": self.salary_range,
            "experience_years": self.experience_years,
            "skills": self.skills.split(",") if self.skills else [],
            "company_logo": self.company_logo or "fa-building",
            "application_url": self.application_url,
            "is_active": bool(self.is_active),
            "source": self.source,
            "posted_date": posted.isoformat() if posted else None,
            "days_ago": (now - posted).days if posted else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# JobApplication
# ─────────────────────────────────────────────────────────────────────────────
VALID_APPLICATION_STATUSES = frozenset(
    ["submitted", "under_review", "shortlisted", "rejected", "withdrawn"]
)


class JobApplication(db.Model):  # type: ignore
    __tablename__ = "job_application"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False)
    job_id: Mapped[int] = mapped_column(ForeignKey("job_posting.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    application_type: Mapped[str] = mapped_column(String(20), default="direct")
    cover_letter: Mapped[Optional[str]] = mapped_column(Text)
    resume_filename: Mapped[Optional[str]] = mapped_column(String(255))
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    user: Mapped["User"] = relationship("User", back_populates="job_applications")
    job: Mapped["JobPosting"] = relationship("JobPosting", back_populates="applications")

    def __repr__(self) -> str:
        return f"<JobApplication {self.user_id} -> Job {self.job_id}>"


# ─────────────────────────────────────────────────────────────────────────────
# Newsletter Subscriber
# ─────────────────────────────────────────────────────────────────────────────
class Subscriber(db.Model):  # type: ignore
    __tablename__ = "subscriber"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(150))
    subscribed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(50), default="website")

    def __repr__(self) -> str:
        return f"<Subscriber {self.email}>"


# ─────────────────────────────────────────────────────────────────────────────
# UserConsent — stores T&C / Privacy acceptance records (GDPR/legal compliance)
# ─────────────────────────────────────────────────────────────────────────────
class UserConsent(db.Model):  # type: ignore
    __tablename__ = "user_consent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("user.id"), nullable=True)
    email: Mapped[str] = mapped_column(String(120), nullable=False)
    consent_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "terms", "privacy", "marketing"
    version: Mapped[str] = mapped_column(String(20), default="1.0")
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(500))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped[Optional["User"]] = relationship("User", back_populates="consents")

    def __repr__(self) -> str:
        return f"<UserConsent {self.email} - {self.consent_type}>"


# ─────────────────────────────────────────────────────────────────────────────
# Seed helper
# ─────────────────────────────────────────────────────────────────────────────
def init_services() -> None:
    """Seed default HireBridge Africa services (idempotent)."""
    if Service.query.count() > 0:
        return

    services = [
        Service(name="LinkedIn Profile Optimization", description="Transform your LinkedIn profile to attract recruiters and opportunities. Our experts craft compelling headlines, summaries, and experience sections.", price=1500, category="individual", tag="Professional Branding"),
        Service(name="ATS CV Writing – Junior Level", description="Professional ATS-compliant CV for entry to mid-level professionals (0–5 yrs). Keyword-optimised to pass automated screening systems.", price=750, category="individual", tag="Documents", details="Price range: KES 500–1,000"),
        Service(name="ATS CV Writing – Senior Level", description="Executive-level ATS-compliant CV for senior professionals (5+ yrs). Showcases leadership, impact and strategic achievements.", price=1250, category="individual", tag="Documents", details="Price range: KES 1,000–1,500"),
        Service(name="Job Application Support", description="Hands-on support with targeted job applications. We identify suitable roles, tailor your application materials, and track submissions.", price=2000, category="individual", tag="Career Support"),
        Service(name="Interview Coaching", description="Mock interviews and confidence-building sessions tailored to your target role and industry. Includes feedback and improvement strategies.", price=3500, category="individual", tag="Coaching"),
        Service(name="Salary Negotiation Preparation", description="Learn how to negotiate your compensation effectively using proven frameworks and role-specific market data.", price=2500, category="individual", tag="Coaching"),
        Service(name="Career Consultation Call", description="One-on-one consultation to map your career strategy, identify opportunities, and create a clear action plan.", price=6000, category="individual", tag="Consultation"),
        Service(name="Shortlisting & CV Filtering", description="Efficient candidate shortlisting using AI-powered tools and expert human review to surface top talent fast.", price=None, category="corporate", tag="Recruitment"),
        Service(name="Full Recruitment (End-to-End)", description="Complete recruitment from job posting to onboarding — sourcing, screening, interviews, offer management and placement.", price=None, category="corporate", tag="Recruitment"),
        Service(name="Mass Recruitment", description="Customised recruitment solutions for large volumes. Ideal for organisations scaling rapidly across multiple roles.", price=None, category="corporate", tag="Recruitment"),
        Service(name="Background & Reference Checks", description="Comprehensive candidate verification including employment history, education, criminal records and professional references.", price=None, category="corporate", tag="Recruitment"),
    ]

    for svc in services:
        db.session.add(svc)
    db.session.commit()
    print("HireBridge Africa services initialised successfully!")
