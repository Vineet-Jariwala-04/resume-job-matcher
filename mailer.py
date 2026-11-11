"""
Gmail notifications for the Resume & Job Matcher.

Emails are sent to the address the user signed up / logged in with
(normal users and admins alike):

  * signup           -> "You have signed up"   (+ account details)
  * login            -> "You have logged in"   (+ account details)
  * admin approved   -> "Your admin account is approved"
  * logout           -> "You have logged out"  (+ latest resume/job match result
                        and the match report PDF attached)

Setup (once):
  1. Turn on 2-Step Verification on the sending Gmail account.
  2. Create an App Password: https://myaccount.google.com/apppasswords
  3. Copy .env.example to .env and fill in GMAIL_ADDRESS / GMAIL_APP_PASSWORD.
  4. Run:  python test_email.py   (sends a test email and shows any problem)

Sending runs on a background thread, so a slow or failing Gmail call can
never delay or break signup / login / logout. Failures are only logged.
Passwords and password hashes are never put in an email.
"""

import logging
import os
import re
import smtplib
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

log = logging.getLogger("mailer")


def _say(text):
    """Always visible in the terminal (the default logger hides INFO lines)."""
    print(f"[mailer] {text}", flush=True)

BASE_DIR = Path(__file__).resolve().parent
APP_NAME = "Resume & Job Matcher"
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------- settings --

def _load_env_file():
    """Tiny .env reader (KEY=VALUE per line) so no extra package is needed.
    Real environment variables always win over the file."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _settings():
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    # Google shows app passwords in groups of 4 with spaces; both forms work.
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    enabled = os.environ.get("EMAIL_ENABLED", "true").strip().lower() != "false"
    return address, password, enabled


def is_configured():
    address, password, enabled = _settings()
    return bool(address and password and enabled)


# ------------------------------------------------------------------ helpers --

def now_text():
    """Current time in Indian Standard Time, e.g. 24 Oct 2026, 03:41 PM IST."""
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")


def _fmt_time(db_text):
    """Database times look like '2026-10-24 15:41:07 IST'; show them nicely."""
    if not db_text:
        return "—"
    try:
        dt = datetime.strptime(str(db_text).replace(" IST", ""), "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d %b %Y, %I:%M %p IST")
    except ValueError:
        return str(db_text)


def _kind(user):
    return "Admin" if user["account_type"] == "admin" else "User"


def _row(label, value):
    return (
        f'<tr><td style="padding:8px 12px;color:#5b6b7a;border-bottom:1px solid #e8eef3;'
        f'width:38%">{escape(label)}</td>'
        f'<td style="padding:8px 12px;color:#12263a;border-bottom:1px solid #e8eef3;'
        f'font-weight:600">{escape(str(value))}</td></tr>'
    )


def _wrap_html(heading, intro, table_html, extra_html=""):
    return f"""\
<html><body style="margin:0;padding:0;background:#f2f6fa;font-family:Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden">
<tr><td style="background:#2f3e9e;color:#ffffff;padding:20px 24px">
<div style="font-size:12px;opacity:.85">{escape(APP_NAME)}</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">{escape(heading)}</div></td></tr>
<tr><td style="padding:22px 24px;color:#12263a;font-size:14px;line-height:1.55">
<p style="margin:0 0 14px">{intro}</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e8eef3;border-radius:8px;border-collapse:separate">{table_html}</table>
{extra_html}
<p style="margin:18px 0 0;color:#8794a1;font-size:12px">If this wasn't you, please contact the system administrator. We never send your password by email.</p>
</td></tr></table></td></tr></table></body></html>"""


def _account_pairs(user, action_label, action_time):
    pairs = [
        ("Name", user["name"]),
        ("User ID", user["user_id"]),
        ("Account type", _kind(user)),
        ("Email", user["email"]),
        ("Age", user["age"]),
        (action_label, action_time),
        ("Total logins", user["login_count"]),
        ("Signed up on", _fmt_time(user["signup_time"])),
    ]
    if user["account_type"] == "admin":
        pairs.append(("Admin approval", "Approved" if user["is_approved"] else "Pending"))
    return pairs


def _account_rows(user, action_label, action_time):
    return "".join(_row(k, v) for k, v in _account_pairs(user, action_label, action_time))


def _account_text(user, action_label, action_time):
    return "\n".join(f"{k + ':':<16}{v}" for k, v in _account_pairs(user, action_label, action_time))


# ------------------------------------------------------------------- sending --

class _SMTP4(smtplib.SMTP):
    """SMTP that connects over IPv4 only (some networks break IPv6 mid-session)."""
    def _get_socket(self, host, port, timeout):
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        return socket.create_connection(addr, timeout, self.source_address)


class _SMTP_SSL4(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        raw = socket.create_connection(addr, timeout, self.source_address)
        return self.context.wrap_socket(raw, server_hostname=self._host)


def _open(mode, port, ipv4, context, debug=False):
    """Open + greet + (STARTTLS) a Gmail connection. Returns the server."""
    if mode == "ssl":
        server = (_SMTP_SSL4 if ipv4 else smtplib.SMTP_SSL)("smtp.gmail.com", port, context=context, timeout=30)
    else:
        server = (_SMTP4 if ipv4 else smtplib.SMTP)("smtp.gmail.com", port, timeout=30)
        server.ehlo()
        server.starttls(context=context)
    server.ehlo()
    if debug:
        server.set_debuglevel(1)
        server._print_debug = _safe_debug  # never print the password / AUTH payloads
    return server


_SAFE_VERBS = {"EHLO", "HELO", "STARTTLS", "MAIL", "RCPT", "DATA", "QUIT", "RSET", "NOOP"}


def _safe_debug(*args):
    """smtplib debug printer that hides everything sensitive (AUTH payloads)."""
    text = " ".join(str(a) for a in args)
    if text.startswith("reply:"):
        print("   ", text[:200])
    elif text.startswith("send:"):
        verb = text[5:].strip().strip("'\"").split(" ")[0].upper().rstrip("\\R\\N")
        if verb.startswith("AUTH"):
            print("    send: AUTH <hidden>")
        elif verb in _SAFE_VERBS:
            print("    send:", verb)
        else:
            print("    send: <hidden>")


def _login(server, address, password, auth):
    """Log in forcing one AUTH method ('PLAIN' or 'LOGIN'); some networks only
    break one of them."""
    server.esmtp_features["auth"] = auth
    server.login(address, password)


_AUTH_METHODS = ("PLAIN", "LOGIN")


def _send_now(message):
    """Try Gmail over IPv4 then the system default, ports 587 then 465, and
    AUTH PLAIN then AUTH LOGIN. Some networks / antivirus 'email shields'
    break one of these paths."""
    address, password, _ = _settings()
    context = ssl.create_default_context()
    errors = []
    for ipv4 in (True, False):
        for mode, port in (("starttls", 587), ("ssl", 465)):
            for auth in _AUTH_METHODS:
                tag = f"{'IPv4' if ipv4 else 'default'} port {port} AUTH {auth}"
                try:
                    with _open(mode, port, ipv4, context) as server:
                        _login(server, address, password, auth)
                        server.send_message(message)
                    return
                except smtplib.SMTPAuthenticationError as exc:
                    # Gmail answered and refused the credentials: other paths fail the same way.
                    raise RuntimeError(
                        "Gmail rejected the login. Use a 16-character App Password (not your normal "
                        "password) from the SAME Gmail account as GMAIL_ADDRESS, with 2-Step Verification on."
                    ) from exc
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{tag}: {type(exc).__name__}: {exc}")
    raise RuntimeError("Could not send via Gmail. " + " | ".join(errors[:4]) +
                       " ... -> run python test_email.py for details.")


def _build(to_email, subject, text_body, html_body, pdf_bytes=None, pdf_name="resume_match_report.pdf"):
    address, _, _ = _settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{APP_NAME} <{address}>"
    msg["To"] = to_email
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if pdf_bytes:
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_name)
    return msg


def _dispatch(message, label):
    """Send on a background thread; never raise into the caller."""
    def _worker():
        try:
            _send_now(message)
            _say(f"Email SENT ({label}) to {message['To']}")
        except Exception as exc:  # noqa: BLE001 - email must never break the app
            _say(f"Email FAILED ({label}) to {message['To']}: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


def _ready(label, user):
    if not is_configured():
        _say(f"Email '{label}' for {user['email']} SKIPPED: Gmail is not configured (check the .env file).")
        return False
    return True


# ------------------------------------------------------------ public emails --

def _approver_text(approvers):
    """'Name (email)' for each existing admin who can approve, or ''."""
    return ", ".join(f"{a['name']} ({a['email']})" for a in (approvers or []))


def send_signup_email(user, pending_approval=False, approvers=None):
    """approvers: existing approved admins (rows) who must approve a new admin."""
    if not _ready("signup", user):
        return
    when = now_text()
    approver_line = _approver_text(approvers)
    if pending_approval:
        heading = "Signup received — approval pending"
        who = (f" by the existing admin: <b>{escape(approver_line)}</b>" if approver_line
               else " by an existing admin")
        who_text = f" by the existing admin: {approver_line}" if approver_line else " by an existing admin"
        intro = (f"Hi {escape(user['name'])}, you have signed up as an <b>admin</b>. "
                 f"A new admin account needs the <b>approval of the previous admin</b>. "
                 f"Your request must be approved{who} before you can log in. "
                 "You will get another email as soon as it is approved.")
        text_intro = (f"Hi {user['name']}, you have signed up as an admin. A new admin account needs the "
                      f"approval of the previous admin. Your request must be approved{who_text} before you "
                      "can log in. You will get another email as soon as it is approved.")
    else:
        heading = "You have signed up"
        intro = f"Hi {escape(user['name'])}, you have signed up successfully. Here are your account details:"
        text_intro = f"Hi {user['name']}, you have signed up successfully. Your account details:"
    html = _wrap_html(heading, intro, _account_rows(user, "Signed up at", when))
    text = f"{heading}\n\n{text_intro}\n\n{_account_text(user, 'Signed up at', when)}\n"
    _dispatch(_build(user["email"], f"{APP_NAME}: {heading}", text, html), "signup")


def send_admin_request_notice(admins, new_user):
    """Tells each existing admin that someone asked for an admin account and
    is waiting for approval (Admin dashboard -> Approve)."""
    when = now_text()
    heading = "New admin request waiting for your approval"
    for admin in admins or []:
        if not _ready("admin-request", admin):
            continue
        intro = (f"Hi {escape(admin['name'])}, <b>{escape(new_user['name'])}</b> has asked for an admin "
                 "account. It needs your approval. Log in, open the <b>Admin dashboard</b> and click "
                 "<b>Approve</b> if you know this person.")
        rows = (_row("Name", new_user["name"]) + _row("Email", new_user["email"])
                + _row("Age", new_user["age"]) + _row("Requested at", when))
        html = _wrap_html(heading, intro, rows)
        text = (f"{heading}\n\nHi {admin['name']}, {new_user['name']} has asked for an admin account. "
                "It needs your approval. Log in, open the Admin dashboard and click Approve if you know "
                f"this person.\n\nName: {new_user['name']}\nEmail: {new_user['email']}\n"
                f"Age: {new_user['age']}\nRequested at: {when}\n")
        _dispatch(_build(admin["email"], f"{APP_NAME}: {heading}", text, html), "admin-request")


def send_login_email(user):
    if not _ready("login", user):
        return
    when = now_text()
    heading = "You have logged in"
    intro = f"Hi {escape(user['name'])}, you have logged in to your account. Login details:"
    html = _wrap_html(heading, intro, _account_rows(user, "Logged in at", when))
    text = (f"{heading}\n\nHi {user['name']}, you have logged in to your account.\n\n"
            f"{_account_text(user, 'Logged in at', when)}\n")
    _dispatch(_build(user["email"], f"{APP_NAME}: {heading}", text, html), "login")


def send_admin_approved_email(user):
    if not _ready("approved", user):
        return
    when = now_text()
    heading = "Your admin account is approved"
    intro = f"Hi {escape(user['name'])}, your admin account has been approved. You can now log in."
    html = _wrap_html(heading, intro, _account_rows(user, "Approved at", when))
    text = (f"{heading}\n\nHi {user['name']}, your admin account has been approved. You can now log in.\n\n"
            f"{_account_text(user, 'Approved at', when)}\n")
    _dispatch(_build(user["email"], f"{APP_NAME}: {heading}", text, html), "approved")


def no_analysis_mode():
    """What to do at logout when no analysis was run during that login:
       first_time (default) - send the 'no analysis done' message only on the user's very
                              first logout; stay silent on later logins that do nothing
       always               - send it every time
       never                - never send it"""
    mode = os.environ.get("NO_ANALYSIS_EMAIL", "first_time").strip().lower()
    return mode if mode in ("first_time", "always", "never") else "first_time"


def send_no_analysis_email(user):
    """Logout email for a login session in which no match analysis was run.
    No PDF and no old analysis are included."""
    if not _ready("logout-no-analysis", user):
        return
    when = now_text()
    heading = "You have logged out"
    intro = f"Hi {escape(user['name'])}, you have logged out."
    note = "No match analysis was done in this login, so there is no report to send."
    extra = (f'<p style="margin:18px 0 0;padding:12px 14px;background:#f4f6fb;border-radius:8px">'
             f'<b>{escape(note)}</b></p>')
    html = _wrap_html(heading, intro, _account_rows(user, "Logged out at", when), extra)
    text = (f"{heading}\n\nHi {user['name']}, you have logged out.\n\n"
            f"{_account_text(user, 'Logged out at', when)}\n\n{note}\n")
    _dispatch(_build(user["email"], f"{APP_NAME}: {heading} — no analysis done", text, html), "logout-no-analysis")


def send_logout_email(user, pdf_bytes=None, new_matches=None):
    """Logout email WITH an analysis (only call it when one was run this login).
    user: fresh user_details row. pdf_bytes: the latest match report PDF.
    new_matches: how many analyses were run during this login."""
    if not _ready("logout", user):
        return
    when = now_text()
    heading = "You have logged out"
    intro = f"Hi {escape(user['name'])}, you have logged out. Here is your resume match analysis from this login."

    has_match = bool(user["latest_match_markdown"])
    if has_match:
        pairs = [
            ("Match score", user["latest_match_percent"] or "—"),
            ("Resume file", user["latest_resume_filename"] or "—"),
            ("Job description file", user["latest_job_filename"] or "—"),
            ("Last matched at", _fmt_time(user["match_updated_at"])),
            ("Matches run in this login", new_matches if new_matches else 1),
            ("Total matches run", user["match_count"]),
        ]
    else:
        pairs = [("Latest match", "No resume match has been run yet")]

    match_table = (
        '<div style="margin:18px 0 8px;font-weight:700">Resume match analysis</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border:1px solid #e8eef3;border-radius:8px;border-collapse:separate">'
        + "".join(_row(k, v) for k, v in pairs) + "</table>"
    )
    if pdf_bytes:
        pdf_note = "Your match report PDF is attached to this email — open the attachment to download it."
    else:
        pdf_note = "No PDF report was attached because you haven't generated a match report yet."
    extra = match_table + f'<p style="margin:14px 0 0"><b>📎 {escape(pdf_note)}</b></p>'

    html = _wrap_html(heading, intro, _account_rows(user, "Logged out at", when), extra)
    text = (f"{heading}\n\nHi {user['name']}, you have logged out.\n\n"
            f"{_account_text(user, 'Logged out at', when)}\n\nResume match analysis\n"
            + "\n".join(f"{k + ':':<28}{v}" for k, v in pairs) + f"\n\n{pdf_note}\n")
    _dispatch(_build(user["email"], f"{APP_NAME}: {heading}", text, html, pdf_bytes,
                     pdf_name="resume_match_report.pdf"), "logout")


# ------------------------------------------------ one-time code + lockout --

def send_otp_email(to_email, name, code, minutes=10, wait_seconds=25):
    """Sends the signup verification code and WAITS for the result, so the
    app can tell the user straight away if the code could not be sent.
    Returns (ok, error_text). The code itself is never logged."""
    if not is_configured():
        _say(f"Email 'otp' for {to_email} SKIPPED: Gmail is not configured (check the .env file).")
        return False, "Email sending is not set up on the server, so the code could not be sent."
    heading = "Your verification code"
    spaced = " ".join(code)
    name = (name or "").strip() or "there"
    intro = (f"Hi {escape(name)}, use this code to verify your email and finish creating your account. "
             f"It is valid for {minutes} minutes.")
    code_box = (
        '<div style="margin:18px 0 6px;text-align:center">'
        '<div style="display:inline-block;padding:14px 26px;border-radius:10px;background:#eef3ff;'
        f'color:#0b2a6f;font-size:32px;font-weight:700;letter-spacing:8px">{escape(code)}</div></div>'
        '<p style="margin:10px 0 0;color:#8794a1;font-size:12px;text-align:center">'
        'Never share this code with anyone. We will never ask you for it.</p>'
    )
    html = _wrap_html(heading, intro, _row("Valid for", f"{minutes} minutes") + _row("Email", to_email), code_box)
    text = (f"{heading}\n\nHi {name}, use this code to finish creating your account:\n\n"
            f"    {spaced}\n\nIt is valid for {minutes} minutes. Never share this code with anyone.\n")
    message = _build(to_email, f"{APP_NAME}: {code} is your verification code", text, html)

    outcome = {}

    def _worker():
        try:
            _send_now(message)
            outcome["ok"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = str(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(wait_seconds)
    if outcome.get("ok"):
        _say(f"Email SENT (otp) to {to_email}")
        return True, None
    _say(f"Email FAILED (otp) to {to_email}: {outcome.get('error', 'timed out')}")
    return False, "The verification code could not be emailed right now. Please try again in a moment."


def send_account_locked_email(user):
    if not _ready("locked", user):
        return
    when = now_text()
    heading = "Your account has been locked"
    intro = (f"Hi {escape(user['name'])}, your account was <b>locked</b> after 3 wrong password attempts. "
             "For your security, only an administrator can unlock it. "
             "Please contact an admin and ask them to unlock your account.")
    html = _wrap_html(heading, intro, _row("Account", user["email"]) + _row("Locked at", when))
    text = (f"{heading}\n\nHi {user['name']}, your account was locked after 3 wrong password attempts.\n"
            "Only an administrator can unlock it. Please contact an admin.\n\n"
            f"Account: {user['email']}\nLocked at: {when}\n")
    _dispatch(_build(user["email"], f"{APP_NAME}: {heading}", text, html), "locked")


def send_account_unlocked_email(user):
    if not _ready("unlocked", user):
        return
    when = now_text()
    heading = "Your account is unlocked"
    intro = f"Hi {escape(user['name'])}, an administrator has unlocked your account. You can log in again."
    html = _wrap_html(heading, intro, _row("Account", user["email"]) + _row("Unlocked at", when))
    text = (f"{heading}\n\nHi {user['name']}, an administrator has unlocked your account. "
            f"You can log in again.\n\nAccount: {user['email']}\nUnlocked at: {when}\n")
    _dispatch(_build(user["email"], f"{APP_NAME}: {heading}", text, html), "unlocked")
