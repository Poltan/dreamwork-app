"""Dreamwork — job providers layer (hybrid sourcing + country routing + dedup)."""
import os, re, hashlib, requests
from concurrent.futures import ThreadPoolExecutor

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
TIMEOUT = 20

def _hash_id(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]

def _clean(text):
    if not text: return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

ADZUNA_COUNTRIES = {"gb","us","ca","au","at","br","de","es","fr","in","it","mx","nl","nz","pl","sg","za"}

def _currency_for(country):
    return {"us":"USD","ca":"CAD","gb":"GBP","au":"AUD","in":"INR","de":"EUR","fr":"EUR","es":"EUR",
            "it":"EUR","nl":"EUR","pl":"PLN","br":"BRL","mx":"MXN","nz":"NZD","sg":"SGD","za":"ZAR","at":"EUR"}.get(country,"")

def _fmt_salary(lo, hi, cur):
    if not lo and not hi: return None
    def k(v):
        try: return f"{int(v):,}".replace(",", " ")
        except Exception: return str(v)
    if lo and hi and lo != hi: return f"{k(lo)}-{k(hi)} {cur}".strip()
    return f"{k(lo or hi)} {cur}".strip()

def fetch_adzuna(keywords, location="", country="us", salary_min=None, limit=20):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    country = (country or "us").lower()
    if not (app_id and app_key) or country not in ADZUNA_COUNTRIES: return []
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    params = {"app_id": app_id, "app_key": app_key, "results_per_page": min(limit, 50),
              "what": keywords, "content-type": "application/json"}
    if location: params["where"] = location
    try:
        r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT); r.raise_for_status(); data = r.json()
    except Exception as e:
        return [{"_error": f"adzuna: {e}"}]
    cur = _currency_for(country); out = []
    for j in data.get("results", []):
        out.append({"id": _hash_id("adzuna", j.get("id")), "title": _clean(j.get("title")),
                    "company": _clean((j.get("company") or {}).get("display_name")),
                    "location": _clean((j.get("location") or {}).get("display_name")), "country": country.upper(),
                    "salary_min": j.get("salary_min"), "salary_max": j.get("salary_max"), "currency": cur,
                    "salary": _fmt_salary(j.get("salary_min"), j.get("salary_max"), cur),
                    "remote": "remote" in (str(j.get("title"))+str(j.get("description"))).lower(),
                    "url": j.get("redirect_url"), "description": _clean(j.get("description"))[:1500], "source": "Adzuna"})
    return out

def fetch_jooble(keywords, location="", country=None, salary_min=None, limit=20):
    key = os.getenv("JOOBLE_API_KEY")
    if not key: return []
    try:
        r = requests.post(f"https://jooble.org/api/{key}",
                          json={"keywords": keywords, "location": location or "", "page": "1"},
                          headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
        if r.status_code != 200:
            return [{"_error": f"jooble HTTP {r.status_code}: {r.text[:160]}"}]
        data = r.json()
    except Exception as e:
        return [{"_error": f"jooble: {e}"}]
    out = []
    for j in data.get("jobs", [])[:limit]:
        out.append({"id": _hash_id("jooble", j.get("id") or j.get("link")), "title": _clean(j.get("title")),
                    "company": _clean(j.get("company")), "location": _clean(j.get("location")),
                    "country": (country or "").upper(), "salary_min": None, "salary_max": None, "currency": "",
                    "salary": _clean(j.get("salary")) or None,
                    "remote": "remote" in (str(j.get("title"))+str(j.get("snippet"))).lower(),
                    "url": j.get("link"), "description": _clean(j.get("snippet"))[:1500], "source": "Jooble"})
    return out

def fetch_hh(keywords, location="", country=None, salary_min=None, limit=20):
    text = f"{keywords} {location}".strip() if location else keywords
    params = {"text": text, "per_page": min(limit, 50), "page": 0}
    headers = dict(UA)
    tok = os.getenv("HH_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = requests.get("https://api.hh.ru/vacancies", params=params, headers=headers, timeout=TIMEOUT)
        r.raise_for_status(); data = r.json()
    except Exception as e:
        return [{"_error": f"hh: {e}"}]
    out = []
    for j in data.get("items", []):
        sal = j.get("salary") or {}; cur = (sal.get("currency") or "").upper()
        out.append({"id": _hash_id("hh", j.get("id")), "title": _clean(j.get("name")),
                    "company": _clean((j.get("employer") or {}).get("name")),
                    "location": _clean((j.get("area") or {}).get("name")), "country": "RU",
                    "salary_min": sal.get("from"), "salary_max": sal.get("to"), "currency": cur,
                    "salary": _fmt_salary(sal.get("from"), sal.get("to"), cur) if sal else None,
                    "remote": (j.get("schedule") or {}).get("id") == "remote", "url": j.get("alternate_url"),
                    "description": _clean((j.get("snippet") or {}).get("responsibility"))[:1500], "source": "hh.ru"})
    return out

def fetch_remotive(keywords, location="", country=None, salary_min=None, limit=20):
    try:
        r = requests.get("https://remotive.com/api/remote-jobs", params={"search": keywords, "limit": limit},
                         headers=UA, timeout=TIMEOUT); r.raise_for_status(); data = r.json()
    except Exception as e:
        return [{"_error": f"remotive: {e}"}]
    out = []
    for j in data.get("jobs", [])[:limit]:
        out.append({"id": _hash_id("remotive", j.get("id")), "title": _clean(j.get("title")),
                    "company": _clean(j.get("company_name")),
                    "location": _clean(j.get("candidate_required_location")) or "Remote", "country": "",
                    "salary_min": None, "salary_max": None, "currency": "", "salary": _clean(j.get("salary")) or None,
                    "remote": True, "url": j.get("url"), "description": _clean(j.get("description"))[:1500],
                    "source": "Remotive"})
    return out

def fetch_trudvsem(keywords, location="", country=None, salary_min=None, limit=20):
    # «Работа России» (госпортал) — открытый API, без ключа и без Cloudflare.
    url = "https://opendata.trudvsem.ru/api/v1/vacancies"
    params = {"text": keywords, "limit": min(limit, 100), "offset": 0}
    try:
        r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
        if r.status_code != 200:
            return [{"_error": f"trudvsem HTTP {r.status_code}"}]
        data = r.json()
    except Exception as e:
        return [{"_error": f"trudvsem: {e}"}]
    out = []
    vacs = ((data.get("results") or {}).get("vacancies")) or []
    for item in vacs[:limit]:
        v = item.get("vacancy") or {}
        sf, st = v.get("salary_min"), v.get("salary_max")
        out.append({"id": _hash_id("trudvsem", v.get("id")),
                    "title": _clean(v.get("job-name")),
                    "company": _clean((v.get("company") or {}).get("name")),
                    "location": _clean((v.get("region") or {}).get("name")),
                    "country": "RU", "salary_min": sf, "salary_max": st, "currency": "RUB",
                    "salary": _fmt_salary(sf, st, "RUB"),
                    "remote": False, "url": v.get("vac_url"),
                    "description": _clean(v.get("duty"))[:1500], "source": "Trudvsem"})
    return out

COUNTRY_CODE = {"russia":"RU","россия":"RU","рф":"RU","usa":"US","united states":"US","сша":"US","america":"US",
                "canada":"CA","канада":"CA","uk":"GB","united kingdom":"GB","великобритания":"GB","england":"GB",
                "israel":"IL","израиль":"IL","saudi arabia":"SA","саудовская аравия":"SA","ksa":"SA","uae":"AE",
                "оаэ":"AE","germany":"DE","германия":"DE","france":"FR","франция":"FR","spain":"ES","испания":"ES",
                "australia":"AU","австралия":"AU","india":"IN","индия":"IN"}

def normalize_country(name):
    if not name: return ""
    return COUNTRY_CODE.get(name.strip().lower(), name.strip().upper()[:2])

GLOBAL_ADZUNA_MARKETS = ["us", "gb", "ca", "au"]

def search_jobs(keywords, location="", country="", salary_min=None, remote_ok=False, per_provider=20):
    code = normalize_country(country)
    is_global = not code
    providers = []
    if code == "RU":
        providers.append(("trudvsem", lambda: fetch_trudvsem(keywords, location, code, salary_min, per_provider)))
        providers.append(("hh", lambda: fetch_hh(keywords, location, code, salary_min, per_provider)))
    adzuna_targets = []
    if code and code.lower() in ADZUNA_COUNTRIES:
        adzuna_targets = [code.lower()]
    elif is_global:
        adzuna_targets = GLOBAL_ADZUNA_MARKETS
    for cc in adzuna_targets:
        loc = "" if is_global else location
        providers.append((f"adzuna:{cc}", lambda cc=cc, loc=loc: fetch_adzuna(keywords, loc, cc, salary_min, per_provider)))
    jloc = "" if is_global else location
    providers.append(("jooble", lambda: fetch_jooble(keywords, jloc, code, salary_min, per_provider)))
    if remote_ok or is_global:
        providers.append(("remotive", lambda: fetch_remotive(keywords, location, code, salary_min, per_provider)))
    jobs, debug = [], {}
    def _run(item):
        name, fn = item
        try:
            return name, fn(), None
        except Exception as e:
            return name, None, e
    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as ex:
        for name, res, err in ex.map(_run, providers):
            if err is not None:
                debug[name] = f"exception: {err}"; continue
            errs = [r["_error"] for r in res if isinstance(r, dict) and r.get("_error")]
            clean = [r for r in res if not (isinstance(r, dict) and r.get("_error"))]
            debug[name] = errs[0] if errs else f"{len(clean)} jobs"
            jobs.extend(clean)
    seen, deduped = set(), []
    for j in jobs:
        sig = (j.get("title","").lower(), j.get("company","").lower())
        if sig in seen: continue
        seen.add(sig); deduped.append(j)
    return deduped, debug
