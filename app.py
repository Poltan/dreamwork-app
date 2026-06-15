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
import pathlib
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
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
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


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


def parse_resume(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception as e:
            raise HTTPException(400, f"Could not read PDF: {e}")
    if name.endswith(".docx"):
        try:
            import docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs)
        except Exception as e:
            raise HTTPException(400, f"Could not read DOCX: {e}")
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
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
  "seniority": "junior|mid|senior|lead|director|c-level",
  "target_titles": ["3-6 реалистичных названий вакансий, на которые стоит откликаться"],
  "search_query": "1-2 СЛОВА — основное название роли для поиска вакансий (на языке рынка кандидата), без навыков и названий программ",
  "skills": ["8-15 ключевых навыков"],
  "industries": ["отрасли кандидата"],
  "languages": ["языки кандидата"],
  "summary": "2-3 предложения о кандидате"
}}

РЕЗЮМЕ:
{resume}"""


RANK_PROMPT = """Ты — карьерный консультант. Ниже профиль кандидата и список вакансий.
Отбери НЕ БОЛЕЕ {top} самых подходящих вакансий: по роли, грейду, отрасли, локации и зарплатным ожиданиям ({salary_note}).
Если страна поиска не задана — допускай сильные вакансии из разных стран мира (география может быть разной).
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
        },
        "always_on": ["hh.ru", "Remotive"],
    }


@app.post("/api/match")
async def match(
    resume: UploadFile = File(...),
    country: str = Form(""),
    location: str = Form(""),
    salary_min: str = Form(""),
    currency: str = Form(""),
    remote_ok: str = Form("false"),
    top: int = Form(5),
):
    top = max(1, min(int(top), 5))

    data = await resume.read()
    if not data:
        raise HTTPException(400, "Empty resume file.")
    resume_text = parse_resume(resume.filename, data)
    if len(resume_text.strip()) < 50:
        raise HTTPException(400, "Could not extract enough text from the resume.")

    profile = _extract_json(_ask_claude(
        PROFILE_PROMPT.format(resume=resume_text[:8000]), max_tokens=900))

    sal = int(salary_min) if str(salary_min).strip().isdigit() else None

    # Build progressively broader queries; use the first that returns jobs.
    candidates = []
    sq = (profile.get("search_query") or "").strip()
    if sq:
        candidates.append(sq)                       # full keyword query
        first2 = " ".join(sq.split()[:2])           # first 2 words (broader)
        if first2 and first2 not in candidates:
            candidates.append(first2)
    for t in (profile.get("target_titles") or [])[:3]:
        t = (t or "").strip()
        if t and t not in candidates:
            candidates.append(t)                    # concise role titles
    hl = (profile.get("headline") or "").strip()
    if hl and hl not in candidates:
        candidates.append(hl)
    if not candidates:
        candidates = ["manager"]

    # Run several query variants IN PARALLEL; aggregate + de-dupe.
    queries = candidates[:3]
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
        min_rub = _to_rub(sal, currency)
        kept = []
        for j in jobs:
            top_sal = j.get("salary_max") or j.get("salary_min")
            if not top_sal or _to_rub(top_sal, j.get("currency")) >= min_rub * 0.9:
                kept.append(j)
        jobs = kept

    if not jobs:
        return JSONResponse({"profile": profile, "jobs": [], "debug": debug,
                             "tried": candidates,
                             "note": "Провайдеры не вернули вакансий по этому запросу."})

    trimmed = [{"id": j["id"], "title": j["title"], "company": j["company"],
                "location": j["location"], "salary": j["salary"],
                "remote": j["remote"], "desc": (j["description"] or "")[:300]}
               for j in jobs[:40]]
    salary_note = f"от {salary_min} {currency}" if sal else "не указаны"
    try:
        ranked = _extract_json(_ask_claude(
            RANK_PROMPT.format(top=top, salary_note=salary_note,
                               profile=json.dumps(profile, ensure_ascii=False),
                               jobs=json.dumps(trimmed, ensure_ascii=False)),
            max_tokens=1500))
    except Exception:
        ranked = [{"id": j["id"], "score": None, "fit": ""} for j in jobs[:top]]

    by_id = {j["id"]: j for j in jobs}
    results = []
    for r in ranked[:top]:
        j = by_id.get(r.get("id"))
        if not j:
            continue
        j = dict(j)
        j["score"] = r.get("score")
        j["fit"] = r.get("fit", "")
        results.append(j)

    return {"profile": profile, "jobs": results, "debug": debug,
            "resume_excerpt": resume_text[:4000]}


@app.post("/api/letter")
async def letter(payload: dict):
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
