import subprocess
import sys
import threading
import time

def run_fastapi():
    subprocess.run([sys.executable, "-m", "uvicorn", "webhook_server:app",
                    "--host", "0.0.0.0", "--port", "8000", "--reload"])

def run_streamlit():
    time.sleep(2)
    subprocess.run([sys.executable, "-m", "streamlit", "run", "app.py",
                    "--server.port", "8501", "--server.address", "0.0.0.0"])

def run_gmail_scanner():
    time.sleep(5)
    subprocess.run([sys.executable, "gmail_scanner.py",
                    "--interval", "5", "--hours", "24"])

if __name__ == "__main__":
    print("\n" + "="*50)
    print("  ScopeOS startet...")
    print("="*50)
    print("  Streamlit UI        → http://localhost:8501")
    print("  FastAPI Webhook API → http://localhost:8000")
    print("  API Docs            → http://localhost:8000/docs")
    print("  Gmail Scanner       → alle 5 Minuten")
    print("="*50 + "\n")

    threads = [
        threading.Thread(target=run_fastapi, daemon=True),
        threading.Thread(target=run_streamlit),
        threading.Thread(target=run_gmail_scanner, daemon=True),
    ]
    for t in threads:
        t.start()
    threads[1].join()