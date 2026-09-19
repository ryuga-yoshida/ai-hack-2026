"""ドメインモデル。DB の行と1対1で対応する dataclass。ORM は使わない。"""
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any, Literal
from uuid import uuid4

Source = Literal["meet", "chat", "excel", "wiki"]
EventKind = Literal["decision", "task_hint", "utterance", "artifact_change"]
TaskStatus = Literal["todo", "in_progress", "blocked", "done"]
Relation = Literal["implements", "discusses", "contradicts", "follows"]
LinkMethod = Literal["explicit", "context", "assignee", "embedding", "llm"]
FindingKind = Literal["contradiction", "stalled", "orphan_change"]


def new_id() -> str:
    return uuid4().hex[:12]


@dataclass
class Event:
    id: str
    source: Source
    kind: EventKind
    text: str
    occurred_at: datetime
    actor: str | None = None
    ref: str | None = None
    quote: str | None = None
    confidence: float = 1.0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    id: str
    title: str
    status: TaskStatus = "todo"
    description: str | None = None
    assignee: str | None = None
    due_date: date | None = None
    created_from: str | None = None
    artifacts: list[str] = field(default_factory=list)


@dataclass
class Link:
    id: str
    from_type: Literal["event", "task"]
    from_id: str
    to_type: Literal["event", "task"]
    to_id: str
    relation: Relation
    confidence: float
    method: LinkMethod
    reason: str | None = None


@dataclass
class Finding:
    id: str
    kind: FindingKind
    severity: Literal["high", "medium", "low"]
    evidence: list[str]          # event id を最低2件
    summary: str
    confidence: float
    task_id: str | None = None
    reason: str | None = None
    model_used: str | None = None
    status: str = "pending"

    def __post_init__(self):
        if len(self.evidence) < 2:
            raise ValueError("Finding には最低2件の根拠が必要")


@dataclass
class CostLog:
    id: str
    task: str
    model: str
    tier: Literal["high", "mid", "embed"]
    input_tokens: int
    output_tokens: int
    cost_usd: float
    occurred_at: datetime
