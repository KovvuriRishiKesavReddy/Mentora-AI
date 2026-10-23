"""Evaluation harness for MentoraAI.

Run from the backend folder:

    python eval/run_eval.py                      # retrieval + follow-up + out-of-scope checks (no API key needed)
    python eval/run_eval.py --mode answers       # also asks the real model and checks the answers (needs GROQ_API_KEY)
    python eval/run_eval.py --notes "../uploads/OS Complete notes.docx" --out eval/REPORT.md

What is measured (the question set is eval/questions.json; every keyword in it is checked against the notes
by `--validate`, so the "ground truth" is the notes themselves):

  retrieval   for each question, does one of the returned sources come from the expected section (hit rate),
              is the best source the expected one (top-1), and what share of the expected keywords is present
              in the context handed to the model.
  follow-ups  multi-turn sequences ("What are its advantages?", "I don't understand it."): does each turn retrieve
              from the expected chapter?
  out-of-scope  questions the notes do not cover: the tutor must NOT claim strong grounding.
  answers     (mode answers) keyword share in the real answer, "Source N" citations that do not exist,
              and for out-of-scope questions whether the answer admits the notes do not cover it.

The run uses a throw-away database and an in-memory vector store, so it never touches your real data or uploads.
The report states which embedder and which model were used - a run on the offline hashing fallback is a lower bound.
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app import chat_service, config, db                       # noqa: E402
from app.security import limiter                                # noqa: E402
from app.services import embedding, llm, memory, rag, vectorstore  # noqa: E402

OWNER = "e" * 32
NOT_COVERED = re.compile(r"(not (covered|in your notes|found|mentioned)|doesn'?t (cover|mention)|do(es)? not (cover|contain|mention)|"
                         r"no (mention|information|relevant)|outside (of )?(your|the) notes|general knowledge)", re.I)


def load_questions(path=None):
    return json.loads(Path(path or HERE / "questions.json").read_text(encoding="utf-8"))


def default_notes():
    for p in [HERE.parent.parent / "uploads" / "OS Complete notes.docx", HERE.parent / "tests" / "os_notes.docx"]:
        if p.exists():
            return p
    raise SystemExit("No notes file found. Pass --notes <file>.")


def section_texts(notes: Path) -> dict:
    blocks, _ = rag.parse_file(notes, notes.suffix.lower())
    out = {}
    for c in rag.build_chunks(blocks):
        out[c.heading] = out.get(c.heading, "") + " " + c.text
    return out


def validate(notes: Path, qs: dict) -> list:
    """Every expected section must exist and contain every expected keyword. Returns a list of problems."""
    texts = section_texts(notes)
    problems = []
    for q in qs["direct"]:
        heads = [h for h in texts if q["section"].lower() in h.lower()]
        if not heads:
            problems.append(f"{q['id']}: no section matching '{q['section']}'")
            continue
        blob = " ".join(texts[h] for h in heads).lower()
        for k in q["keywords"]:
            if k.lower() not in blob:
                problems.append(f"{q['id']}: keyword '{k}' not found in section '{q['section']}'")
    return problems


def setup(notes: Path, embedder: str):
    tmp = Path(tempfile.mkdtemp(prefix="mentora_eval_"))
    config.UPLOAD_DIR, config.DATA_DIR = tmp / "uploads", tmp / "data"
    config.UPLOAD_DIR.mkdir()
    db.configure(tmp / "data" / "eval.db")
    if embedder == "hashing":
        embedding.set_embedder(embedding.HashingEmbedder())
    vectorstore.set_store(vectorstore.MemoryStore())
    limiter.reset()
    dst = rag.owner_dir(OWNER)
    dst.mkdir(parents=True, exist_ok=True)
    p = dst / ("eval_" + notes.name)
    shutil.copy(notes, p)
    rag.ingest_file(OWNER, p, notes.name)
    return tmp


def _stub(system, messages, max_tokens=None):
    if system.startswith("You rewrite questions"):
        return "not json"                         # forces the deterministic heuristic follow-up anchoring
    return "(stub answer - retrieval-only run)"


def _hit(sources, section):
    return [i for i, s in enumerate(sources) if section.lower() in s["chapter"].lower()]


def run(notes: Path, qs: dict, mode: str, embedder: str) -> dict:
    tmp = setup(notes, embedder)
    live = mode == "answers"
    llm.set_backend(None if live else _stub)
    emb = embedding.get_embedder()
    res = {"direct": [], "followups": [], "oos": [], "embedder": emb.name, "semantic": bool(emb.semantic),
           "model": config.GROQ_MODEL if live else "(none - retrieval only)", "mode": mode, "notes": notes.name}
    try:
        for q in qs["direct"]:
            ev = rag.gather(OWNER, q["question"])
            hits = _hit(ev.sources, q["section"])
            ctx = ev.context.lower()
            kw = sum(k.lower() in ctx for k in q["keywords"]) / len(q["keywords"])
            row = {"id": q["id"], "question": q["question"], "section": q["section"], "hit": bool(hits),
                   "top1": bool(hits) and hits[0] == 0, "kw_context": kw, "grounding": ev.grounding}
            if live:
                sid = memory.create_session(OWNER)
                r = chat_service.handle_chat(OWNER, sid, q["question"])
                ans = r["response"].lower()
                row["kw_answer"] = sum(k.lower() in ans for k in q["keywords"]) / len(q["keywords"])
                cited = [int(n) for n in re.findall(r"source\s+(\d+)", ans)]
                row["bad_citation"] = any(n > len(r["retrieval"]["sources"]) or n < 1 for n in cited)
            res["direct"].append(row)
        for f in qs["followups"]:
            sid = memory.create_session(OWNER)
            turns = []
            for t in f["turns"]:
                r = chat_service.handle_chat(OWNER, sid, t["message"])
                chap = [s["chapter"] for s in r["retrieval"]["sources"]]
                ok = any(t["section"].lower() in c.lower() for c in chap)
                turns.append({"message": t["message"], "ok": ok, "first_source": chap[0] if chap else "(none)",
                              "interpreted_as": r.get("interpreted_as"), "topic": r["active_topic"]})
            res["followups"].append({"id": f["id"], "name": f["name"], "turns": turns})
        for o in qs["out_of_scope"]:
            ev = rag.gather(OWNER, o["question"])
            row = {"id": o["id"], "question": o["question"], "grounding": ev.grounding, "ok": ev.grounding != "strong",
                   "clean": ev.grounding == "none"}
            if live:
                sid = memory.create_session(OWNER)
                r = chat_service.handle_chat(OWNER, sid, o["question"])
                row["admits"] = bool(NOT_COVERED.search(r["response"]))
            res["oos"].append(row)
    finally:
        llm.set_backend(None)
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "n/a"


def summarise(res: dict) -> dict:
    d, f, o = res["direct"], res["followups"], res["oos"]
    turns = [t for x in f for t in x["turns"]]
    s = {"n_direct": len(d), "hit": sum(r["hit"] for r in d), "top1": sum(r["top1"] for r in d),
         "kw_context": sum(r["kw_context"] for r in d) / max(1, len(d)),
         "n_turns": len(turns), "turns_ok": sum(t["ok"] for t in turns),
         "n_oos": len(o), "oos_ok": sum(r["ok"] for r in o), "oos_clean": sum(r["clean"] for r in o)}
    if res["mode"] == "answers":
        s["kw_answer"] = sum(r.get("kw_answer", 0) for r in d) / max(1, len(d))
        s["bad_citations"] = sum(r.get("bad_citation", False) for r in d)
        s["oos_admits"] = sum(r.get("admits", False) for r in o)
    return s


def report(res: dict, s: dict) -> str:
    L = ["# MentoraAI evaluation report", "",
         f"- Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
         f"- Notes: {res['notes']}",
         f"- Embedder: {res['embedder']} ({'semantic' if res['semantic'] else 'OFFLINE HASHING FALLBACK - lexical only; real semantic results are expected to be equal or better'})",
         f"- Model: {res['model']}", f"- Mode: {res['mode']}", "",
         "## Summary", "", "| Check | Result |", "|---|---|",
         f"| Retrieval hit rate (expected section among sources) | {s['hit']}/{s['n_direct']} ({pct(s['hit'], s['n_direct'])}) |",
         f"| Best source is the expected section (top-1) | {s['top1']}/{s['n_direct']} ({pct(s['top1'], s['n_direct'])}) |",
         f"| Expected keywords present in retrieved context | {100 * s['kw_context']:.0f}% |",
         f"| Follow-up turns retrieved from the right chapter | {s['turns_ok']}/{s['n_turns']} ({pct(s['turns_ok'], s['n_turns'])}) |",
         f"| Out-of-scope questions NOT strongly grounded | {s['oos_ok']}/{s['n_oos']} ({pct(s['oos_ok'], s['n_oos'])}) |",
         f"| Out-of-scope questions with no evidence at all | {s['oos_clean']}/{s['n_oos']} ({pct(s['oos_clean'], s['n_oos'])}) |"]
    if res["mode"] == "answers":
        L += [f"| Expected keywords present in the model's answer | {100 * s['kw_answer']:.0f}% |",
              f"| Answers citing a source number that does not exist | {s['bad_citations']} |",
              f"| Out-of-scope answers that admit the notes do not cover it | {s['oos_admits']}/{s['n_oos']} |"]
    else:
        L += ["", "_Answer-level checks were NOT RUN (no model used). Re-run with `--mode answers` and a Groq key._"]
    L += ["", "## Direct questions", "", "| ID | Question | Hit | Top-1 | Keywords in context |", "|---|---|---|---|---|"]
    for r in res["direct"]:
        L.append(f"| {r['id']} | {r['question']} | {'yes' if r['hit'] else '**NO**'} | {'yes' if r['top1'] else 'no'} | {100 * r['kw_context']:.0f}% |")
    L += ["", "## Follow-up sequences", ""]
    for f in res["followups"]:
        L += [f"**{f['id']} - {f['name']}**", "", "| Message | Right chapter | Understood as |", "|---|---|---|"]
        for t in f["turns"]:
            L.append(f"| {t['message']} | {'yes' if t['ok'] else '**NO**'} ({t['first_source']}) | {t['interpreted_as'] or '-'} |")
        L.append("")
    L += ["## Out-of-scope questions", "", "| ID | Question | Grounding | OK |", "|---|---|---|---|"]
    for r in res["oos"]:
        L.append(f"| {r['id']} | {r['question']} | {r['grounding']} | {'yes' if r['ok'] else '**NO**'} |")
    fails = [r["id"] for r in res["direct"] if not r["hit"]]
    L += ["", "## Failures", "", ("Retrieval misses: " + ", ".join(fails)) if fails else "No retrieval misses."]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notes", type=Path)
    ap.add_argument("--questions", type=Path)
    ap.add_argument("--mode", choices=["retrieval", "answers"], default="retrieval")
    ap.add_argument("--embedder", choices=["auto", "hashing"], default="auto")
    ap.add_argument("--out", type=Path, default=HERE / "REPORT.md")
    ap.add_argument("--validate", action="store_true", help="only check that the question set matches the notes")
    a = ap.parse_args()
    notes = a.notes or default_notes()
    qs = load_questions(a.questions)
    problems = validate(notes, qs)
    if problems:
        print("Question set does not match the notes:\n  " + "\n  ".join(problems))
        if a.validate:
            sys.exit(1)
    elif a.validate:
        print(f"OK: {len(qs['direct'])} questions match {notes.name}")
        return
    if a.mode == "answers":
        import os
        if not os.environ.get("GROQ_API_KEY"):
            try:
                from dotenv import load_dotenv
                load_dotenv(HERE.parent.parent / ".env")
            except Exception:
                pass
        if not os.environ.get("GROQ_API_KEY"):
            sys.exit("GROQ_API_KEY is not set (put it in .env) - cannot run --mode answers.")
    res = run(notes, qs, a.mode, a.embedder)
    s = summarise(res)
    a.out.write_text(report(res, s), encoding="utf-8")
    print(f"embedder={res['embedder']} semantic={res['semantic']}")
    print(f"retrieval hit {s['hit']}/{s['n_direct']}  top1 {s['top1']}/{s['n_direct']}  kw-in-context {100 * s['kw_context']:.0f}%")
    print(f"follow-ups {s['turns_ok']}/{s['n_turns']}   out-of-scope ok {s['oos_ok']}/{s['n_oos']}")
    print(f"report written to {a.out}")


if __name__ == "__main__":
    main()
