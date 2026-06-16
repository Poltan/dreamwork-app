"""
Dreamwork backend — FastAPI.

Endpoints:
  POST /api/match   multipart: resume file + prefs -> ranked jobs + parsed profile
  POST /api/letter  json: {job, profile, resume_text, lang} -> tailored cover letter
  GET  /            serves the frontend (../frontend/index.html)
  GET  /api/health  liveness + which providers/keys are configured

Run:
  pip install -r requirements.txt
  cp .env.example .env   # fill in keys
  uvicorn app:app --reload --port 8000
"""

import os
import io
import json
import time
import threading
import pathlib
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import providers

ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
_HERE = pathlib.Path(__file__).resolve().parent
# Works for both layouts: flat (index.html next to app.py, used on the host/Render)
# and structured (../frontend/index.html, used locally).
FRONTEND = _HERE / "index.html"
if not FRONTEND.exists():
    FRONTEND = _HERE.parent / "frontend" / "index.html"

app = FastAPI(title="Dreamwork API", version="0.1")
# The frontend is served from this same service, so we don't need a wildcard CORS.
# Restrict to our own origin (+ localhost for dev) so other websites can't drive our
# paid endpoints from a user's browser. Override with ALLOWED_ORIGINS (comma-separated).
_ORIGINS = [o.strip() for o in os.getenv(
    "ALLOWED_ORIGINS",
    "https://dreamwork-0nmr.onrender.com,http://localhost:8000,http://127.0.0.1:8000",
).split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware, allow_origins=_ORIGINS, allow_methods=["*"], allow_headers=["*"],
)

# Basic security headers on every response.
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

# Expose internal debug fields only when explicitly enabled.
DEBUG_API = os.getenv("DEBUG_API") == "1"

# ---- Lightweight in-memory rate limiting (per client IP) -------------------
# Protects the paid endpoints (Anthropic / provider quotas) from abuse. Single
# instance => in-memory is enough; resets on restart.
_RL_LOCK = threading.Lock()
_RL_HITS = defaultdict(deque)

def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"

def _rate_limit(bucket: str, ip: str, limit: int, window: int):
    """Allow `limit` hits per `window` seconds per (bucket, ip). Raise 429 if exceeded."""
    now = time.time()
    key = f"{bucket}:{ip}"
    with _RL_LOCK:
        dq = _RL_HITS[key]
        while dq and dq[0] <= now - window:
            dq.popleft()
        if len(dq) >= limit:
            retry = int(window - (now - dq[0])) + 1
            raise HTTPException(429, f"Too many requests. Try again in {retry}s.")
        dq.append(now)
        # opportunistic cleanup to bound memory
        if len(_RL_HITS) > 5000:
            for k in [k for k, v in list(_RL_HITS.items()) if not v]:
                _RL_HITS.pop(k, None)

# Max resume upload size (bytes). Bounds memory + blocks upload-bomb DoS.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))


def _anthropic():
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set on the server.")
    import anthropic
    return anthropic.Anthropic(api_key=key)


def _ask_claude(prompt, max_tokens=1500, system=None):
    client = _anthropic()
    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system or "You are a precise assistant. Follow instructions exactly.",
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") if "{" in text else text.find("["):]
    for op, cl in (("{", "}"), ("[", "]")):
        i, j = text.find(op), text.rfind(cl)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                pass
    return json.loads(text)


# Caps to keep parsing of untrusted files bounded (anti zip-bomb / huge-doc DoS).
MAX_PDF_PAGES = 30
MAX_DOCX_PARAS = 4000
MAX_TEXT_CHARS = 60000

def parse_resume(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = pdf.pages[:MAX_PDF_PAGES]
                text = "\n".join((p.extract_text() or "") for p in pages)
        except Exception as e:
            raise HTTPException(400, "Could not read PDF.")
        return text[:MAX_TEXT_CHARS]
    if name.endswith(".docx"):
        try:
            import docx
            d = docx.Document(io.BytesIO(data))
            text = "\n".join(p.text for p in d.paragraphs[:MAX_DOCX_PARAS])
        except Exception as e:
            raise HTTPException(400, "Could not read DOCX.")
        return text[:MAX_TEXT_CHARS]
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc)[:MAX_TEXT_CHARS]
        except Exception:
            continue
    raise HTTPException(400, "Unsupported resume format. Use PDF, DOCX or TXT.")


# Rough FX rates to RUB for cross-currency salary comparison.
_FX = {"RUB": 1.0, "USD": 90.0, "EUR": 95.0, "GBP": 110.0, "ILS": 25.0, "SAR": 24.0,
       "AED": 24.0, "CAD": 66.0, "AUD": 60.0, "PLN": 23.0, "INR": 1.1, "BRL": 18.0,
       "MXN": 5.0, "NZD": 55.0, "SGD": 67.0, "ZAR": 5.0}

def _to_rub(amount, currency):
    try:
        amount = float(amount)
    except Exception:
        return 0.0
    return amount * _FX.get((currency or "RUB").upper(), 1.0)


PROFILE_PROMPT = """Извлеки из резюме структурированный профиль кандидата.
Верни ТОЛЬКО JSON без пояснений, по схеме:
{{
  "name": "",
  "headline": "краткая роль одной строкой",
  "country": "страна кандидата по локации/языку резюме, на английском (напр. Russia, USA, Israel); если не ясно — пусто",
  "seniority": "junior|mid|senior|lead|director|c-level",
  "target_titles": ["3-6 реалистичных названий вакансий; если у кандидата есть редкий язык или сильная региональная/отраслевая экспертиза (напр. китайский язык, опыт с Китаем), добавь 1-2 варианта, отражающих это (напр. 'Project Manager China', 'ВЭД-менеджер Китай')"],
  "search_query": "1-2 СЛОВА — основное название роли для поиска вакансий (на языке рынка кандидата), без навыков и названий программ",
  "search_query_en": "1-3 English keywords (role translated to English) for international search, e.g. 'project manager'",
  "skills": ["8-15 ключевых навыков"],
  "industries": ["отрасли кандидата"],
  "languages": ["языки кандидата"],
  "desired_salary": "желаемая зарплата из резюме — ТОЛЬКО число без пробелов/валюты, или null",
  "salary_currency": "валюта желаемой зарплаты (RUB/USD/EUR/CNY/...), или пусто",
  "summary": "2-3 предложения о кандидате, включая ключевые преимущества (языки, отраслевая/региональная экспертиза)"
}}

РЕЗЮМЕ:
{resume}"""


RANK_PROMPT = """Ты — карьерный консультант. Ниже профиль кандидата и список вакансий.
Отбери НЕ БОЛЕЕ {top} самых подходящих вакансий: по роли, грейду, отрасли, локации и зарплатным ожиданиям ({salary_note}).
Если страна поиска не задана — допускай сильные вакансии из разных стран мира (география может быть разной).
Сильные стороны кандидата (редкие языки — напр. китайский; отраслевая или региональная экспертиза — напр. опыт работы с Китаем) — весомый плюс: вакансии, где они прямо востребованы, поднимай выше.
Лучше вернуть 2-3 точных совпадения, чем добивать список слабыми.
Для каждой выбранной верни оценку соответствия и короткую заметку (1-2 предложения, на языке профиля), почему вакансия подходит именно этому кандидату.
Не выдумывай вакансии, используй только id из списка. Отсей нерелевантные (другая профессия/грейд).

Верни ТОЛЬКО JSON-массив:
[{{"id":"<id вакансии>","score":0-100,"fit":"<заметка>"}}]

ПРОФИЛЬ:
{profile}

ВАКАНСИИ:
{jobs}"""


LETTER_PROMPT = """Ты — профессиональный консультант по сопроводительным письмам с 15+ годами опыта.
Напиши короткое, цепляющее, «человеческое» сопроводительное письмо под конкретную вакансию.
Сначала про себя проанализируй вакансию (5-10 требований, домен, ключевые слова) — анализ НЕ выводи.

Правила: {lang_rule} 3-5 абзацев по 1-3 предложения, ~700-1200 знаков; живой стиль без канцелярита и штампов
(«обладаю навыками», «успешно реализовывал», «дружная команда», «данная вакансия» — запрещены); 8-15 ключевых слов
из вакансии естественно; зеркаль 2-3 формулировки работодателя; не дублируй резюме; без выдуманных цифр.
Первый абзац — позиция + хук. Середина — 2-3 конкретных акцента опыта под задачи вакансии. Финал — спокойный
call-to-action одним предложением. Выведи ТОЛЬКО текст письма с приветствием и подписью кандидата.

КАНДИДАТ (профиль): {profile}

РЕЗЮМЕ (фрагмент): {resume}

ВАКАНСИЯ: {title} — {company} ({location}).
ОПИСАНИЕ: {desc}"""


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "model": ANTHROPIC_MODEL,
        "keys": {
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
            "jooble": bool(os.getenv("JOOBLE_API_KEY")),
            "adzuna": bool(os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY")),
            "superjob": bool(os.getenv("SUPERJOB_KEY")),
            "hh_token": bool(os.getenv("HH_TOKEN")),
            "scraper": bool(os.getenv("SCRAPER_API_KEY")),
            "jsearch": bool(os.getenv("RAPIDAPI_KEY")),
            "company_ats": bool(os.getenv("COMPANY_ATS")),
            "proxy": bool(os.getenv("PROXY_URL")),
        },
        "always_on": ["hh.ru", "Remotive", "Trudvsem"],
    }


@app.get("/api/proxytest")
def proxytest(key: str = ""):
    # Diagnostic endpoint — gated behind DIAG_KEY so it doesn't publicly leak the
    # egress/proxy IP. Set DIAG_KEY in the environment and call /api/proxytest?key=...
    diag = os.getenv("DIAG_KEY")
    if not diag or key != diag:
        raise HTTPException(404, "Not found")
    import requests as _rq
    u = os.getenv("PROXY_URL")
    px = {"http": u, "https": u} if u else None
    out = {"proxy_set": bool(u)}
    try:
        r = _rq.get("https://api.ipify.org?format=json", proxies=px, timeout=25)
        out["egress_ip"] = r.json().get("ip")
    except Exception as e:
        out["ip_error"] = str(e)[:200]
    try:
        r2 = _rq.get("https://api.hh.ru/vacancies", params={"text": "менеджер", "per_page": 1},
                     proxies=px, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        out["hh_status"] = r2.status_code
        out["hh_body"] = r2.text[:150]
    except Exception as e:
        out["hh_error"] = str(e)[:200]
    return out


@app.post("/api/match")
async def match(
    request: Request,
    resume: UploadFile = File(...),
    country: str = Form(""),
    location: str = Form(""),
    salary_min: str = Form(""),
    currency: str = Form(""),
    remote_ok: str = Form("false"),
    top: int = Form(10),
):
    _rate_limit("match", _client_ip(request), limit=15, window=600)
    top = max(1, min(int(top), 12))

    data = await resume.read()
    if not data:
        raise HTTPException(400, "Empty resume file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Resume too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB).")
    resume_text = parse_resume(resume.filename, data)
    if len(resume_text.strip()) < 50:
        raise HTTPException(400, "Could not extract enough text from the resume.")

    try:
        profile = _extract_json(_ask_claude(
            PROFILE_PROMPT.format(resume=resume_text[:7000]), max_tokens=1500))
    except Exception:
        profile = {}

    # If the user left the country blank, infer it from the resume (location/language).
    if not (country or "").strip():
        country = (profile.get("country") or "").strip()

    sal = int(salary_min) if str(salary_min).strip().isdigit() else None
    eff_currency = (currency or "RUB").upper()
    # If the user didn't set a salary, use the one stated in the resume.
    if sal is None and profile.get("desired_salary"):
        try:
            sal = int(float(profile.get("desired_salary")))
            eff_currency = (profile.get("salary_currency") or "RUB").upper()
        except Exception:
            sal = None

    # Build query variants. For non-RU markets prefer English keywords/titles.
    code_now = providers.normalize_country(country)
    intl = bool(code_now) and code_now != "RU"
    titles = [(t or "").strip() for t in (profile.get("target_titles") or []) if (t or "").strip()]
    if intl:
        titles.sort(key=lambda t: 0 if all(ord(c) < 128 for c in t) else 1)  # English titles first
    sq = (profile.get("search_query") or "").strip()
    sq_en = (profile.get("search_query_en") or "").strip()
    primary = sq_en if (intl and sq_en) else sq
    candidates = []
    if primary:
        candidates.append(primary)
    for t in titles[:4]:
        if t not in candidates:
            candidates.append(t)
    if sq and sq not in candidates:
        candidates.append(sq)
    hl = (profile.get("headline") or "").strip()
    if hl and hl not in candidates:
        candidates.append(hl)
    if not candidates:
        candidates = ["manager"]

    # Run a couple of query variants IN PARALLEL; aggregate + de-dupe.
    # Capped at 2 to bound memory (each variant fans out to all providers).
    queries = candidates[:2]
    remote_flag = str(remote_ok).lower() == "true"
    jobs, debug, seen_ids = [], {}, set()

    def _q(q):
        return providers.search_jobs(keywords=q, location=location, country=country,
                                     salary_min=sal, remote_ok=remote_flag, per_provider=15)

    with ThreadPoolExecutor(max_workers=max(1, len(queries))) as ex:
        for qjobs, qdebug in ex.map(_q, queries):
            debug.update(qdebug)
            for j in qjobs:
                if j["id"] in seen_ids:
                    continue
                seen_ids.add(j["id"])
                jobs.append(j)

    # Unified salary filter: convert to RUB, keep matches AND jobs with unstated salary.
    if sal:
        min_rub = _to_rub(sal, eff_currency)
        kept = []
        for j in jobs:
            top_sal = j.get("salary_max") or j.get("salary_min")
            if not top_sal or _to_rub(top_sal, j.get("currency")) >= min_rub * 0.9:
                kept.append(j)
        # Soft filter: apply only if enough remain; otherwise keep the full pool so a
        # high salary ask doesn't zero out results (salary stays a ranking preference).
        if len(kept) >= 3:
            jobs = kept

    if not jobs:
        empty = {"profile": profile, "jobs": [],
                 "note": "Провайдеры не вернули вакансий по этому запросу."}
        if DEBUG_API:
            empty["debug"] = debug; empty["tried"] = candidates
        return JSONResponse(empty)

    trimmed = [{"id": j["id"], "title": j["title"], "company": j["company"],
                "location": j["location"], "salary": j["salary"],
                "remote": j["remote"], "desc": (j["description"] or "")[:300]}
               for j in jobs[:40]]
    salary_note = f"от {sal} {eff_currency}" if sal else "не указаны"
    try:
        ranked = _extract_json(_ask_claude(
            RANK_PROMPT.format(top=top, salary_note=salary_note,
                               profile=json.dumps(profile, ensure_ascii=False),
                               jobs=json.dumps(trimmed, ensure_ascii=False)),
            max_tokens=1500))
    except Exception:
        ranked = [{"id": j["id"], "score": None, "fit": ""} for j in jobs[:top]]

    # The model sometimes returns an object instead of a JSON array — normalize.
    if isinstance(ranked, dict):
        ranked = next((v for v in ranked.values() if isinstance(v, list)), [])
    if not isinstance(ranked, list):
        ranked = []

    by_id = {j["id"]: j for j in jobs}
    results, used = [], set()
    for r in ranked[:top]:
        j = by_id.get(r.get("id"))
        if not j or j["id"] in used:
            continue
        used.add(j["id"])
        j = dict(j)
        j["score"] = r.get("score")
        j["fit"] = r.get("fit", "")
        results.append(j)
    # Append remaining pool jobs (unranked extras) for the "show more" button.
    for j in jobs:
        if len(results) >= 25:
            break
        if j["id"] in used:
            continue
        used.add(j["id"])
        j = dict(j)
        j["score"] = None
        j["fit"] = ""
        results.append(j)

    out = {"profile": profile, "jobs": results,
           "resume_excerpt": resume_text[:4000]}
    if DEBUG_API:
        out["debug"] = debug
        out["pool_by_source"] = {}
        for j in jobs:
            out["pool_by_source"][j.get("source", "?")] = out["pool_by_source"].get(j.get("source", "?"), 0) + 1
    return out


@app.post("/api/letter")
async def letter(payload: dict, request: Request):
    _rate_limit("letter", _client_ip(request), limit=30, window=600)
    job = payload.get("job") or {}
    profile = payload.get("profile") or {}
    resume = (payload.get("resume_text") or "")[:4000]
    lang = (payload.get("lang") or "ru").lower()
    lang_rule = ("Пиши письмо на английском, те же правила." if lang == "en"
                 else "Пиши письмо на русском.")
    text = _ask_claude(LETTER_PROMPT.format(
        lang_rule=lang_rule,
        profile=json.dumps(profile, ensure_ascii=False),
        resume=resume,
        title=job.get("title", ""), company=job.get("company", ""),
        location=job.get("location", ""), desc=(job.get("description") or "")[:1500],
    ), max_tokens=1200)
    return {"letter": text}


@app.get("/")
def index():
    if FRONTEND.exists():
        return FileResponse(str(FRONTEND))
    return JSONResponse({"error": "frontend/index.html not found"}, status_code=404)
