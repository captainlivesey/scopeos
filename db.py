import sqlite3
from pathlib import Path
import json

DB_PATH = Path("scopeos.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_text TEXT,
                company_name TEXT,
                contact_name TEXT,
                email TEXT,
                website TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER,
                analysis TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def create_lead(lead_text, company_name, contact_name, email, website):
    init_db()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO leads (lead_text, company_name, contact_name, email, website)
            VALUES (?, ?, ?, ?, ?)
            """,
            (lead_text, company_name, contact_name, email, website),
        )
        conn.commit()
        return cur.lastrowid


def save_analysis(lead_id, analysis):
    init_db()
    if isinstance(analysis, (dict, list)):
        analysis = json.dumps(analysis, ensure_ascii=False)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO analyses (lead_id, analysis)
            VALUES (?, ?)
            """,
            (lead_id, analysis),
        )
        conn.commit()
        return cur.lastrowid