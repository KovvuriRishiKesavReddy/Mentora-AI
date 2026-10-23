"""Groq client + prompt construction.

Prompt design: one fixed layer of *grounding & conversation rules* (facts, coverage, follow-ups) followed by a
*presentation* layer driven by detected mood. Mood may change tone, pacing and depth of scaffolding — never the
facts, the coverage, or the subject."""
import os
import re
from dataclasses import dataclass

from app import config
from app.services.mood import MoodResult


class LLMError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


@dataclass
class ToneProfile:
    frustration_level: float = 0.0
    confidence_boost_needed: float = 0.0
    pace_slowdown: float = 0.0
    encouragement_intensity: float = 0.5


MOOD_TONE_MAP = {
    "frustrated": ToneProfile(0.9, 0.7, 0.8, 0.9),
    "confused":   ToneProfile(0.3, 0.8, 0.9, 0.6),
    "confident":  ToneProfile(0.0, 0.1, 0.0, 0.2),
    "bored":      ToneProfile(0.1, 0.2, 0.0, 0.4),
    "neutral":    ToneProfile(0.2, 0.4, 0.3, 0.5),
}


def get_tone_profile(mood: MoodResult) -> ToneProfile:
    return MOOD_TONE_MAP.get(mood.mood_label, MOOD_TONE_MAP["neutral"])


TEACHING_STRATEGIES = {
    "frustrated": ("Calm and short", "One warm sentence, then short numbered steps. No long explanations."),
    "confused":   ("Simplify", "Plain words, one small example, go step by step, check understanding."),
    "confident":  ("Stretch", "Peer-level wording, skip basics, end with a harder follow-up."),
    "bored":      ("Challenge", "Skip what you already know, go deeper, end with a harder application question."),
    "neutral":    ("Balanced", "Clear explanation with a short example when it helps."),
}


def get_teaching_strategy(mood: MoodResult) -> dict:
    label, detail = TEACHING_STRATEGIES.get(mood.mood_label, TEACHING_STRATEGIES["neutral"])
    return {"label": label, "detail": detail}


def get_tone_explanation(mood: MoodResult, tone: ToneProfile) -> str:
    emoji = config.MOOD_EMOJI.get(mood.mood_label, "🙂")
    conf = int(mood.confidence_score * 100)
    parts = []
    if tone.frustration_level > 0.7:      parts.append("empathy-first")
    if tone.pace_slowdown > 0.7:          parts.append("slowed pace")
    if tone.confidence_boost_needed > 0.6: parts.append("plain wording")
    if tone.encouragement_intensity > 0.7: parts.append("encouragement")
    if tone.confidence_boost_needed < 0.2: parts.append("peer-level+challenge")
    return f"{emoji} Detected: {mood.mood_label.capitalize()} ({conf}%) → {', '.join(parts) or 'balanced'}"


_CORE_RULES = """You are MentoraAI, a patient, accurate tutor for a student's own uploaded notes.

GROUNDING
- The notes are provided below as numbered sources, each a separate document section. Never merge facts from different sources unless the question asks you to compare them, and never present something as "in your notes" unless a source actually says it.
- Do not invent facts, page numbers or sources. If the sources are silent on part of the question, say so plainly for that part only.
- Refer to sources by number (for example "Source 2") when it helps the student find the material.
- Never invent specific numbers, timings or figures and attribute them to the notes. If a worked example needs made-up values, keep it tiny and label it "illustration (not from your notes)". Do not add outside facts (for example "real systems use 10-100 ms") unless you label them as general knowledge.

COVERAGE
- When the student asks for all / every / the types / a list / an overview, cover EVERY item the sources contain — do not stop at a few. If the notes list N items, present all N, in the notes' order, each with its key points from the notes (definition, characteristics, advantages/disadvantages, examples where given).
- If the retrieved context was cut for length, say which part is not yet covered and offer to continue.

CONVERSATION
- Use the conversation history ONLY to understand what the student refers to ("it", "the second one", "the remaining types", "an example of that"). The student's latest message decides what to answer. If it clearly starts a new subject, switch to it.
- Do not repeat explanations you already gave; build on them. Do not re-define terms the student did not ask about.

STYLE
- Write naturally, like a good tutor. No filler openers ("Great question!"), no repeated disclaimers, no gloss after every technical term. Explain a term only when it is likely new to this student.
- Be exactly as detailed as the question needs — do not oversimplify technical content."""

_GROUNDING_TEXT = {
    "strong": "EVIDENCE STATUS: The notes below contain relevant material. Answer from them.",
    "weak": ("EVIDENCE STATUS: The notes below are only loosely related. Use them only where they genuinely answer the question. "
             "If they do not, say in one short sentence that the notes do not cover this, then IMMEDIATELY give a brief answer from "
             "general knowledge, labelled as general knowledge. Do not ask permission or offer to explain later."),
    "none": ("EVIDENCE STATUS: No relevant passage was found in the student's notes. Say once, in one short natural sentence, "
             "that this is not covered in their notes (do not use a fixed template, and do not repeat this if you already said it "
             "earlier in this conversation), then answer from general knowledge, clearly labelled as such."),
}


def _mood_block(tone: ToneProfile) -> str:
    lines = []
    if tone.frustration_level > 0.7:
        lines.append("The student seems frustrated: you may open with ONE brief, warm acknowledgement, then go straight to a clear answer. Use short steps.")
    elif tone.frustration_level > 0.3:
        lines.append("Keep a calm, reassuring tone.")
    if tone.pace_slowdown > 0.7:
        lines.append("Go step by step and use one concrete example if it helps.")
    elif tone.pace_slowdown > 0.3:
        lines.append("Keep it clearly structured; one example if useful.")
    else:
        lines.append("Be concise; technical vocabulary is fine. You may end with one short extension question.")
    if tone.confidence_boost_needed <= 0.2:
        lines.append("The student is comfortable with the material: skip the basics and end with ONE harder application "
                     "question based on the notes.")
    if tone.confidence_boost_needed > 0.6:
        lines.append("Prefer plain wording for new terms (explain a term once, the first time it appears).")
    if tone.encouragement_intensity > 0.7:
        lines.append("You may close with one specific, genuine word of encouragement.")
    return ("PRESENTATION (tone and pacing only — never change facts, coverage or subject because of mood):\n- "
            + "\n- ".join(lines))


_CLARIFY_RULES = """CLARIFICATION MODE: the student said they did not understand your previous explanation.
- Re-explain the ACTIVE TOPIC from the basics (what it is and why it exists), in a DIFFERENT and SIMPLER way. Do not just repeat the last detail or example from your previous answer, unless the student pointed at that specific part (then re-explain only that part). Do not add new sub-topics.
- Keep it short (about 120 words, at most 150). Plain everyday words, short sentences. No tables, no headings, no long lists.
- Start from the single core idea in one sentence, then give one small everyday analogy or a very small example (label it as an illustration if it is not from the notes), then one sentence tying it back to the notes.
- End with one short question checking which part is still unclear."""


def build_system_prompt(tone: ToneProfile, topic: str = "", grounding: str = "none", broad: bool = False,
                        truncated: bool = False, summary: str = "", clarify: bool = False) -> str:
    parts = [_CORE_RULES, _GROUNDING_TEXT.get(grounding, _GROUNDING_TEXT["none"])]
    if clarify:
        parts.append(_CLARIFY_RULES)
    if broad and grounding != "none":
        parts.append("This is a broad / enumerated request: be complete.")
    if truncated:
        parts.append("NOTE: the retrieved notes were cut to fit the context limit; some sections may be missing.")
    parts.append(_mood_block(tone))
    if topic:
        parts.append(f"ACTIVE TOPIC OF THIS CONVERSATION: {topic}")
    if summary:
        parts.append(f"EARLIER IN THIS CONVERSATION (summary): {summary}")
    return "\n\n".join(parts)


# ── Groq client ───────────────────────────────────────────────────────────────
_client = None
_test_backend = None          # tests inject a fake: fn(system, messages, max_tokens) -> str


def set_backend(fn) -> None:
    global _test_backend
    _test_backend = fn


def get_client():
    global _client
    if _client is None:
        key = os.environ.get("GROQ_API_KEY", "")
        if not key:
            raise LLMError("no_key", "GROQ_API_KEY is not set. Copy .env.example to .env and add your key.")
        try:
            from groq import Groq
        except ImportError:
            raise LLMError("config", "The 'groq' package is not installed. Run: pip install groq")
        _client = Groq(api_key=key, timeout=config.LLM_TIMEOUT_S, max_retries=1)
    return _client


def _safe_detail(exc: Exception) -> str:
    """Upstream error text with any key-looking token removed (never contains our prompt/messages)."""
    import re
    return re.sub(r"gsk_[A-Za-z0-9]+", "gsk_***", str(exc))[:240]


def _classify(exc: Exception) -> LLMError:
    name, msg = type(exc).__name__, str(exc)
    low = msg.lower()
    if "timeout" in name.lower() or "timed out" in low:
        return LLMError("timeout", "The language model took too long to respond. Please try again.")
    if "ratelimit" in name.lower() or "rate_limit" in low or "429" in low:
        return LLMError("rate_limit", "The language model is rate-limited right now. Wait a minute and try again.")
    if "authentication" in name.lower() or "invalid api key" in low or "401" in low:
        return LLMError("auth", "The Groq API key was rejected. Check GROQ_API_KEY in your .env file.")
    if "decommission" in low or "model_not_found" in low or "does not exist" in low or "not found" in low or "model_terms" in low:
        return LLMError("model", "The configured Groq model is unavailable.")
    if "connection" in name.lower() or "connect" in low:
        return LLMError("network", "Could not reach the language model service. Check your internet connection.")
    return LLMError("upstream", "The language model returned an error. Please try again.")


_discovered = None


def _discover_models(client) -> list:
    """Ask Groq which models this key can use and keep the chat-capable ones (best first)."""
    global _discovered
    if _discovered is not None:
        return _discovered
    try:
        ids = [m.id for m in client.models.list().data]
    except Exception as e:
        print(f"[llm] could not list models: {type(e).__name__}")
        ids = []
    skip = ("whisper", "guard", "tts", "embed", "orpheus", "playai", "safeguard", "prompt-guard")
    chat = [i for i in ids if not any(b in i.lower() for b in skip)]
    def rank(i):
        l = i.lower()
        return (0 if "gpt-oss-120b" in l else 1 if "gpt-oss" in l else 2 if "llama" in l else 3, i)
    chat.sort(key=rank)
    _discovered = chat[:4]
    print(f"[llm] models available to this key (chat): {_discovered}")
    return _discovered


def _try_model(client, model: str, payload: list, max_tokens: int) -> str:
    kwargs = {}
    if "gpt-oss" in model.lower():          # reasoning models: keep thinking short and leave room for the answer
        kwargs["extra_body"] = {"reasoning_effort": config.REASONING_EFFORT}
        max_tokens += config.REASONING_HEADROOM
    r = client.chat.completions.create(model=model, messages=payload, max_tokens=max_tokens, temperature=0.4, **kwargs)
    text = (r.choices[0].message.content or "").strip() if r.choices else ""
    if not text:
        raise LLMError("empty", "The language model returned an empty answer. Please try again.")
    return text


def call_llm(system_prompt: str, messages: list, max_tokens: int = None) -> str:
    if _test_backend is not None:
        return _test_backend(system_prompt, messages, max_tokens)
    max_tokens = max_tokens or config.LLM_MAX_TOKENS
    client = get_client()
    payload = [{"role": "system", "content": system_prompt}] + messages
    tried, last = [], None

    def attempt(model):
        nonlocal last
        tried.append(model)
        try:
            text = _try_model(client, model, payload, max_tokens)
            if model != config.GROQ_MODEL:
                print(f"[llm] answered with fallback model {model}")
            return text
        except LLMError as e:
            last = e
        except Exception as e:                    # never log message content, only the upstream error text
            last = _classify(e)
            print(f"[llm] model={model} failed: {last.kind} | {_safe_detail(e)}")
        return None

    for model in [config.GROQ_MODEL] + [m for m in config.GROQ_FALLBACK_MODELS if m != config.GROQ_MODEL]:
        text = attempt(model)
        if text:
            return text
        if last.kind not in ("model", "rate_limit", "upstream", "empty"):
            raise last                            # auth / timeout / network: another model will not help
    if last is not None and last.kind == "model":  # configured names are gone: use what the account really offers
        for model in _discover_models(client):
            if model in tried:
                continue
            text = attempt(model)
            if text:
                return text
        raise LLMError("model", "None of the configured Groq models are available to your key (tried: "
                                + ", ".join(tried) + "). Run `python scripts/check_groq.py` in the backend folder to see "
                                "the models your key can use, then set GROQ_MODEL in .env.")
    raise last or LLMError("upstream", "No language model available.")


# Backwards-compatible aliases
call_claude = call_llm


def summarise(transcript: str) -> str:
    prompt = ("Summarise this tutoring conversation in 3-5 sentences: topics covered, what the student understood, "
              "what they found hard.\n\nCONVERSATION:\n" + transcript)
    return call_llm("You are a concise summariser.", [{"role": "user", "content": prompt}], 300)
