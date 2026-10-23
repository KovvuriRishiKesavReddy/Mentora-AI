"""One tutoring turn: resolve the question in context -> retrieve -> build prompt -> call LLM -> persist.

Framework-free on purpose (raises ApiError) so it can be tested without a web server."""
from app.errors import ApiError
from app.services import gap_tracker, llm, memory, query, rag
from app.services.llm import LLMError
from app.services.mood import AudioFeatures, BehaviouralSignals, classify_mood

_LLM_STATUS = {"no_key": 503, "config": 503, "auth": 503, "rate_limit": 429, "timeout": 504}


def llm_error_to_api(e: LLMError) -> ApiError:
    return ApiError(_LLM_STATUS.get(e.kind, 502), e.message, f"llm_{e.kind}")


def handle_chat(owner: str, session_id: str, message: str, response_latency_ms: float = 0.0,
                backspace_count: int = 0, attempt_number: int = 1, audio_features: dict = None) -> dict:
    message = message.strip()
    if not message:
        raise ApiError(400, "Message is empty.", "empty_message")
    session = memory.get_session(owner, session_id)                    # 404 for unknown / foreign sessions

    # 1. context BEFORE saving the new message -> it can never be duplicated in the history window
    history, summary = memory.get_context(session)

    # 2. mood (presentation only)
    audio = None
    if audio_features:
        try:
            audio = AudioFeatures(**audio_features)
        except Exception:
            audio = None
    mood = classify_mood(message, BehaviouralSignals(response_latency_ms, backspace_count, attempt_number), audio)
    tone = llm.get_tone_profile(mood)

    # 3. understand what the student means ("its types", "the second one", "continue") and what is being asked
    resolved = query.resolve(message, history, session["active_topic"], llm_fn=llm.call_llm)
    broad = query.is_broad(message) or (resolved.followup and query.is_broad(resolved.standalone))

    # 4. retrieve evidence for the *resolved* question, scoped to this owner's documents
    ev = rag.gather(owner, resolved.standalone, broad=broad)

    # 5. prompt + LLM
    clarify = bool(history) and query.is_confusion_statement(message)
    system_prompt = llm.build_system_prompt(tone, topic=resolved.topic, grounding=ev.grounding, broad=broad,
                                            truncated=ev.truncated, summary=summary, clarify=clarify)
    interp = (f"(This message was interpreted as: {resolved.standalone})"
              if resolved.followup and resolved.rewritten_by == "llm" else "")
    if ev.context or interp:
        user_content = "\n\n".join(p for p in (ev.context, f"STUDENT MESSAGE: {message}", interp) if p)
    else:
        user_content = message
    try:
        answer = llm.call_llm(system_prompt, history + [{"role": "user", "content": user_content}])
    except LLMError as e:
        raise llm_error_to_api(e)                                       # nothing persisted -> no corrupted history

    # 6. persist atomically
    tone_dict = {"frustration_level": tone.frustration_level, "confidence_boost_needed": tone.confidence_boost_needed,
                 "pace_slowdown": tone.pace_slowdown, "encouragement_intensity": tone.encouragement_intensity}
    memory.add_exchange(session_id, message, answer, mood.mood_label, tone_dict, ev.sources, ev.grounding,
                        active_topic=resolved.topic or None)

    # 7. explicit confusion is its own signal; it never touches quiz scores or mastery
    if query.is_confusion_statement(message):
        topic = resolved.topic or session["active_topic"]
        if topic:
            try:
                gap_tracker.record_confusion(owner, session_id, topic)
            except Exception as e:
                print(f"[chat] confusion signal not stored: {type(e).__name__}")
    memory.summarize_if_long(session_id)

    return {
        "session_id": session_id,
        "response": answer,
        "mood": {"label": mood.mood_label, "confidence": mood.confidence_score,
                 "frustration_score": mood.frustration_score, "confusion_score": mood.confusion_score,
                 "engagement_score": mood.engagement_score, "emoji": mood.emoji, "color": mood.color,
                 "raw_signals": mood.raw_signals},
        "tone_explanation": llm.get_tone_explanation(mood, tone),
        "teaching_strategy": llm.get_teaching_strategy(mood),
        "tone_profile": tone_dict,
        "active_topic": resolved.topic,
        "interpreted_as": resolved.standalone if resolved.followup else None,
        "retrieval": {"is_grounded": ev.grounding != "none", "grounding": ev.grounding,
                      "truncated": ev.truncated, "sources": ev.sources},
    }
