"""Gmail diagnosis:  python test_email.py
Shows exactly WHICH step fails (connect / secure / hello / login / send).
Passwords are never printed."""
import smtplib
import ssl

import mailer

address, password, enabled = mailer._settings()
print("GMAIL_ADDRESS     :", address or "(missing)")
print("GMAIL_APP_PASSWORD:", "set (%d chars)" % len(password) if password else "(missing)")
print("EMAIL_ENABLED     :", enabled)
if not (address and password):
    raise SystemExit("Fill GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first.")
if not password.isalnum() or not password.isascii():
    print("WARNING: App Password should be 16 plain letters only. Re-copy it without quotes/symbols.")

ctx = ssl.create_default_context()
msg = mailer._build(address, f"{mailer.APP_NAME}: Gmail test",
                    "Gmail is working. Signup/login/logout emails will arrive.",
                    "<p><b>Gmail is working.</b> Signup/login/logout emails will arrive.</p>")


def attempt(ipv4, mode, port, auth, debug=False):
    tag = f"{'IPv4   ' if ipv4 else 'default'} port {port} AUTH {auth}"
    stage = "connect + secure + hello"
    try:
        server = mailer._open(mode, port, ipv4, ctx, debug=debug)
        with server:
            stage = "login"
            mailer._login(server, address, password, auth)
            stage = "send"
            server.send_message(msg)
        print(f"[ OK ] {tag}: test email sent to {address} (check Inbox/Spam)")
        return True
    except smtplib.SMTPAuthenticationError as exc:
        print(f"[FAIL] {tag}: Gmail answered but REJECTED the login ({exc.smtp_code}). "
              "Wrong App Password or wrong Gmail account.")
        raise SystemExit(1)
    except Exception as exc:
        print(f"[FAIL] {tag}: stopped at '{stage}' -> {type(exc).__name__}: {exc}")
        return False


ok = False
for ipv4 in (True, False):
    for mode, port in (("starttls", 587), ("ssl", 465)):
        for auth in mailer._AUTH_METHODS:
            if attempt(ipv4, mode, port, auth):
                ok = True
                break
        if ok:
            break
    if ok:
        break

if not ok:
    print("\n--- Detailed view of one attempt (passwords hidden) ---")
    attempt(True, "starttls", 587, "PLAIN", debug=True)
    print("\nAll paths failed. Copy this WHOLE output and send it back.")
