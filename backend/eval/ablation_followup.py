"""Follow-up ablation: retrieval on the follow-up turns WITH vs WITHOUT query rewriting (retrieval only, no model).
Run from the backend folder:  python eval/ablation_followup.py [--embedder hashing]"""
import argparse, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import run_eval as R
from app.services import query

ap = argparse.ArgumentParser(); ap.add_argument("--embedder", choices=["auto", "hashing"], default="auto")
a = ap.parse_args(); notes = R.default_notes(); qs = R.load_questions()
def score(label):
    res = R.run(notes, qs, "retrieval", a.embedder)
    ok = sum(t["ok"] for f in res["followups"] for t in f["turns"]); n = sum(len(f["turns"]) for f in res["followups"])
    print(f"{label:28s} {ok}/{n} turns retrieved from the right chapter  (embedder={res['embedder']})")
score("WITH follow-up resolution")
orig = query.resolve
query.resolve = lambda message, history, active_topic, llm_fn=None: query.Resolved(message, active_topic, False, False, "none")
import app.chat_service as cs; cs.query.resolve = query.resolve
score("WITHOUT (message as typed)")
query.resolve = orig
