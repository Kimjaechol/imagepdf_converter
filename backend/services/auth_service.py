"""Authentication service – Supabase Auth integration.

Uses Supabase GoTrue for user registration, login, and token verification.
Falls back to local SQLite when Supabase is not configured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import base64
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase client (lazy-init)
# ---------------------------------------------------------------------------
_supabase_client = None
_supabase_available = None  # None = not checked yet


def _get_supabase():
    """Return Supabase client if configured, else None."""
    global _supabase_client, _supabase_available
    if _supabase_available is False:
        return None
    if _supabase_client is not None:
        return _supabase_client

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        logger.info("Supabase not configured – using local SQLite auth fallback")
        _supabase_available = False
        return None

    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        _supabase_available = True
        logger.info("Supabase auth initialized: %s", url)
        return _supabase_client
    except ImportError:
        logger.warning("supabase package not installed – using local SQLite auth fallback")
        _supabase_available = False
        return None
    except Exception as e:
        logger.error("Failed to init Supabase client: %s", e)
        _supabase_available = False
        return None


# ---------------------------------------------------------------------------
# Local HMAC token helpers (fallback when Supabase is not configured)
# ---------------------------------------------------------------------------
_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", secrets.token_hex(32))
_TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hashed.hex(), salt


def _verify_password(password: str, hash_hex: str, salt: str) -> bool:
    computed, _ = _hash_password(password, salt)
    return hmac.compare_digest(computed, hash_hex)


def _create_token(user_id: str, email: str) -> str:
    payload = {
        "user_id": user_id,
        "email": email,
        "exp": time.time() + _TOKEN_EXPIRY_SECONDS,
        "iat": time.time(),
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")
    sig = hmac.new(
        _SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()
    return f"{payload_b64}.{sig}"


def _verify_local_token(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        expected_sig = hmac.new(
            _SECRET_KEY.encode(), payload_b64.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AuthService – Supabase-first, SQLite-fallback
# ---------------------------------------------------------------------------

class AuthService:
    """Manages user accounts with Supabase Auth (primary) or SQLite (fallback)."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "users.db"
        self._supabase = _get_supabase()
        # Always init local DB (migration support)
        self._init_db()

    @property
    def using_supabase(self) -> bool:
        return self._supabase is not None

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    nationality TEXT NOT NULL DEFAULT '',
                    gender TEXT NOT NULL DEFAULT '',
                    birth_date TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    last_login REAL
                )
            """)
            conn.commit()
            self._migrate_columns(conn)

    def _migrate_columns(self, conn: sqlite3.Connection):
        cursor = conn.execute("PRAGMA table_info(users)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        new_cols = {
            "phone": "TEXT NOT NULL DEFAULT ''",
            "nationality": "TEXT NOT NULL DEFAULT ''",
            "gender": "TEXT NOT NULL DEFAULT ''",
            "birth_date": "TEXT NOT NULL DEFAULT ''",
        }
        for col, col_def in new_cols.items():
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def}")
                logger.info("Migrated users table: added column '%s'", col)
        conn.commit()

    # ─── Registration ─────────────────────────────────────────

    def register(self, email: str, password: str, display_name: str = "",
                 phone: str = "", nationality: str = "", gender: str = "",
                 birth_date: str = "") -> dict:
        """Register a new user. Uses Supabase Auth if available."""
        email = email.strip().lower()
        if not email or not password:
            raise ValueError("Email and password are required")
        if len(password) < 6:
            raise ValueError("Password must be at least 6 characters")

        display_name = display_name or email.split("@")[0]

        if self._supabase:
            return self._register_supabase(
                email, password, display_name,
                phone, nationality, gender, birth_date,
            )
        return self._register_local(
            email, password, display_name,
            phone, nationality, gender, birth_date,
        )

    def _register_supabase(self, email, password, display_name,
                           phone, nationality, gender, birth_date) -> dict:
        """Register via Supabase GoTrue."""
        try:
            result = self._supabase.auth.sign_up({
                "email": email,
                "password": password,
                "options": {
                    "data": {
                        "display_name": display_name,
                        "phone": phone,
                        "nationality": nationality,
                        "gender": gender,
                        "birth_date": birth_date,
                    }
                }
            })

            user = result.user
            session = result.session

            if not user:
                raise ValueError("Registration failed – no user returned from Supabase")

            # If email confirmation is required, session may be None
            access_token = session.access_token if session else ""
            refresh_token = session.refresh_token if session else ""

            return {
                "user_id": user.id,
                "email": user.email or email,
                "display_name": display_name,
                "token": access_token,
                "refresh_token": refresh_token,
                "provider": "supabase",
                "email_confirmed": user.email_confirmed_at is not None if hasattr(user, 'email_confirmed_at') else bool(session),
            }
        except Exception as e:
            err_msg = str(e)
            if "already registered" in err_msg.lower() or "already been registered" in err_msg.lower():
                raise ValueError("Email already registered")
            logger.error("Supabase registration failed: %s", e)
            raise ValueError(f"Registration failed: {err_msg}")

    def _register_local(self, email, password, display_name,
                        phone, nationality, gender, birth_date) -> dict:
        """Register in local SQLite (fallback)."""
        user_id = secrets.token_hex(16)
        hash_hex, salt = _hash_password(password)
        now = time.time()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO users (id, email, display_name, password_hash, password_salt, "
                    "phone, nationality, gender, birth_date, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (user_id, email, display_name, hash_hex, salt,
                     phone.strip(), nationality.strip(), gender.strip(),
                     birth_date.strip(), now),
                )
                conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("Email already registered")

        token = _create_token(user_id, email)
        return {
            "user_id": user_id,
            "email": email,
            "display_name": display_name,
            "token": token,
            "provider": "local",
        }

    # ─── Login ────────────────────────────────────────────────

    def login(self, email: str, password: str) -> dict:
        """Authenticate a user. Uses Supabase Auth if available."""
        email = email.strip().lower()

        if self._supabase:
            return self._login_supabase(email, password)
        return self._login_local(email, password)

    def _login_supabase(self, email: str, password: str) -> dict:
        """Login via Supabase GoTrue."""
        try:
            result = self._supabase.auth.sign_in_with_password({
                "email": email,
                "password": password,
            })

            user = result.user
            session = result.session

            if not user or not session:
                raise ValueError("Invalid email or password")

            # Extract display_name from user metadata
            metadata = user.user_metadata or {}
            display_name = metadata.get("display_name", email.split("@")[0])

            return {
                "user_id": user.id,
                "email": user.email or email,
                "display_name": display_name,
                "token": session.access_token,
                "refresh_token": session.refresh_token,
                "provider": "supabase",
            }
        except Exception as e:
            err_msg = str(e)
            if "invalid" in err_msg.lower() or "credentials" in err_msg.lower():
                raise ValueError("Invalid email or password")
            logger.error("Supabase login failed: %s", e)
            raise ValueError("Invalid email or password")

    def _login_local(self, email: str, password: str) -> dict:
        """Login via local SQLite (fallback)."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, email, display_name, password_hash, password_salt FROM users WHERE email = ?",
                (email,),
            ).fetchone()

        if not row:
            raise ValueError("Invalid email or password")

        user_id, db_email, display_name, hash_hex, salt = row
        if not _verify_password(password, hash_hex, salt):
            raise ValueError("Invalid email or password")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), user_id))
            conn.commit()

        token = _create_token(user_id, db_email)
        return {
            "user_id": user_id,
            "email": db_email,
            "display_name": display_name,
            "token": token,
            "provider": "local",
        }

    # ─── Token Verification ───────────────────────────────────

    def verify_token(self, token: str) -> dict | None:
        """Verify a token. Tries Supabase first, then local."""
        if self._supabase:
            result = self._verify_supabase_token(token)
            if result:
                return result
        # Fallback to local token verification
        return _verify_local_token(token)

    def _verify_supabase_token(self, token: str) -> dict | None:
        """Verify a Supabase JWT access token."""
        try:
            result = self._supabase.auth.get_user(token)
            user = result.user
            if not user:
                return None
            metadata = user.user_metadata or {}
            return {
                "user_id": user.id,
                "email": user.email,
                "display_name": metadata.get("display_name", ""),
                "provider": "supabase",
            }
        except Exception:
            return None

    # ─── Refresh Token ────────────────────────────────────────

    def refresh_session(self, refresh_token: str) -> dict | None:
        """Refresh a Supabase session using the refresh token."""
        if not self._supabase:
            return None
        try:
            result = self._supabase.auth.refresh_session(refresh_token)
            session = result.session
            user = result.user
            if not session or not user:
                return None
            metadata = user.user_metadata or {}
            return {
                "user_id": user.id,
                "email": user.email,
                "display_name": metadata.get("display_name", ""),
                "token": session.access_token,
                "refresh_token": session.refresh_token,
                "provider": "supabase",
            }
        except Exception as e:
            logger.warning("Session refresh failed: %s", e)
            return None

    # ─── User Info ────────────────────────────────────────────

    def get_user(self, user_id: str) -> dict | None:
        """Get user info by ID."""
        if self._supabase:
            info = self._get_user_supabase(user_id)
            if info:
                return info
        return self._get_user_local(user_id)

    def _get_user_supabase(self, user_id: str) -> dict | None:
        """Get user from Supabase using service key if available."""
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        if not service_key:
            # Without service key, we can't lookup by ID directly
            # Caller should use verify_token instead
            return None
        try:
            from supabase import create_client
            url = os.environ.get("SUPABASE_URL", "").strip()
            admin_client = create_client(url, service_key)
            result = admin_client.auth.admin.get_user_by_id(user_id)
            user = result.user
            if not user:
                return None
            metadata = user.user_metadata or {}
            return {
                "user_id": user.id,
                "email": user.email,
                "display_name": metadata.get("display_name", ""),
                "phone": metadata.get("phone", ""),
                "nationality": metadata.get("nationality", ""),
                "gender": metadata.get("gender", ""),
                "birth_date": metadata.get("birth_date", ""),
                "created_at": user.created_at,
                "provider": "supabase",
            }
        except Exception as e:
            logger.warning("Supabase get_user failed: %s", e)
            return None

    def _get_user_local(self, user_id: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, email, display_name, phone, nationality, gender, "
                "birth_date, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "email": row[1],
            "display_name": row[2],
            "phone": row[3],
            "nationality": row[4],
            "gender": row[5],
            "birth_date": row[6],
            "created_at": row[7],
            "provider": "local",
        }

    # ─── Logout (Supabase) ────────────────────────────────────

    def logout(self, token: str) -> bool:
        """Sign out from Supabase (invalidate the session)."""
        if not self._supabase:
            return True  # Local tokens just expire naturally
        try:
            self._supabase.auth.sign_out(token)
            return True
        except Exception as e:
            logger.warning("Supabase logout failed: %s", e)
            return False
