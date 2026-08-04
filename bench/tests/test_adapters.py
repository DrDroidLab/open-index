"""Unit tests for benchmark adapters and IR types."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from datasets import Dataset

from bench.data import longmemeval, memoryagentbench
from bench.ir import types


# ---------------------------------------------------------------------------
# LongMemEval synthetic fixtures
# ---------------------------------------------------------------------------


def _synthetic_longmemeval_path(tmp_path: Path) -> Path:
    data = [
        {
            "question_id": "lme_0",
            "question_type": "knowledge-update",
            "question": "Who is the current manager?",
            "answer": "Alice Smith",
            "question_date": "2023-05-25",
            "haystack_session_ids": ["s0", "s1", "s2"],
            "haystack_sessions": [
                [{"role": "user", "content": "2023/05/20 (Sat) 09:23\nHello"}],
                [
                    {"role": "user", "content": "2023/05/22 (Mon) 14:00\nUpdate"},
                    {"role": "assistant", "content": "Manager is Bob."},
                ],
                [{"role": "user", "content": "No date here"}],
            ],
            "answer_session_ids": ["s1"],
        }
    ]
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_longmemeval_event_count_and_source_ids(tmp_path: Path) -> None:
    path = _synthetic_longmemeval_path(tmp_path)
    instances = list(longmemeval.iter_instances(file=path))
    assert len(instances) == 1
    inst = instances[0]
    assert inst.event_count == 3
    assert [e.source_id for e in inst.events] == ["s0", "s1", "s2"]


def test_longmemeval_question_date_parsing(tmp_path: Path) -> None:
    path = _synthetic_longmemeval_path(tmp_path)
    inst = list(longmemeval.iter_instances(file=path))[0]
    assert inst.events[0].timestamp == "2023-05-20"
    assert inst.events[1].timestamp == "2023-05-22"
    assert inst.events[2].timestamp is None


def test_longmemeval_question_mapping(tmp_path: Path) -> None:
    path = _synthetic_longmemeval_path(tmp_path)
    q = list(longmemeval.iter_instances(file=path))[0].questions[0]
    assert q.question_id == "lme_0"
    assert q.text == "Who is the current manager?"
    assert q.gold_answers == ["Alice Smith"]
    assert q.ability == "knowledge-update"
    assert q.gold_evidence_ids == ["s1"]
    assert q.question_timestamp == "2023-05-25"


def test_longmemeval_session_text_has_role_prefixes(tmp_path: Path) -> None:
    path = _synthetic_longmemeval_path(tmp_path)
    inst = list(longmemeval.iter_instances(file=path))[0]
    assert inst.events[1].text.startswith("User: 2023/05/22")
    assert "\nAssistant: Manager is Bob." in inst.events[1].text


def test_longmemeval_max_instances(tmp_path: Path) -> None:
    data = [
        {
            "question_id": f"lme_{i}",
            "question_type": "single-session-user",
            "question": f"q{i}",
            "answer": f"a{i}",
            "question_date": "2023-05-25",
            "haystack_session_ids": ["s0"],
            "haystack_sessions": [[{"role": "user", "content": "x"}]],
            "answer_session_ids": ["s0"],
        }
        for i in range(5)
    ]
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    instances = list(longmemeval.iter_instances(file=path, max_instances=2))
    assert len(instances) == 2


def test_longmemeval_source_filter(tmp_path: Path) -> None:
    data = [
        {
            "question_id": "a",
            "question_type": "knowledge-update",
            "question": "x",
            "answer": "y",
            "question_date": "2023-05-25",
            "haystack_session_ids": ["s0"],
            "haystack_sessions": [[{"role": "user", "content": "x"}]],
            "answer_session_ids": ["s0"],
        },
        {
            "question_id": "b",
            "question_type": "multi-session",
            "question": "x",
            "answer": "y",
            "question_date": "2023-05-25",
            "haystack_session_ids": ["s0"],
            "haystack_sessions": [[{"role": "user", "content": "x"}]],
            "answer_session_ids": ["s0"],
        },
    ]
    path = tmp_path / "longmemeval_s_cleaned.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    instances = list(longmemeval.iter_instances(file=path, source_filter="knowledge-update"))
    assert len(instances) == 1
    assert instances[0].questions[0].question_id == "a"


# ---------------------------------------------------------------------------
# MemoryAgentBench synthetic fixtures
# ---------------------------------------------------------------------------


def _save_synthetic_mab_split(tmp_path: Path, split: str, rows: list[dict]) -> Path:
    ds = Dataset.from_dict(
        {
            "context": [r["context"] for r in rows],
            "questions": [r["questions"] for r in rows],
            "answers": [r["answers"] for r in rows],
            "metadata": [r["metadata"] for r in rows],
        }
    )
    split_file = tmp_path / f"{split}.jsonl"
    ds.to_json(str(split_file))
    return split_file


def _synthetic_mab_rows() -> list[dict]:
    return [
        {
            "context": "Chunk A\n\nChunk B\n\nChunk C",
            "questions": ["What is the first fact?", "What is the second fact?"],
            "answers": [["answer A"], ["answer B1", "answer B2"]],
            "metadata": {
                "haystack_sessions": ["Chunk A", "Chunk B", "Chunk C"],
                "qa_pair_ids": ["ar_0_0", "ar_0_1"],
                "source": "longmemeval_s_0",
            },
        },
        {
            "context": "Fallback context line 1\nFallback context line 2",
            "questions": ["Conflict question?"],
            "answers": [["latest"]],
            "metadata": {
                "haystack_sessions": [],
                "qa_pair_ids": ["cr_1_0"],
                "source": "factconsolidation_sh_6k",
            },
        },
    ]


def test_memoryagentbench_event_count_and_ids(tmp_path: Path) -> None:
    rows = _synthetic_mab_rows()
    split_dir = _save_synthetic_mab_split(tmp_path, "Accurate_Retrieval", rows)
    inst = list(
        memoryagentbench.iter_instances(
            split="Accurate_Retrieval", cache_dir=tmp_path, max_instances=1
        )
    )[0]
    assert inst.event_count == 3
    assert [e.source_id for e in inst.events] == ["0", "1", "2"]
    assert inst.events[0].text == "Chunk A"


def test_memoryagentbench_per_row_question_indexing(tmp_path: Path) -> None:
    rows = _synthetic_mab_rows()
    split_dir = _save_synthetic_mab_split(tmp_path, "Accurate_Retrieval", rows)
    instances = list(
        memoryagentbench.iter_instances(
            split="Accurate_Retrieval", cache_dir=tmp_path, max_instances=1
        )
    )
    assert len(instances) == 1
    questions = instances[0].questions
    assert len(questions) == 2
    assert questions[0].question_id == "ar_0_0"
    assert questions[1].question_id == "ar_0_1"
    assert questions[0].text == "What is the first fact?"
    assert questions[1].text == "What is the second fact?"


def test_memoryagentbench_gold_answer_list(tmp_path: Path) -> None:
    rows = _synthetic_mab_rows()
    split_dir = _save_synthetic_mab_split(tmp_path, "Accurate_Retrieval", rows)
    inst = list(
        memoryagentbench.iter_instances(
            split="Accurate_Retrieval", cache_dir=tmp_path, max_instances=1
        )
    )[0]
    assert inst.questions[0].gold_answers == ["answer A"]
    assert inst.questions[1].gold_answers == ["answer B1", "answer B2"]


def test_memoryagentbench_ability_tags(tmp_path: Path) -> None:
    rows = _synthetic_mab_rows()
    _save_synthetic_mab_split(tmp_path, "Accurate_Retrieval", [rows[0]])
    _save_synthetic_mab_split(tmp_path, "Conflict_Resolution", [rows[1]])

    ar_inst = list(
        memoryagentbench.iter_instances(
            split="Accurate_Retrieval", cache_dir=tmp_path, max_instances=1
        )
    )[0]
    assert ar_inst.questions[0].ability == "longmemeval_s_0"
    assert ar_inst.questions[0].metadata["coarse_group"] == "accurate_retrieval"

    cr_inst = list(
        memoryagentbench.iter_instances(
            split="Conflict_Resolution", cache_dir=tmp_path, max_instances=1
        )
    )[0]
    assert cr_inst.questions[0].ability == "factconsolidation_sh_6k"
    assert cr_inst.questions[0].metadata["coarse_group"] == "conflict_resolution_sh"


def test_memoryagentbench_fallback_split(tmp_path: Path) -> None:
    rows = _synthetic_mab_rows()
    _save_synthetic_mab_split(tmp_path, "Conflict_Resolution", [rows[1]])
    inst = list(
        memoryagentbench.iter_instances(
            split="Conflict_Resolution", cache_dir=tmp_path, max_instances=1
        )
    )[0]
    assert inst.event_count == 2
    assert all(e.metadata["fallback_split"] for e in inst.events)
    assert inst.metadata["fallback_split"] is True


# ---------------------------------------------------------------------------
# IR type tests
# ---------------------------------------------------------------------------


def test_ir_jsonl_roundtrip(tmp_path: Path) -> None:
    event = types.EvidenceEvent(
        event_id="e1", source_id="s1", timestamp="2023-05-20", text="hello"
    )
    question = types.Question(
        question_id="q1",
        text="what?",
        gold_answers=["a"],
        ability="knowledge-update",
        gold_evidence_ids=["s1"],
        question_timestamp="2023-05-21",
    )
    instance = types.BenchmarkInstance(
        instance_id="i1", events=[event], questions=[question]
    )

    path = tmp_path / "instances.jsonl"
    types.write_instances(path, [instance])
    loaded = list(types.read_instances(path))
    assert len(loaded) == 1
    assert loaded[0] == instance


# ---------------------------------------------------------------------------
# Integration smoke tests (use real cache if present)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIntegration:
    def test_real_longmemeval_first_two_instances(self) -> None:
        from bench.config import DatasetCacheConfig

        cfg = DatasetCacheConfig()
        path = cfg.longmemeval_dir / "longmemeval_s_cleaned.json"
        if not path.exists():
            pytest.skip("LongMemEval cache not present")
        instances = list(longmemeval.iter_instances(file=path, max_instances=2))
        assert len(instances) == 2
        for inst in instances:
            assert inst.event_count > 0
            assert inst.question_count == 1
            assert inst.questions[0].ability

    def test_real_memoryagentbench_first_two_instances(self) -> None:
        from bench.config import DatasetCacheConfig

        cfg = DatasetCacheConfig()
        for split in ["Accurate_Retrieval", "Conflict_Resolution"]:
            split_file = cfg.memoryagentbench_dir / f"{split}.jsonl"
            if not split_file.exists():
                pytest.skip(f"MemoryAgentBench/{split} cache not present")
            instances = list(
                memoryagentbench.iter_instances(
                    split=split, cache_dir=cfg.memoryagentbench_dir, max_instances=2
                )
            )
            assert len(instances) == 2
            for inst in instances:
                assert inst.event_count > 0
                assert inst.question_count >= 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
