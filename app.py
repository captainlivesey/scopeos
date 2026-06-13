import streamlit as st
import sqlite3
import json
import requests
import pandas as pd
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from datetime import datetime, timedelta

from follow_up.logic import init_followup_tables, create_followup_from_lead, run_due_sequences, get_active_sequences

DB_PATH = Path("scopeos.db")
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "llama3.2"
SEED = 42
TEMPERATURE = 0

TIER_EMOJI = {"Hot": "🔥", "Warm": "🌤️", "Cold": "❄️"}
TIER_COLOR = {"Hot": "#ff4d6d", "Warm": "#ffb347", "Cold": "#4dd8ff"}
TIER_GLOW = {"Hot": "rgba(255,77,109,0.35)", "Warm": "rgba(255,179,71,0.30)", "Cold": "rgba(77,216,255,0.30)"}

FIT_RULES = {
    "title": {"ceo": 15, "founder": 15, "coo": 12, "cmo": 12, "sales": 10, "marketing": 8, "head": 8, "manager": 5, "assistant": 0, "intern": -10},
    "company_size": [(1, 10, -10), (11, 49, 5), (50, 199, 15), (200, 999, 10), (1000, 1000000, 0)],
    "intent_keywords": {"demo": 20, "pricing": 15, "budget": 15, "urgent": 15, "next week": 15, "follow up": 10, "contact": 8, "schedule": 12, "compare": 10, "trial": 18, "proposal": 12, "decision": 12, "implement": 10, "book": 12, "call": 8, "quote": 12, "buy": 15, "need now": 18},
    "negative_keywords": {"just curious": -10, "not now": -15, "maybe later": -10, "no budget": -20, "student": -20, "spam": -30, "research": -5, "vendor list": -10}
}

WORKFLOW_RULES = {
    "Hot": {"owner": "sales", "action": "call_now", "sequence": "hot_sequence"},
    "Warm": {"owner": "sdr", "action": "follow_up", "sequence": "warm_sequence"},
    "Cold": {"owner": "nurture", "action": "nurture", "sequence": "cold_sequence"}
}

EMAIL_TEMPLATES = {
    "Hot": "Hallo {contact_name},\n\nwir haben Ihre Anfrage erhalten und möchten Sie umgehend kontaktieren.\nUnser Team meldet sich innerhalb von 2 Stunden.\n\nViele Grüße,\nScopeOS Team",
    "Warm": "Hallo {contact_name},\n\nvielen Dank für Ihr Interesse. Darf ich kurz nachhaken?\n\nViele Grüße,\nScopeOS Team",
    "Cold": "Hallo {contact_name},\n\ndanke für Ihr Interesse. Wir melden uns bei relevanten Updates.\n\nViele Grüße,\nScopeOS Team"
}

SLACK_TEMPLATES = {
    "Hot": "🔥 HOT LEAD — {company_name} ({contact_name}) | Score: {score}/100 | {email}",
    "Warm": "🌤️ WARM LEAD — {company_name} ({contact_name}) | Score: {score}/100 | {email}",
    "Cold": "❄️ Cold Lead — {company_name} | Score: {score}/100"
}

# ---------------------------------------------------------------------------
# Data layer (unchanged logic)
# ---------------------------------------------------------------------------

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_column(cur, table, col, col_type):
    cur.execute(f"PRAGMA table_info({table})")
    if col not in [r[1] for r in cur.fetchall()]:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")

def init_db():
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_text TEXT, company_name TEXT, contact_name TEXT, email TEXT, website TEXT, source TEXT, raw_payload TEXT, unique_key TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, score INTEGER, tier TEXT, fit_score INTEGER, intent_score INTEGER, reason TEXT, next_step TEXT, analysis TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS activities (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, activity_type TEXT, payload TEXT, status TEXT DEFAULT 'queued', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
        for col in ["source", "raw_payload", "updated_at", "unique_key"]:
            ensure_column(cur, "leads", col, "TEXT")
        conn.commit()

def upsert_setting(key, value):
    with get_connection() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()

def get_setting(key, default=""):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

# Keys that may be provided via st.secrets (Streamlit Cloud "Secrets" UI).
# If present there, they take precedence over the settings table and the
# sidebar input is shown as locked/read-only.
SECRET_KEYS = ["slack_url", "crm_url", "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "notify_email"]

def get_secret(key, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default

def is_locked_by_secret(key):
    return key in SECRET_KEYS and get_secret(key) not in (None, "")

def get_secret_or_setting(key, default=""):
    val = get_secret(key)
    if val not in (None, ""):
        return str(val)
    return get_setting(key, default)

def normalize(text):
    return " ".join((text or "").lower().strip().split())

def make_unique_key(d):
    return "|".join([normalize(d.get(k, "")) for k in ["lead_text", "company_name", "contact_name", "email", "website"]])[:500]

def upsert_lead(data):
    init_db()
    d = {k: (data.get(k) or "").strip() for k in ["lead_text", "company_name", "contact_name", "email", "website", "source", "raw_payload"]}
    d["unique_key"] = make_unique_key(d)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM leads WHERE unique_key=?", (d["unique_key"],))
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE leads SET lead_text=?,company_name=?,contact_name=?,email=?,website=?,source=?,raw_payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (d["lead_text"], d["company_name"], d["contact_name"], d["email"], d["website"], d["source"], d["raw_payload"], row["id"]))
            conn.commit()
            return row["id"], True
        cur.execute("INSERT INTO leads(lead_text,company_name,contact_name,email,website,source,raw_payload,unique_key) VALUES(?,?,?,?,?,?,?,?)", (d["lead_text"], d["company_name"], d["contact_name"], d["email"], d["website"], d["source"], d["raw_payload"], d["unique_key"]))
        conn.commit()
        return cur.lastrowid, False

def bulk_upsert(df, source="bulk"):
    results = []
    for _, row in df.iterrows():
        lead_id, dup = upsert_lead({"lead_text": row.get("lead_text", ""), "company_name": row.get("company_name", ""), "contact_name": row.get("contact_name", ""), "email": row.get("email", ""), "website": row.get("website", ""), "source": source, "raw_payload": json.dumps(row.to_dict(), ensure_ascii=False)})
        results.append((lead_id, dup))
    return results

def get_leads():
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM leads ORDER BY id DESC")
        return cur.fetchall()

def get_analyses_for_lead(lead_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM analyses WHERE lead_id=? ORDER BY id DESC", (lead_id,))
        return cur.fetchall()

def save_analysis(lead_id, score, tier, fit_score, intent_score, reason, next_step, analysis):
    if isinstance(analysis, (dict, list)):
        analysis = json.dumps(analysis, ensure_ascii=False)
    with get_connection() as conn:
        conn.execute("INSERT INTO analyses(lead_id,score,tier,fit_score,intent_score,reason,next_step,analysis) VALUES(?,?,?,?,?,?,?,?)", (lead_id, score, tier, fit_score, intent_score, reason, next_step, analysis))
        conn.commit()

def log_activity(lead_id, activity_type, payload, status="done"):
    with get_connection() as conn:
        conn.execute("INSERT INTO activities(lead_id,activity_type,payload,status) VALUES(?,?,?,?)", (lead_id, activity_type, json.dumps(payload, ensure_ascii=False), status))
        conn.commit()

def get_lead_table():
    rows = []
    for lead in get_leads():
        latest_rows = get_analyses_for_lead(lead["id"])
        latest = latest_rows[0] if latest_rows else None
        rows.append({"id": lead["id"], "tier": latest["tier"] if latest else "—", "score": latest["score"] if latest else None, "company": lead["company_name"] or "—", "contact": lead["contact_name"] or "—", "email": lead["email"] or "—", "website": lead["website"] or "—", "source": lead["source"] or "—", "next_step": latest["next_step"] if latest else "—", "lead_text": lead["lead_text"] or "—", "reason": latest["reason"] if latest else "—", "analysis": latest["analysis"] if latest else "—", "created_at": lead["created_at"], "updated_at": lead["updated_at"]})
    return pd.DataFrame(rows)

def extract_company_size(text):
    t = (text or "").lower()
    nums = [int(tok) for tok in t.replace("+", " ").replace("employees", " ").replace("people", " ").split() if tok.isdigit()]
    if len(nums) >= 2:
        return min(nums), max(nums)
    if "startup" in t:
        return (1, 10)
    if "small" in t:
        return (1, 49)
    if "mid" in t or "growing" in t:
        return (50, 199)
    if "enterprise" in t:
        return (200, 1000000)
    return None

def enrich_lead(lead):
    text = f"{lead['lead_text'] or ''} {lead['company_name'] or ''} {lead['website'] or ''}".lower()
    size = extract_company_size(text)
    employee_band = None
    if size:
        lo, hi = size
        for a, b, label in [(1, 10, "startup"), (11, 49, "small"), (50, 199, "midmarket"), (200, 999999, "enterprise")]:
            if lo >= a and hi <= b:
                employee_band = label
                break
    industry = next((kw for kw in ["saas", "agency", "consulting", "software", "marketplace", "ai", "fintech", "health", "ecommerce"] if kw in text), None)
    domain = (lead["website"] or "").replace("https://", "").replace("http://", "").split("/")[0].lower()
    return {"domain": domain or "—", "industry": industry or "unbekannt", "employee_band": employee_band or "unbekannt", "enriched_at": datetime.utcnow().isoformat(timespec="seconds")}

def score_lead(lead):
    text = f"{lead['lead_text'] or ''} {lead['company_name'] or ''} {lead['contact_name'] or ''} {lead['email'] or ''} {lead['website'] or ''}".lower()
    fit, intent = 0, 0
    for k, v in FIT_RULES["title"].items():
        if k in (lead["contact_name"] or "").lower():
            fit += v
            break
    size = extract_company_size(text)
    if size:
        lo, hi = size
        for a, b, pts in FIT_RULES["company_size"]:
            if lo >= a and hi <= b:
                fit += pts
                break
    if any(d in text for d in ["gmail", "outlook", "hotmail", "yahoo"]):
        fit -= 5
    if lead["website"]:
        fit += 5
    for k, v in FIT_RULES["intent_keywords"].items():
        if k in text:
            intent += v
    for k, v in FIT_RULES["negative_keywords"].items():
        if k in text:
            intent += v
    score = max(0, min(100, round(fit + intent)))
    tier = "Hot" if score >= 70 else "Warm" if score >= 40 else "Cold"
    reason = f"Fit {fit}, Intent {intent}."
    next_step = {"Hot": "Sofort anrufen und Demo terminieren.", "Warm": "Follow-up-Mail senden.", "Cold": "Ins Nurture aufnehmen."}[tier]
    return score, tier, fit, intent, reason, next_step

def llm_explain(lead, score, tier, fit_score, intent_score, reason, next_step):
    prompt = f"Du bist ein Sales Ops Assistent. Erkläre kurz warum dieser Lead den Score {score}/100 hat. Tier={tier}, Fit={fit_score}, Intent={intent_score}. Nächster Schritt: {next_step}. Lead: Firma={lead['company_name']}, Kontakt={lead['contact_name']}, Text={lead['lead_text']}"
    r = requests.post(OLLAMA_URL, json={"model": MODEL, "prompt": prompt, "stream": False, "options": {"temperature": TEMPERATURE, "seed": SEED}}, timeout=120)
    r.raise_for_status()
    return r.json().get("response", "")

def send_slack(lead, score, tier):
    url = get_secret_or_setting("slack_url")
    if not url:
        return False, "Keine Slack URL"
    msg = SLACK_TEMPLATES.get(tier, SLACK_TEMPLATES["Cold"]).format(company_name=lead["company_name"] or "—", contact_name=lead["contact_name"] or "—", email=lead["email"] or "—", score=score)
    try:
        r = requests.post(url, json={"text": msg}, timeout=10)
        r.raise_for_status()
        return True, "OK"
    except Exception as e:
        return False, str(e)

def send_to_crm(lead, score, tier, enrichment):
    url = get_secret_or_setting("crm_url")
    if not url:
        return False, "Keine CRM URL"
    payload = {"lead_id": lead["id"], "company_name": lead["company_name"], "contact_name": lead["contact_name"], "email": lead["email"], "website": lead["website"], "score": score, "tier": tier, "enrichment": enrichment}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True, "OK"
    except Exception as e:
        return False, str(e)

def send_email_notification(lead, score, tier):
    smtp_host = get_secret_or_setting("smtp_host")
    smtp_port = get_secret_or_setting("smtp_port", "587")
    smtp_user = get_secret_or_setting("smtp_user")
    smtp_pass = get_secret_or_setting("smtp_pass")
    notify_to = get_secret_or_setting("notify_email")
    if not all([smtp_host, smtp_user, smtp_pass, notify_to]):
        return False, "SMTP nicht konfiguriert"
    body = EMAIL_TEMPLATES.get(tier, EMAIL_TEMPLATES["Cold"]).format(contact_name=lead["contact_name"] or "Kontakt")
    try:
        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = notify_to
        msg["Subject"] = f"[ScopeOS] {tier} Lead: {lead['company_name'] or 'Unbekannt'} ({score}/100)"
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(smtp_host, int(smtp_port)) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, notify_to, msg.as_string())
        return True, "OK"
    except Exception as e:
        return False, str(e)

def run_workflow(lead, score, tier):
    enrichment = enrich_lead(lead)
    wf = WORKFLOW_RULES[tier]
    results = {"tier": tier, "score": score, "workflow": wf, "enrichment": enrichment, "slack": "skipped", "crm": "skipped", "email": "skipped"}
    if get_setting("slack_enabled", "true") == "true":
        ok, msg = send_slack(lead, score, tier)
        results["slack"] = "ok" if ok else f"error: {msg}"
        log_activity(lead["id"], "slack", {"result": results["slack"]})
    if get_setting("crm_enabled", "true") == "true":
        ok, msg = send_to_crm(lead, score, tier, enrichment)
        results["crm"] = "ok" if ok else f"error: {msg}"
        log_activity(lead["id"], "crm", {"result": results["crm"]})
    if get_setting("email_enabled", "true") == "true":
        ok, msg = send_email_notification(lead, score, tier)
        results["email"] = "ok" if ok else f"error: {msg}"
        log_activity(lead["id"], "email", {"result": results["email"]})
    log_activity(lead["id"], "workflow", results)
    return results

def analyze_lead(lead, force=False, run_automations=True):
    if not force and get_analyses_for_lead(lead["id"]):
        return None
    score, tier, fit_score, intent_score, reason, next_step = score_lead(lead)
    try:
        analysis = llm_explain(lead, score, tier, fit_score, intent_score, reason, next_step)
    except Exception as e:
        analysis = f"Ollama nicht erreichbar: {e}"
    save_analysis(lead["id"], score, tier, fit_score, intent_score, reason, next_step, analysis)
    if run_automations:
        run_workflow(lead, score, tier)
    return score, tier

def analyze_all(skip_existing=True, run_automations=True):
    results = []
    for lead in get_leads():
        if skip_existing and get_analyses_for_lead(lead["id"]):
            continue
        res = analyze_lead(lead, force=True, run_automations=run_automations)
        if res:
            results.append((lead["id"], *res))
    return results

def parse_bulk_text(text):
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")] + [""] * 5
        rows.append({"lead_text": parts[0], "company_name": parts[1], "contact_name": parts[2], "email": parts[3], "website": parts[4]})
    return pd.DataFrame(rows)

def simulate_webhook(payload_text):
    try:
        data = json.loads(payload_text)
        if isinstance(data, dict):
            return upsert_lead({"lead_text": data.get("lead_text", ""), "company_name": data.get("company_name", ""), "contact_name": data.get("contact_name", ""), "email": data.get("email", ""), "website": data.get("website", ""), "source": data.get("source", "webhook"), "raw_payload": payload_text})
    except Exception:
        pass
    return None, False

def get_stats():
    leads = get_leads()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM analyses")
        ac = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM activities")
        act = cur.fetchone()["c"]
        cur.execute("SELECT tier, COUNT(*) c FROM analyses GROUP BY tier")
        tiers = {r["tier"]: r["c"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE created_at >= date('now')")
        today = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE created_at >= datetime('now', '-7 days')")
        week = cur.fetchone()["c"]
    return len(leads), ac, act, tiers, today, week

def check_ollama():
    try:
        requests.get("http://127.0.0.1:11434", timeout=2)
        return True
    except Exception:
        return False

def freshness_hint(created_at):
    if pd.isna(created_at):
        return "—"
    delta = datetime.now() - created_at.to_pydatetime()
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"vor {mins}m"
    return f"vor {mins//60}h"

# ---------------------------------------------------------------------------
# UI helpers — modern / futuristic visual layer
# ---------------------------------------------------------------------------

def inject_css():
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700;800&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

    :root{
        --bg-0:#06080d;
        --bg-1:#0a0e16;
        --panel:rgba(255,255,255,0.035);
        --panel-strong:rgba(255,255,255,0.06);
        --border:rgba(255,255,255,0.08);
        --border-soft:rgba(255,255,255,0.05);
        --text:#eaf0f7;
        --text-dim:#8a96a8;
        --accent:#7dd3fc;
        --accent-2:#a78bfa;
        --hot:#ff4d6d;
        --warm:#ffb347;
        --cold:#4dd8ff;
    }

    html, body, [class*='css']{
        font-family:'Inter', system-ui, -apple-system, sans-serif;
    }

    .stApp{
        background:
            radial-gradient(1200px 700px at 85% -10%, rgba(124,92,255,0.16), transparent 60%),
            radial-gradient(900px 600px at -5% 10%, rgba(77,216,255,0.10), transparent 55%),
            linear-gradient(180deg, var(--bg-0) 0%, var(--bg-1) 100%);
        color:var(--text);
    }

    /* Sidebar */
    section[data-testid='stSidebar']{
        background:linear-gradient(180deg, #060a10 0%, #08101a 100%);
        border-right:1px solid var(--border-soft);
    }
    section[data-testid='stSidebar'] .block-container{padding-top:1.4rem;}

    .block-container{padding-top:1.2rem; padding-bottom:3rem; max-width:1400px;}

    /* Headings */
    h1, h2, h3{
        font-family:'Space Grotesk', sans-serif !important;
        letter-spacing:-0.01em;
    }

    /* Tabs */
    .stTabs [data-baseweb='tab-list']{ gap:6px; border-bottom:1px solid var(--border-soft); }
    .stTabs [data-baseweb='tab']{
        background:transparent;
        border-radius:10px 10px 0 0;
        padding:10px 18px;
        color:var(--text-dim);
        font-weight:600;
        font-size:0.92rem;
        transition:all .15s ease;
    }
    .stTabs [data-baseweb='tab']:hover{ color:var(--text); background:rgba(255,255,255,0.03); }
    .stTabs [aria-selected='true']{
        background:linear-gradient(180deg, rgba(125,211,252,0.12), rgba(167,139,250,0.06)) !important;
        color:var(--text) !important;
        box-shadow: inset 0 -2px 0 var(--accent);
    }

    /* Buttons */
    .stButton > button{
        border-radius:10px;
        border:1px solid var(--border);
        background:linear-gradient(180deg, rgba(255,255,255,0.05), rgba(255,255,255,0.02));
        color:var(--text);
        font-weight:600;
        transition:all .15s ease;
    }
    .stButton > button:hover{
        border-color:var(--accent);
        box-shadow:0 0 0 1px var(--accent), 0 0 18px rgba(125,211,252,0.18);
        color:var(--accent);
    }
    .stDownloadButton > button{
        border-radius:10px;
        border:1px solid var(--border);
        background:linear-gradient(135deg, rgba(125,211,252,0.10), rgba(167,139,250,0.10));
        font-weight:600;
    }

    /* Inputs */
    .stTextInput input, .stTextArea textarea, .stSelectbox div[data-baseweb='select'] > div, .stMultiSelect div[data-baseweb='select'] > div{
        background:rgba(255,255,255,0.03) !important;
        border:1px solid var(--border) !important;
        border-radius:10px !important;
        color:var(--text) !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus{
        border-color:var(--accent) !important;
        box-shadow:0 0 0 1px var(--accent) !important;
    }

    /* Expander */
    .streamlit-expanderHeader{
        background:rgba(255,255,255,0.03);
        border-radius:10px;
        border:1px solid var(--border-soft);
        font-weight:600;
    }

    /* Dataframe */
    [data-testid='stDataFrame']{ border-radius:12px; overflow:hidden; border:1px solid var(--border-soft); }

    /* Toggle */
    .stToggle{ font-weight:600; }

    /* Divider */
    hr{ border-color:var(--border-soft) !important; }

    /* Scrollbar */
    ::-webkit-scrollbar{ width:10px; height:10px; }
    ::-webkit-scrollbar-thumb{ background:rgba(255,255,255,0.08); border-radius:6px; }
    ::-webkit-scrollbar-track{ background:transparent; }

    /* ---- Custom components ---- */

    .hero{
        display:flex; align-items:center; justify-content:space-between;
        padding:22px 28px;
        border-radius:20px;
        border:1px solid var(--border-soft);
        background:linear-gradient(135deg, rgba(125,211,252,0.07), rgba(167,139,250,0.05));
        margin-bottom:1.4rem;
        position:relative;
        overflow:hidden;
    }
    .hero::before{
        content:'';
        position:absolute; inset:0;
        background:radial-gradient(500px 200px at 90% 0%, rgba(125,211,252,0.18), transparent 70%);
        pointer-events:none;
    }
    .hero-title{
        font-family:'Space Grotesk', sans-serif;
        font-size:2.1rem; font-weight:800;
        background:linear-gradient(90deg, #ffffff 20%, var(--accent) 60%, var(--accent-2) 100%);
        -webkit-background-clip:text; background-clip:text; color:transparent;
        margin:0; line-height:1.1;
    }
    .hero-sub{ color:var(--text-dim); font-size:0.95rem; margin-top:4px; }
    .hero-status{
        display:flex; align-items:center; gap:8px;
        font-size:0.8rem; font-weight:600; color:var(--text-dim);
        padding:8px 14px; border-radius:999px;
        border:1px solid var(--border-soft);
        background:rgba(255,255,255,0.03);
    }
    .dot{ width:8px; height:8px; border-radius:50%; display:inline-block; }
    .dot-on{ background:#3ee08a; box-shadow:0 0 8px #3ee08a; }
    .dot-off{ background:#ff4d6d; box-shadow:0 0 8px #ff4d6d; }

    .stat-card{
        background:var(--panel);
        border:1px solid var(--border-soft);
        border-radius:16px;
        padding:16px 18px 14px 18px;
        position:relative;
        overflow:hidden;
        transition:transform .15s ease, border-color .15s ease;
    }
    .stat-card:hover{ transform:translateY(-2px); border-color:rgba(255,255,255,0.16); }
    .stat-card .glow{
        position:absolute; top:-30px; right:-30px; width:90px; height:90px; border-radius:50%;
        filter:blur(30px); opacity:0.5;
    }
    .stat-label{ font-size:0.78rem; color:var(--text-dim); font-weight:600; text-transform:uppercase; letter-spacing:0.06em; }
    .stat-value{ font-family:'Space Grotesk', sans-serif; font-size:2rem; font-weight:800; line-height:1.15; margin-top:6px; }
    .stat-sub{ font-size:0.78rem; color:var(--text-dim); margin-top:8px; }

    .section-title{
        display:flex; align-items:center; gap:10px;
        font-family:'Space Grotesk', sans-serif;
        font-weight:700; font-size:1.05rem;
        margin:18px 0 10px 0;
        padding-bottom:8px;
        border-bottom:1px solid var(--border-soft);
    }
    .section-pill{
        font-size:0.72rem; font-weight:700; padding:2px 9px; border-radius:999px;
        border:1px solid var(--border-soft); color:var(--text-dim);
    }

    .lead-card{
        position:relative;
        background:linear-gradient(135deg, rgba(255,255,255,0.04), rgba(255,255,255,0.015));
        border:1px solid var(--border-soft);
        border-radius:16px;
        padding:16px 18px 12px 18px;
        margin-bottom:12px;
        transition:transform .12s ease, border-color .12s ease, box-shadow .12s ease;
    }
    .lead-card:hover{
        transform:translateY(-1px);
        border-color:rgba(255,255,255,0.14);
    }
    .lead-card::before{
        content:''; position:absolute; left:0; top:14px; bottom:14px; width:3px; border-radius:3px;
    }
    .lead-card.tier-hot::before{ background:var(--hot); box-shadow:0 0 10px var(--hot); }
    .lead-card.tier-warm::before{ background:var(--warm); box-shadow:0 0 10px var(--warm); }
    .lead-card.tier-cold::before{ background:var(--cold); box-shadow:0 0 10px var(--cold); }
    .lead-card.tier-none::before{ background:var(--text-dim); }

    .lead-company{ font-size:1.02rem; font-weight:700; color:var(--text); }
    .lead-meta{ font-size:0.8rem; color:var(--text-dim); margin-top:2px; }
    .lead-link{ font-size:0.85rem; color:var(--accent) !important; text-decoration:none; }
    .lead-link:hover{ text-decoration:underline; }
    .lead-next{ font-size:0.8rem; color:var(--text-dim); margin-top:10px; }
    .lead-next b{ color:var(--text); }

    .score-ring{
        width:54px; height:54px; border-radius:50%;
        display:flex; align-items:center; justify-content:center;
        font-family:'Space Grotesk', sans-serif; font-weight:800; font-size:1.05rem;
        border:3px solid currentColor;
        margin:0 auto;
    }
    .score-unit{ font-size:0.6rem; color:var(--text-dim); text-align:center; margin-top:4px; font-weight:600; }

    .tier-badge{
        display:inline-flex; align-items:center; gap:6px;
        padding:4px 11px; border-radius:999px; font-size:0.78rem; font-weight:700;
        border:1px solid var(--border-soft);
    }

    .badge-hot{ color:var(--hot); background:rgba(255,77,109,0.10); border-color:rgba(255,77,109,0.25); }
    .badge-warm{ color:var(--warm); background:rgba(255,179,71,0.10); border-color:rgba(255,179,71,0.25); }
    .badge-cold{ color:var(--cold); background:rgba(77,216,255,0.10); border-color:rgba(77,216,255,0.25); }

    .timestamp-chip{
        font-size:0.72rem; color:var(--text-dim);
        margin-top:6px; display:block; text-align:right;
    }

    .empty-state{
        text-align:center; padding:50px 20px;
        border:1px dashed var(--border-soft); border-radius:16px;
        color:var(--text-dim);
    }
    .empty-state .icon{ font-size:2.4rem; margin-bottom:8px; }

    .source-code-box{
        font-family:'JetBrains Mono', monospace;
    }
    </style>
    """, unsafe_allow_html=True)


def hero_header(ollama_ok):
    status_dot = "dot-on" if ollama_ok else "dot-off"
    status_text = "Ollama Online" if ollama_ok else "Ollama Offline"
    st.markdown(f"""
    <div class="hero">
        <div>
            <div class="hero-title">⚡ ScopeOS</div>
            <div class="hero-sub">Automation-first B2B Lead Operating System</div>
        </div>
        <div class="hero-status"><span class="dot {status_dot}"></span>{status_text}</div>
    </div>
    """, unsafe_allow_html=True)


def stat_card(title, value, subtitle="", color="#eaf0f7", icon="◆"):
    st.markdown(f"""
    <div class="stat-card">
        <div class="glow" style="background:{color};"></div>
        <div class="stat-label">{icon} {title}</div>
        <div class="stat-value" style="color:{color};">{value}</div>
        <div class="stat-sub">{subtitle}</div>
    </div>
    """, unsafe_allow_html=True)


def section_title(text, pill=None):
    pill_html = f'<span class="section-pill">{pill}</span>' if pill else ""
    st.markdown(f'<div class="section-title">{text}{pill_html}</div>', unsafe_allow_html=True)


def lead_row(row):
    score_val = row["score"]
    tier = row["tier"]
    tier_class = {"Hot": "tier-hot", "Warm": "tier-warm", "Cold": "tier-cold"}.get(tier, "tier-none")
    color_s = TIER_COLOR.get(tier, "#8a96a8")
    badge_class = {"Hot": "badge-hot", "Warm": "badge-warm", "Cold": "badge-cold"}.get(tier, "")

    with st.container():
        st.markdown(f'<div class="lead-card {tier_class}">', unsafe_allow_html=True)
        c1, c2, c3, c4, c5 = st.columns([2.8, 2.0, 2.0, 1.0, 1.3])
        with c1:
            st.markdown(f'<div class="lead-company">{row["company"]}</div>', unsafe_allow_html=True)
            st.markdown(f'<div class="lead-meta">👤 {row["contact"]} &nbsp;·&nbsp; 📡 {row["source"]}</div>', unsafe_allow_html=True)
        with c2:
            if row["email"] and row["email"] != "—":
                st.markdown(f'<a class="lead-link" href="mailto:{row["email"]}">📧 {row["email"]}</a>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="lead-meta">📧 kein E-Mail-Wert</div>', unsafe_allow_html=True)
        with c3:
            if row["website"] and row["website"] != "—":
                url = row["website"] if str(row["website"]).startswith("http") else f"https://{row['website']}"
                st.markdown(f'<a class="lead-link" href="{url}" target="_blank">🌐 {row["website"]}</a>', unsafe_allow_html=True)
            else:
                st.markdown('<div class="lead-meta">🌐 keine Website</div>', unsafe_allow_html=True)
        with c4:
            if score_val is not None:
                st.markdown(f"""
                <div class="score-ring" style="color:{color_s};">{score_val}</div>
                <div class="score-unit">/ 100</div>
                """, unsafe_allow_html=True)
            else:
                st.markdown('<div style="text-align:center;color:var(--text-dim);">—</div>', unsafe_allow_html=True)
        with c5:
            if tier in TIER_EMOJI:
                st.markdown(f'<span class="tier-badge {badge_class}">{TIER_EMOJI[tier]} {tier}</span>', unsafe_allow_html=True)
            else:
                st.markdown('<span class="tier-badge">— unbewertet</span>', unsafe_allow_html=True)
            st.markdown(f'<div class="timestamp-chip">🕒 {freshness_hint(row["created_at"])}</div>', unsafe_allow_html=True)

        st.markdown(f'<div class="lead-next">↳ <b>Next step:</b> {row["next_step"]}</div>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)


def empty_state(icon, text):
    st.markdown(f"""
    <div class="empty-state">
        <div class="icon">{icon}</div>
        <div>{text}</div>
    </div>
    """, unsafe_allow_html=True)


def require_login():
    """Simple password gate using st.secrets['app_password'].
    If no password is configured, the app stays open (e.g. local dev)."""
    app_password = get_secret("app_password")
    if not app_password:
        return  # no password configured -> open access (local/dev mode)

    if st.session_state.get("authed"):
        return

    st.markdown("""
    <div class="hero" style="max-width:480px; margin:8vh auto 0 auto; flex-direction:column; align-items:flex-start; gap:6px;">
        <div class="hero-title">⚡ ScopeOS</div>
        <div class="hero-sub">Bitte anmelden, um fortzufahren</div>
    </div>
    """, unsafe_allow_html=True)

    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        with st.form("login_form"):
            pw = st.text_input("Passwort", type="password")
            submitted = st.form_submit_button("Einloggen", use_container_width=True)
        if submitted:
            if pw == app_password:
                st.session_state["authed"] = True
                st.rerun()
            else:
                st.error("Falsches Passwort.")

    st.stop()


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main():
    init_db()
    init_followup_tables()
    st.set_page_config(page_title="ScopeOS", page_icon="⚡", layout="wide", initial_sidebar_state="expanded")
    inject_css()
    require_login()

    ollama_ok = check_ollama()

    with st.sidebar:
        st.markdown("## ⚡ ScopeOS")
        st.caption("B2B Lead Automation OS")
        st.divider()
        st.markdown(f"""
        <div class="hero-status" style="width:100%;justify-content:flex-start;margin-bottom:14px;">
            <span class="dot {'dot-on' if ollama_ok else 'dot-off'}"></span>
            {'Ollama läuft' if ollama_ok else 'Ollama offline'}
        </div>
        """, unsafe_allow_html=True)

        st.markdown("### 🔌 Integrationen")
        slack_enabled = st.toggle("Slack", value=get_setting("slack_enabled", "true") == "true")
        crm_enabled = st.toggle("CRM", value=get_setting("crm_enabled", "true") == "true")
        email_enabled = st.toggle("E-Mail", value=get_setting("email_enabled", "true") == "true")
        with st.expander("⚙️ Verbindungen konfigurieren"):
            if any(is_locked_by_secret(k) for k in SECRET_KEYS):
                st.caption("🔒 Felder mit Schloss sind über App-Secrets gesperrt und können hier nicht geändert werden.")

            def secret_field(label, key, **kwargs):
                if is_locked_by_secret(key):
                    st.text_input(f"🔒 {label}", value="•••••••• (via Secrets)", disabled=True)
                    return None
                return st.text_input(label, value=get_setting(key, kwargs.get("default", "")), type=kwargs.get("type", "default"))

            slack_url = secret_field("Slack Webhook URL", "slack_url", type="password")
            crm_url = secret_field("CRM Endpoint", "crm_url", type="password")
            smtp_host = secret_field("SMTP Host", "smtp_host")
            smtp_port = secret_field("SMTP Port", "smtp_port", default="587")
            smtp_user = secret_field("SMTP User", "smtp_user")
            smtp_pass = secret_field("SMTP Passwort", "smtp_pass", type="password")
            notify_email = secret_field("Notify E-Mail", "notify_email")

            if st.button("💾 Einstellungen speichern", use_container_width=True):
                if slack_url is not None:
                    upsert_setting("slack_url", slack_url)
                if crm_url is not None:
                    upsert_setting("crm_url", crm_url)
                if smtp_host is not None:
                    upsert_setting("smtp_host", smtp_host)
                if smtp_port is not None:
                    upsert_setting("smtp_port", smtp_port)
                if smtp_user is not None:
                    upsert_setting("smtp_user", smtp_user)
                if smtp_pass is not None:
                    upsert_setting("smtp_pass", smtp_pass)
                if notify_email is not None:
                    upsert_setting("notify_email", notify_email)
                upsert_setting("slack_enabled", str(slack_enabled).lower())
                upsert_setting("crm_enabled", str(crm_enabled).lower())
                upsert_setting("email_enabled", str(email_enabled).lower())
                st.success("Gespeichert ✓")

    leads_count, analyses_count, activities_count, tier_counts, today_count, week_count = get_stats()

    hero_header(ollama_ok)

    cols = st.columns(7)
    stats = [
        ("Leads gesamt", leads_count, "Gespeicherte Leads", "#eaf0f7", "📋"),
        ("Hot", tier_counts.get("Hot", 0), "Priorisierte Leads", TIER_COLOR["Hot"], "🔥"),
        ("Warm", tier_counts.get("Warm", 0), "Mittlere Priorität", TIER_COLOR["Warm"], "🌤️"),
        ("Cold", tier_counts.get("Cold", 0), "Nurture-Kandidaten", TIER_COLOR["Cold"], "❄️"),
        ("Heute", today_count, "Neue Leads heute", "#a78bfa", "✨"),
        ("7 Tage", week_count, "Neue Leads diese Woche", "#a78bfa", "📈"),
        ("Aktionen", activities_count, "Automations-Events", "#7dd3fc", "⚙️"),
    ]
    for c, s in zip(cols, stats):
        with c:
            stat_card(*s)

    st.markdown("<div style='height:1.2rem'></div>", unsafe_allow_html=True)
    tab_pipeline, tab_leads_add, tab_analyse, tab_followup, tab_quellen, tab_log = st.tabs([
        "🚀 Pipeline", "➕ Leads hinzufügen", "🧠 Analyse & Automationen", "🔁 Follow-up", "🔗 Quellen & Webhooks", "📜 Aktivitäten-Log"
    ])

    with tab_pipeline:
        df_all = get_lead_table()
        if df_all.empty:
            empty_state("🌌", "Noch keine Leads vorhanden — füge deinen ersten Lead unter <b>Leads hinzufügen</b> hinzu.")
        else:
            f1, f2, f3, f4 = st.columns([2, 2, 2, 2])
            with f1:
                tier_filter = st.multiselect("Tier", ["Hot", "Warm", "Cold"], default=["Hot", "Warm", "Cold"])
            with f2:
                source_opts = ["Alle"] + sorted(df_all["source"].dropna().astype(str).unique().tolist())
                source_filter = st.selectbox("Quelle", source_opts, index=0)
            with f3:
                search_term = st.text_input("Suche", placeholder="🔍 Firma, Kontakt oder E-Mail...")
            with f4:
                sort_by = st.selectbox("Sortieren nach", ["most recent", "score", "tier", "company"], index=0)
            t1, _ = st.columns([2, 6])
            with t1:
                time_filter = st.selectbox("Zeitraum", ["Alle Zeit", "Heute", "Letzte 7 Tage", "Letzte 30 Tage"], index=0)

            df_view = df_all.copy()
            df_view["created_at"] = pd.to_datetime(df_view["created_at"], errors="coerce")
            if tier_filter:
                df_view = df_view[df_view["tier"].isin(tier_filter)]
            if source_filter != "Alle":
                df_view = df_view[df_view["source"] == source_filter]
            if search_term:
                t = search_term.lower()
                df_view = df_view[df_view.apply(lambda r: t in str(r["company"]).lower() or t in str(r["contact"]).lower() or t in str(r["email"]).lower(), axis=1)]
            if time_filter == "Heute":
                df_view = df_view[df_view["created_at"] >= datetime.now() - timedelta(days=1)]
            elif time_filter == "Letzte 7 Tage":
                df_view = df_view[df_view["created_at"] >= datetime.now() - timedelta(days=7)]
            elif time_filter == "Letzte 30 Tage":
                df_view = df_view[df_view["created_at"] >= datetime.now() - timedelta(days=30)]
            if sort_by == "most recent":
                df_view = df_view.sort_values("created_at", ascending=False, na_position="last")
            elif sort_by == "score":
                df_view = df_view.sort_values("score", ascending=False, na_position="last")
            elif sort_by == "company":
                df_view = df_view.sort_values("company", ascending=True, na_position="last")
            elif sort_by == "tier":
                df_view["_tier_ord"] = df_view["tier"].map({"Hot": 1, "Warm": 2, "Cold": 3}).fillna(99)
                df_view = df_view.sort_values(["_tier_ord", "created_at"], ascending=[True, False]).drop(columns=["_tier_ord"])

            st.markdown(f"<div style='color:var(--text-dim); font-size:0.85rem; margin:10px 0;'>{len(df_view)} Leads gefunden</div>", unsafe_allow_html=True)

            if df_view.empty:
                empty_state("🔍", "Keine Leads passen zu deinen Filtern.")
            else:
                for tier in ["Hot", "Warm", "Cold"]:
                    tier_df = df_view[df_view["tier"] == tier]
                    if tier_df.empty:
                        continue
                    section_title(f"{TIER_EMOJI[tier]} {tier} Leads", pill=f"{len(tier_df)}")
                    for _, row in tier_df.iterrows():
                        lead_row(row)

                other_df = df_view[~df_view["tier"].isin(["Hot", "Warm", "Cold"])]
                if not other_df.empty:
                    section_title("⚪ Unbewertet", pill=f"{len(other_df)}")
                    for _, row in other_df.iterrows():
                        lead_row(row)

            st.download_button("⬇️ Pipeline als CSV exportieren", df_view.to_csv(index=False).encode("utf-8"), file_name=f"scopeos_pipeline_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", mime="text/csv")

    with tab_leads_add:
        st.markdown("### ➕ Leads hinzufügen")
        sub1, sub2, sub3, sub4 = st.tabs(["✍️ Einzellead", "📋 Bulk Paste", "📁 CSV Import", "🪝 Webhook Simulator"])
        with sub1:
            with st.form("single_lead"):
                lead_text = st.text_area("Leadbeschreibung / Notiz")
                c1, c2 = st.columns(2)
                with c1:
                    company_name = st.text_input("Firmenname")
                    email = st.text_input("E-Mail")
                with c2:
                    contact_name = st.text_input("Kontaktname / Titel")
                    website = st.text_input("Website")
                source = st.selectbox("Quelle", ["manual", "inbound", "outbound", "event", "referral", "webhook"])
                auto_analyze = st.checkbox("Direkt analysieren", value=True)
                submitted = st.form_submit_button("Lead speichern", use_container_width=True)
            if submitted:
                if not lead_text.strip() and not company_name.strip():
                    st.warning("Bitte mindestens Leadtext oder Firmenname eingeben.")
                else:
                    lead_id, dup = upsert_lead({"lead_text": lead_text, "company_name": company_name, "contact_name": contact_name, "email": email, "website": website, "source": source, "raw_payload": "{}"})
                    st.success(f"{'Aktualisiert' if dup else 'Gespeichert'}: Lead #{lead_id}")
                    if auto_analyze:
                        with get_connection() as conn:
                            cur = conn.cursor()
                            cur.execute("SELECT * FROM leads WHERE id=?", (lead_id,))
                            lead = cur.fetchone()
                        with st.spinner("KI-Analyse läuft..."):
                            res = analyze_lead(lead, force=True, run_automations=True)
                        if res:
                            st.info(f"{TIER_EMOJI.get(res[1], '')} Score: {res[0]}/100 — {res[1]}")
                    st.rerun()

        with sub2:
            st.markdown("### Bulk Paste")
            bulk_text = st.text_area("Leads einfügen", height=220)
            if st.button("👁️ Vorschau", use_container_width=True) and bulk_text.strip():
                st.session_state["bulk_df"] = parse_bulk_text(bulk_text)
            bulk_df = st.session_state.get("bulk_df", pd.DataFrame())
            if not bulk_df.empty:
                edited = st.data_editor(bulk_df, num_rows="dynamic", use_container_width=True, key="bulk_ed")
                c1, c2 = st.columns(2)
                with c1:
                    if st.button("💾 Nur speichern", use_container_width=True):
                        df = edited.fillna("")
                        df = df[df.apply(lambda r: any(str(v).strip() for v in r), axis=1)]
                        res = bulk_upsert(df, source="bulk_paste")
                        st.success(f"{sum(1 for _, d in res if not d)} neue Leads")
                        st.session_state.pop("bulk_df", None)
                        st.rerun()
                with c2:
                    if st.button("⚡ Speichern & Analysieren", use_container_width=True):
                        df = edited.fillna("")
                        df = df[df.apply(lambda r: any(str(v).strip() for v in r), axis=1)]
                        res = bulk_upsert(df, source="bulk_paste")
                        with st.spinner("Analyse..."):
                            ares = analyze_all(skip_existing=True, run_automations=True)
                        st.success(f"{sum(1 for _, d in res if not d)} gespeichert | {len(ares)} analysiert")
                        st.session_state.pop("bulk_df", None)
                        st.rerun()

        with sub3:
            st.markdown("### CSV Import")
            uploaded = st.file_uploader("CSV hochladen", type=["csv"])
            if uploaded:
                try:
                    df = pd.read_csv(uploaded)
                    for col in ["lead_text", "company_name", "contact_name", "email", "website"]:
                        if col not in df.columns:
                            df[col] = ""
                    df = df[["lead_text", "company_name", "contact_name", "email", "website"]].fillna("")
                    edited_csv = st.data_editor(df, num_rows="dynamic", use_container_width=True)
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("📥 Importieren", use_container_width=True):
                            res = bulk_upsert(edited_csv.fillna(""), source="csv")
                            st.success(f"{sum(1 for _, d in res if not d)} neu")
                            st.rerun()
                    with c2:
                        if st.button("⚡ Importieren & Analysieren", use_container_width=True):
                            bulk_upsert(edited_csv.fillna(""), source="csv")
                            with st.spinner("Läuft..."):
                                ares = analyze_all(skip_existing=True, run_automations=True)
                            st.success(f"{len(ares)} analysiert")
                            st.rerun()
                except Exception as e:
                    st.error(f"Fehler: {e}")

        with sub4:
            default_payload = json.dumps({"lead_text": "Demo angefragt, Budget vorhanden", "company_name": "ACME GmbH", "contact_name": "Max Müller CEO", "email": "max@acme.de", "website": "acme.de", "source": "webhook"}, indent=2, ensure_ascii=False)
            payload_text = st.text_area("JSON Payload", value=default_payload, height=220)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("🪝 Simulieren", use_container_width=True):
                    lead_id, dup = simulate_webhook(payload_text)
                    if lead_id:
                        st.success(f"Lead #{lead_id} {'(Duplikat)' if dup else 'gespeichert'}")
                    else:
                        st.error("Ungültiges JSON")
                    st.rerun()
            with c2:
                if st.button("⚡ Simulieren & Analysieren", use_container_width=True):
                    lead_id, dup = simulate_webhook(payload_text)
                    if lead_id:
                        with get_connection() as conn:
                            cur = conn.cursor()
                            cur.execute("SELECT * FROM leads WHERE id=?", (lead_id,))
                            lead = cur.fetchone()
                        with st.spinner("Analyse..."):
                            res = analyze_lead(lead, force=True, run_automations=True)
                        if res:
                            st.info(f"Score {res[0]}/100 — {TIER_EMOJI.get(res[1], '')} {res[1]}")
                        st.rerun()

    with tab_analyse:
        st.markdown("### 🧠 Analyse & Automationen")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("✨ Neue analysieren", use_container_width=True):
                with st.spinner("Analysiere..."):
                    res = analyze_all(skip_existing=True, run_automations=True)
                st.success(f"{len(res)} analysiert")
                st.rerun()
        with c2:
            if st.button("🔄 Alle neu berechnen", use_container_width=True):
                with get_connection() as conn:
                    conn.execute("DELETE FROM analyses")
                    conn.commit()
                with st.spinner("Rebuild..."):
                    res = analyze_all(skip_existing=False, run_automations=True)
                st.success(f"{len(res)} neu berechnet")
                st.rerun()
        with c3:
            if st.button("🗑️ Aktivitäten löschen", use_container_width=True):
                with get_connection() as conn:
                    conn.execute("DELETE FROM activities")
                    conn.commit()
                st.success("Gelöscht")
                st.rerun()

    with tab_followup:
        st.markdown("### 🔁 Follow-up Automation")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("▶️ Due Sequenzen ausführen", use_container_width=True):
                with st.spinner("Follow-ups werden geprüft..."):
                    results = run_due_sequences()
                st.success(f"{len(results)} Sequenzen verarbeitet")
        with c2:
            df_leads = get_lead_table()
            if not df_leads.empty:
                lead_options = {f"#{row.id} | {row.company} | {row.contact} | {row.email}": row.id for _, row in df_leads.iterrows() if str(row.email) != "—"}
                if lead_options:
                    selected_label = st.selectbox("Lead auswählen", list(lead_options.keys()))
                    if st.button("🚀 Follow-up für Lead starten", use_container_width=True):
                        seq_id = create_followup_from_lead(lead_options[selected_label], delay_days=0)
                        st.success(f"Sequenz #{seq_id} gestartet")
        with c3:
            if st.button("🔄 Refresh", use_container_width=True):
                st.rerun()

        active = get_active_sequences()
        if active:
            section_title("Aktive Sequenzen", pill=f"{len(active)}")
            for seq in active:
                with st.expander(f"Lead #{seq['lead_id']} | Stage {seq['stage']} | {seq['status']}"):
                    st.write({"Sequence ID": seq["id"], "Lead ID": seq["lead_id"], "Stage": seq["stage"], "Status": seq["status"], "Next Run": seq["next_run_at"], "Last Sent": seq["last_sent_at"], "Channel": seq["channel"]})
        else:
            empty_state("💤", "Keine aktiven Follow-up-Sequenzen.")

    with tab_quellen:
        st.markdown("### 🔗 Quellen & Webhooks")
        df_src = get_lead_table()
        if not df_src.empty:
            source_stats = df_src.groupby("source").agg(Anzahl=("id", "count"), Hot=("tier", lambda x: (x == "Hot").sum()), Warm=("tier", lambda x: (x == "Warm").sum()), Cold=("tier", lambda x: (x == "Cold").sum())).reset_index().rename(columns={"source": "Quelle"})
            st.dataframe(source_stats, use_container_width=True)
        st.markdown('<div class="source-code-box">', unsafe_allow_html=True)
        st.code("POST /webhook/tally\nPOST /webhook/generic\nPOST /webhook/lead", language=None)
        st.markdown('</div>', unsafe_allow_html=True)

    with tab_log:
        st.markdown("### 📜 Aktivitäten-Log")
        c1, c2 = st.columns([3, 1])
        with c1:
            log_filter = st.multiselect("Typ filtern", ["slack", "crm", "email", "workflow", "gmail_scan", "webhook_ingest", "generic_ingest"], default=[])
        with c2:
            log_limit = st.selectbox("Max Einträge", [50, 100, 250, 500], index=0)
        with get_connection() as conn:
            cur = conn.cursor()
            if log_filter:
                placeholders = ",".join(["?"] * len(log_filter))
                cur.execute(f"SELECT * FROM activities WHERE activity_type IN ({placeholders}) ORDER BY id DESC LIMIT ?", (*log_filter, log_limit))
            else:
                cur.execute("SELECT * FROM activities ORDER BY id DESC LIMIT ?", (log_limit,))
            acts = cur.fetchall()
        if acts:
            for act in acts:
                with st.expander(f"{act['activity_type']} — Lead #{act['lead_id']} — {act['created_at']}"):
                    try:
                        st.json(json.loads(act["payload"]))
                    except Exception:
                        st.write(act["payload"])
        else:
            empty_state("📭", "Noch keine Aktivitäten geloggt.")

if __name__ == "__main__":
    main()