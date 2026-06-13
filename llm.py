import json
import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "llama3.2"


PROMPT_TEMPLATE = """
Du bist ScopeOS, ein lokales AI-System für Agenturen und Dienstleister.

Analysiere den folgenden Lead und gib ausschließlich JSON zurück.

Pflichtfelder:
- summary
- ideal_customer_fit
- urgency
- estimated_budget
- missing_information
- project_scope
- offer_draft
- followup_draft

Lead:
{lead_text}
"""


def analyze_lead(lead_text: str):
    prompt = PROMPT_TEMPLATE.format(lead_text=lead_text.strip())

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "prompt": prompt,
            "stream": False
        },
        timeout=120,
    )
    response.raise_for_status()
    data = response.json()

    text = data.get("response", "").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "summary": text,
            "ideal_customer_fit": "",
            "urgency": "",
            "estimated_budget": "",
            "missing_information": "",
            "project_scope": "",
            "offer_draft": "",
            "followup_draft": "",
        }