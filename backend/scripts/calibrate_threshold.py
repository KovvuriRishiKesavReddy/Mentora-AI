"""Calibrate the retrieval confidence thresholds on YOUR notes with the REAL embedder.

    cd backend
    python scripts/calibrate_threshold.py --owner <client id> --relevant "Explain round robin" "What is a page fault" \
                                          --irrelevant "Who won the world cup" "How to bake bread"

Prints the best true-cosine similarity per question. Set RAG_SIM_WEAK just below the lowest 'relevant' score and
above the highest 'irrelevant' score; set RAG_SIM_STRONG near the median of the relevant scores (.env)."""
import argparse, statistics, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.services import embedding, rag

ap = argparse.ArgumentParser()
ap.add_argument("--owner", required=True)
ap.add_argument("--relevant", nargs="+", default=[])
ap.add_argument("--irrelevant", nargs="+", default=[])
a = ap.parse_args()
print("embedder:", embedding.get_embedder().name, "defaults (weak, strong):", embedding.get_embedder().thresholds)
res = {}
for label, qs in (("relevant", a.relevant), ("irrelevant", a.irrelevant)):
    res[label] = []
    for q in qs:
        hits = rag.retrieve(a.owner, q, 6)
        s = max((h.similarity for h in hits), default=0.0)
        res[label].append(s)
        print(f"{label:10s} {s:.3f}  {q}")
if res["relevant"] and res["irrelevant"]:
    lo, hi = min(res["relevant"]), max(res["irrelevant"])
    print(f"\nlowest relevant={lo:.3f}  highest irrelevant={hi:.3f}  ->",
          f"suggested RAG_SIM_WEAK≈{(lo + hi) / 2:.2f}" if lo > hi else "classes overlap: collect more examples / check notes coverage")
    print(f"suggested RAG_SIM_STRONG≈{statistics.median(res['relevant']):.2f}")
