"""Calibrate retrieval thresholds on the evaluation notes (no client id needed).
Run from backend:  python eval/calibrate_on_eval_notes.py [--embedder hashing]
Relevant = the 41 direct questions, irrelevant = the 10 out-of-scope questions. Prints the best true-cosine
similarity per question and a suggested weak/strong threshold."""
import argparse, shutil, statistics, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import run_eval as R
from app.services import embedding, rag
ap = argparse.ArgumentParser(); ap.add_argument("--embedder", choices=["auto", "hashing"], default="auto")
a = ap.parse_args(); qs = R.load_questions(); tmp = R.setup(R.default_notes(), a.embedder)
best = lambda q: max((h.similarity for h in rag.retrieve(R.OWNER, q, 6)), default=0.0)
try:
    rel = [best(q["question"]) for q in qs["direct"]]; irr = [best(q["question"]) for q in qs["out_of_scope"]]
finally:
    shutil.rmtree(tmp, ignore_errors=True)
print("embedder:", embedding.get_embedder().name)
print(f"relevant   n={len(rel)} min={min(rel):.3f} median={statistics.median(rel):.3f} max={max(rel):.3f}")
print(f"irrelevant n={len(irr)} min={min(irr):.3f} median={statistics.median(irr):.3f} max={max(irr):.3f}")
lo, hi = min(rel), max(irr)
if lo > hi: print(f"classes separate: RAG_SIM_WEAK between {hi:.3f} and {lo:.3f}; RAG_SIM_STRONG near median relevant {statistics.median(rel):.3f}")
else: print(f"classes overlap (lowest relevant {lo:.3f} <= highest irrelevant {hi:.3f}): the weak threshold alone cannot separate them")
