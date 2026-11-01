"""Export real-model answers for MANUAL accuracy rating.
Run from the backend folder:  python eval/export_answers_for_rating.py [--embedder hashing] [--limit 41]
Needs GROQ_API_KEY in .env. Writes eval/answers_to_rate.csv. Open it in Excel, read each answer against your
notes, and put 1 (correct and supported by the notes), 0.5 (partly) or 0 (wrong / invented) in the 'rating' column.
Then run:  python eval/score_ratings.py"""
import argparse, csv, os, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import run_eval as R
from app import chat_service
from app.services import llm, memory

ap = argparse.ArgumentParser()
ap.add_argument("--embedder", choices=["auto", "hashing"], default="auto")
ap.add_argument("--limit", type=int, default=41)
ap.add_argument("--stub", action="store_true", help="dry run without the model (to test the script)")
a = ap.parse_args()
if not a.stub and not os.environ.get("GROQ_API_KEY"):
    try:
        from dotenv import load_dotenv; load_dotenv(HERE.parent.parent / ".env")
    except Exception: pass
    if not os.environ.get("GROQ_API_KEY"): sys.exit("Set GROQ_API_KEY in .env first.")
notes = R.default_notes(); qs = R.load_questions()
tmp = R.setup(notes, a.embedder)
llm.set_backend(R._stub if a.stub else None)
rows = []
try:
    for q in qs["direct"][:a.limit]:
        sid = memory.create_session(R.OWNER)
        r = chat_service.handle_chat(R.OWNER, sid, q["question"])
        rows.append({"id": q["id"], "question": q["question"], "expected_section": q["section"],
                     "grounding": r["retrieval"]["grounding"], "answer": r["response"], "rating": ""})
finally:
    llm.set_backend(None)
    import shutil; shutil.rmtree(tmp, ignore_errors=True)
out = HERE / "answers_to_rate.csv"
with open(out, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
print("wrote", out, "-", len(rows), "answers")
