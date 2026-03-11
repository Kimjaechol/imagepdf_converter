"""Authentication service – user registration, login, JWT tokens.

Uses SQLite for user storage and bcrypt-compatible hashing via hashlib
(no external dependency). JWT is implemented with PyJWT.
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
from pathlib import Path

logger = logging.getLogger(__name__)

# JWT-like token using HMAC-SHA256 (lightweight, no PyJWT dependency needed)
_SECRET_KEY = os.environ.get("AUTH_SECRET_KEY", secrets.token_hex(32))
_TOKEN_EXPIRY_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Hash a password with a random salt. Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hashed.hex(), salt


def _verify_password(password: str, hash_hex: str, salt: str) -> bool:
    computed, _ = _hash_password(password, salt)
    return hmac.compare_digest(computed, hash_hex)


import base64


def _create_token(user_id: str, email: str) -> str:
    """Create a simple HMAC-signed token (JWT-like)."""
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


def _verify_token(token: str) -> dict | None:
    """Verify and decode a token. Returns payload dict or None."""
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
        # Restore padding
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


class AuthService:
    """Manages user accounts with SQLite storage."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "users.db"
        self._init_db()

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
            # Migrate existing tables: add new columns if missing
            self._migrate_columns(conn)

    def _migrate_columns(self, conn: sqlite3.Connection):
        """Add new profile columns to existing users table if missing."""
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

    def register(self, email: str, password: str, display_name: str = "",
                 phone: str = "", nationality: str = "", gender: str = "",
                 birth_date: str = "") -> dict:
        """Register a new user. Returns user info + token."""
        email = email.strip().lower()
        if not email or not password:
            raise ValueError("Email and password are required")
        if len(password) < 6:
            raise ValueError("Password must be at least 6 characters")

        user_id = secrets.token_hex(16)
        hash_hex, salt = _hash_password(password)
        now = time.time()

        display_name = display_name or email.split("@")[0]
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
        }

    def login(self, email: str, password: str) -> dict:
        """Authenticate a user. Returns user info + token."""
        email = email.strip().lower()
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

        # Update last_login
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), user_id))
            conn.commit()

        token = _create_token(user_id, db_email)
        return {
            "user_id": user_id,
            "email": db_email,
            "display_name": display_name,
            "token": token,
        }

    def verify_token(self, token: str) -> dict | None:
        """Verify a token and return payload, or None if invalid."""
        return _verify_token(token)

    def get_user(self, user_id: str) -> dict | None:
        """Get user info by ID."""
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
        }
