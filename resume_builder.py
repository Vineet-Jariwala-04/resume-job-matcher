"""Builds a new, ATS-friendly version of a resume for a specific job description.

The golden rule: NOTHING is invented. The new resume only contains
  * facts that are already in the original resume (re-ordered, cleaned, given
    standard ATS headings, and using the job description's wording), and
  * skills / languages the user explicitly ticked as "I really have this".
Skills the job asks for that the resume does not show are never added silently;
they are reported as "missing" so the user can add them only if they are true.

The (small, local) AI model is used only for two optional polish steps - the
summary and a few bullet points - and every model answer is checked against the
original resume; anything that adds new names, tools or numbers is thrown away
and the original wording is kept.
"""
import html
import io
import json
import re

# --------------------------------------------------------------------------
# Skills vocabulary: canonical name -> (category, aliases). Used to (a) find the
# skills a job asks for even when the AI model misses them, and (b) recognise
# the same skill under different spellings (JS = JavaScript, K8s = Kubernetes).
# --------------------------------------------------------------------------
_RAW_SKILLS = {
    "Languages": {
        "Python": [], "Java": [], "JavaScript": ["js", "ecmascript"], "TypeScript": ["ts"],
        "C++": ["cpp"], "C#": ["c sharp", "csharp"], "Go": ["golang"], "Rust": [], "PHP": [], "Ruby": [],
        "Kotlin": [], "Swift": [], "R": [], "MATLAB": [], "SQL": ["t-sql", "pl/sql"], "HTML": ["html5"],
        "CSS": ["css3"], "Bash": ["shell scripting", "shell script"], "Dart": [], "Scala": [],
    },
    "Frameworks & Libraries": {
        "React": ["reactjs", "react.js"], "Angular": ["angularjs"], "Vue.js": ["vue", "vuejs"],
        "Node.js": ["node", "nodejs"], "Express.js": ["express", "expressjs"], "Django": [], "Flask": [],
        "FastAPI": [], "Spring Boot": ["springboot", "spring"], ".NET": ["dotnet", "asp.net"], "Laravel": [],
        "TensorFlow": [], "PyTorch": [], "Scikit-learn": ["sklearn", "scikit learn"], "Pandas": [],
        "NumPy": [], "Keras": [], "OpenCV": [], "Bootstrap": [], "Tailwind CSS": ["tailwind"],
        "Next.js": ["nextjs"], "jQuery": [], "Streamlit": [],
    },
    "Databases": {
        "MySQL": [], "PostgreSQL": ["postgres", "postgre sql"], "MongoDB": ["mongo"], "SQLite": [],
        "Oracle": ["oracle db"], "Redis": [], "Firebase": [], "SQL Server": ["mssql", "ms sql"],
        "DynamoDB": [], "Elasticsearch": [],
    },
    "Cloud & DevOps": {
        "AWS": ["amazon web services"], "Azure": ["microsoft azure"], "Google Cloud": ["gcp"], "Docker": [],
        "Kubernetes": ["k8s"], "Jenkins": [], "Terraform": [], "CI/CD": ["cicd", "ci cd"], "Linux": [],
        "Git": [], "GitHub": [], "GitLab": [], "Ansible": [], "Nginx": [],
    },
    "Data & AI": {
        "Machine Learning": ["ml"], "Deep Learning": [], "Natural Language Processing": ["nlp"],
        "Computer Vision": [], "Data Analysis": ["data analytics"], "Data Visualization": ["data visualisation"],
        "Power BI": ["powerbi"], "Tableau": [], "Excel": ["ms excel", "microsoft excel"], "Statistics": ["statistical analysis"],
        "Generative AI": ["genai", "gen ai"], "LLM": ["llms", "large language models"], "Data Science": [],
        "Big Data": [], "Apache Spark": ["spark", "pyspark"], "Hadoop": [], "ETL": [],
    },
    "APIs & Architecture": {
        "REST APIs": ["rest api", "restful", "restful api", "restful apis", "rest"], "GraphQL": [],
        "Microservices": ["microservice"], "Object-Oriented Programming": ["oop", "oops", "object oriented"],
        "Data Structures": ["dsa"], "Algorithms": [], "System Design": [], "Unit Testing": ["unit tests"],
    },
    "Tools": {
        "Postman": [], "Jira": [], "Selenium": [], "VS Code": ["visual studio code", "vscode"],
        "Figma": [], "Agile": [], "Scrum": [], "Kanban": [],
    },
    "IoT & Embedded": {
        "IoT": ["internet of things"], "Arduino": [], "Raspberry Pi": ["raspberrypi"], "MQTT": [],
        "Embedded Systems": ["embedded"], "ESP32": [], "Sensors": [],
    },
    "Business & Office": {
        "Project Management": [], "Digital Marketing": [], "SEO": [], "Content Writing": [], "Sales": [],
        "Customer Service": [], "Financial Analysis": [], "Accounting": [], "Recruitment": [],
        "Microsoft Office": ["ms office"], "Tally": [], "Social Media Marketing": [],
    },
    "Soft Skills": {
        "Communication": [], "Leadership": [], "Teamwork": ["team work", "collaboration"],
        "Problem Solving": ["problem-solving"], "Time Management": [], "Critical Thinking": [],
        "Adaptability": [], "Analytical Skills": ["analytical thinking"],
    },
}
SKILL_CATEGORY = {}
SKILL_ALIASES = {}
for _cat, _skills in _RAW_SKILLS.items():
    for _name, _aliases in _skills.items():
        SKILL_CATEGORY[_name] = _cat
        SKILL_ALIASES[_name.lower()] = [_name.lower()] + _aliases
_ALIAS_TO_CANON = {a: n for n in SKILL_CATEGORY for a in SKILL_ALIASES[n.lower()]}

SPOKEN_LANGUAGES = [
    "English", "Hindi", "Gujarati", "Marathi", "Tamil", "Telugu", "Kannada", "Malayalam", "Bengali", "Punjabi",
    "Urdu", "German", "French", "Spanish", "Japanese", "Chinese", "Mandarin", "Arabic", "Portuguese", "Russian", "Italian",
]

# --------------------------------------------------------------------------
# Section headings recognised in a resume
# --------------------------------------------------------------------------
_SECTION_PATTERNS = [
    ("summary", r"(professional\s+|career\s+)?(summary|profile|objective|about\s+me)|career\s+objective"),
    ("skills", r"((technical|key|core|relevant)\s+)?(skills?|competenc(y|ies)|technologies|tech\s+stack|skills?\s*(&|and)\s*tools?)"),
    ("experience", r"((work|professional|employment|industrial|relevant)\s+)?(experience|history)|internships?|employment"),
    ("projects", r"((academic|personal|key|major|notable)\s+)?projects?"),
    ("education", r"(education(al)?(\s+(qualifications?|background))?|academics?|qualifications?)"),
    ("certifications", r"(certifications?|certificates?|licen[cs]es?|courses?|training)(\s*(&|and)\s*\w+)?"),
    ("achievements", r"(achievements?|awards?|honou?rs?|accomplishments?|extra[\s-]?curricular(\s+activities)?|activities)"),
    ("languages", r"languages?(\s+known)?"),
    ("interests", r"(interests?|hobbies)"),
]
SECTION_TITLES = {
    "experience": "Work Experience", "projects": "Projects", "education": "Education",
    "certifications": "Certifications", "achievements": "Achievements", "interests": "Interests", "extra": "Additional Information",
}
_BULLET_RE = re.compile(r"^[\s\u2022\u25CF\u25AA\u25E6\u2023\u25B6\u27A2\u27A4\u2043\u25AB\u2013\u2014\-*\u00B7]+\s*")
_DATE_RANGE = re.compile(
    r"((jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*)?(19|20)\d{2}\s*[-\u2013\u2014to]+\s*"
    r"(((jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*)?(19|20)\d{2}|present|current|now|ongoing)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(\+?\d[\d\-\s().]{7,}\d)")
_LINK = re.compile(r"(https?://\S+|(?:www\.)?(?:linkedin\.com|github\.com)/\S+)", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
STRONG_VERBS = {
    "achieved", "analyzed", "analysed", "automated", "built", "collaborated", "configured", "created", "delivered",
    "deployed", "designed", "developed", "directed", "enhanced", "established", "evaluated", "implemented", "improved",
    "increased", "initiated", "integrated", "launched", "led", "maintained", "managed", "mentored", "migrated",
    "optimized", "optimised", "organized", "organised", "planned", "presented", "reduced", "researched", "resolved",
    "streamlined", "supported", "tested", "trained", "translated", "wrote", "coordinated", "executed", "generated",
}
WEAK_PHRASES = ["responsible for", "worked on", "helped", "duties included", "assisted with", "tasked with", "was involved in"]


def clean(text):
    """Strip characters that break Word/PDF files and collapse spaces."""
    return re.sub(r"[ \t]+", " ", _CONTROL.sub("", str(text or ""))).strip()


# --------------------------------------------------------------------------
# Skill matching
# --------------------------------------------------------------------------
def _term_regex(term):
    return re.compile(r"(?<![A-Za-z0-9+#])" + re.escape(term) + r"(?![A-Za-z0-9+#])", re.I)


def skill_in_text(skill, text):
    """True if the skill (or a known alias of it) appears in the text as a whole word."""
    canon = _ALIAS_TO_CANON.get(skill.lower().strip(), skill)
    terms = SKILL_ALIASES.get(canon.lower(), [skill.lower().strip()])
    return any(_term_regex(t).search(text) for t in terms if t)


def canonical_skill(skill):
    s = skill.strip()
    return _ALIAS_TO_CANON.get(s.lower(), s)


def extract_skills_from_text(text):
    """Skills from the vocabulary found in the text, in order of first appearance: [(skill, category)]."""
    found = []
    for name, category in SKILL_CATEGORY.items():
        positions = [m.start() for t in SKILL_ALIASES[name.lower()] if (m := _term_regex(t).search(text))]
        if positions:
            found.append((min(positions), name, category))
    return [(n, c) for _, n, c in sorted(found)]


def job_skill_list(job_text, keywords_data):
    """Skills the job asks for: the AI model's list (if any) merged with vocabulary matches."""
    skills, seen = [], set()

    def add(name, category):
        name = clean(name)[:60]
        if not name or len(name.split()) > 4:
            return
        key = canonical_skill(name).lower()
        if key not in seen:
            seen.add(key)
            canon = canonical_skill(name)
            skills.append({"skill": canon, "category": SKILL_CATEGORY.get(canon) or clean(category or "Other")[:40] or "Other"})

    for item in (keywords_data or {}).get("skills") or []:
        if isinstance(item, dict) and item.get("skill"):
            add(str(item["skill"]), str(item.get("category") or "Other"))
    for name, category in extract_skills_from_text(job_text):
        add(name, category)
    return skills


def spoken_languages_in(text):
    return [lang for lang in SPOKEN_LANGUAGES if _term_regex(lang).search(text)]


# --------------------------------------------------------------------------
# Resume parsing
# --------------------------------------------------------------------------
def _heading_key(line):
    s = re.sub(r"[:\-\u2013\u2014_|*#\s]+$", "", line.strip())
    s = re.sub(r"^[\s*#\-_|]+", "", s)
    if not s or len(s.split()) > 5 or len(s) > 40:
        return None
    for key, pattern in _SECTION_PATTERNS:
        if re.fullmatch(pattern, s, re.I):
            return key
    return None


def _split_inline_heading(line):
    """'Skills: Python, SQL' -> ('skills', 'Python, SQL')"""
    m = re.match(r"^\s*([A-Za-z &]{3,30})\s*[:\-]\s*(\S.*)$", line)
    if m:
        key = _heading_key(m.group(1))
        if key in ("skills", "languages", "summary"):
            return key, m.group(2)
    return None, None


def _strip_bullet(line):
    return _BULLET_RE.sub("", line).strip()


def _is_bulleted(line):
    return bool(re.match(r"^\s*[\u2022\u25CF\u25AA\u25E6\u2023\u25B6\u27A2\u27A4\u2043\u25AB\-*\u00B7]\s*\S", line))


def parse_resume(text):
    """Splits resume text into a header and sections. Best-effort and tolerant of messy PDF/OCR text."""
    lines = [clean(l) for l in str(text or "").splitlines()]
    lines = [l for l in lines if l]
    header_lines, sections, current = [], {}, None
    order = []
    for raw in lines:
        key = _heading_key(raw)
        inline_key, inline_rest = (None, None)
        if key is None and current != "skills":
            inline_key, inline_rest = _split_inline_heading(raw)
        if key or inline_key:
            current = key or inline_key
            if current not in sections:
                sections[current] = []
                order.append(current)
            if inline_rest:
                sections[current].append(inline_rest)
            continue
        if current is None:
            header_lines.append(raw)
        else:
            sections[current].append(raw)

    header_text = "\n".join(header_lines)
    email_m = _EMAIL.search(text or "")
    email = email_m.group(0) if email_m else ""
    phone_m = _PHONE.search(header_text) or _PHONE.search(text or "")
    phone = clean(phone_m.group(1)) if phone_m else ""
    links = []
    for m in _LINK.finditer(text or ""):
        link = m.group(1).rstrip(".,;)")
        if link not in links:
            links.append(link)
    name = ""
    for l in header_lines:
        if _EMAIL.search(l) or _LINK.search(l):
            continue
        digits = sum(c.isdigit() for c in l)
        if 2 <= len(l) <= 50 and digits <= 2 and len(l.split()) <= 6:
            name = l
            break
    extra_header = [l for l in header_lines if l != name and not _EMAIL.search(l) and not _LINK.search(l)
                    and not (phone and phone in l)]
    location = next((l for l in extra_header if len(l) <= 40 and re.search(r"[A-Za-z]{3}", l) and not _DATE_RANGE.search(l)), "")
    return {"name": name, "email": email, "phone": phone, "links": links[:3], "location": location, "header_extra": extra_header,
            "sections": sections, "order": order}


def _entries(lines, allow_headings=True):
    """Section lines -> [{'type': 'heading'|'bullet'|'text', 'text': ...}]"""
    items = []
    for line in lines:
        text = _strip_bullet(line)
        if not text:
            continue
        if _is_bulleted(line):
            items.append({"type": "bullet", "text": text})
        elif allow_headings and (_DATE_RANGE.search(text) or (
                len(text.split()) <= 12 and not text.endswith((".", ";", ","))
                and text.split()[0].lower().rstrip(",:") not in STRONG_VERBS)):
            items.append({"type": "heading", "text": text})
        else:
            items.append({"type": "bullet" if allow_headings else "text", "text": text})
    return items


def _skill_tokens(lines):
    tokens = []
    for line in lines:
        body = re.sub(r"^[A-Za-z &/]{2,30}:\s*", "", _strip_bullet(line))
        for part in re.split(r"[,;|\u2022\u25CF\u00B7]|\s{2,}", body):
            part = clean(part)
            if 1 < len(part) <= 40 and not part.endswith("."):
                tokens.append(part)
    seen, out = set(), []
    for t in tokens:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


# --------------------------------------------------------------------------
# Checks: requirements, writing language, ATS format
# --------------------------------------------------------------------------
def requirement_checks(resume_text, job_text, parsed):
    out = []
    # years of experience
    m = re.search(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*(?:\d{1,2}\s*)?(?:years?|yrs?)", job_text, re.I)
    if m:
        need = int(m.group(1))
        stated = re.findall(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)", resume_text, re.I)
        years = [int(y) for y in re.findall(r"\b(19[89]\d|20[0-3]\d)\b", "\n".join(parsed["sections"].get("experience", [])))]
        span = (max(years) - min(years)) if len(years) >= 2 else None
        if stated and max(int(s) for s in stated) >= need:
            out.append({"label": "Years of experience", "status": "ok", "detail": f"Job asks for {need}+ years; your resume states {max(int(s) for s in stated)}."})
        elif span is not None and span >= need:
            out.append({"label": "Years of experience", "status": "info", "detail": f"Job asks for {need}+ years. Your dates span about {span} years - say this clearly in your summary."})
        else:
            out.append({"label": "Years of experience", "status": "warn", "detail": f"Job asks for {need}+ years and your resume does not clearly show that much. Emphasise internships, projects and relevant work."})
    # degree
    degree_re = r"b\.?\s?tech|b\.?\s?e\b|bachelor|b\.?\s?sc|bca|m\.?\s?tech|master|m\.?\s?sc|mca|mba|ph\.?d|diploma"
    if re.search(degree_re, job_text, re.I):
        has = re.search(degree_re, "\n".join(parsed["sections"].get("education", [])) or resume_text, re.I)
        out.append({"label": "Education", "status": "ok" if has else "warn",
                    "detail": "The job lists a degree requirement and your resume shows a degree." if has else
                              "The job lists a degree requirement but no degree was found in your resume."})
    # work type
    types = [t for t in ("Remote", "Hybrid", "On-site", "Full-time", "Part-time", "Internship") if re.search(t.replace("-", "[- ]?"), job_text, re.I)]
    if types:
        out.append({"label": "Work type", "status": "info", "detail": "The job mentions: " + ", ".join(types) + "."})
    # spoken languages
    need_langs = spoken_languages_in(job_text)
    if need_langs:
        have = set(spoken_languages_in(resume_text))
        miss = [l for l in need_langs if l not in have]
        out.append({"label": "Languages", "status": "ok" if not miss else "warn",
                    "detail": ("Your resume mentions every language the job lists." if not miss else
                               "The job mentions " + ", ".join(need_langs) + "; your resume does not show " + ", ".join(miss) + ".")})
    return out


def writing_tips(parsed, resume_text):
    tips = []
    bullets = [i["text"] for key in ("experience", "projects") for i in _entries(parsed["sections"].get(key, [])) if i["type"] == "bullet"]
    if len(re.findall(r"\b(I|my|me)\b", resume_text)) >= 3:
        tips.append("Remove first-person words (I, my, me). Resumes read better in implied first person, e.g. \"Built a dashboard...\" instead of \"I built...\".")
    weak = [p for p in WEAK_PHRASES if re.search(re.escape(p), resume_text, re.I)]
    if weak:
        tips.append("Replace weak phrases (" + ", ".join(f'"{w}"' for w in weak[:3]) + ") with action verbs such as Built, Led, Improved, Automated.")
    if bullets:
        with_numbers = sum(bool(re.search(r"\d", b)) for b in bullets)
        if with_numbers / len(bullets) < 0.3:
            tips.append("Add measurable results to your bullet points (numbers, percentages, time or cost saved).")
        strong = sum(b.split()[0].lower().rstrip(",:") in STRONG_VERBS for b in bullets if b.split())
        if strong / len(bullets) < 0.5:
            tips.append("Start more bullet points with a strong action verb (Developed, Implemented, Designed, Reduced...).")
        if any(len(b.split()) > 32 for b in bullets):
            tips.append("Shorten long bullet points to one or two lines each.")
    if not parsed["sections"].get("summary"):
        tips.append("Add a 2-3 line professional summary at the top that mentions the role you are applying for.")
    return tips[:6]


def format_checks(text, has_tables=False, is_scanned=False):
    """The same 7 format checks as the app's 'ATS Friendliness' panel: [(label, passed)]."""
    lowered = text.lower()
    words = len(text.split())
    return [
        ("Contact info detected (email & phone)", bool(_EMAIL.search(text)) and bool(_PHONE.search(text))),
        ("Standard section headings (Experience, Education, Skills)",
         all(a in lowered for a in ("experience", "education", "skills"))),
        ("Uses bullet points for readability", bool(re.search(r"(^|\n)\s*[\u2022\-*\u25AA\u25E6]\s+", text))),
        ("Readable length (150-1200 words)", 150 <= words <= 1200),
        ("No tables/columns detected (ATS-safe layout)", not has_tables),
        ("Text is machine-readable, not a scanned image", not is_scanned),
        ("Minimal special characters/symbols", len(re.findall(r"[^\x00-\x7F]", text)) < 20),
    ]


def ats_score(text, job_skills, has_tables=False, is_scanned=False):
    """Estimated ATS score: 60% keyword coverage + 40% format. Returns a dict of percentages.
    Keywords are matched by the job's EXACT wording ("JavaScript", not "JS") because many
    ATS look for the literal words of the job post."""
    checks = format_checks(text, has_tables, is_scanned)
    fmt = round(100 * sum(ok for _, ok in checks) / len(checks))
    if job_skills:
        found = [s for s in job_skills if _term_regex(s["skill"]).search(text)]
        kw = round(100 * len(found) / len(job_skills))
        overall = round(0.6 * kw + 0.4 * fmt)
    else:
        kw, overall = None, fmt
    return {"keywords": kw, "format": fmt, "overall": overall,
            "checks": [{"label": l, "ok": bool(ok)} for l, ok in checks]}


# --------------------------------------------------------------------------
# Optional AI polish (always verified against the original resume)
# --------------------------------------------------------------------------
def _parse_json(raw, opener="{"):
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", str(raw or "").strip())
    closer = "}" if opener == "{" else "]"
    m = re.search(re.escape(opener) + r".*" + re.escape(closer), text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (json.JSONDecodeError, TypeError):
        return None


def _proper_tokens(text):
    """Capitalised words / acronyms / tech names (apart from a sentence's first word)."""
    words = text.split()
    return {re.sub(r"[^\w+#.\-]", "", w) for w in words[1:] if re.match(r"^[A-Z0-9]", w)} - {""}


def _bullet_ok(original, new):
    new = clean(new)
    if not new or new.lower() == original.lower() or len(new.split()) > 40:
        return False
    if not 0.5 * len(original) <= len(new) <= 1.6 * len(original):
        return False
    if re.match(r"^(I|My|We)\b", new):
        return False
    nums = lambda t: set(re.findall(r"\d+(?:[.,]\d+)?", t))
    if nums(original) != nums(new):
        return False
    if not _proper_tokens(original) <= _proper_tokens(new) | {new.split()[0]}:
        return False
    if _proper_tokens(new) - _proper_tokens(original):
        return False
    return True


def polish_bullets(bullets, role, llm):
    """Asks the model to reword up to 10 bullets; keeps only rewrites that pass the fact check."""
    if not llm or not bullets:
        return {}
    picks = sorted(range(len(bullets)), key=lambda i: (bullets[i].split()[0].lower() in STRONG_VERBS, i))[:10]
    sample = [bullets[i] for i in picks]
    prompt = (
        "Rewrite each resume bullet point to start with a strong action verb and be concise.\n"
        "STRICT RULES: keep every number, tool, technology and company name exactly as written; do NOT add new "
        "facts, numbers, tools or skills; do not use 'I'. Keep the same meaning.\n"
        f"Target role: {role}\n"
        'Reply with ONLY a JSON array of strings, the same length and order as the input.\n'
        f"Input: {json.dumps(sample)}"
    )
    try:
        data = _parse_json(llm(prompt, 700, 0.1), "[")
    except Exception:  # noqa: BLE001 - model unavailable: keep original wording
        return {}
    if not isinstance(data, list) or len(data) != len(sample):
        return {}
    accepted = {}
    for idx, original, new in zip(picks, sample, data):
        if isinstance(new, str) and _bullet_ok(original, new):
            accepted[idx] = clean(new)
    return accepted


def job_title(job_text):
    m = re.search(r"(?:job\s*title|position|role)\s*[:\-]\s*([^\n]{3,70})", job_text, re.I)
    if m:
        return clean(m.group(1))
    first = clean(next((l for l in job_text.splitlines() if l.strip()), ""))
    if 2 <= len(first) <= 60 and len(first.split()) <= 8 and not first.endswith("."):
        return first
    return "the advertised role"


def _summary_ok(text, resume_text, role):
    text = clean(text)
    if not 40 <= len(text) <= 600 or re.search(r"\b(I|my|me)\b", text) or "\n" in text:
        return False
    allowed = _proper_tokens(resume_text) | _proper_tokens(role) | set(role.split()) | {text.split()[0]}
    if _proper_tokens(text) - allowed - {w for w in _proper_tokens(text) if w.lower() in resume_text.lower()}:
        return False
    return set(re.findall(r"\d+", text)) <= set(re.findall(r"\d+", resume_text))


def build_summary(parsed, resume_text, verified_skills, role, llm):
    original = " ".join(parsed["sections"].get("summary", [])).strip()
    top = [s["skill"] for s in verified_skills[:5]]
    if llm and top:
        prompt = (
            "Write a professional resume summary of 2 or 3 sentences for the candidate below, aimed at the target role.\n"
            "STRICT RULES: use ONLY facts found in the resume text; do not invent employers, degrees, numbers, tools or "
            "years; no first person (no I/my); plain text on a single line.\n"
            f"Target role: {role}\nSkills to mention if true: {', '.join(top)}\n\nResume:\n{resume_text[:3500]}"
        )
        try:
            candidate = clean(llm(prompt, 180, 0.2)).strip('"')
            if _summary_ok(candidate, resume_text, role):
                return candidate, "ai"
        except Exception:  # noqa: BLE001
            pass
    if original and not re.search(r"\b(I|my|me)\b", original):
        missing = [s for s in top if not skill_in_text(s, original)]
        extra = f" Core strengths: {', '.join(missing)}." if missing else ""
        return clean(original) + extra, "original"
    if top:
        target = f" a {role} position" if role != "the advertised role" else " the advertised role"
        return f"Motivated candidate with hands-on experience in {', '.join(top)}, seeking{target}.", "template"
    return (clean(original), "original") if original else ("", "none")


# --------------------------------------------------------------------------
# The builder
# --------------------------------------------------------------------------
def missing_items(resume_text, job_text, keywords_data):
    """What the job asks for that the resume does not show: skills + spoken languages."""
    skills = [s for s in job_skill_list(job_text, keywords_data) if not skill_in_text(s["skill"], resume_text)]
    have = set(spoken_languages_in(resume_text))
    langs = [l for l in spoken_languages_in(job_text) if l not in have]
    return {"skills": skills, "languages": langs}


def build_improved_resume(resume_text, job_text, keywords_data=None, confirmed=None, llm=None, meta=None, progress=None):
    meta = meta or {}
    step = progress or (lambda i: None)
    confirmed = set(c.lower() for c in (confirmed or []))
    parsed = parse_resume(resume_text)
    step(1)
    jd_skills = job_skill_list(job_text, keywords_data)
    verified = [s for s in jd_skills if skill_in_text(s["skill"], resume_text)]
    offered = missing_items(resume_text, job_text, keywords_data)
    added = [s for s in offered["skills"] if s["skill"].lower() in confirmed]
    added_langs = [l for l in offered["languages"] if l.lower() in confirmed]
    still_missing = [s for s in offered["skills"] if s["skill"].lower() not in confirmed]
    still_missing_langs = [l for l in offered["languages"] if l.lower() not in confirmed]
    role = job_title(job_text)
    changes = []

    # ---- skills, grouped by the job's categories, using the job's wording
    groups, seen = {}, set()
    for s in verified + added:
        groups.setdefault(s["category"], []).append(s["skill"])
        seen.add(s["skill"].lower())
    original_skills = _skill_tokens(parsed["sections"].get("skills", []))
    extras = [t for t in original_skills if canonical_skill(t).lower() not in seen]
    skills_out = [{"category": c, "items": items} for c, items in groups.items()]
    if extras:
        skills_out.append({"category": "Additional Skills", "items": extras})
    renamed = [(t, canonical_skill(t)) for t in original_skills if canonical_skill(t) != t and canonical_skill(t).lower() in seen]
    if renamed:
        changes.append("Used the job's wording for skills: " + ", ".join(f"{a} -> {b}" for a, b in renamed[:5]) + ".")
    if added:
        changes.append("Added skills you confirmed you have: " + ", ".join(s["skill"] for s in added) + ".")

    # ---- summary
    step(2)
    summary, how = build_summary(parsed, resume_text, verified, role, llm)
    if summary:
        changes.append({"ai": "Rewrote the professional summary for this job (checked against your resume).",
                        "original": "Kept your summary and added the job's key skills you already have.",
                        "template": "Wrote a professional summary built only from skills found in your resume (replacing a first-person objective, if any)."}[how])

    # ---- experience / projects / education / others, preserved and cleaned
    sections = []
    polished_total = 0
    for key in ("experience", "projects", "education", "certifications", "achievements"):
        lines = parsed["sections"].get(key)
        if not lines:
            continue
        items = _entries(lines, allow_headings=key in ("experience", "projects"))
        if key in ("experience", "projects"):
            idxs = [i for i, it in enumerate(items) if it["type"] == "bullet"]
            fixes = polish_bullets([items[i]["text"] for i in idxs], role, llm) if idxs else {}
            for n, new in fixes.items():
                items[idxs[n]]["text"] = new
            polished_total += len(fixes)
        sections.append({"title": SECTION_TITLES[key], "items": items})
    if polished_total:
        changes.append(f"Reworded {polished_total} bullet point(s) with stronger verbs (facts, tools and numbers unchanged).")
    extra_lines = parsed["sections"].get("interests", [])
    if extra_lines:
        sections.append({"title": SECTION_TITLES["interests"], "items": _entries(extra_lines, allow_headings=False)})
    if parsed["header_extra"] and not parsed["sections"]:
        sections.append({"title": SECTION_TITLES["extra"], "items": _entries(parsed["header_extra"], allow_headings=False)})
    if not sections and not skills_out:   # unreadable structure: keep all text under one heading
        body = [l for l in str(resume_text).splitlines() if l.strip()]
        sections.append({"title": "Work Experience", "items": _entries(body, True)})
        changes.append("Your resume had no clear sections, so all of its text was kept under one heading.")

    # ---- spoken languages
    lang_lines = parsed["sections"].get("languages", [])
    langs = [t for t in _skill_tokens(lang_lines) if t.split()[0].strip("(),").lower() in {l.lower() for l in SPOKEN_LANGUAGES}]
    if not langs:
        langs = spoken_languages_in("\n".join(lang_lines))
    for l in added_langs:
        if l not in langs:
            langs.append(l)
    if added_langs:
        changes.append("Added languages you confirmed: " + ", ".join(added_langs) + ".")

    changes.insert(0, "Rebuilt the resume in a single-column, ATS-safe layout with standard headings (no tables, columns, images or special symbols).")

    improved = {
        "header": {"name": parsed["name"], "location": parsed["location"], "email": parsed["email"], "phone": parsed["phone"], "links": parsed["links"]},
        "summary": summary, "skills": skills_out, "sections": sections, "languages": langs,
        "role": role, "changes": changes,
        "missing": {"skills": still_missing, "languages": still_missing_langs},
        "requirements": requirement_checks(resume_text, job_text, parsed),
        "tips": writing_tips(parsed, resume_text),
    }
    step(3)
    text_after = to_plain_text(improved)
    improved["ats"] = {
        "before": ats_score(resume_text, jd_skills, meta.get("has_tables", False), meta.get("is_scanned", False)),
        "after": ats_score(text_after, jd_skills),
    }
    if not (parsed["email"] and parsed["phone"]):
        improved["tips"].insert(0, "Your resume is missing an email or phone number. Add them - they cannot be invented for you.")
    return improved


# --------------------------------------------------------------------------
# Output: plain text (for scoring), HTML (preview + PDF), DOCX
# --------------------------------------------------------------------------
def _contact_line(h):
    return " | ".join(p for p in [h.get("location"), h.get("email"), h.get("phone"), *(h.get("links") or [])] if p)


def to_plain_text(r):
    out = []
    h = r["header"]
    if h.get("name"):
        out.append(h["name"])
    if _contact_line(h):
        out.append(_contact_line(h))
    if r.get("summary"):
        out += ["", "PROFESSIONAL SUMMARY", r["summary"]]
    if r.get("skills"):
        out += ["", "SKILLS"]
        out += [f"- {g['category']}: {', '.join(g['items'])}" for g in r["skills"]]
    for sec in r.get("sections", []):
        out += ["", sec["title"].upper()]
        for it in sec["items"]:
            out.append(f"- {it['text']}" if it["type"] == "bullet" else it["text"])
    if r.get("languages"):
        out += ["", "LANGUAGES", ", ".join(r["languages"])]
    return "\n".join(out)


def to_html(r, for_pdf=False):
    e = html.escape
    h = r["header"]
    parts = []
    if h.get("name"):
        parts.append(f'<h1 class="r-name">{e(h["name"])}</h1>')
    if _contact_line(h):
        parts.append(f'<p class="r-contact">{e(_contact_line(h))}</p>')
    if r.get("summary"):
        parts.append(f'<h2>Professional Summary</h2><p>{e(r["summary"])}</p>')
    if r.get("skills"):
        parts.append("<h2>Skills</h2>" + "".join(
            f'<p><b>{e(g["category"])}:</b> {e(", ".join(g["items"]))}</p>' for g in r["skills"]))
    for sec in r.get("sections", []):
        body, in_list = "", False
        for it in sec["items"]:
            if it["type"] == "bullet":
                if not in_list:
                    body += "<ul>"
                    in_list = True
                body += f"<li>{e(it['text'])}</li>"
            else:
                if in_list:
                    body += "</ul>"
                    in_list = False
                body += f'<p class="{"r-entry" if it["type"] == "heading" else ""}">{"<b>" if it["type"] == "heading" else ""}{e(it["text"])}{"</b>" if it["type"] == "heading" else ""}</p>'
        if in_list:
            body += "</ul>"
        parts.append(f'<h2>{e(sec["title"])}</h2>{body}')
    if r.get("languages"):
        parts.append(f'<h2>Languages</h2><p>{e(", ".join(r["languages"]))}</p>')
    return "".join(parts)


_PDF_CSS = """
@page { size: A4; margin: 1.7cm 1.9cm; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 10pt; color: #111; line-height: 1.35; }
h1.r-name { font-size: 19pt; text-align: center; margin: 0 0 2px 0; }
p.r-contact { text-align: center; color: #333; margin: 0 0 8px 0; font-size: 9.5pt; }
h2 { font-size: 10.5pt; text-transform: uppercase; border-bottom: 1px solid #444; padding-bottom: 1px; margin: 11px 0 4px 0; }
p { margin: 2px 0; } p.r-entry { margin-top: 5px; } ul { margin: 2px 0 2px 14px; } li { margin: 1px 0; }
"""


def to_pdf_bytes(r):
    from xhtml2pdf import pisa
    doc = f'<html><head><meta charset="utf-8"><style>{_PDF_CSS}</style></head><body>{to_html(r, True)}</body></html>'
    buf = io.BytesIO()
    result = pisa.CreatePDF(src=doc, dest=buf)
    if result.err:
        raise RuntimeError("Could not create the PDF.")
    return buf.getvalue()


def to_docx_bytes(r):
    """ATS-safe Word file: one column, real bullet list, plain fonts, no tables / text boxes / images."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, Pt

    doc = Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Cm(1.7)
        s.left_margin = s.right_margin = Cm(1.9)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(2)

    def para(text="", bold=False, size=None, align=None, style=None, space_before=0):
        p = doc.add_paragraph(style=style)
        run = p.add_run(clean(text))
        run.bold = bold
        if size:
            run.font.size = Pt(size)
        if align:
            p.alignment = align
        if space_before:
            p.paragraph_format.space_before = Pt(space_before)
        return p

    def heading(text):
        para(text.upper(), bold=True, size=11.5, space_before=9)

    h = r["header"]
    if h.get("name"):
        para(h["name"], bold=True, size=18, align=WD_ALIGN_PARAGRAPH.CENTER)
    if _contact_line(h):
        para(_contact_line(h), align=WD_ALIGN_PARAGRAPH.CENTER)
    if r.get("summary"):
        heading("Professional Summary")
        para(r["summary"])
    if r.get("skills"):
        heading("Skills")
        for g in r["skills"]:
            p = doc.add_paragraph()
            p.add_run(clean(g["category"]) + ": ").bold = True
            p.add_run(clean(", ".join(g["items"])))
    for sec in r.get("sections", []):
        heading(sec["title"])
        for it in sec["items"]:
            if it["type"] == "bullet":
                para(it["text"], style="List Bullet")
            else:
                para(it["text"], bold=it["type"] == "heading", space_before=4 if it["type"] == "heading" else 0)
    if r.get("languages"):
        heading("Languages")
        para(", ".join(r["languages"]))
    doc.core_properties.author = ""
    doc.core_properties.title = clean(h.get("name") or "Resume")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
