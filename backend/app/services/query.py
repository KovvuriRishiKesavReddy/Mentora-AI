"""Understanding the student's message: follow-up detection, broad/enumerated requests, topic extraction.
Pure functions (no I/O) except `resolve`, which may call an injected LLM rewriter."""
import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

_NUM_WORDS = r"(?:two|three|four|five|six|seven|eight|nine|ten|\d+)"

_FOLLOWUP_CUES = re.compile(
    r"\b(it|its|it's|this|that|these|those|they|them|their|theirs|one|ones|former|latter|above|previous|earlier|"
    r"remaining|rest|next|continue|go on|carry on|more|another|else|elaborate|example|examples|why|how so|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|last|1st|2nd|3rd|4th|5th|6th)\b", re.I)
_FOLLOWUP_START = re.compile(r"^\s*(and|so|also|then|but|what about|how about|ok(ay)?[, ]|ok then)\b", re.I)
_GENERIC_ATTR = re.compile(
    r"^\s*(what|list|give|explain|tell|describe|name|show)\b[^.?!]*\b(advantages|disadvantages|types|kinds|examples?|"
    r"drawbacks|benefits|uses|applications|steps|differences|limitations|pros|cons|features|characteristics)\b[^.?!]*[.?!]?\s*$", re.I)
_HAS_OBJECT = re.compile(r"\b(of|in|for|about|between|from)\b\s+\w+", re.I)

_BROAD = re.compile(
    r"\b(all|every|each|everything|entire|whole|complete|completely|full|overview|summar(y|ise|ize)|outline|"
    r"enumerate|list|remaining|rest of|the rest|compare|comparison|differences?|versus|vs\.?|"
    r"types of|kinds of|categories|classification|advantages and disadvantages|pros and cons)\b"
    rf"|\b{_NUM_WORDS}\s+(types|kinds|categories|methods|algorithms|layers|levels|steps|stages|functions|structures|approaches)\b",
    re.I)

_TOPIC_PREFIX = re.compile(
    r"^\s*((now|ok|okay|so|and|then|next|alright|right)[,]?\s+)?(please\s+)?(can|could|would)?\s*(you\s+)?(please\s+)?"
    r"(explain|describe|define|tell me about|tell me|what is|what are|what's|whats|how does|how do|how is|why is|why does|"
    r"give me|show me|summari[sz]e|teach me|walk me through|i want to learn about|i want to know about)\s*"
    r"(me\s+)?(about\s+)?(all\s+|the\s+|an?\s+)?", re.I)
_TOPIC_SUFFIX = re.compile(r"\s*(from|in|using|based on|according to)\s+(my|the|our)\s+(uploaded\s+)?"
                           r"(notes|document|documents|pdf|file|files|material|materials)\b.*$", re.I)

_CONFUSION = re.compile(
    r"(i\s*(do not|don'?t|dont)\s+(really\s+)?(understand|get)\b|not\s+(understanding|getting)\b|"
    r"am not understanding|(still\s+)?confused|can'?t\s+understand|cannot\s+understand|"
    r"doesn'?t\s+make\s+sense|does not make sense|makes? no sense|not making sense|explain (it |that )?again|"
    r"i('m| am)\s+lost)", re.I)


def is_followup(message: str, has_history: bool = True) -> bool:
    if not has_history:
        return False
    m = message.strip()
    words = m.split()
    if len(words) <= 2:
        return True
    if _FOLLOWUP_START.match(m) or _FOLLOWUP_CUES.search(m):
        return True
    if _GENERIC_ATTR.match(m) and not _HAS_OBJECT.search(m):
        return True
    return False


def is_broad(message: str) -> bool:
    return bool(_BROAD.search(message))


def is_confusion_statement(message: str) -> bool:
    """Explicit statements of not understanding only. Plain questions ('what is X'), mood labels and
    frustration are NOT evidence of a knowledge gap."""
    return bool(_CONFUSION.search(message))


_CONF_PREFIX = re.compile(
    r"^\s*(i\s*(am|'m|’m)?\s*(still\s+)?(do not|don'?t|dont|can'?t|cannot)\s+(really\s+)?(understand|get)|"
    r"i\s*(am|'m|’m)?\s*(still\s+)?(confused|lost|not understanding|not getting)|"
    r"(i\s+)?(still\s+)?(do not|don'?t)\s+(really\s+)?(understand|get))\s*(about|by|on|with|how|why|what|the|this|that)?\s*", re.I)


_BARE_REFERENCES = {"", "it", "this", "that", "these", "those", "them", "all", "any of it", "any of this", "anything"}


def extract_topic(message: str) -> str:
    t = message.strip().rstrip("?.! ")
    t = _CONF_PREFIX.sub("", t)
    t = _TOPIC_SUFFIX.sub("", t)
    t = re.sub(r"\s+(at all|completely|properly|very well|really|yet|too)$", "", t, flags=re.I)
    t = _TOPIC_PREFIX.sub("", t).strip(" ,.:;")
    words = t.split()
    if not words or len(words) > 10:
        return ""
    return " ".join(words)


@dataclass
class Resolved:
    standalone: str
    topic: str
    followup: bool
    topic_changed: bool
    rewritten_by: str = "none"      # none | llm | heuristic


def _history_tail(history: list, n: int = 4, cap: int = 900) -> str:
    out = []
    for m in history[-n:]:
        out.append(f"{m['role'].upper()}: {m['content'][:cap]}")
    return "\n".join(out)


def _parse_rewrite(raw: str) -> Optional[dict]:
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    s = str(d.get("standalone", "")).strip()
    if not s or len(s) > 400:
        return None
    return {"standalone": s, "topic": str(d.get("topic", "")).strip()[:100]}


def rewrite_with_llm(message: str, history: list, active_topic: str, llm_fn: Callable) -> Optional[dict]:
    prompt = (
        "Rewrite the student's LAST message as one standalone question that needs no conversation context. "
        "Resolve pronouns and references like 'it', 'the second one', 'remaining types', 'that', 'example' using the "
        "conversation. Do not answer it, and do not add opinions or feelings. Keep the subject the student is actually "
        "asking about; if the last message clearly starts a NEW subject, keep it as is. If the last message says the "
        "student did not understand ('I don't understand it', 'confusing'), rewrite it as: 'Explain <what the assistant "
        "just explained> again in simpler words.'\n"
        'Reply ONLY with JSON: {"standalone": "...", "topic": "<2-6 word subject>"}\n\n'
        f"CURRENT TOPIC: {active_topic or 'unknown'}\n\nCONVERSATION:\n{_history_tail(history)}\n\n"
        f"LAST MESSAGE: {message}")
    try:
        raw = llm_fn("You rewrite questions. Output JSON only.", [{"role": "user", "content": prompt}], 160)
    except Exception:
        return None
    return _parse_rewrite(raw)


def resolve(message: str, history: list, active_topic: str,
            llm_fn: Optional[Callable] = None) -> Resolved:
    # Bare "I don't understand it / this / that": the student means the TOPIC being discussed, not whatever
    # sub-detail (an example, one algorithm) happened to be in the last answer. Anchor to the active topic.
    if history and active_topic and is_confusion_statement(message) \
            and extract_topic(message).lower() in _BARE_REFERENCES:
        return Resolved(f"Explain {active_topic} in simple words, from the basics.", active_topic, True, False, "heuristic")
    followup = is_followup(message, has_history=bool(history))
    if not followup:
        topic = extract_topic(message) or active_topic
        return Resolved(message, topic, False, bool(topic) and topic.lower() != (active_topic or "").lower())
    if llm_fn is not None:
        rw = rewrite_with_llm(message, history, active_topic, llm_fn)
        if rw:
            topic = rw["topic"] or active_topic
            return Resolved(rw["standalone"], topic, True,
                            bool(topic) and bool(active_topic) and topic.lower() != active_topic.lower(), "llm")
    # heuristic fallback: anchor the follow-up to the active topic
    anchor = active_topic or ""
    standalone = f"{message.strip()} (regarding: {anchor})" if anchor else message
    return Resolved(standalone, anchor, True, False, "heuristic")
