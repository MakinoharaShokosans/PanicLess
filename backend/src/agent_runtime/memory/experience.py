"""Historical cases, ranked by the current question rather than recency."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_runtime.memory.models import Fact


@dataclass(frozen=True, slots=True)
class HistoricalCase:
    task_id: str
    session_id: str
    goal: str
    outcome: str
    content: str
    created_at: float

    def render(self) -> str:
        collected = timestamp(self.created_at)
        return (
            f"历史案例 task_id={self.task_id}；记录时间={collected}；结果={self.outcome}\n"
            f"当时目标：{self.goal}\n{self.content}\n"
            "这是历史处理经验，不能据此认定当前机器状态；操作前重新采集现场证据。"
        )


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")


def fact_text(fact: Fact) -> str:
    label = "稳定经验（待结合现场验证）" if fact.kind == "experience" else "历史观察（非当前状态）"
    time_label = (
        f"提炼时间={timestamp(fact.created_at)}；原始采集时间未知，需回读原任务"
        if fact.source == "compressor"
        else f"记录时间={timestamp(fact.created_at)}（任务收尾记录，现场采样时间以原始证据为准）"
    )
    return f"[{label}；{time_label}；task_id={fact.task_id}] {fact.text}"


def _terms(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9_./:-]{2,}", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def rank_cases(query: str, cases: Sequence[HistoricalCase], limit: int) -> list[HistoricalCase]:
    terms = _terms(query)
    if not terms or limit <= 0:
        return []
    documents = [_terms(f"{case.goal}\n{case.content}") for case in cases]
    frequency = Counter(term for document in documents for term in document)
    scored: list[tuple[float, HistoricalCase]] = []
    for case, document in zip(cases, documents, strict=True):
        matched = terms & document
        if not matched:
            continue
        score = sum(math.log(1 + len(cases) / frequency[t]) for t in matched)
        score /= math.sqrt(max(len(document), 1))
        scored.append((score, case))
    scored.sort(key=lambda item: (-item[0], -item[1].created_at, item[1].task_id))
    return [case for _, case in scored[:limit]]
