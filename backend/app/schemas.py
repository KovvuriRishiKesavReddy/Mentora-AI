"""Request/response models."""
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class SessionResponse(BaseModel):
    session_id: str


class UploadResponse(BaseModel):
    success: bool
    status: str = "added"            # added | replaced | unchanged | duplicate
    doc_id: str
    filename: str
    chunks_created: int
    chapters_detected: list[str]


class FileInfo(BaseModel):
    doc_id: str
    filename: str
    chunks: int
    pages: int = 0
    chapters: list[str]


class FilesResponse(BaseModel):
    files: list[FileInfo]


class AudioFeatures(BaseModel):
    pitch_mean:     float = Field(0.5, ge=0.0, le=1.0)
    pitch_variance: float = Field(0.3, ge=0.0, le=1.0)
    speech_rate:    float = Field(0.5, ge=0.0, le=1.0)
    pause_count:    int   = Field(0,   ge=0)


class AudioResponse(BaseModel):
    pitch_mean:     float
    pitch_variance: float
    speech_rate:    float
    pause_count:    int
    duration_s:     float = 0.0
    format:         Optional[str] = None
    transcript:     Optional[str] = None     # only when STT_PROVIDER=groq
    stt:            str = "browser"
    stt_error:      Optional[str] = None


class ChatRequest(BaseModel):
    session_id:          str = Field(min_length=8, max_length=64)
    message:             str = Field(min_length=1, max_length=4000)
    attempt_number:      int   = Field(1,   ge=1, le=100)
    response_latency_ms: float = Field(0.0, ge=0.0, le=3_600_000)
    backspace_count:     int   = Field(0,   ge=0, le=100_000)
    audio_features:      Optional[AudioFeatures] = None

    @field_validator("message")
    @classmethod
    def _not_blank(cls, v):
        if not v.strip():
            raise ValueError("message must not be blank")
        return v


class MoodInfo(BaseModel):
    label:             str
    confidence:        float
    frustration_score: float
    confusion_score:   float
    engagement_score:  float
    emoji:             str
    color:             str
    raw_signals:       dict


class ToneInfo(BaseModel):
    frustration_level:       float
    confidence_boost_needed: float
    pace_slowdown:           float
    encouragement_intensity: float


class TeachingStrategy(BaseModel):
    label:  str
    detail: str


class SourceInfo(BaseModel):
    filename: str
    chapter:  str
    page:     int = 0
    score:    float
    preview:  str
    doc_id:   Optional[str] = None


class RetrievalInfo(BaseModel):
    is_grounded: bool
    grounding:   str = "none"        # strong | weak | none
    truncated:   bool = False
    sources:     list[SourceInfo]


class ChatResponse(BaseModel):
    session_id:       str
    response:         str
    mood:             MoodInfo
    tone_explanation: str
    tone_profile:     ToneInfo
    teaching_strategy: Optional[TeachingStrategy] = None
    active_topic:     str = ""
    interpreted_as:   Optional[str] = None
    retrieval:        RetrievalInfo


class QuizGenerateRequest(BaseModel):
    session_id:    str = Field(min_length=8, max_length=64)
    topic:         str = Field(min_length=1, max_length=200)
    question_type: str = Field("mcq", pattern="^(mcq|short|feynman)$")
    num_questions: int = Field(3, ge=1, le=5)
    difficulty:    str = Field("auto", pattern="^(auto|easy|medium|hard)$")


class QuizQuestion(BaseModel):
    index:    int
    question: str
    options:  Optional[dict[str, str]] = None


class QuizGenerateResponse(BaseModel):
    quiz_id:       str
    topic:         str
    question_type: str
    difficulty:    str = "medium"
    difficulty_reason: str = ""
    questions:     list[QuizQuestion]
    sources_used:  list[str]


class QuizGradeRequest(BaseModel):
    quiz_id:        str = Field(min_length=8, max_length=64)
    question_index: int = Field(ge=0, le=50)
    student_answer: str = Field(min_length=1, max_length=4000)   # MCQ: the chosen letter


class TopicStatus(BaseModel):
    status:   str
    mastery:  Optional[float] = None
    attempts: int = 0


class QuizGradeResponse(BaseModel):
    score:               float
    score_percent:       str
    correct:             Optional[bool] = None
    correct_answer:      Optional[str] = None
    explanation:         str = ""
    option_notes:        dict = {}
    already_graded:      bool = False
    topic_status:        TopicStatus
    similarity_score:    float
    completeness_score:  float
    clarity_score:       float
    concepts_present:    list[str]
    concepts_missing:    list[str]
    misconceptions:      list[str]
    feedback:            str
    gap_analysis:        dict


class MoodHistoryResponse(BaseModel):
    frustrated: int = 0
    confused:   int = 0
    confident:  int = 0
    bored:      int = 0
    neutral:    int = 0
    timeline:   list[str] = []


class SummaryRequest(BaseModel):
    doc_id:  str = Field(min_length=8, max_length=64)
    refresh: bool = False


class FlashcardRequest(BaseModel):
    doc_id: Optional[str] = Field(None, min_length=8, max_length=64)
    topic:  Optional[str] = Field(None, max_length=200)
    count:  int = Field(10, ge=1, le=20)
