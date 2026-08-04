"""MemoryAgentBench adapter."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

from bench.config import CACHE_DIR, DatasetCacheConfig
from bench.ir.types import BenchmarkInstance, EvidenceEvent, Question

MEMORYAGENTBENCH_SPLITS = {
    "Accurate_Retrieval": "Accurate_Retrieval",
    "Conflict_Resolution": "Conflict_Resolution",
}


def _derive_coarse_group(source: str, split: str) -> str:
    """Derive a coarse ability group from the source string and split name."""
    s = (source or "").lower()
    if "factconsolidation_sh" in s or s.startswith("fc_sh"):
        return "conflict_resolution_sh"
    if "factconsolidation_mh" in s or s.startswith("fc_mh"):
        return "conflict_resolution_mh"
    if split.lower() == "accurate_retrieval":
        return "accurate_retrieval"
    return split.lower().replace(" ", "_")


def _split_context(context: str, max_chunk_chars: int = 2000) -> list[str]:
    """Split a long context string into sensible chunks.

    Splits by line, then by sentence for oversized lines, preserving order.
    """
    chunks: list[str] = []
    for line in context.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(line) <= max_chunk_chars:
            chunks.append(line)
            continue
        # Sentence-level split for lines that exceed the chunk budget.
        sentences = []
        current = ""
        for sentence in _split_sentences(line):
            if current and len(current) + len(sentence) + 1 > max_chunk_chars:
                sentences.append(current)
                current = sentence
            else:
                current = (current + " " + sentence).strip() if current else sentence
        if current:
            sentences.append(current)
        chunks.extend(sentences)
    return chunks


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences heuristically."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _build_events(
    row: dict[str, Any], split: str
) -> tuple[list[EvidenceEvent], dict[str, Any]]:
    metadata = row.get("metadata") or {}
    haystack_sessions = metadata.get("haystack_sessions")
    if haystack_sessions:
        chunks = list(haystack_sessions)
        fallback_used = False
    else:
        chunks = _split_context(str(row.get("context", "")))
        fallback_used = True

    events: list[EvidenceEvent] = []
    for i, chunk in enumerate(chunks):
        if chunk is None:
            chunk = ""
        events.append(
            EvidenceEvent(
                event_id=f"{split}_{i}",
                source_id=str(i),
                timestamp=str(i),
                text=str(chunk),
                metadata={
                    "position": i,
                    "split": split,
                    "fallback_split": fallback_used,
                },
            )
        )
    extra_meta = {
        "fallback_split": fallback_used,
        "source": metadata.get("source"),
        "split": split,
    }
    return events, extra_meta


def _build_questions(
    row: dict[str, Any], coarse_group: str, row_index: int
) -> list[Question]:
    questions = list(row.get("questions", []))
    answers = list(row.get("answers", []))
    metadata = row.get("metadata") or {}
    qa_pair_ids = metadata.get("qa_pair_ids") or []
    source = metadata.get("source", "")

    out: list[Question] = []
    for i, q in enumerate(questions):
        gold = answers[i] if i < len(answers) and answers[i] is not None else []
        raw_qid = (
            qa_pair_ids[i]
            if i < len(qa_pair_ids) and qa_pair_ids[i] is not None
            else f"q_{i}"
        )
        # Disambiguate ids that are reused across rows (e.g., Accurate_Retrieval).
        qid = f"{source}::{row_index}::{raw_qid}"
        out.append(
            Question(
                question_id=str(qid),
                text=str(q),
                gold_answers=[str(a) for a in gold] if isinstance(gold, list) else [str(gold)],
                ability=str(source),
                gold_evidence_ids=[],
                question_timestamp=None,
                metadata={
                    "coarse_group": coarse_group,
                    "question_index": i,
                    "split": metadata.get("split"),
                    "qa_pair_id": str(raw_qid),
                    "source": str(source),
                    "row_index": row_index,
                },
            )
        )
    return out


def iter_instances(
    split: str | None = None,
    max_instances: int | None = None,
    source_filter: str | None = None,
    cache_dir: Path | str | None = None,
) -> Iterator[BenchmarkInstance]:
    """Yield MemoryAgentBench instances as BenchmarkInstance objects.

    Args:
        split: Split name to load, e.g. "Accurate_Retrieval" or "Conflict_Resolution".
        max_instances: Maximum number of rows to yield.
        source_filter: If set, only yield rows whose metadata.source contains this string.
        cache_dir: Optional override for the cache directory.
    """
    if split is None:
        raise ValueError("split is required for MemoryAgentBench")
    if split not in MEMORYAGENTBENCH_SPLITS:
        raise ValueError(f"Unknown split {split!r}; expected one of {list(MEMORYAGENTBENCH_SPLITS)}")

    if cache_dir is None:
        cache_dir = DatasetCacheConfig().memoryagentbench_dir
    split_path = Path(cache_dir) / f"{split}.jsonl"
    if not split_path.exists():
        raise FileNotFoundError(
            f"MemoryAgentBench split not found: {split_path}. Run fetch_eval_assets."
        )

    yielded = 0
    with split_path.open("r", encoding="utf-8") as f:
        for row_index, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if max_instances is not None and yielded >= max_instances:
                break
            metadata = row.get("metadata") or {}
            source = metadata.get("source", "")
            if source_filter is not None and source_filter not in str(source):
                continue

            coarse_group = _derive_coarse_group(str(source), split)
            events, extra_meta = _build_events(row, split)
            questions = _build_questions(row, coarse_group, row_index)

            raw_qa_ids = metadata.get("qa_pair_ids") or [f"{split}_{yielded}"]
            raw_instance_id = str(raw_qa_ids[0])
            # Disambiguate the instance id as well, since the first raw qa_pair_id
            # is also reused across Accurate_Retrieval rows.
            instance_id = f"{source}::{row_index}::{raw_instance_id}"
            instance = BenchmarkInstance(
                instance_id=instance_id,
                events=events,
                questions=questions,
                metadata={
                    "source": source,
                    "coarse_group": coarse_group,
                    "split": split,
                    "raw_instance_id": raw_instance_id,
                    "row_index": row_index,
                    **extra_meta,
                },
            )
            yield instance
            yielded += 1


if __name__ == "__main__":
    import sys

    for split in ["Accurate_Retrieval", "Conflict_Resolution"]:
        print(f"--- {split} ---")
        for i, instance in enumerate(iter_instances(split=split, max_instances=2)):
            print(f"  instance {i}: {instance.event_count} events, {instance.question_count} questions")
            if instance.questions:
                q = instance.questions[0]
                print(f"    q0: {q.question_id} | ability={q.ability} | group={q.metadata.get('coarse_group')}")
        print()
