"""Revision plan with spaced repetition.

Inputs are the signals the project already stores: graded quiz attempts (mastery) and explicit "I don't
understand" statements. Nothing is guessed from mood.

Review interval by topic state (days after the last graded attempt):
    weak 1 | needs_revision 2 | learning 3 | mastered 7 (14 once 4+ attempts) | repeated confusion 1
A topic is DUE when last_attempt + interval <= now. Topics never quizzed but flagged as confusing are due now.
Order inside "today": lowest mastery first, then most overdue.
"""
from datetime import datetime, timedelta, timezone

from app.services import gap_tracker, rag

ACTIONS = {
    "weak": "Re-read the section, ask the tutor to explain it in simple words, then retake a short quiz.",
    "needs_revision": "Take another quiz on this topic to lock it in.",
    "learning": "Take a second quiz to confirm you really know it.",
    "mastered": "Quick refresher quiz - keeps it fresh.",
    "confusion": "Ask the tutor to explain it in simple words, then try a quiz.",
}


def _interval_days(t: dict) -> int:
    if t["status"] == "mastered":
        return 14 if t["attempts"] >= 4 else 7
    if t["needs_attention"] and t["status"] in ("not_assessed", "learning"):
        return 1
    return gap_tracker.REVIEW_DAYS.get(t["status"], 1)


def _section_hint(owner: str, topic: str):
    try:
        hits = rag.retrieve(owner, topic, 1)
        if hits:
            return {"filename": hits[0].filename, "doc_id": hits[0].doc_id, "heading": hits[0].heading}
    except Exception:
        pass
    return None


def build_plan(owner: str, now: datetime = None) -> dict:
    now = now or datetime.now(timezone.utc)
    today, upcoming, mastered = [], [], 0
    for t in gap_tracker.get_topic_stats(owner):
        if t["status"] == "not_assessed" and t["confusion_signals"] == 0:
            continue
        confusion_only = t["status"] == "not_assessed"
        days = _interval_days(t) if not confusion_only else 1
        if t["last_attempt"]:
            try:
                last = datetime.fromisoformat(t["last_attempt"])
            except Exception:
                last = now - timedelta(days=days + 1)
        else:
            last = now - timedelta(days=days + 1)
        due_at = last + timedelta(days=days)
        days_until = round((due_at - now).total_seconds() / 86400, 2)
        if t["status"] == "mastered":
            mastered += 1
        if confusion_only or (t["needs_attention"] and t["status"] != "mastered"):
            reason, action = "you said you did not understand it several times" if t["confusion_signals"] >= gap_tracker.CONFUSION_FLAG_AT \
                else "you said you did not understand it", ACTIONS["confusion"]
        else:
            reason = {"weak": "low quiz results", "needs_revision": "mixed quiz results", "learning": "only one quiz so far",
                      "mastered": "scheduled refresher"}.get(t["status"], "")
            action = ACTIONS.get(t["status"], ACTIONS["needs_revision"])
        item = {"topic": t["topic"], "status": "needs_attention" if confusion_only else t["status"],
                "mastery": t["mastery"], "attempts": t["attempts"], "confusion_signals": t["confusion_signals"],
                "reason": reason, "action": action, "interval_days": days, "due_in_days": days_until,
                "overdue": days_until <= 0}
        (today if days_until <= 0 else upcoming).append(item)
    today.sort(key=lambda x: (x["mastery"] if x["mastery"] is not None else -1, x["due_in_days"]))
    upcoming.sort(key=lambda x: x["due_in_days"])
    for it in today[:6]:                                   # point to where in the notes to look (cheap, top items only)
        it["where_to_look"] = _section_hint(owner, it["topic"])
    if today:
        summary = f"{len(today)} topic(s) to revise today. Start with: {today[0]['topic']}."
    elif upcoming:
        summary = f"Nothing is due today. Next review: {upcoming[0]['topic']} in {max(1, round(upcoming[0]['due_in_days']))} day(s)."
    else:
        summary = "No revision data yet - take a quiz on a topic and your plan will appear here."
    return {"summary": summary, "today": today, "upcoming": upcoming, "mastered_count": mastered}
