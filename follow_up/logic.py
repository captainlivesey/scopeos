import sqlite3
import json
import smtplib
from pathlib import Path
from datetime import datetime, timedelta

DB_PATH = Path("scopeos.db")
FOLLOW_UP_RULES = [
    {"day": 0, "name": "sofort", "channel": "email", "template": "followup_0"},
    {"day": 2, "name": "reminder_1", "channel": "email", "template": "followup_2"},
    {"day": 5, "name": "reminder_2", "channel": "email", "template": "followup_5"},
    {"day": 10, "name": "final_touch", "channel": "email", "template": "followup_10"},
]

TEMPLATES = {
    "followup_0": "Hallo {contact_name},\n\nvielen Dank für Ihr Interesse. Ich wollte mich kurz melden und den nächsten Schritt sauber mit Ihnen abstimmen.\n\nViele Grüße\nScopeOS Team",
    "followup_2": "Hallo {contact_name},\n\nich wollte kurz freundlich nachfassen, ob Sie noch Fragen haben oder einen Termin möchten.\n\nViele Grüße\nScopeOS Team",
    "followup_5": "Hallo {contact_name},\n\nich wollte mich noch einmal kurz melden. Falls das Thema weiter relevant ist, antworte einfach auf diese Nachricht.\n\nViele Grüße\nScopeOS Team",
    "followup_10": "Hallo {contact_name},\n\nletzter kurzer Follow-up von meiner Seite. Wenn es später wieder relevant wird, melden Sie sich gern jederzeit.\n\nViele Grüße\nScopeOS Team",
}

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_followup_tables():
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS followup_sequences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER NOT NULL,
            stage INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            last_sent_at TIMESTAMP,
            next_run_at TIMESTAMP,
            channel TEXT DEFAULT 'email',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS followup_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sequence_id INTEGER,
            lead_id INTEGER,
            event_type TEXT,
            payload TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit()

def get_setting(key, default=''):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row['value'] if row else default

def log_event(sequence_id, lead_id, event_type, payload):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO followup_events(sequence_id, lead_id, event_type, payload) VALUES(?,?,?,?)",
            (sequence_id, lead_id, event_type, json.dumps(payload, ensure_ascii=False))
        )
        conn.commit()

def add_followup_sequence(lead_id, first_delay_days=0):
    init_followup_tables()
    next_run = datetime.utcnow() + timedelta(days=first_delay_days)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO followup_sequences(lead_id, stage, status, next_run_at, channel) VALUES(?,?,?,?,?)",
            (lead_id, 0, 'active', next_run.isoformat(timespec='seconds'), 'email')
        )
        conn.commit()
        seq_id = cur.lastrowid
    log_event(seq_id, lead_id, 'sequence_created', {'next_run_at': next_run.isoformat(timespec='seconds')})
    return seq_id

def get_lead(lead_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM leads WHERE id=?", (lead_id,))
        return cur.fetchone()

def get_active_sequences():
    init_followup_tables()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM followup_sequences WHERE status='active' ORDER BY next_run_at ASC")
        return cur.fetchall()

def send_email(lead, subject, body):
    smtp_host = get_setting('smtp_host')
    smtp_port = int(get_setting('smtp_port','587'))
    smtp_user = get_setting('smtp_user')
    smtp_pass = get_setting('smtp_pass')
    notify_to = lead['email']
    if not all([smtp_host, smtp_user, smtp_pass, notify_to]):
        return False, 'SMTP oder Empfänger fehlt'
    msg = f"Subject: {subject}\nTo: {notify_to}\nFrom: {smtp_user}\n\n{body}"
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [notify_to], msg.encode('utf-8'))
        return True, 'OK'
    except Exception as e:
        return False, str(e)

def next_followup_rule(stage):
    if stage >= len(FOLLOW_UP_RULES):
        return None
    return FOLLOW_UP_RULES[stage]

def should_stop_for_response(lead_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM activities WHERE lead_id=? AND activity_type IN ('reply','meeting_booked','deal_won')", (lead_id,))
        return cur.fetchone()['c'] > 0

def process_sequence(seq):
    lead = get_lead(seq['lead_id'])
    if not lead:
        return False, 'Lead fehlt'
    if should_stop_for_response(seq['lead_id']):
        with get_connection() as conn:
            conn.execute("UPDATE followup_sequences SET status='stopped', updated_at=CURRENT_TIMESTAMP WHERE id=?", (seq['id'],))
            conn.commit()
        log_event(seq['id'], seq['lead_id'], 'stopped_due_response', {})
        return False, 'Gestoppt wegen Antwort'

    rule = next_followup_rule(seq['stage'])
    if not rule:
        with get_connection() as conn:
            conn.execute("UPDATE followup_sequences SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (seq['id'],))
            conn.commit()
        log_event(seq['id'], seq['lead_id'], 'completed', {})
        return False, 'Sequenz beendet'

    contact_name = lead['contact_name'] or 'dort'
    body = TEMPLATES[rule['template']].format(contact_name=contact_name)
    subject = f"Kurze Rückfrage zu {lead['company_name'] or 'Ihrer Anfrage'}"
    ok, msg = send_email(lead, subject, body)
    if ok:
        stage = seq['stage'] + 1
        next_rule = next_followup_rule(stage)
        next_run = None
        if next_rule:
            next_run = (datetime.utcnow() + timedelta(days=next_rule['day'])).isoformat(timespec='seconds')
        with get_connection() as conn:
            conn.execute(
                "UPDATE followup_sequences SET stage=?, last_sent_at=CURRENT_TIMESTAMP, next_run_at=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (stage, next_run, seq['id'])
            )
            conn.commit()
        log_event(seq['id'], seq['lead_id'], 'email_sent', {'subject': subject, 'rule': rule['name']})
        return True, 'Gesendet'
    else:
        log_event(seq['id'], seq['lead_id'], 'email_error', {'error': msg})
        return False, msg

def run_due_sequences(limit=50):
    init_followup_tables()
    now = datetime.utcnow().isoformat(timespec='seconds')
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM followup_sequences WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at ASC LIMIT ?", (now, limit))
        seqs = cur.fetchall()
    results = []
    for seq in seqs:
        results.append((seq['id'],) + process_sequence(seq))
    return results

def create_followup_from_lead(lead_id, delay_days=0):
    return add_followup_sequence(lead_id, first_delay_days=delay_days)