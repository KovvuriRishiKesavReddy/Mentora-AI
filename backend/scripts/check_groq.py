"""Check your Groq key and list the chat models it can use.   cd backend && python scripts/check_groq.py"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
from app import config
from app.services import llm
if not os.environ.get("GROQ_API_KEY"):
    sys.exit("GROQ_API_KEY is not set in .env")
c = llm.get_client()
try:
    ids = sorted(m.id for m in c.models.list().data)
except Exception as e:
    sys.exit(f"Could not list models: {type(e).__name__}: {llm._safe_detail(e)}")
print("Models available to your key:\n  " + "\n  ".join(ids))
print(f"\nConfigured: GROQ_MODEL={config.GROQ_MODEL}  fallbacks={config.GROQ_FALLBACK_MODELS}")
for m in [config.GROQ_MODEL] + config.GROQ_FALLBACK_MODELS:
    try:
        c.chat.completions.create(model=m, messages=[{"role": "user", "content": "Say OK"}], max_tokens=5)
        print(f"  OK     {m}")
    except Exception as e:
        print(f"  FAILED {m}: {llm._safe_detail(e)}")
