"""Reset website/users.db (or wherever TUKZIE_DB_PATH points) back to
the three default demo accounts.

This used to duplicate database.py's user-seeding logic with its own
copy — including its own now-removed `passlib` import (see
database.py's history: passlib broke against bcrypt>=4.1) and its own
hardcoded "users.db" path that ignored TUKZIE_DB_PATH (used by the
Docker Compose setup to put the db in a mounted volume). Now it just
deletes the existing file and re-runs database.py's own
initialize_db(), so there's exactly one place — DEFAULT_USERS in
database.py — that defines the seed accounts and how they're hashed.
"""
import database


def create_fresh_db():
    if database.DB_FILE.exists():
        try:
            database.DB_FILE.unlink()
            print(f"Removed old database at {database.DB_FILE}")
        except PermissionError:
            print(f"Error: {database.DB_FILE} is currently in use. Close your backend/terminal and try again.")
            return

    database.initialize_db()
    print(f"Successfully created {database.DB_FILE}")


if __name__ == "__main__":
    create_fresh_db()
