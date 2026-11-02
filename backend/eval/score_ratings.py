"""Compute answer accuracy from the manually rated CSV.   python eval/score_ratings.py"""
import csv, sys
from pathlib import Path
p = Path(__file__).resolve().parent / "answers_to_rate_AI_judged.csv"
rs = [r for r in csv.DictReader(open(p, encoding="utf-8-sig")) if r["rating"].strip() != ""]
if not rs: sys.exit("No ratings filled in yet.")
v = [float(r["rating"]) for r in rs]
print(f"rated {len(v)} answers")
print(f"fully correct (1): {sum(x == 1 for x in v)}/{len(v)} = {100*sum(x == 1 for x in v)/len(v):.0f}%")
print(f"mean score: {100*sum(v)/len(v):.0f}%   wrong or invented (0): {sum(x == 0 for x in v)}")
