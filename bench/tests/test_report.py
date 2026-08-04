"""Tests for the markdown report generator and smoke runner selection logic."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Iterator

import pytest

from bench.harness import report
from bench.run_smoke import _select_longmemeval_smoke_indices
from bench.ir.types import BenchmarkInstance, EvidenceEvent, Question
from bench.llm.client import FakeLLMClient, Usage
from bench.systems.base import Answer, MemorySystem


def _make_metrics(dataset: str, system: str, **kwargs: Any) -> dict:
    base = {
        "run_dir": f"bench/results/{dataset}/{system}",
        "dataset": dataset,
        "system": system,
        "instances": 1,
        "questions": 1,
        "ops": {
            "total_tokens": 1000,
            "total_cost_usd": 0.05,
            "mean_latency_ms": 1000.0,
            "total_tool_calls": 5,
            "truncation_rate": 0.0,
        },
        "probes": {"count": 0, "accuracy": None},
    }
    base.update(kwargs)
    return base


def test_report_accuracy_table() -> None:
    metrics = [
        _make_metrics("longmemeval", "structured", judge={"overall": 0.6, "per_question_type": {"knowledge-update": 0.5}}),
        _make_metrics("longmemeval", "flat", judge={"overall": 0.4, "per_question_type": {"knowledge-update": 0.3}}),
    ]
    md = report.build_report(metrics)
    assert "knowledge-update" in md
    assert "structured" in md
    assert "flat" in md
    assert "60.00%" in md
    assert "40.00%" in md


def test_report_subem_table() -> None:
    metrics = [
        _make_metrics(
            "mab",
            "structured",
            subem={
                "overall": 0.7,
                "per_group": {"accurate_retrieval": 0.8, "conflict_resolution_sh": 0.6},
            },
        ),
        _make_metrics(
            "mab",
            "flat",
            subem={
                "overall": 0.6,
                "per_group": {"accurate_retrieval": 0.7, "conflict_resolution_sh": 0.5},
            },
        ),
    ]
    md = report.build_report(metrics)
    assert "accurate_retrieval" in md
    assert "conflict_resolution_sh" in md
    assert "70.00%" in md


def test_report_writes_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    metrics = _make_metrics("longmemeval", "structured", judge={"overall": 1.0, "per_question_type": {}})
    (run_dir / "metrics.json").write_text(json.dumps(metrics))
    out = tmp_path / "report.md"
    report.write_report([run_dir], out)
    assert out.exists()
    text = out.read_text()
    assert "droid-brain benchmark report" in text


def test_smoke_selection_deterministic_knowledge_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Create a synthetic LongMemEval file with 10 rows: knowledge-update at indices 3, 7, 8.
    rows = [
        {
            "question_id": f"q{i}",
            "question_type": "knowledge-update" if i in (3, 7, 8) else "single-session-user",
            "question": f"q{i}?",
            "answer": f"a{i}",
            "question_date": "2023-05-25",
            "haystack_session_ids": ["s0"],
            "haystack_sessions": [[{"role": "user", "content": "x"}]],
            "answer_session_ids": ["s0"],
        }
        for i in range(10)
    ]
    cache_dir = tmp_path / "cache" / "longmemeval"
    cache_dir.mkdir(parents=True)
    (cache_dir / "longmemeval_s_cleaned.json").write_text(json.dumps(rows))

    from bench import run_smoke as run_smoke_mod

    monkeypatch.setattr(
        run_smoke_mod, "DatasetCacheConfig", lambda: type("C", (), {"longmemeval_dir": cache_dir})()
    )

    indices_5 = _select_longmemeval_smoke_indices(5)
    # Should include the first 2 knowledge-update indices (3, 7) and fill with 0, 1, 2.
    assert indices_5 == [0, 1, 2, 3, 7]

    indices_1 = _select_longmemeval_smoke_indices(1)
    # With n=1, we still take the first knowledge-update instance.
    assert indices_1 == [3]
