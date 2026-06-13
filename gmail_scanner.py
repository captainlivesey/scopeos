import os
import json
import base64
import sqlite3
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

DB_PATH = Path("scopeos.db")
CREDENTIALS_FILE = Path("gmail_credentials.json")
TOKEN_FILE = Path("gmail_token.json")
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "llama3.2"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.modify"]

LEAD_SIGNALS = [
    "demo","pricing","budget","interesse","interested","anfrage","request",
    "angebot","proposal","trial","test","kaufen","buy","kosten","cost",
    "termin","meeting","call","schedule","kontakt","contact","implementieren",
    "implement","lösung","solution","software","tool","platform","subscribe",
    "subscription","enterprise","sales","vertrieb","partnerschaft","partner"
]

IGNORE_SENDERS = [
    "noreply","no-reply","newsletter","notifications","mailer-daemon",
    "donotreply","do-not-reply","support","info@google","accounts@google",
    "linkedin.com","facebook.com","twitter.com","instagram.com","youtube.com"
]


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize(text):
    return " ".join((text or "").lower().strip().split())


def make_unique_key(d):
    return "|".join([normalize(d.get(k, "")) for k in
                     ["lead_text", "company_name", "contact_name", "email", "website"]])[:500]


def upsert_lead(data):
    d = {k: (data.get(k) or "").strip() for k in
         ["lead_text", "company_name", "contact_name", "email", "website", "source", "raw_payload"]}
    d["unique_key"] = make_unique_key(d)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM leads WHERE unique_key=?", (d["unique_key"],))
        row = cur.fetchone()
        if row:
            return row["id"], True
        cur.execute("""INSERT INTO leads(lead_text,company_name,contact_name,email,website,source,raw_payload,unique_key)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (d["lead_text"], d["company_name"], d["contact_name"],
                     d["email"], d["website"], d["source"], d["raw_payload"], d["unique_key"]))
        conn.commit()
        return cur.lastrowid, False


def score_lead(data):
    FIT = {"ceo":15,"founder":15,"coo":12,"cmo":12,"sales":10,"marketing":8,"head":8,"manager":5,"intern":-10}
    INTENT = {"demo":20,"pricing":15,"budget":15,"urgent":15,"trial":18,"proposal":12,"call":8,"buy":15,"schedule":12}
    NEG = {"just curious":-10,"no budget":-20,"student":-20,"spam":-30,"newsletter":-15}
    text = " ".join([data.get(k,"") or "" for k in ["lead_text","contact_name","email"]]).lower()
    fit = next((v for k,v in FIT.items() if k in text), 0)
    if data.get("website"): fit += 5
    if any(d in text for d in ["gmail","outlook","hotmail","yahoo"]): fit -= 5
    intent = sum(v for k,v in INTENT.items() if k in text)
    intent += sum(v for k,v in NEG.items() if k in text)
    score = max(0, min(100, fit + intent))
    tier = "Hot" if score >= 70 else "Warm" if score >= 40 else "Cold"
    return score, tier


def log_activity(lead_id, activity_type, payload):
    with get_connection() as conn:
        conn.execute("INSERT INTO activities(lead_id,activity_type,payload,status) VALUES(?,?,?,?)",
                     (lead_id, activity_type, json.dumps(payload, ensure_ascii=False), "done"))
        conn.commit()


def save_analysis(lead_id, score, tier, fit, intent, reason, next_step, analysis):
    with get_connection() as conn:
        conn.execute("""INSERT INTO analyses(lead_id,score,tier,fit_score,intent_score,reason,next_step,analysis)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (lead_id, score, tier, fit, intent, reason, next_step, analysis))
        conn.commit()


def get_analyses_for_lead(lead_id):
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM analyses WHERE lead_id=? ORDER BY id DESC", (lead_id,))
        return cur.fetchall()


def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    "gmail_credentials.json nicht gefunden!\n"
                    "Bitte Google Cloud Credentials herunterladen und als gmail_credentials.json speichern.\n"
                    "Anleitung: https://developers.google.com/gmail/api/quickstart/python"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_email_body(msg):
    body = ""
    try:
        payload = msg.get("payload", {})
        if "parts" in payload:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/plain":
                    data = part.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                        break
        else:
            data = payload.get("body", {}).get("data", "")
            if data:
                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    except Exception:
        pass
    return body[:3000]


def parse_sender(from_header):
    name, email = "", ""
    if "<" in from_header:
        parts = from_header.split("<")
        name = parts[0].strip().strip('"')
        email = parts[1].strip().rstrip(">")
    else:
        email = from_header.strip()
    return name, email


def is_lead_email(subject, body, sender_email):
    if any(ign in sender_email.lower() for ign in IGNORE_SENDERS):
        return False
    text = f"{subject} {body}".lower()
    matches = sum(1 for sig in LEAD_SIGNALS if sig in text)
    return matches >= 1


def ollama_extract(subject, body, sender_name, sender_email):
    prompt = f"""Du bist ein Lead-Extraction-Assistent für ein B2B Sales Tool.
Analysiere diese E-Mail und extrahiere die Lead-Informationen.

Von: {sender_name} <{sender_email}>
Betreff: {subject}
Inhalt:
{body[:1500]}

Antworte NUR mit einem JSON-Objekt (kein Text davor oder danach):
{{
  "is_lead": true oder false,
  "company_name": "Firmenname oder leer",
  "contact_name": "Name des Absenders",
  "lead_text": "Kurze Zusammenfassung des Interesses in 1-2 Sätzen",
  "intent": "demo|pricing|trial|info|other",
  "urgency": "high|medium|low"
}}"""

    try:
        r = requests.post(OLLAMA_URL, json={
            "model": MODEL, "prompt": prompt, "stream": False,
            "options": {"temperature": 0, "seed": 42}
        }, timeout=60)
        r.raise_for_status()
        response_text = r.json().get("response", "")
        start = response_text.find("{")
        end = response_text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(response_text[start:end])
    except Exception as e:
        print(f"Ollama Fehler: {e}")
    return None


def scan_gmail(max_emails=50, since_hours=24):
    print(f"\n{'='*50}")
    print(f"Gmail-Scan startet: {datetime.now().strftime('%H:%M:%S')}")
    print(f"Suche E-Mails der letzten {since_hours} Stunden...")

    service = get_gmail_service()
    since = datetime.utcnow() - timedelta(hours=since_hours)
    query = f"after:{int(since.timestamp())} is:inbox -is:sent"

    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_emails
    ).execute()

    messages = results.get("messages", [])
    print(f"Gefunden: {len(messages)} E-Mails")

    new_leads = 0
    duplicates = 0

    for msg_ref in messages:
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ).execute()

            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            subject = headers.get("Subject", "")
            from_header = headers.get("From", "")
            sender_name, sender_email = parse_sender(from_header)
            body = get_email_body(msg)

            if not is_lead_email(subject, body, sender_email):
                continue

            print(f"\n  Lead-Signal erkannt: {sender_email}")
            print(f"  Betreff: {subject[:60]}")

            extracted = ollama_extract(subject, body, sender_name, sender_email)

            if extracted and extracted.get("is_lead"):
                domain = sender_email.split("@")[-1] if "@" in sender_email else ""
                website = domain if domain and not any(
                    d in domain for d in ["gmail","outlook","hotmail","yahoo","gmx","web.de"]
                ) else ""

                lead_data = {
                    "lead_text": extracted.get("lead_text", f"{subject}: {body[:200]}"),
                    "company_name": extracted.get("company_name", ""),
                    "contact_name": extracted.get("contact_name", sender_name),
                    "email": sender_email,
                    "website": website,
                    "source": "gmail",
                    "raw_payload": json.dumps({
                        "subject": subject,
                        "from": from_header,
                        "intent": extracted.get("intent"),
                        "urgency": extracted.get("urgency"),
                        "body_preview": body[:300]
                    }, ensure_ascii=False)
                }

                lead_id, duplicate = upsert_lead(lead_data)

                if duplicate:
                    duplicates += 1
                    print(f"  Duplikat: Lead {lead_id} existiert bereits")
                    continue

                score, tier = score_lead(lead_data)
                tier_emoji = {"Hot": "🔥", "Warm": "🌤️", "Cold": "❄️"}.get(tier, "")

                reason = f"Gmail Lead. Intent: {extracted.get('intent','?')}. Urgency: {extracted.get('urgency','?')}."
                next_step = {
                    "Hot": "Sofort anrufen und Demo terminieren.",
                    "Warm": "Follow-up-Mail senden.",
                    "Cold": "Ins Nurture aufnehmen."
                }[tier]

                if not get_analyses_for_lead(lead_id):
                    save_analysis(lead_id, score, tier, score//2, score//2, reason, next_step,
                                  f"Gmail-Lead automatisch erkannt. Betreff: {subject}")
                    log_activity(lead_id, "gmail_scan", {
                        "score": score, "tier": tier,
                        "intent": extracted.get("intent"),
                        "urgency": extracted.get("urgency"),
                        "subject": subject
                    })

                new_leads += 1
                print(f"  {tier_emoji} Neuer Lead: {sender_name} | {sender_email} | Score: {score}/100 | {tier}")

        except Exception as e:
            print(f"  Fehler bei E-Mail: {e}")
            continue

    print(f"\nScan abgeschlossen: {new_leads} neue Leads, {duplicates} Duplikate")
    print(f"{'='*50}\n")
    return new_leads


def run_continuous(interval_minutes=5, since_hours=24):
    print("ScopeOS Gmail-Scanner läuft...")
    print(f"Scannt alle {interval_minutes} Minuten nach neuen Leads.")
    print("Stoppen mit Ctrl+C\n")
    while True:
        try:
            scan_gmail(since_hours=since_hours)
        except KeyboardInterrupt:
            print("\nScanner gestoppt.")
            break
        except Exception as e:
            print(f"Scan-Fehler: {e}")
        time.sleep(interval_minutes * 60)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ScopeOS Gmail Scanner")
    parser.add_argument("--once", action="store_true", help="Einmalig scannen")
    parser.add_argument("--hours", type=int, default=24, help="E-Mails der letzten X Stunden")
    parser.add_argument("--interval", type=int, default=5, help="Intervall in Minuten")
    args = parser.parse_args()
    if args.once:
        scan_gmail(since_hours=args.hours)
    else:
        run_continuous(interval_minutes=args.interval, since_hours=args.hours)