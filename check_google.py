"""Finds out why the Google Sheets sync is slow on this computer.
Run:  python check_google.py      (takes up to ~1 minute if the network is bad)
Then send the printed output back so the slow step can be fixed.
"""
import socket
import time

HOSTS = ["oauth2.googleapis.com", "sheets.googleapis.com"]


def timed(label, fn):
    t = time.time()
    try:
        fn()
        print(f"  OK    {label:34s} {time.time() - t:5.1f}s")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {label:34s} {time.time() - t:5.1f}s  {type(exc).__name__}: {str(exc)[:70]}")


print("1) Network reachability (IPv4 vs IPv6)")
for host in HOSTS:
    for fam, name in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        def conn(host=host, fam=fam):
            addr = socket.getaddrinfo(host, 443, fam, socket.SOCK_STREAM)[0][4]
            s = socket.socket(fam); s.settimeout(8); s.connect(addr); s.close()
        timed(f"{host} via {name}", conn)

print("2) Google login token")
import config
from google.oauth2 import service_account
from google.auth.transport.requests import Request
creds = service_account.Credentials.from_service_account_file(
    str(config.GOOGLE_SERVICE_ACCOUNT_FILE),
    scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"])
timed("get access token", lambda: creds.refresh(Request()))

print("3) Open the sheet")
import gspread
def open_sheet():
    c = gspread.authorize(creds)
    c.set_timeout((6, 15))
    c.open_by_url(config.GOOGLE_SHEET_URL)
timed("open spreadsheet", open_sheet)
print("Done.")
