"""LongMemEval-S cleaned adapter."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from bench.config import CACHE_DIR, DatasetCacheConfig
from bench.ir.types import BenchmarkInstance, EvidenceEvent, Question

LONGMEMEVAL_FILE = "longmemeval_s_cleaned.json"


def load_rows(file: Path | str | None = None) -> list[dict[str, Any]]:
    """Load the raw LongMemEval-S cleaned JSON rows.

    Handles JSON files that are either a top-level list or a dict keyed by split.
    """
    if file is None:
        file = DatasetCacheConfig().longmemeval_dir / LONGMEMEVAL_FILE
    path = Path(file)
    if not path.exists():
        raise FileNotFoundError(f"LongMemEval cache not found: {path}. Run fetch_eval_assets.")

    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        # Some HF JSON files are keyed by split; take the first list value.
        for v in data.values():
            if isinstance(v, list):
                data = v
                break
    if not isinstance(data, list):
        raise ValueError(f"Expected a list of rows in {path}, got {type(data).__name__}")
    return data


_DATE_RE = re.compile(
    r"(\d{4})\s*[/-]\s*(\d{1,2})\s*[/-]\s*(\d{1,2})"
)


def _normalize_date(date_value: str | None) -> str | None:
    """Parse a date like '2023/05/20 (Sat) 09:23' into ISO 'YYYY-MM-DD'."""
    if not date_value:
        return None
    match = _DATE_RE.search(str(date_value))
    if not match:
        return None
    year, month, day = match.groups()
    try:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return None


def _extract_session_date(first_message_content: str) -> str | None:
    """Parse a date like '2023/05/20 (Sat) 09:23' from the first message."""
    return _normalize_date(first_message_content)


def _session_to_text(session: list[dict[str, str]]) -> str:
    """Concatenate session messages with role prefixes."""
    lines: list[str] = []
    for msg in session:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        display_role = role.capitalize() if role in ("user", "assistant") else role
        lines.append(f"{display_role}: {content}")
    return "\n".join(lines)


def _build_events(row: dict[str, Any]) -> list[EvidenceEvent]:
    session_ids = row.get("haystack_session_ids", [])
    sessions = row.get("haystack_sessions", [])
    haystack_dates = row.get("haystack_dates", [])
    if len(session_ids) != len(sessions):
        raise ValueError(
            f"haystack_session_ids length ({len(session_ids)}) does not match "
            f"haystack_sessions length ({len(sessions)}) in row {row.get('question_id')}"
        )
    # Pad dates if the list is shorter than sessions (defensive).
    if len(haystack_dates) < len(sessions):
        haystack_dates = list(haystack_dates) + [None] * (len(sessions) - len(haystack_dates))
    events: list[EvidenceEvent] = []
    for i, (source_id, session, session_date) in enumerate(zip(session_ids, sessions, haystack_dates)):
        if not isinstance(session, list) or not session:
            timestamp = _normalize_date(session_date) if session_date else None
            text = ""
        else:
            first = session[0]
            first_content = first.get("content", "") if isinstance(first, dict) else ""
            timestamp = _normalize_date(session_date) if session_date else _extract_session_date(first_content)
            text = _session_to_text(session)
        events.append(
            EvidenceEvent(
                event_id=f"{row.get('question_id', 'unk')}_{i}",
                source_id=str(source_id),
                timestamp=timestamp,
                text=text,
                metadata={"position": i, "session_messages": len(session)},
            )
        )
    return events


def _build_question(row: dict[str, Any]) -> Question:
    return Question(
        question_id=str(row.get("question_id", "")),
        text=str(row.get("question", "")),
        gold_answers=[str(row.get("answer", ""))],
        ability=str(row.get("question_type", "")),
        gold_evidence_ids=[str(x) for x in row.get("answer_session_ids", [])],
        question_timestamp=_normalize_date(row.get("question_date")),
        metadata={"question_type": row.get("question_type")},
    )


def iter_instances(
    file: Path | str | None = None,
    max_instances: int | None = None,
    source_filter: str | None = None,
) -> Iterator[BenchmarkInstance]:
    """Yield LongMemEval-S cleaned instances as BenchmarkInstance objects.

    Args:
        file: Path to the cached JSON file. Defaults to bench/cache/longmemeval/longmemeval_s_cleaned.json.
        max_instances: Maximum number of instances to yield.
        source_filter: Unused for LongMemEval (kept for a uniform adapter signature).
    """
    data = load_rows(file)

    yielded = 0
    for row in data:
        if max_instances is not None and yielded >= max_instances:
            break
        if source_filter is not None and row.get("question_type") != source_filter:
            continue
        events = _build_events(row)
        question = _build_question(row)
        instance = BenchmarkInstance(
            instance_id=str(row.get("question_id", f"row_{yielded}")),
            events=events,
            questions=[question],
            metadata={"question_type": row.get("question_type")},
        )
        yield instance
        yielded += 1


if __name__ == "__main__":
    import sys

    for i, instance in enumerate(iter_instances(max_instances=3)):
        print(f"--- instance {i} ---")
        print(f"events: {len(instance.events)}")
        print(f"question: {instance.questions[0].question_id}")
        print(f"ability: {instance.questions[0].ability}")
        print(f"question_date: {instance.questions[0].question_timestamp}")
        print(f"gold_evidence: {instance.questions[0].gold_evidence_ids}")
        print()
