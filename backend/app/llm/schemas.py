"""Pydantic models the LLM must return (OpenAI structured outputs enforce the JSON schema)."""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "insurance",
    "legal",
    "finance",
    "hr",
    "technical",
    "medical",
    "government",
    "sales_marketing",
    "academic",
    "personal",
    "other",
]


class KeyFact(BaseModel):
    label: str = Field(description="What the value is, e.g. 'Flood deductible'")
    value: str = Field(description="The exact value as written, e.g. '$25,000'")
    page: int | None = Field(description="Page number where it appears, or null if unknown")


class Entities(BaseModel):
    organizations: list[str]
    people: list[str]
    locations: list[str]
    dates: list[str]
    monetary_amounts: list[str]


class Sentiment(BaseModel):
    label: Literal["positive", "neutral", "negative", "mixed"]
    score: float = Field(description="-1.0 (very negative) to 1.0 (very positive)")
    rationale: str


class InsightsOutput(BaseModel):
    summary: str
    document_type: str
    category: Category
    tags: list[str]
    key_points: list[str]
    key_facts: list[KeyFact]
    entities: Entities
    sentiment: Sentiment
    suggested_questions: list[str]
    language: str


class FollowUpQuestions(BaseModel):
    questions: list[str]


class AnsweredSuggestions(BaseModel):
    answered: list[int] = Field(description="1-based numbers of the suggestions the answer already covers")


class ComparedValue(BaseModel):
    document_label: str = Field(description="The document label, e.g. 'A' or 'B'")
    value: str
    page: int | None


class Difference(BaseModel):
    aspect: str
    values: list[ComparedValue]
    significance: Literal["high", "medium", "low"]
    explanation: str


class DocumentBrief(BaseModel):
    document_label: str
    one_line_summary: str


class ComparisonOutput(BaseModel):
    overview: str
    documents: list[DocumentBrief]
    similarities: list[str]
    differences: list[Difference]
    gaps_or_risks: list[str] = Field(description="Things missing in one document or worth attention")


class SectionNotes(BaseModel):
    """Map step for very long documents: dense notes for one section."""

    notes: str
