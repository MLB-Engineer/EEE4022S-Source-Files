import os
import sqlite3
from pathlib import Path

import bcrypt

# Configurable so a Docker named volume can be mounted at a directory
# (volumes are directories, not files — mounting one directly onto a
# single file path like the old DB_FILE made Docker create an empty
# *directory* there instead, since users.db doesn't exist in the built
# image, which broke sqlite3.connect with "unable to open database
# file"). Defaults to website/ itself when running outside Docker.
DB_FILE = Path(os.getenv("TUKZIE_DB_PATH", str(Path(__file__).resolve().parent / "users.db")))
DB_FILE.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_USERS = [
    # Regenerated correct bcrypt hashes — the previous defaults (from the
    # original zip) didn't actually verify against their documented
    # passwords at all (a pre-existing bug: initialize_db() only seeds
    # these if users.db doesn't exist yet, so a first run with no prior
    # users.db would have logins fail out of the box, silently, until
    # someone happened to run seed_db.py instead).
    ("operator", "$2b$12$dPaRjRp0w.QNmXNoB8bat.eBrx1W98Cdb0S1dzCpk.igC0SFJx9/W", "operator"),
    ("developer", "$2b$12$qGOO8oH95H6aWS7AE/NkveIZGlmsLf5aKJ8q9OLx9P3cqYqvDIwCu", "developer"),
    ("guest", "$2b$12$JpCD6/Ye5QuxeqD4TLHULeXdlfirdIL073MhNFOqBJKWIbL2onGFa", "guest"),
]


def initialize_db() -> Path:
    conn = sqlite3.connect(DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
        """
    )

    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count == 0:
        for email, hashed_password, role in DEFAULT_USERS:
            conn.execute(
                "INSERT INTO users (email, password, role) VALUES (?, ?, ?)",
                (email, hashed_password, role),
            )

    conn.commit()
    conn.close()
    return DB_FILE


def _connect() -> sqlite3.Connection:
    initialize_db()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_user(email):
    conn = _connect()
    try:
        return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()


def create_user(email, password, role="operator"):
    conn = _connect()
    try:
        # bcrypt hard-caps input at 72 bytes; truncate rather than crash
        # (this is also what passlib's bcrypt__truncate_error=True was
        # trying and failing to do safely on newer bcrypt).
        password_bytes = password.encode("utf-8")[:72]
        hashed_password = bcrypt.hashpw(password_bytes, bcrypt.gensalt(rounds=12)).decode("utf-8")
        conn.execute(
            "INSERT INTO users (email, password, role) VALUES (?, ?, ?)",
            (email, hashed_password, role),
        )
        conn.commit()
    finally:
        conn.close()


def verify_password(plain, hashed):
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except ValueError:
        # malformed/foreign hash format — treat as "wrong password" rather
        # than a 500
        return False