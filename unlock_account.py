"""Emergency unlock, run on the computer that hosts the database.

Normally an admin unlocks accounts from the Admin dashboard ("Locked accounts").
Use this only if NO admin can log in (for example the only admin locked
themselves out).

Usage:
    python unlock_account.py admin@example.com
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "resume_matcher.db"


def main(email, db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        user = conn.execute("SELECT user_id, email, is_locked FROM user_details WHERE email = ?",
                            (email.strip().lower(),)).fetchone()
        if user is None:
            return f"No account found for {email}."
        if not user["is_locked"]:
            return f"{user['email']} is not locked."
        conn.execute("UPDATE user_details SET is_locked = 0, failed_attempts = 0, locked_at = NULL WHERE user_id = ?",
                     (user["user_id"],))
        conn.commit()
        return f"Unlocked {user['email']}. They can log in again."
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python unlock_account.py <email>")
    print(main(sys.argv[1]))
