"""Pushes the user_details table to a Google Sheet, live, on every change.

Uses a Google Cloud service account (config.GOOGLE_SERVICE_ACCOUNT_FILE) to
authenticate - no user's Google login is ever involved. If anything about
the sync fails (no internet, sheet not shared with the service account,
wrong URL, etc.) it's printed as a warning and the app keeps working
normally; a Sheets hiccup should never break signup, login, or matching.
"""

import socket
import threading
import time

import config

# Column order shown in the sheet - mirrors user_details, minus the
# password hash and the raw resume/JD file bytes (none of those belong in
# a spreadsheet). Full extracted resume/job text is left out too, to keep
# rows readable - the admin dashboard's Download links cover that.
_COLUMNS = [
    "user_id", "name", "age", "email", "account_type", "is_approved",
    "login_count", "signup_time", "last_login_time", "last_logout_time",
    "latest_resume_filename", "latest_job_filename", "latest_match_percent",
    "match_count", "match_updated_at", "last_pdf_generated_time",
]

_lock = threading.Lock()
_offline_until = 0.0     # after a failed reachability check, skip syncing until this time
_OFFLINE_COOLDOWN = 30   # seconds
_client = None
_worksheet = None


def _get_worksheet():
    """Lazily authenticates and opens the worksheet, and caches it for reuse."""
    global _client, _worksheet
    if _worksheet is not None:
        return _worksheet

    import gspread  # imported lazily so the app still runs if it's not installed

    if not config.GOOGLE_SERVICE_ACCOUNT_FILE.exists():
        raise FileNotFoundError(
            f"Service account key not found at {config.GOOGLE_SERVICE_ACCOUNT_FILE}"
        )

    _client = gspread.service_account(filename=str(config.GOOGLE_SERVICE_ACCOUNT_FILE))
    # Fail fast (connect 6s, read 15s) if Google can't be reached, instead of
    # freezing for minutes on a blocked / offline network.
    try:
        _client.set_timeout((6, 15))
    except Exception:  # noqa: BLE001 - older gspread versions: just skip
        pass
    spreadsheet = _client.open_by_url(config.GOOGLE_SHEET_URL)
    try:
        _worksheet = spreadsheet.worksheet(config.GOOGLE_SHEET_WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        _worksheet = spreadsheet.sheet1
    return _worksheet


def _check_google_reachable():
    """Quick TCP check (max ~4s per host) so a blocked / offline network is
    detected immediately. The Google auth library's own token request has no
    short connect timeout, which is what made scripts appear to freeze."""
    for host in ("oauth2.googleapis.com", "sheets.googleapis.com"):
        socket.create_connection((host, 443), timeout=4).close()


def sync_rows(rows):
    """rows: list of dicts (one per user, straight from user_details).
    Overwrites the whole sheet with a fresh header + data dump, so the sheet
    always exactly matches the database - safe to call after every write."""
    if not config.GOOGLE_SHEETS_ENABLED:
        return

    global _offline_until
    with _lock:
        if _worksheet is None:
            if time.time() < _offline_until:
                print("[sheets_sync] Skipped Google Sheets sync: Google was unreachable a moment ago.")
                return
            try:
                _check_google_reachable()
            except OSError as exc:
                _offline_until = time.time() + _OFFLINE_COOLDOWN
                print(f"[sheets_sync] Skipped Google Sheets sync: can't reach Google ({exc}). "
                      "The next change will retry.")
                return
        try:
            t0 = time.time()
            worksheet = _get_worksheet()
            t1 = time.time()
            values = [_COLUMNS]
            for row in rows:
                values.append(["" if row.get(col) is None else row.get(col) for col in _COLUMNS])
            worksheet.clear()
            t2 = time.time()
            worksheet.update(values, "A1")
            t3 = time.time()
            print(f"[sheets_sync] Synced {len(rows)} row(s) in {t3 - t0:.1f}s "
                  f"(connect/open {t1 - t0:.1f}s, clear {t2 - t1:.1f}s, write {t3 - t2:.1f}s)")
        except Exception as exc:  # noqa: BLE001 - a sync failure must never crash the app
            print(f"[sheets_sync] Skipped Google Sheets sync: {exc}")
