"""Tests for the benchmark scorer."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from bench.harness import scoring
from bench.llm.client import FakeLLMClient, Usage


def test_subem_literal_substring_first() -> None:
    # Official semantics: literal case-insensitive substring match.
    assert scoring.subem_score("I saw a cat", ["a"]) == 1
    assert scoring.subem_score("The cat sat on the mat", ["the"]) == 1
    assert scoring.subem_score("I saw a cat", ["a cat"]) == 1


def test_subem_empty_gold_never_matches() -> None:
    assert scoring.subem_score("I saw a cat", [""]) == 0
    assert scoring.subem_score("I saw a cat", ["", "cat"]) == 1


def test_subem_normalization_fallback_punctuation_and_articles() -> None:
    assert scoring.subem_score("The manager is Alice Smith.", ["Alice Smith"]) == 1
    assert scoring.subem_score("Manager: Alice", ["alice"]) == 1
    assert scoring.subem_score("A quick, brown fox!", ["quick brown fox"]) == 1


def test_subem_multiple_gold_answers() -> None:
    assert scoring.subem_score("Bob is the lead", ["Alice", "Bob"]) == 1
    assert scoring.subem_score("Alice is the lead", ["Alice", "Bob"]) == 1
    assert scoring.subem_score("Charlie is the lead", ["Alice", "Bob"]) == 0


def test_subem_miss() -> None:
    assert scoring.subem_score("The answer is 42", ["forty-three"]) == 0
    assert scoring.subem_score("", ["answer"]) == 0


def test_recall_at_k_math() -> None:
    references = {
        "q1": {"answer_session_ids": ["s1", "s2"]},
        "q2": {"answer_session_ids": ["s3"]},
    }
    metadata = [
        {"question_id": "q1", "retrieved_source_ids": ["s1"]},
        {"question_id": "q2", "retrieved_source_ids": ["s3", "s4"]},
    ]
    result = scoring.compute_recall_at_k(metadata, references)
    assert result["count"] == 2
    # q1: 1/2 = 0.5, q2: 1/1 = 1.0, mean = 0.75
    assert result["mean"] == 0.75


def test_recall_at_k_missing_gold() -> None:
    references = {"q1": {"answer_session_ids": []}}
    metadata = [{"question_id": "q1", "retrieved_source_ids": ["s1"]}]
    result = scoring.compute_recall_at_k(metadata, references)
    assert result["count"] == 0
    assert result["mean"] == 0.0


def test_judge_input_construction_with_fake_client() -> None:
    fake = FakeLLMClient(model="gpt-4o", temperature=0)
    fake.queue_text("yes")
    references = {
        "q1": {
            "question_type": "knowledge-update",
            "question": "Who is the manager now?",
            "answer": "Alice",
        }
    }
    predictions_path = Path(tempfile.mkdtemp()) / "preds.jsonl"
    predictions_path.write_text(json.dumps({"question_id": "q1", "hypothesis": "Alice is the manager."}) + "\n")

    results = scoring.run_longmemeval_judge(predictions_path, fake, references)
    assert len(results) == 1
    assert results[0]["correct"] is True

    # The fake client records the prompt sent to the judge.
    assert len(fake.calls) == 1
    prompt = fake.calls[0][0]["content"]
    assert "Who is the manager now?" in prompt
    assert "Alice" in prompt
    assert "Alice is the manager." in prompt


def test_judge_aggregation() -> None:
    results = [
        {"question_id": "q1", "question_type": "a", "correct": True},
        {"question_id": "q2", "question_type": "a", "correct": False},
        {"question_id": "q3", "question_type": "b", "correct": True},
    ]
    agg = scoring.aggregate_judge_results(results)
    assert agg["overall"] == pytest.approx(2 / 3, rel=1e-3)
    assert agg["per_question_type"]["a"] == pytest.approx(0.5, rel=1e-3)
    assert agg["per_question_type"]["b"] == 1.0
    assert agg["count"] == 3


def test_mab_subem_scoring_uses_references() -> None:
    predictions = [
        {"qa_pair_id": "ar_0_0", "hypothesis": "The answer is Alice", "source": "longmemeval_s_0", "split": "Accurate_Retrieval"},
        {"qa_pair_id": "cr_1_0", "hypothesis": "Latest", "source": "factconsolidation_sh_6k", "split": "Conflict_Resolution"},
        {"qa_pair_id": "cr_2_0", "hypothesis": "Stale", "source": "factconsolidation_mh_6k", "split": "Conflict_Resolution"},
    ]
    references = {
        "ar_0_0": {"gold_answers": ["Alice"], "source": "longmemeval_s_0", "split": "Accurate_Retrieval"},
        "cr_1_0": {"gold_answers": ["latest"], "source": "factconsolidation_sh_6k", "split": "Conflict_Resolution"},
        "cr_2_0": {"gold_answers": ["new"], "source": "factconsolidation_mh_6k", "split": "Conflict_Resolution"},
    }
    result = scoring.score_mab_predictions(predictions, references)
    assert result["overall"] == pytest.approx(2 / 3, rel=1e-3)
    assert result["per_group"]["accurate_retrieval"] == 1.0
    assert result["per_group"]["conflict_resolution_sh"] == 1.0
    assert result["per_group"]["conflict_resolution_mh"] == 0.0


def test_probe_aggregation() -> None:
    probes = [{"correct": True}, {"correct": True}, {"correct": False}]
    result = scoring.aggregate_probes(probes)
    assert result["count"] == 3
    assert result["accuracy"] == pytest.approx(2 / 3, rel=1e-3)

    assert scoring.aggregate_probes([]) == {"count": 0, "accuracy": None}


def test_ops_aggregation() -> None:
    metadata = [
        {"usage": {"prompt_tokens": 100, "completion_tokens": 50}, "latency_ms": 1000.0, "tool_calls": 2, "truncated": False},
        {"usage": {"prompt_tokens": 200, "completion_tokens": 100}, "latency_ms": 2000.0, "tool_calls": 4, "truncated": True},
    ]
    summary = {"total_cost_usd": 0.123456}
    ops = scoring.aggregate_ops(metadata, summary)
    assert ops["total_tokens"] == 450
    assert ops["total_cost_usd"] == pytest.approx(0.123456, rel=1e-6)
    assert ops["mean_latency_ms"] == 1500.0
    assert ops["total_tool_calls"] == 6
    assert ops["truncation_rate"] == 0.5


def test_score_run_longmemeval_writes_metrics(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "dataset": "longmemeval",
                "system": "flat",
                "instances": 1,
                "questions": 1,
                "total_cost_usd": 0.01,
            }
        )
    )
    (run_dir / "metadata.jsonl").write_text(
        json.dumps(
            {
                "question_id": "q1",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "latency_ms": 100.0,
                "tool_calls": 1,
                "retrieved_source_ids": ["s1"],
                "truncated": False,
                "dropped_events": 0,
                "cost_usd": 0.01,
            }
        )
        + "\n"
    )
    (run_dir / "predictions.jsonl").write_text(
        json.dumps({"question_id": "q1", "hypothesis": "answer"}) + "\n"
    )

    fake = FakeLLMClient(model="gpt-4o", temperature=0)
    fake.queue_text("yes")
    references = {
        "q1": {
            "question_type": "single-session-user",
            "question": "q?",
            "answer": "answer",
            "answer_session_ids": ["s1"],
        }
    }

    metrics = scoring.score_run(run_dir, judge_client=fake, references=references)
    assert metrics["dataset"] == "longmemeval"
    assert metrics["judge"]["overall"] == 1.0
    assert metrics["recall_at_k"]["mean"] == 1.0
    assert (run_dir / "metrics.json").exists()
    assert (run_dir / "metrics.md").exists()


def test_score_run_mab_writes_metrics(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "dataset": "mab",
                "system": "flat",
                "split": "Accurate_Retrieval",
                "instances": 1,
                "questions": 2,
                "total_cost_usd": 0.01,
            }
        )
    )
    (run_dir / "metadata.jsonl").write_text(
        json.dumps(
            {
                "question_id": "ar_0_0",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "latency_ms": 100.0,
                "tool_calls": 1,
                "retrieved_source_ids": [],
                "truncated": False,
                "dropped_events": 0,
                "cost_usd": 0.01,
            }
        )
        + "\n"
    )
    # Predictions must NOT contain gold answers; scorer joins them from references.
    (run_dir / "predictions.jsonl").write_text(
        json.dumps(
            {
                "qa_pair_id": "ar_0_0",
                "hypothesis": "Alice",
                "source": "longmemeval_s_0",
                "split": "Accurate_Retrieval",
            }
        )
        + "\n"
    )
    references = {
        "ar_0_0": {"gold_answers": ["Alice"], "source": "longmemeval_s_0", "split": "Accurate_Retrieval"}
    }

    metrics = scoring.score_run(run_dir, references=references)
    assert metrics["dataset"] == "mab"
    assert metrics["subem"]["overall"] == 1.0
    assert (run_dir / "metrics.json").exists()
    assert metrics.get("judge_cost_usd", 0.0) == 0.0


def test_recall_at_k_capped_to_top_k() -> None:
    references = {"q1": {"answer_session_ids": ["s1", "s2", "s3"]}}
    metadata = [
        {"question_id": "q1", "retrieved_source_ids": ["s1", "s2", "s3", "s4", "s5", "s6"]}
    ]
    result = scoring.compute_recall_at_k(metadata, references, k=3)
    assert result["mean"] == 1.0
    assert result["count"] == 1

    result = scoring.compute_recall_at_k(metadata, references, k=2)
    assert result["mean"] == pytest.approx(2 / 3, rel=1e-3)


def test_window_coverage_uses_full_included_list() -> None:
    references = {"q1": {"answer_session_ids": ["s1", "s2", "s3"]}}
    metadata = [
        {"question_id": "q1", "retrieved_source_ids": ["s1", "s2", "s3", "s4", "s5", "s6"]}
    ]
    result = scoring.compute_window_coverage(metadata, references)
    assert result["mean"] == 1.0
    assert result["count"] == 1


def test_judge_cost_usd_recorded(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "dataset": "longmemeval",
                "system": "flat",
                "instances": 1,
                "questions": 1,
                "total_cost_usd": 0.01,
                "k": 5,
            }
        )
    )
    (run_dir / "metadata.jsonl").write_text(
        json.dumps(
            {
                "question_id": "q1",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "latency_ms": 100.0,
                "tool_calls": 1,
                "retrieved_source_ids": ["s1"],
                "truncated": False,
                "dropped_events": 0,
                "cost_usd": 0.01,
            }
        )
        + "\n"
    )
    (run_dir / "predictions.jsonl").write_text(
        json.dumps({"question_id": "q1", "hypothesis": "answer"}) + "\n"
    )

    fake = FakeLLMClient(model="gpt-4o", temperature=0)
    fake.queue_text("yes")
    references = {
        "q1": {
            "question_type": "single-session-user",
            "question": "q?",
            "answer": "answer",
            "answer_session_ids": ["s1"],
        }
    }
    metrics = scoring.score_run(run_dir, judge_client=fake, references=references)
    assert metrics["judge_cost_usd"] > 0.0
    assert metrics["judge_cost_usd"] == round(fake.ledger.total_cost_usd, 6)


def test_judge_label_parsing_startswith_yes() -> None:
    """The judge must parse labels with startswith, not substring."""
    fake = FakeLLMClient(model="gpt-4o", temperature=0)
    fake.queue_text("yes, the answer is correct")
    references = {
        "q1": {
            "question_type": "single-session-user",
            "question": "q?",
            "answer": "answer",
        }
    }
    predictions_path = Path(tempfile.mkdtemp()) / "preds.jsonl"
    predictions_path.write_text(json.dumps({"question_id": "q1", "hypothesis": "answer"}) + "\n")
    results = scoring.run_longmemeval_judge(predictions_path, fake, references)
    assert results[0]["correct"] is True

    fake2 = FakeLLMClient(model="gpt-4o", temperature=0)
    fake2.queue_text("not yes")
    results2 = scoring.run_longmemeval_judge(predictions_path, fake2, references)
    assert results2[0]["correct"] is False
