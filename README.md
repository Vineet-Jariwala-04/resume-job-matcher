# Resume & Job Matcher (HTML/CSS + Flask version)

A local web app that compares a Resume against a Job Description using a
local LLM (via Ollama) and gives a fit score, strengths, and suggestions.

This version uses a **Flask backend** with a plain **HTML/CSS/JS frontend**
(no Streamlit), styled with the **Cambria** font, a light/dark theme toggle,
and a slightly larger base font size.

## Setup

1. Install [Ollama](https://ollama.com/download) and pull the model:
   ```
   ollama pull qwen2.5:0.5b
   ```
2. Create a virtual environment and install dependencies:
   ```
   python -m venv venv
   venv\Scripts\activate        (Windows)
   pip install -r requirements.txt
   ```
3. Run the app:
   ```
   python app.py
   ```
4. Open your browser to: http://127.0.0.1:5000

## Notes

- The model used is set in `app.py` via the `MODEL_NAME` constant — change it
  if you have a more powerful machine and want to use a bigger model.
- The light/dark mode toggle remembers your choice using the browser's
  local storage.
- Long resumes/job descriptions are automatically trimmed so the model
  responds quickly and doesn't run out of context.
- Resumes and job descriptions can be uploaded as PDF, DOCX, or TXT.
- Every report includes an **ATS Friendliness** check (contact info,
  standard section headings, bullet usage, length, tables/scanned-image
  detection, special characters) computed directly from the file — not by
  the LLM — so it stays accurate regardless of the model's own analysis.


---

## 📧 Gmail notifications

Emails go to the address each user (normal user **or** admin) signed up / logged in with:

| Event | Email |
|---|---|
| Sign up | "You have signed up" + name, user ID, account type, email, age, date & time. A new **admin** instead gets "approval pending", naming the existing admin who must approve; the existing admin(s) also get a "new admin request" email |
| Admin approved | "Your admin account is approved" |
| Log in | "You have logged in" + the same account details, login time, total logins |
| Log out | Only what you did **in that login**: if you ran an analysis, "You have logged out" + that analysis (score, files, time) + the **match report PDF attached**. If you did nothing, a short "no analysis done" message (first logout only) or no email at all. An older analysis is never re-sent. |

**Setup (one time)**
1. On the sending Gmail account turn on **2-Step Verification**.
2. Create an **App Password**: https://myaccount.google.com/apppasswords
3. Copy `.env.example` to `.env` (same folder as `app.py`) and fill in `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` (16 letters, no spaces, no quotes).
4. Run `python test_email.py` — it should print `[ OK ]` and send you a test email.
5. Restart the app (`python app.py`).

Notes: emails send on a background thread, so a Gmail problem never blocks signup/login/logout (failures print in the app window). Passwords are never emailed. The logout email is sent when the user clicks **Log out**. Set `EMAIL_ENABLED=false` in `.env` to switch emails off.

**Logout email rules** (set `NO_ANALYSIS_EMAIL` in `.env`):
- `first_time` (default): login → do nothing → logout sends "no analysis done" only the first time; later do-nothing logins send nothing.
- `always`: send the "no analysis done" message on every do-nothing login.
- `never`: never send it.


---

## 🔐 Email verification (OTP) on the signup form + account lock

**Signup form order:** Name and Age → **Email with a "Send OTP" button beside it** → the code box right below the email → Password → Sign Up.
1. Type your email and click **Send OTP**. A 6-digit code is emailed (works for 10 minutes).
2. Type the code in the box under the email and click **Verify**.
3. Only after "Email verified" does the **Sign Up** button turn on. **No account is created before the email is verified** (the server checks this too, not just the page).

Details: 5 wrong codes cancel the code (click Resend OTP); a new code can be requested once every 60 seconds (max 5 per hour per email, 20 per hour per device). Changing the email after verifying means verifying again. To switch this off, set `SIGNUP_OTP=false` in `.env` (not recommended).

**3 wrong passwords lock the account.**
- The 3rd wrong password locks the account (the owner gets an email). A locked account cannot log in, even with the right password.
- Only an **admin** can unlock it: Admin dashboard → **Locked accounts** → **Unlock**. The owner gets an "unlocked" email.
- A correct login resets the wrong-password counter.
- If the only admin is locked, run on the server PC: `python unlock_account.py admin@example.com`

Security note: the "email verified" flag lives in the signed login session. The app now creates its own private secret key on first start (file `.flask_secret_key`, keep it private) or uses `FLASK_SECRET_KEY` from `.env` if you set one.

Existing databases are upgraded automatically the first time `python app.py` starts.


---

## 🆕 History, progress bar, image uploads and ATS resume builder

**Match history (History tab):** every analysis is saved (newest first, up to 200 per user). Reopen a report, download its PDF, delete it, or build an ATS resume from it. Works for users and admins.

**Progress indicator:** the analysis now runs in the background with a live progress bar and step list (Reading files, ATS check, AI comparing, Skills, Saving). The local model handles one task at a time, so if two people analyse at once the second sees "you are number 1 in the queue" and starts automatically.

**File types:** PDF, DOCX and TXT (as before) plus **PNG, JPG, JPEG**. Images are read with OCR (RapidOCR, offline, pure `pip`; the first image loads the OCR model, a few seconds). Scanned PDFs are OCR'd too. A resume that is only a picture is flagged in the ATS panel, because real ATS software cannot read pictures.

**ATS resume builder (below every analysis, and on a history entry):**
1. The tool lists what the job asks for but your resume does not show (skills and spoken languages) with checkboxes. **Tick only what you genuinely have.**
2. It rebuilds your resume in a single-column, ATS-safe layout (standard headings, real bullet list, no tables/columns/images/symbols), uses the job's wording (JS -> JavaScript), adds your confirmed skills, and writes a summary for the job.
3. It shows an estimated **ATS score before -> after** (60% job keywords + 40% format checks), what changed, a job-requirements check (years, degree, work type, languages), writing tips, what is still missing, a preview, and **Word (.docx) + PDF downloads**.

**Nothing is invented.** The new resume contains only what is already in your resume plus what you tick. Skills you do not tick are listed as "still missing" and are never added. The AI model only polishes the summary and a few bullet points, and every answer is checked against your original resume; if it adds a tool, number or name that was not there, that rewrite is thrown away and your original wording is kept.

**Model note:** the default model `qwen2.5:0.5b` is tiny, so most of the resume rebuilding is rule-based and safe, and the AI polish is often rejected. For better summaries run `ollama pull qwen2.5:3b` and set `OLLAMA_MODEL=qwen2.5:3b` in `.env`.

**Install:** run `pip install -r requirements.txt` again (adds the OCR package).
