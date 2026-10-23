import re
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
from app.config import MOOD_COLORS, MOOD_EMOJI

MOOD_REFS = {
    "frustrated":[
        "I give up this is impossible","I hate this nothing makes sense",
        "I keep getting it wrong and failing","I am so frustrated I want to stop",
        "Why is this so hard I cannot do it","I have tried so many times still failing",
        "This is pointless I will never understand it","I am losing my mind over this",
        "your explanation is terrible and not helping me",
        "this is the worst explanation I have ever heard",
        "you are confusing me even more","nothing you say makes sense to me",
    ],
    "confused":[
        "I do not understand this concept at all",
        "Can you explain that again more clearly please",
        "I am confused about how this works",
        "Could you clarify this part for me",
        "I am not sure I follow the logic here",
        "What does this mean I do not get it",
        "I partially understand but not completely",
        "I am lost on this topic","not making sense to me",
        "still not understanding this","not able to understand",
    ],
    "confident":[
        "I understand this now it makes sense","Got it that is very clear thank you",
        "I know the answer to this question","This is straightforward and I get it",
        "Makes total sense now I understand","I am confident about this concept",
        "Yes I understand let me try the next one","I figured it out",
    ],
    "bored":[
        "This is too easy I already know this","Can we move on to something more challenging",
        "This is boring I want harder material","I already mastered this topic completely",
        "Whatever just give me the next topic","This is too basic for my level",
    ],
    "neutral":[
        "What is the definition of this term","Explain how this process works step by step",
        "Tell me about this topic","What are the main points of this concept",
        "Can you give me an example of this","What should I know about this subject",
        "Summarise this concept for me","How does this work","What is this",
    ],
}

IMMEDIATE_FRUSTRATED = [
    "worst","terrible","bad explanation","not helping","useless","give up",
    "i hate","hate this","so bad","pathetic","horrible","not understanding at all",
    "making it worse","confusing me more","this sucks","you are wrong",
    "wrong explanation","bad answer","so frustrated","am frustrated",
    "i am frustrated","very frustrated","really frustrated","getting frustrated",
]
IMMEDIATE_CONFUSED = [
    "i don't understand","i dont understand","not understanding",
    "am not understanding","still confused","still not getting","not getting it",
    "makes no sense","doesn't make sense","explain again","can't understand",
    "cannot understand","not able to understand","unable to understand",
]
IMMEDIATE_CONFIDENT = [
    "i understand now","i get it now","got it","makes sense now",
    "now i understand","i understand","clear now","i see now",
]
IMMEDIATE_BORED = ["too easy","i know this","already know","boring","too simple"]   # "move on"/"next topic" removed: they are normal navigation phrases

MIN_SEMANTIC_CONFIDENCE = 0.6
_embedder = None
_ref_embs = None

def _load_embedder():
    """Reuse the single shared embedder. If only the weak hashing fallback is available, mood detection
    runs on keywords alone (semantic reference matching would be meaningless)."""
    global _embedder
    if _embedder is None:
        try:
            from app.services.embedding import get_embedder
            e = get_embedder()
            _embedder = e if getattr(e, "semantic", False) else "unavailable"
            if _embedder == "unavailable":
                print("[mood] semantic embedder unavailable - keyword-only mood detection")
        except Exception as e:
            print(f"[mood] keyword-only: {type(e).__name__}")
            _embedder = "unavailable"
    return None if _embedder == "unavailable" else _embedder

def _has(text, phrases):
    """Whole-word / whole-phrase match (so 'worst-case' or 'got it wrong' do not trigger moods)."""
    return any(re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", text) for p in phrases)

def _load_refs():
    global _ref_embs
    if _ref_embs is not None: return _ref_embs
    emb = _load_embedder()
    if emb is None: return None
    _ref_embs = {}
    for mood,sents in MOOD_REFS.items():
        e = emb.encode(sents, show_progress_bar=False)
        norms = np.linalg.norm(e,axis=1,keepdims=True)+1e-8
        _ref_embs[mood] = e/norms
    print("[mood] ✅ References ready")
    return _ref_embs

@dataclass
class BehaviouralSignals:
    response_latency_ms:float=0.0
    backspace_count:int=0
    attempt_number:int=1

@dataclass
class AudioFeatures:
    pitch_mean:float=0.0
    pitch_variance:float=0.0
    speech_rate:float=0.0
    pause_count:int=0

@dataclass
class MoodResult:
    mood_label:str="neutral"
    confidence_score:float=0.5
    frustration_score:float=0.0
    confusion_score:float=0.0
    engagement_score:float=0.5
    raw_signals:dict=field(default_factory=dict)
    @property
    def emoji(self): return MOOD_EMOJI.get(self.mood_label,"🙂")
    @property
    def color(self): return MOOD_COLORS.get(self.mood_label,"#1f8a8a")

def classify_mood(text:str, behavioural:Optional[BehaviouralSignals]=None, audio_features:Optional[AudioFeatures]=None) -> MoodResult:
    if behavioural is None: behavioural=BehaviouralSignals()
    lower = text.lower().strip()
    lower = re.sub(r"worst[- ]?case|best[- ]?case|average[- ]?case|got it wrong|get it wrong|getting it wrong", " ", lower)
    lat = min(behavioural.response_latency_ms/15000.0,1.0)
    bks = min(behavioural.backspace_count/20.0,1.0)
    att = min((behavioural.attempt_number-1)/3.0,1.0)
    eng = max(0.0,1.0-lat*0.4-bks*0.2-att*0.4)

    # Step 1: immediate keyword override
    if _has(lower, IMMEDIATE_FRUSTRATED):
        return MoodResult(mood_label="frustrated",confidence_score=0.95,frustration_score=0.95,confusion_score=0.3,engagement_score=eng,raw_signals={"method":"keyword","matched":"frustrated"})
    if _has(lower, IMMEDIATE_CONFUSED):
        return MoodResult(mood_label="confused",confidence_score=0.90,frustration_score=0.2,confusion_score=0.90,engagement_score=0.5,raw_signals={"method":"keyword","matched":"confused"})
    if _has(lower, IMMEDIATE_CONFIDENT):
        return MoodResult(mood_label="confident",confidence_score=0.90,frustration_score=0.0,confusion_score=0.0,engagement_score=0.9,raw_signals={"method":"keyword","matched":"confident"})
    if _has(lower, IMMEDIATE_BORED):
        return MoodResult(mood_label="bored",confidence_score=0.85,frustration_score=0.0,confusion_score=0.0,engagement_score=0.3,raw_signals={"method":"keyword","matched":"bored"})

    # Step 2: semantic similarity
    emb = _load_embedder()
    refs = _load_refs()
    if emb and refs:
        tv = emb.encode([text],show_progress_bar=False)[0]
        tv = tv/(np.linalg.norm(tv)+1e-8)
        sem = {mood:float(np.max(r@tv)) for mood,r in refs.items()}
    else:
        sem = {"frustrated":0.30,"confused":0.30,"confident":0.30,"bored":0.20,"neutral":0.40}

    final = {
        "frustrated": sem["frustrated"]*0.75+(lat*0.3+bks*0.4+att*0.3)*0.25,
        "confused":   sem["confused"]*0.75+(lat*0.5+bks*0.5)*0.25,
        "confident":  sem["confident"]*0.80+eng*0.20,
        "bored":      sem["bored"]*1.00,
        "neutral":    sem["neutral"]*1.00,
    }
    best = max(final,key=final.get)
    sv = sorted(final.values(),reverse=True)
    conf = min(0.4+(sv[0]-sv[1])*5.0,0.99)
    # No explicit emotional wording and no clear winner (near tie) -> do not guess an emotion. Short technical
    # questions ('worst case time complexity') otherwise drift to 'frustrated' on a single shared word.
    if best != "neutral" and conf < MIN_SEMANTIC_CONFIDENCE:
        best, conf = "neutral", 0.6
    return MoodResult(mood_label=best,confidence_score=round(conf,3),frustration_score=round(final["frustrated"],3),confusion_score=round(final["confused"],3),engagement_score=round(eng,3),raw_signals={"semantic":{k:round(v,3) for k,v in sem.items()},"final":{k:round(v,3) for k,v in final.items()}})
