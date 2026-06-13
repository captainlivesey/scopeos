CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT,
    contact_name TEXT,
    email TEXT,
    website TEXT,
    source TEXT,
    raw_input TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS lead_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    summary TEXT,
    ideal_customer_fit TEXT,
    urgency TEXT,
    estimated_budget TEXT,
    missing_information TEXT,
    project_scope TEXT,
    offer_draft TEXT,
    followup_draft TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);