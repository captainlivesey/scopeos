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
TIER_COLOR = {"Hot": "#ff5d5d", "Warm": "#ffb347", "Cold": "#5b8def"}
SOURCE_ICON = {
    "gmail": "📩", "webhook": "🔗", "tally": "📋", "typeform": "📝",
    "calendly": "📅", "bulk_paste": "📂", "csv": "📂", "manual": "✍️",
    "inbound": "📥", "outbound": "📣", "event": "🎤", "referral": "🤝"
}

FIT_RULES = {
    "title": {
        "ceo": 15, "founder": 15, "coo": 12, "cmo": 12,
        "sales": 10, "marketing": 8, "head": 8, "manager": 5,
        "assistant": 0, "intern": -10
    },
    "company_size": [
        (1, 10, -10), (11, 49, 5), (50, 199, 15),
        (200, 999, 10), (1000, 1000000, 0)
    ],
    "intent_keywords": {
        "demo": 20, "pricing": 15, "budget": 15, "urgent": 15,
        "next week": 15, "follow up": 10, "contact": 8, "schedule": 12,
        "compare": 10, "trial": 18, "proposal": 12, "decision": 12,
        "implement": 10, "book": 12, "call": 8, "quote": 12,
        "buy": 15, "need now": 18
    },
    "negative_keywords": {
        "just curious": -10, "not now": -15, "maybe later": -10,
        "no budget": -20, "student": -20, "spam": -30,
        "research": -5, "vendor list": -10
    }
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
    "Hot": "🔥 *HOT LEAD* — {company_name} ({contact_name}) | Score: {score}/100 | {email} | Sofort anrufen!",
    "Warm": "🌤️ *WARM LEAD* — {company_name} ({contact_name}) | Score: {score}/100 | {email} | Follow-up.",
    "Cold": "❄️ *Cold Lead* — {company_name} | Score: {score}/100 | Nurture."
}

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
        cur.execute("""CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_text TEXT, company_name TEXT, contact_name TEXT,
            email TEXT, website TEXT, source TEXT, raw_payload TEXT,
            unique_key TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER, score INTEGER, tier TEXT,
            fit_score INTEGER, intent_score INTEGER,
            reason TEXT, next_step TEXT, analysis TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER, activity_type TEXT,
            payload TEXT, status TEXT DEFAULT 'queued',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT
        )""")
        for col in ["source", "raw_payload", "updated_at", "unique_key"]:
            ensure_column(cur, "leads", col, "TEXT")
        conn.commit()

def upsert_setting(key, value):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        conn.commit()

def get_setting(key, default=""):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default

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
            cur.execute(
                """UPDATE leads SET lead_text=?,company_name=?,contact_name=?,email=?,website=?,source=?,raw_payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (d["lead_text"], d["company_name"], d["contact_name"], d["email"], d["website"], d["source"], d["raw_payload"], row["id"])
            )
            conn.commit()
            return row["id"], True
        cur.execute(
            """INSERT INTO leads(lead_text,company_name,contact_name,email,website,source,raw_payload,unique_key) VALUES(?,?,?,?,?,?,?,?)""",
            (d["lead_text"], d["company_name"], d["contact_name"], d["email"], d["website"], d["source"], d["raw_payload"], d["unique_key"])
        )
        conn.commit()
        return cur.lastrowid, False

def bulk_upsert(df, source="bulk"):
    results = []
    for _, row in df.iterrows():
        lead_id, dup = upsert_lead({
            "lead_text": row.get("lead_text", ""),
            "company_name": row.get("company_name", ""),
            "contact_name": row.get("contact_name", ""),
            "email": row.get("email", ""),
            "website": row.get("website", ""),
            "source": source,
            "raw_payload": json.dumps(row.to_dict(), ensure_ascii=False)
        })
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
        conn.execute(
            """INSERT INTO analyses(lead_id,score,tier,fit_score,intent_score,reason,next_step,analysis) VALUES(?,?,?,?,?,?,?,?)""",
            (lead_id, score, tier, fit_score, intent_score, reason, next_step, analysis)
        )
        conn.commit()

def log_activity(lead_id, activity_type, payload, status="done"):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO activities(lead_id,activity_type,payload,status) VALUES(?,?,?,?)",
            (lead_id, activity_type, json.dumps(payload, ensure_ascii=False), status)
        )
        conn.commit()

def get_lead_table():
    leads = get_leads()
    rows = []
    for lead in leads:
        latest_rows = get_analyses_for_lead(lead["id"])
        latest = latest_rows[0] if latest_rows else None
        rows.append({
            "id": lead["id"],
            "tier": latest["tier"] if latest else "—",
            "score": latest["score"] if latest else None,
            "company": lead["company_name"] or "—",
            "contact": lead["contact_name"] or "—",
            "email": lead["email"] or "—",
            "website": lead["website"] or "—",
            "source": lead["source"] or "—",
            "next_step": latest["next_step"] if latest else "—",
            "lead_text": lead["lead_text"] or "—",
            "reason": latest["reason"] if latest else "—",
            "analysis": latest["analysis"] if latest else "—",
            "created_at": lead["created_at"],
            "updated_at": lead["updated_at"],
        })
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
        buckets = [(1,10,"startup"),(11,49,"small"),(50,199,"midmarket"),(200,999999,"enterprise")]
        for a, b, label in buckets:
            if lo >= a and hi <= b:
                employee_band = label
                break
    keywords = ["saas","agency","consulting","software","marketplace","ai","fintech","health","ecommerce"]
    industry = next((kw for kw in keywords if kw in text), None)
    domain = (lead["website"] or "").replace("https://", "").replace("http://", "").split("/")[0].lower()
    return {
        "domain": domain or "—",
        "industry": industry or "unbekannt",
        "employee_band": employee_band or "unbekannt",
        "enriched_at": datetime.utcnow().isoformat(timespec="seconds")
    }

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
    prompt = (
        f"Du bist ein Sales Ops Assistent. Erkläre kurz warum dieser Lead den Score {score}/100 hat. "
        f"Tier={tier}, Fit={fit_score}, Intent={intent_score}. Nächster Schritt: {next_step}. "
        f"Lead: Firma={lead['company_name']}, Kontakt={lead['contact_name']}, Text={lead['lead_text']}"
    )
    r = requests.post(
        OLLAMA_URL,
        json={"model": MODEL, "prompt": prompt, "stream": False, "options": {"temperature": TEMPERATURE, "seed": SEED}},
        timeout=120
    )
    r.raise_for_status()
    return r.json().get("response", "")

def send_slack(lead, score, tier):
    url = get_setting("slack_url")
    if not url:
        return False, "Keine Slack URL"
    msg = SLACK_TEMPLATES.get(tier, SLACK_TEMPLATES["Cold"]).format(
        company_name=lead["company_name"] or "—",
        contact_name=lead["contact_name"] or "—",
        email=lead["email"] or "—",
        score=score
    )
    try:
        r = requests.post(url, json={"text": msg}, timeout=10)
        r.raise_for_status()
        return True, "OK"
    except Exception as e:
        return False, str(e)

def send_to_crm(lead, score, tier, enrichment):
    url = get_setting("crm_url")
    if not url:
        return False, "Keine CRM URL"
    payload = {
        "lead_id": lead["id"],
        "company_name": lead["company_name"],
        "contact_name": lead["contact_name"],
        "email": lead["email"],
        "website": lead["website"],
        "score": score,
        "tier": tier,
        "enrichment": enrichment
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True, "OK"
    except Exception as e:
        return False, str(e)

def send_email_notification(lead, score, tier):
    smtp_host = get_setting("smtp_host")
    smtp_port = get_setting("smtp_port", "587")
    smtp_user = get_setting("smtp_user")
    smtp_pass = get_setting("smtp_pass")
    notify_to = get_setting("notify_email")
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
    results = {
        "tier": tier,
        "score": score,
        "workflow": wf,
        "enrichment": enrichment,
        "slack": "skipped",
        "crm": "skipped",
        "email": "skipped"
    }
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
        rows.append({
            "lead_text": parts[0], "company_name": parts[1],
            "contact_name": parts[2], "email": parts[3], "website": parts[4]
        })
    return pd.DataFrame(rows)

def simulate_webhook(payload_text):
    try:
        data = json.loads(payload_text)
        if isinstance(data, dict):
            return upsert_lead({
                "lead_text": data.get("lead_text", ""),
                "company_name": data.get("company_name", ""),
                "contact_name": data.get("contact_name", ""),
                "email": data.get("email", ""),
                "website": data.get("website", ""),
                "source": data.get("source", "webhook"),
                "raw_payload": payload_text
            })
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
    hours = mins // 60
    return f"vor {hours}h"

def card(title, value, subtitle="", color="#ffffff"):
    st.markdown(
        f"""
        <div style="
            background: linear-gradient(180deg, rgba(26,26,26,0.98), rgba(18,18,18,0.98));
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 18px;
            padding: 18px 18px 14px 18px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.18);
        ">
            <div style="font-size: 0.82rem; color: #9aa4b2; margin-bottom: 6px;">{title}</div>
            <div style="font-size: 1.85rem; font-weight: 800; color: {color}; line-height: 1.1;">{value}</div>
            <div style="font-size: 0.78rem; color: #7b8794; margin-top: 8px;">{subtitle}</div>
        </div>
        """,
        unsafe_allow_html=True
    )

def lead_row(row):
    score_val = row["score"]
    color_s = "#ff5d5d" if score_val is not None and score_val >= 70 else "#ffb347" if score_val is not None and score_val >= 40 else "#5b8def"
    with st.container():
        st.markdown(
            """
            <div style="
                background: rgba(255,255,255,0.03);
                border: 1px solid rgba(255,255,255,0.07);
                border-radius: 16px;
                padding: 16px 16px 10px 16px;
                margin-bottom: 12px;
            ">
            """,
            unsafe_allow_html=True
        )
        c1, c2, c3, c4, c5 = st.columns([2.7, 2.0, 2.0, 1.2, 1.3])
        with c1:
            st.markdown(f"**{row['company']}**")
            st.caption(f"👤 {row['contact']} · {row['source']}")
        with c2:
            if row["email"] and row["email"] != "—":
                st.markdown(f"📧 [{row['email']}](mailto:{row['email']})")
            else:
                st.caption("📧 kein E-Mail-Wert")
        with c3:
            if row["website"] and row["website"] != "—":
                url = row["website"] if str(row["website"]).startswith("http") else f"https://{row['website']}"
                st.markdown(f"🌐 [{row['website']}]({url})")
            else:
                st.caption("🌐 keine Website")
        with c4:
            st.markdown(
                f"<div style='text-align:center'><div style='font-size:1.4rem;font-weight:800;color:{color_s}'>{score_val if score_val is not None else '—'}</div><div style='font-size:0.72rem;color:#8d98a6;'>/100</div></div>",
                unsafe_allow_html=True
            )
        with c5:
            st.caption(f"{TIER_EMOJI.get(row['tier'], '')} {row['tier']}")
            st.caption(f"🕒 {freshness_hint(row['created_at'])}")
        st.caption(f"↳ {row['next_step']}")
        st.markdown("</div>", unsafe_allow_html=True)

def main():
    init_db()
    init_followup_tables()

    st.set_page_config(
        page_title="ScopeOS",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    st.markdown(
        """
        <style>
        html, body, [class*="css"]  {
            font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        .stApp {
            background:
                radial-gradient(circle at top left, rgba(91,141,239,0.12), transparent 25%),
                radial-gradient(circle at top right, rgba(255,93,93,0.08), transparent 22%),
                linear-gradient(180deg, #0b0f14 0%, #0f141b 100%);
            color: #e5ecf3;
        }
        section[data-testid="stSidebar"] {
            background: linear-gradient(180deg, #0c1117, #0b0f14);
            border-right: 1px solid rgba(255,255,255,0.07);
        }
        .block-container {
            padding-top: 1.2rem;
            padding-bottom: 2rem;
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: 8px;
        }
        .stTabs [data-baseweb="tab"] {
            background: rgba(255,255,255,0.03);
            border-radius: 12px 12px 0 0;
            padding: 10px 14px;
        }
        .stTabs [aria-selected="true"] {
            background: rgba(255,255,255,0.08) !important;
        }
        div[data-testid="metric-container"] {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            padding: 14px 12px;
            border-radius: 16px;
        }
        .lead-hot { border-left: 4px solid #ff5d5d; }
        .lead-warm { border-left: 4px solid #ffb347; }
        .lead-cold { border-left: 4px solid #5b8def; }
        </style>
        """,
        unsafe_allow_html=True
    )

    with st.sidebar:
        st.markdown("## 🎯 ScopeOS")
        st.caption("B2B Lead Automation OS")
        st.divider()

        if check_ollama():
            st.success("🟢 Ollama läuft")
        else:
            st.error("🔴 Ollama offline")

        st.markdown("### 🔌 Integrationen")
        slack_enabled = st.toggle("Slack", value=get_setting("slack_enabled", "true") == "true")
        crm_enabled = st.toggle("CRM", value=get_setting("crm_enabled", "true") == "true")
        email_enabled = st.toggle("E-Mail", value=get_setting("email_enabled", "true") == "true")

        with st.expander("⚙️ Verbindungen konfigurieren"):
            slack_url = st.text_input("Slack Webhook URL", value=get_setting("slack_url", ""), type="password")
            crm_url = st.text_input("CRM Endpoint", value=get_setting("crm_url", ""), type="password")
            smtp_host = st.text_input("SMTP Host", value=get_setting("smtp_host", ""))
            smtp_port = st.text_input("SMTP Port", value=get_setting("smtp_port", "587"))
            smtp_user = st.text_input("SMTP User", value=get_setting("smtp_user", ""))
            smtp_pass = st.text_input("SMTP Passwort", value=get_setting("smtp_pass", ""), type="password")
            notify_email = st.text_input("Notify E-Mail", value=get_setting("notify_email", ""))

            if st.button("💾 Einstellungen speichern", use_container_width=True):
                upsert_setting("slack_url", slack_url)
                upsert_setting("crm_url", crm_url)
                upsert_setting("smtp_host", smtp_host)
                upsert_setting("smtp_port", smtp_port)
                upsert_setting("smtp_user", smtp_user)
                upsert_setting("smtp_pass", smtp_pass)
                upsert_setting("notify_email", notify_email)
                upsert_setting("slack_enabled", str(slack_enabled).lower())
                upsert_setting("crm_enabled", str(crm_enabled).lower())
                upsert_setting("email_enabled", str(email_enabled).lower())
                st.success("✅ Gespeichert")

        st.divider()
        st.markdown("### 🌐 Webhooks")
        st.caption("Diese Endpoints kannst du an Tally, Typeform, Calendly oder Make anbinden.")
        st.code("POST /webhook/tally", language=None)
        st.code("POST /webhook/generic", language=None)
        st.code("POST /webhook/lead", language=None)

    leads_count, analyses_count, activities_count, tier_counts, today_count, week_count = get_stats()

    st.markdown("# 🎯 ScopeOS")
    st.caption("Automation-first B2B Lead OS — sauber, schnell, strukturiert")
    st.divider()

    m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
    with m1:
        card("Leads gesamt", leads_count, "Gespeicherte Leads", "#f3f7ff")
    with m2:
        card("Hot", tier_counts.get("Hot", 0), "Priorisierte Leads", TIER_COLOR["Hot"])
    with m3:
        card("Warm", tier_counts.get("Warm", 0), "Mittlere Priorität", TIER_COLOR["Warm"])
    with m4:
        card("Cold", tier_counts.get("Cold", 0), "Nurture-Kandidaten", TIER_COLOR["Cold"])
    with m5:
        card("Heute", today_count, "Neue Leads heute", "#cbe7ff")
    with m6:
        card("7 Tage", week_count, "Neue Leads diese Woche", "#cbe7ff")
    with m7:
        card("Aktionen", activities_count, "Automations-Events", "#ffffff")

    st.divider()

    tab_pipeline, tab_leads_add, tab_analyse, tab_followup, tab_quellen, tab_log = st.tabs([
        "📊 Pipeline",
        "➕ Leads hinzufügen",
        "⚡ Analyse & Automationen",
        "🔁 Follow-up",
        "🔗 Quellen & Webhooks",
        "📋 Aktivitäten-Log"
    ])

    with tab_pipeline:
        df_all = get_lead_table()
        if df_all.empty:
            st.info("Noch keine Leads vorhanden.")
        else:
            f1, f2, f3, f4 = st.columns([2,2,2,2])
            with f1:
                tier_filter = st.multiselect("Tier", ["Hot", "Warm", "Cold"], default=["Hot", "Warm", "Cold"])
            with f2:
                source_opts = ["Alle"] + sorted(df_all["source"].dropna().astype(str).unique().tolist())
                source_filter = st.selectbox("Quelle", source_opts, index=0)
            with f3:
                search_term = st.text_input("🔎 Suche", placeholder="Firma, Kontakt oder E-Mail...")
            with f4:
                sort_by = st.selectbox("Sortieren nach", ["most recent", "score", "tier", "company"], index=0)

            t1, t2 = st.columns([2, 6])
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
                df_view = df_view[df_view.apply(
                    lambda r: t in str(r["company"]).lower() or t in str(r["contact"]).lower() or t in str(r["email"]).lower(),
                    axis=1
                )]

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

            st.caption(f"{len(df_view)} Leads gefunden")
            st.divider()

            for tier in ["Hot", "Warm", "Cold"]:
                tier_df = df_view[df_view["tier"] == tier]
                if tier_df.empty:
                    continue
                st.markdown(f"### {TIER_EMOJI[tier]} {tier} Leads ({len(tier_df)})")
                for _, row in tier_df.iterrows():
                    lead_row(row)

            st.markdown("### ⬇️ Export")
            export_df = df_view[["id", "tier", "score", "company", "contact", "email", "website", "source", "next_step", "created_at", "updated_at"]].copy()
            export_df.columns = ["ID", "Tier", "Score", "Firma", "Kontakt", "E-Mail", "Website", "Quelle", "Nächster Schritt", "Erstellt", "Aktualisiert"]
            st.dataframe(export_df, use_container_width=True)
            st.download_button(
                "⬇️ Pipeline als CSV exportieren",
                df_view.to_csv(index=False).encode("utf-8"),
                file_name=f"scopeos_pipeline_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv"
            )

    with tab_leads_add:
        st.markdown("### Leads hinzufügen")
        sub1, sub2, sub3, sub4 = st.tabs(["✍️ Einzellead", "📋 Bulk Paste", "📂 CSV Import", "📡 Webhook Simulator"])

        with sub1:
            with st.form("single_lead"):
                st.markdown("#### Neuen Lead manuell erfassen")
                lead_text = st.text_area("Leadbeschreibung / Notiz", placeholder='z.B. "Hat Demo angefragt, Budget 50k, Entscheidung nächste Woche"')
                c1, c2 = st.columns(2)
                with c1:
                    company_name = st.text_input("Firmenname")
                    email = st.text_input("E-Mail")
                with c2:
                    contact_name = st.text_input("Kontaktname / Titel")
                    website = st.text_input("Website")
                source = st.selectbox("Quelle", ["manual", "inbound", "outbound", "event", "referral", "webhook"])
                auto_analyze = st.checkbox("Direkt analysieren", value=True)
                submitted = st.form_submit_button("💾 Lead speichern", use_container_width=True)

            if submitted:
                if not lead_text.strip() and not company_name.strip():
                    st.warning("Bitte mindestens Leadtext oder Firmenname eingeben.")
                else:
                    lead_id, dup = upsert_lead({
                        "lead_text": lead_text,
                        "company_name": company_name,
                        "contact_name": contact_name,
                        "email": email,
                        "website": website,
                        "source": source,
                        "raw_payload": "{}"
                    })
                    st.success(f"{'Aktualisiert' if dup else '✅ Gespeichert'}: Lead #{lead_id}")
                    if auto_analyze:
                        with get_connection() as conn:
                            cur = conn.cursor()
                            cur.execute("SELECT * FROM leads WHERE id=?", (lead_id,))
                            lead = cur.fetchone()
                        with st.spinner("KI-Analyse läuft..."):
                            res = analyze_lead(lead, force=True, run_automations=True)
                        if res:
                            score, tier = res
                            st.info(f"{TIER_EMOJI.get(tier, '')} Score: {score}/100 — **{tier}**")
                    st.rerun()

        with sub2:
            st.markdown("#### Mehrere Leads auf einmal einfügen")
            st.info("Format: `lead_text | company_name | contact_name | email | website`")
            bulk_text = st.text_area("Leads einfügen", height=220)
            if st.button("🔄 Vorschau", use_container_width=True) and bulk_text.strip():
                st.session_state["bulk_df"] = parse_bulk_text(bulk_text)
            bulk_df = st.session_state.get("bulk_df", pd.DataFrame())
            if not bulk_df.empty:
                st.caption(f"{len(bulk_df)} Leads erkannt")
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
                    if st.button("🚀 Speichern & Analysieren", use_container_width=True):
                        df = edited.fillna("")
                        df = df[df.apply(lambda r: any(str(v).strip() for v in r), axis=1)]
                        res = bulk_upsert(df, source="bulk_paste")
                        with st.spinner("Analyse..."):
                            ares = analyze_all(skip_existing=True, run_automations=True)
                        st.success(f"{sum(1 for _, d in res if not d)} gespeichert | {len(ares)} analysiert")
                        st.session_state.pop("bulk_df", None)
                        st.rerun()

        with sub3:
            st.markdown("#### CSV-Datei importieren")
            c1, _ = st.columns([1, 4])
            with c1:
                tpl = pd.DataFrame(columns=["lead_text", "company_name", "contact_name", "email", "website"])
                st.download_button("⬇️ Vorlage", tpl.to_csv(index=False), file_name="scopeos_template.csv", mime="text/csv")
            uploaded = st.file_uploader("CSV hochladen", type=["csv"])
            if uploaded:
                try:
                    df = pd.read_csv(uploaded)
                    for col in ["lead_text", "company_name", "contact_name", "email", "website"]:
                        if col not in df.columns:
                            df[col] = ""
                    df = df[["lead_text", "company_name", "contact_name", "email", "website"]].fillna("")
                    st.caption(f"{len(df)} Zeilen")
                    edited_csv = st.data_editor(df, num_rows="dynamic", use_container_width=True)
                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("💾 Importieren", use_container_width=True):
                            res = bulk_upsert(edited_csv.fillna(""), source="csv")
                            st.success(f"{sum(1 for _, d in res if not d)} neu")
                            st.rerun()
                    with c2:
                        if st.button("🚀 Importieren & Analysieren", use_container_width=True):
                            bulk_upsert(edited_csv.fillna(""), source="csv")
                            with st.spinner("Läuft..."):
                                ares = analyze_all(skip_existing=True, run_automations=True)
                            st.success(f"{len(ares)} analysiert")
                            st.rerun()
                except Exception as e:
                    st.error(f"Fehler: {e}")

        with sub4:
            st.markdown("#### Webhook-Payload simulieren")
            default_payload = json.dumps({
                "lead_text": "Demo angefragt, Budget vorhanden",
                "company_name": "ACME GmbH",
                "contact_name": "Max Müller CEO",
                "email": "max@acme.de",
                "website": "acme.de",
                "source": "webhook"
            }, indent=2, ensure_ascii=False)
            payload_text = st.text_area("JSON Payload", value=default_payload, height=220)
            c1, c2 = st.columns(2)
            with c1:
                if st.button("📡 Simulieren", use_container_width=True):
                    lead_id, dup = simulate_webhook(payload_text)
                    if lead_id:
                        st.success(f"Lead #{lead_id} {'(Duplikat)' if dup else 'gespeichert'}")
                    else:
                        st.error("Ungültiges JSON")
                    st.rerun()
            with c2:
                if st.button("📡 Simulieren & Analysieren", use_container_width=True):
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
                    else:
                        st.error("Ungültiges JSON")

    with tab_analyse:
        st.markdown("### ⚡ Analyse & Automationen")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("▶️ Neue analysieren", use_container_width=True):
                with st.spinner("Analysiere..."):
                    res = analyze_all(skip_existing=True, run_automations=True)
                st.success(f"✅ {len(res)} analysiert")
                st.rerun()
        with c2:
            if st.button("🔁 Alle neu berechnen", use_container_width=True):
                with get_connection() as conn:
                    conn.execute("DELETE FROM analyses")
                    conn.commit()
                with st.spinner("Rebuild..."):
                    res = analyze_all(skip_existing=False, run_automations=True)
                st.success(f"✅ {len(res)} neu berechnet")
                st.rerun()
        with c3:
            if st.button("🗑️ Aktivitäten löschen", use_container_width=True):
                with get_connection() as conn:
                    conn.execute("DELETE FROM activities")
                    conn.commit()
                st.success("✅ Gelöscht")
                st.rerun()

        st.divider()
        st.markdown("#### Workflow-Regeln")
        for tier, wf in WORKFLOW_RULES.items():
            c1, c2, c3, c4 = st.columns([1,2,2,3])
            c1.markdown(f"**{TIER_EMOJI[tier]} {tier}**")
            c2.caption(f"Owner: {wf['owner']}")
            c3.caption(f"Aktion: {wf['action']}")
            c4.caption(f"Sequenz: {wf['sequence']}")

        st.divider()
        st.markdown("#### Gmail-Scanner")
        st.info("Läuft automatisch alle 5 Minuten wenn `start.py` aktiv ist.")
        st.code("python gmail_scanner.py --once\npython gmail_scanner.py --hours 48", language="bash")

    with tab_followup:
        st.markdown("### 🔁 Follow-up Automation")
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("▶️ Due Sequenzen jetzt ausführen", use_container_width=True):
                with st.spinner("Follow-ups werden geprüft..."):
                    results = run_due_sequences()
                st.success(f"{len(results)} Sequenzen verarbeitet")
        with c2:
            df_leads = get_lead_table()
            if not df_leads.empty:
                lead_options = {
                    f"#{row.id} | {row.company} | {row.contact} | {row.email}": row.id
                    for _, row in df_leads.iterrows()
                    if str(row.email) != "—"
                }
                if lead_options:
                    selected_label = st.selectbox("Lead auswählen", list(lead_options.keys()))
                    if st.button("➕ Follow-up für Lead starten", use_container_width=True):
                        seq_id = create_followup_from_lead(lead_options[selected_label], delay_days=0)
                        st.success(f"Sequenz #{seq_id} gestartet")
                else:
                    st.info("Keine Leads mit E-Mail vorhanden.")
            else:
                st.info("Noch keine Leads vorhanden.")
        with c3:
            if st.button("🔄 Refresh", use_container_width=True):
                st.rerun()

        st.divider()
        st.markdown("#### Aktive Sequenzen")
        active = get_active_sequences()
        if active:
            for seq in active:
                with st.expander(f"Lead #{seq['lead_id']} | Stage {seq['stage']} | {seq['status']}"):
                    st.write({
                        "Sequence ID": seq["id"],
                        "Lead ID": seq["lead_id"],
                        "Stage": seq["stage"],
                        "Status": seq["status"],
                        "Next Run": seq["next_run_at"],
                        "Last Sent": seq["last_sent_at"],
                        "Channel": seq["channel"],
                    })
        else:
            st.info("Keine aktiven Follow-up-Sequenzen.")

    with tab_quellen:
        st.markdown("### 🔗 Quellen & Webhook-Verbindungen")
        df_src = get_lead_table()
        if not df_src.empty:
            source_stats = df_src.groupby("source").agg(
                Anzahl=("id", "count"),
                Hot=("tier", lambda x: (x == "Hot").sum()),
                Warm=("tier", lambda x: (x == "Warm").sum()),
                Cold=("tier", lambda x: (x == "Cold").sum())
            ).reset_index().rename(columns={"source": "Quelle"})
            st.dataframe(source_stats, use_container_width=True)
        else:
            st.info("Noch keine Leads vorhanden.")

        st.divider()
        st.markdown("#### Webhook Endpoints")
        endpoints = [
            ("Tally.so", "POST", "/webhook/tally", "Tally-Formular direkt verbinden"),
            ("Typeform", "POST", "/webhook/typeform", "Typeform-Webhook eintragen"),
            ("Calendly", "POST", "/webhook/calendly", "Calendly-Webhook eintragen"),
            ("Make / Zapier / n8n", "POST", "/webhook/generic", "Generischer JSON-Webhook"),
            ("Eigene App", "POST", "/webhook/lead", "Standard ScopeOS Payload"),
        ]
        for name, method, path, desc in endpoints:
            e1, e2, e3, e4 = st.columns([2,1,3,4])
            e1.markdown(f"**{name}**")
            e2.markdown(f"`{method}`")
            e3.code(path, language=None)
            e4.caption(desc)

        st.divider()
        st.markdown("#### ngrok")
        st.code("ngrok http 8000", language="bash")
        st.caption("Gibt eine öffentliche URL für Tally, Calendly, Typeform.")

        st.divider()
        st.markdown("#### Gmail-Integration")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Token**")
            st.success("✅ gmail_token.json vorhanden") if Path("gmail_token.json").exists() else st.warning("⚠️ Noch nicht verbunden")
        with c2:
            st.markdown("**Credentials**")
            st.success("✅ gmail_credentials.json vorhanden") if Path("gmail_credentials.json").exists() else st.error("❌ Fehlt")

    with tab_log:
        st.markdown("### 📋 Aktivitäten-Log")
        c1, c2 = st.columns([3,1])
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
            st.caption(f"{len(acts)} Einträge")
            for act in acts:
                icon = {
                    "slack": "💬", "crm": "🏢", "email": "📧", "workflow": "⚡",
                    "gmail_scan": "📩", "webhook_ingest": "🔗", "generic_ingest": "🌐"
                }.get(act["activity_type"], "📋")
                with st.expander(f"{icon} Lead #{act['lead_id']} — {act['activity_type']} — {act['status']} — {act['created_at']}"):
                    try:
                        st.json(json.loads(act["payload"]))
                    except Exception:
                        st.write(act["payload"])
        else:
            st.info("Noch keine Aktivitäten.")

if __name__ == "__main__":
    main()