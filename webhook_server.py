from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path("scopeos.db")
app = FastAPI(title="ScopeOS Webhook API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

class LeadPayload(BaseModel):
    lead_text: str = ""
    company_name: str = ""
    contact_name: str = ""
    email: str = ""
    website: str = ""
    source: str = "webhook"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def normalize(text):
    return ' '.join((text or '').lower().strip().split())

def make_unique_key(d):
    return '|'.join([normalize(d.get(k,'')) for k in ['lead_text','company_name','contact_name','email','website']])[:500]

def upsert_lead_db(data: dict):
    d = {k: (data.get(k) or '').strip() for k in ['lead_text','company_name','contact_name','email','website','source','raw_payload']}
    d['unique_key'] = make_unique_key(d)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM leads WHERE unique_key=?", (d['unique_key'],))
        row = cur.fetchone()
        if row:
            cur.execute("""UPDATE leads SET lead_text=?,company_name=?,contact_name=?,email=?,website=?,source=?,raw_payload=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (d['lead_text'],d['company_name'],d['contact_name'],d['email'],d['website'],d['source'],d['raw_payload'],row['id']))
            conn.commit()
            return row['id'], True
        cur.execute("""INSERT INTO leads(lead_text,company_name,contact_name,email,website,source,raw_payload,unique_key) VALUES(?,?,?,?,?,?,?,?)""",
            (d['lead_text'],d['company_name'],d['contact_name'],d['email'],d['website'],d['source'],d['raw_payload'],d['unique_key']))
        conn.commit()
        return cur.lastrowid, False

def score_lead_quick(data: dict):
    FIT_RULES_TITLE = {"ceo":15,"founder":15,"coo":12,"cmo":12,"sales":10,"marketing":8,"head":8,"manager":5,"assistant":0,"intern":-10}
    INTENT_KW = {"demo":20,"pricing":15,"budget":15,"urgent":15,"next week":15,"follow up":10,"schedule":12,"trial":18,"proposal":12,"decision":12,"call":8,"quote":12,"buy":15,"need now":18}
    NEG_KW = {"just curious":-10,"not now":-15,"no budget":-20,"student":-20,"spam":-30}
    text = f"{data.get('lead_text','')} {data.get('company_name','')} {data.get('contact_name','')} {data.get('email','')}".lower()
    fit = next((v for k,v in FIT_RULES_TITLE.items() if k in text), 0)
    if data.get('website'): fit += 5
    if any(d in text for d in ['gmail','outlook','hotmail','yahoo']): fit -= 5
    intent = sum(v for k,v in INTENT_KW.items() if k in text)
    intent += sum(v for k,v in NEG_KW.items() if k in text)
    score = max(0, min(100, fit + intent))
    tier = 'Hot' if score >= 70 else 'Warm' if score >= 40 else 'Cold'
    return score, tier

def log_activity_db(lead_id, activity_type, payload):
    with get_connection() as conn:
        conn.execute("INSERT INTO activities(lead_id,activity_type,payload,status) VALUES(?,?,?,?)",
            (lead_id, activity_type, json.dumps(payload), 'done'))
        conn.commit()

# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ScopeOS Webhook API läuft", "version": "1.0"}

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.post("/webhook/lead")
async def receive_lead(payload: LeadPayload):
    """Standard Webhook Endpoint für alle Lead-Quellen."""
    data = payload.dict()
    data['raw_payload'] = json.dumps(data)
    lead_id, duplicate = upsert_lead_db(data)
    score, tier = score_lead_quick(data)
    log_activity_db(lead_id, 'webhook_ingest', {'source': data['source'], 'score': score, 'tier': tier, 'duplicate': duplicate})
    return {
        "success": True,
        "lead_id": lead_id,
        "duplicate": duplicate,
        "score": score,
        "tier": tier,
        "message": f"Lead gespeichert: {tier} ({score}/100)"
    }

@app.post("/webhook/tally")
async def receive_tally(request: Request):
    """Tally.so Formular-Webhook."""
    body = await request.json()
    fields = body.get('data', {}).get('fields', [])
    def get_field(label):
        for f in fields:
            if label.lower() in (f.get('label','') or '').lower():
                v = f.get('value')
                if isinstance(v, list): return ' '.join(str(x) for x in v)
                return str(v) if v else ''
        return ''
    data = {
        'lead_text': get_field('message') or get_field('nachricht') or get_field('interest') or get_field('interesse'),
        'company_name': get_field('company') or get_field('firma') or get_field('unternehmen'),
        'contact_name': get_field('name') or get_field('full name'),
        'email': get_field('email') or get_field('e-mail'),
        'website': get_field('website') or get_field('url'),
        'source': 'tally',
        'raw_payload': json.dumps(body)
    }
    lead_id, duplicate = upsert_lead_db(data)
    score, tier = score_lead_quick(data)
    log_activity_db(lead_id, 'tally_ingest', {'score': score, 'tier': tier})
    return {"success": True, "lead_id": lead_id, "score": score, "tier": tier}

@app.post("/webhook/typeform")
async def receive_typeform(request: Request):
    """Typeform Webhook."""
    body = await request.json()
    answers = body.get('form_response', {}).get('answers', [])
    definition = body.get('form_response', {}).get('definition', {}).get('fields', [])
    field_map = {}
    for i, ans in enumerate(answers):
        if i < len(definition):
            label = definition[i].get('title','').lower()
            val = ans.get('text') or ans.get('email') or ans.get('url') or ''
            field_map[label] = val
    def get_tf(keywords):
        for k,v in field_map.items():
            if any(kw in k for kw in keywords): return v
        return ''
    data = {
        'lead_text': get_tf(['message','nachricht','interest','interesse','kommentar','comment']),
        'company_name': get_tf(['company','firma','unternehmen']),
        'contact_name': get_tf(['name','vorname']),
        'email': get_tf(['email','mail']),
        'website': get_tf(['website','url','web']),
        'source': 'typeform',
        'raw_payload': json.dumps(body)
    }
    lead_id, duplicate = upsert_lead_db(data)
    score, tier = score_lead_quick(data)
    log_activity_db(lead_id, 'typeform_ingest', {'score': score, 'tier': tier})
    return {"success": True, "lead_id": lead_id, "score": score, "tier": tier}

@app.post("/webhook/calendly")
async def receive_calendly(request: Request):
    """Calendly Webhook."""
    body = await request.json()
    event = body.get('payload', {})
    invitee = event.get('invitee', {})
    questions = event.get('questions_and_answers', [])
    def get_qa(keywords):
        for qa in questions:
            q = (qa.get('question','') or '').lower()
            if any(kw in q for kw in keywords):
                return qa.get('answer','')
        return ''
    data = {
        'lead_text': f"Calendly Buchung: {event.get('event_type_name','')} {get_qa(['interest','interesse','thema','topic','comment','kommentar'])}",
        'company_name': get_qa(['company','firma','unternehmen']) or invitee.get('company',''),
        'contact_name': invitee.get('name',''),
        'email': invitee.get('email',''),
        'website': get_qa(['website','web','url']),
        'source': 'calendly',
        'raw_payload': json.dumps(body)
    }
    lead_id, duplicate = upsert_lead_db(data)
    score, tier = score_lead_quick(data)
    log_activity_db(lead_id, 'calendly_ingest', {'score': score, 'tier': tier})
    return {"success": True, "lead_id": lead_id, "score": score, "tier": tier}

@app.post("/webhook/generic")
async def receive_generic(request: Request):
    """Generischer Webhook für Make, Zapier, n8n etc."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Ungültiges JSON")
    data = {
        'lead_text': str(body.get('lead_text','') or body.get('message','') or body.get('text','')),
        'company_name': str(body.get('company_name','') or body.get('company','')),
        'contact_name': str(body.get('contact_name','') or body.get('name','') or body.get('full_name','')),
        'email': str(body.get('email','') or body.get('email_address','')),
        'website': str(body.get('website','') or body.get('url','') or body.get('domain','')),
        'source': str(body.get('source','generic')),
        'raw_payload': json.dumps(body)
    }
    lead_id, duplicate = upsert_lead_db(data)
    score, tier = score_lead_quick(data)
    log_activity_db(lead_id, 'generic_ingest', {'source': data['source'], 'score': score, 'tier': tier})
    return {"success": True, "lead_id": lead_id, "score": score, "tier": tier}

@app.get("/leads")
def get_all_leads():
    """Alle Leads abrufen."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, company_name, contact_name, email, source, created_at FROM leads ORDER BY id DESC LIMIT 100")
        return {"leads": [dict(r) for r in cur.fetchall()]}

@app.get("/leads/{lead_id}")
def get_lead(lead_id: int):
    """Einzelnen Lead abrufen."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM leads WHERE id=?", (lead_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(status_code=404, detail="Lead nicht gefunden")
        return dict(row)

@app.get("/stats")
def get_stats():
    """Statistiken."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM leads"); leads = cur.fetchone()['c']
        cur.execute("SELECT COUNT(*) AS c FROM analyses"); analyses = cur.fetchone()['c']
        cur.execute("SELECT tier, COUNT(*) c FROM analyses GROUP BY tier")
        tiers = {r['tier']: r['c'] for r in cur.fetchall()}
    return {"leads": leads, "analyses": analyses, "tiers": tiers}