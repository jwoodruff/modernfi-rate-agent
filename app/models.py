from datetime import datetime

from pydantic import BaseModel, field_validator


class QuestionRequest(BaseModel):
    question: str

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be empty")
        return v


class QueryRecord(BaseModel):
    id: int
    question: str
    answer: str | None
    status: str
    created_at: datetime
    iterations: int | None
    tool_calls: int | None
    duration_ms: int | None
    tools_used: list[str]
