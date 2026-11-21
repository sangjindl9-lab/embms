"""Runtime configuration. All secrets come from environment variables only."""
import os

VERSION = "0.3.0"


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./eumbms.db")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _db_url()
BOOTSTRAP_ADMIN_USER = os.getenv("ADMIN_USER", "")
BOOTSTRAP_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
# Login throttling (DB-backed, shared across instances)
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "10"))  # per username
LOGIN_MAX_FAILURES_IP = int(os.getenv("LOGIN_MAX_FAILURES_IP", "50"))  # per source IP (NAT-friendly)
LOGIN_WINDOW_SECONDS = int(os.getenv("LOGIN_WINDOW_SECONDS", "300"))
# Remote control is hard-disabled until P6 safety approval. There is no write path in the code.
REMOTE_CONTROL_ENABLED = False
MIN_PASSWORD_LENGTH = 12
