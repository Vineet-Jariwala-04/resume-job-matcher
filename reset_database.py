"""Run this any time you want a completely fresh start on the user_details
table: it DROPS user_details and recreates it with the same name and
columns (via app.init_db()), which also resets SQLite's AUTOINCREMENT
counter - so the next signup gets user_id = 1 again instead of continuing
from wherever the old table left off.

The Google Sheet is then cleared to match. If Google can't be reached the
script skips that step after a few seconds instead of hanging.

Usage:
    python reset_database.py          reset + update the Google Sheet
    python reset_database.py --fast   reset only (instant); the sheet is
                                      refreshed on the next signup / login
"""

import sys
import threading
import time

import config

FAST = "--fast" in sys.argv
if FAST:
    config.GOOGLE_SHEETS_ENABLED = False  # skip the internet step entirely

import app

if __name__ == "__main__":
    t0 = time.time()
    removed = app.clear_all_users()
    print(f"Dropped and recreated user_details (had {removed} row(s)). "
          f"Next signup will start at user_id = 1.", flush=True)

    # The sheet update runs in a background thread (see app._sync_to_sheets);
    # wait for it so the script doesn't exit before the sheet is cleared.
    for t in threading.enumerate():
        if t.name == "sheets-sync":
            if not FAST:
                print("Updating Google Sheet...", flush=True)
            t.join(timeout=30)
    print(f"Done in {time.time() - t0:.1f}s", flush=True)
