"""App-wide configuration."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Google Sheets live sync -------------------------------------------
# Every time a user row is created or updated, the whole user_details
# table is pushed to this sheet so it always mirrors the database instantly.
#
# Reuses the same service account as the aimdd project, but points at a
# brand new, separate spreadsheet for this app.
#
# SETUP (one-time):
#   1. Create a new Google Sheet.
#   2. Share it (Editor access) with this service account's email:
#        ahv-sheets-writer@erudite-marker-505605-i6.iam.gserviceaccount.com
#   3. Paste the sheet's URL below as GOOGLE_SHEET_URL.
GOOGLE_SHEETS_ENABLED = True
GOOGLE_SERVICE_ACCOUNT_FILE = BASE_DIR / "config" / "google_service_account.json"
GOOGLE_SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/1l106sTklarJcTapAFC3p7UpjHQPC9OIRc8vW1tX2MEg/edit?usp=sharing"
)
GOOGLE_SHEET_WORKSHEET_NAME = "Sheet1"
