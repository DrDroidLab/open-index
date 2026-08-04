"""Scoring for benchmark runs.

Produces metrics.json and metrics.md for a run directory.

- MemoryAgentBench: case-insensitive normalized SubEM.
- LongMemEval: official LLM judge (adapted to use the harness Azure endpoint).
- LongMemEval: Recall@k from retrieved source_ids.
- All systems: token/cost/latency/truncation operations metrics.
- Probe aggregation: update-correctness % over structured/flat probe results.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import string
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from bench.config import AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, JUDGE_MODEL, RESULTS_DIR
from bench.data import longmemeval
from bench.data.memoryagentbench import _derive_coarse_group
from bench.llm.client import LLMClient


# ---------------------------------------------------------------------------
# SubEM scoring
# ---------------------------------------------------------------------------


def _subem_normalize(text: str) -> str:
    """Lowercase, strip punctuation/articles, collapse whitespace."""
    text = str(text).lower()
    text = re.sub(rf"[{re.escape(string.punctuation)}]", " ", text)
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def subem_score(hypothesis: str, gold_answers: list[str]) -> int:
    """Return 1 if any normalized gold answer is a substring of the normalized hypothesis."""
    if not gold_answers:
        return 0
    norm_hyp = _subem_normalize(hypothesis)
    for gold in gold_answers:
        if _subem_normalize(gold) in norm_hyp:
            return 1
    return 0


# ---------------------------------------------------------------------------
# LongMemEval official judge (adapted to Azure endpoint)
# ---------------------------------------------------------------------------


def _longmemeval_cache_path() -> Path:
    return Path(__file__).resolve().parents[1] / "cache" / "longmemeval" / "longmemeval_s_cleaned.json"


def _load_official_judge_prompt() -> Optional[Any]:
    """Import the official LongMemEval get_anscheck_prompt from the cached script."""
    assets = Path(__file__).resolve().parents[1] / "cache" / "eval_assets"
    script = assets / "evaluate_qa.py"
    if not script.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("official_evaluate_qa", script)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, "get_anscheck_prompt", None)
    except Exception:
        return None


_JUDGE_QA_INTRO = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response is equivalent to the correct answer or contains all the intermediate steps "
    "to get the correct answer, you should also answer yes. "
    "If the response only contains a subset of the information required by the answer, answer no. "
)

_JUDGE_TYPE_NOTES: dict[str, str] = {
    "single-session-user": "",
    "single-session-assistant": "",
    "multi-session": "",
    "temporal-reasoning": (
        "In addition, do not penalize off-by-one errors for the number of days. "
        "If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors "
        "(e.g., predicting 19 days when the answer is 18), the model's response is still correct. "
    ),
    "knowledge-update": (
        "If the response contains some previous information along with an updated answer, "
        "the response should be considered as correct as long as the updated answer is the required answer."
    ),
}


def _build_qa_judge_prompt(
    question_type: str,
    question: str,
    answer: str,
    hypothesis: str,
) -> str:
    """Build the standard QA judge prompt for a known question type."""
    return (
        _JUDGE_QA_INTRO
        + _JUDGE_TYPE_NOTES[question_type]
        + "\n\n"
        + f"Question: {question}\n\n"
        + f"Correct Answer: {answer}\n\n"
        + f"Model Response: {hypothesis}\n\n"
        + "Is the model response correct? Answer yes or no only."
    )


def _fallback_judge_prompt(
    question_type: str,
    question: str,
    answer: str,
    hypothesis: str,
    abstention: bool = False,
) -> str:
    """Re-implement the official judge prompts verbatim.

    These are the exact rubrics used in the official evaluate_qa.py from the
    LongMemEval repository (xiaowu0162/LongMemEval, src/evaluation/evaluate_qa.py).
    """
    if abstention:
        return (
            "I will give you an unanswerable question, an explanation, and a response from a model. "
            "Please answer yes if the model correctly identifies the question as unanswerable. "
            "The model could say that the information is incomplete, or some other information is given "
            "but the asked information is not.\n\n"
            f"Question: {question}\n\n"
            f"Explanation: {answer}\n\n"
            f"Model Response: {hypothesis}\n\n"
            "Does the model correctly identify the question as unanswerable? Answer yes or no only."
        )

    if question_type == "single-session-preference":
        return (
            "I will give you a question, a rubric for desired personalized response, and a response from a model. "
            "Please answer yes if the response satisfies the desired response. Otherwise, answer no. "
            "The model does not need to reflect all the points in the rubric. "
            "The response is correct as long as it recalls and utilizes the user's personal information correctly."
            "\n\n"
            f"Question: {question}\n\n"
            f"Rubric: {answer}\n\n"
            f"Model Response: {hypothesis}\n\n"
            "Is the model response correct? Answer yes or no only."
        )

    if question_type in _JUDGE_TYPE_NOTES:
        return _build_qa_judge_prompt(question_type, question, answer, hypothesis)

    raise NotImplementedError(f"No judge prompt for question type {question_type!r}")


def _load_longmemeval_references() -> dict[str, dict[str, Any]]:
    """Load the cleaned LongMemEval dataset keyed by question_id."""
    path = _longmemeval_cache_path()
    if not path.exists():
        raise FileNotFoundError(f"LongMemEval cache not found: {path}")
    rows = longmemeval.load_rows(path)
    return {str(row.get("question_id", "")): row for row in rows if row.get("question_id")}


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def run_longmemeval_judge(
    predictions_path: Path,
    judge_client: LLMClient,
    references: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Run the official LongMemEval LLM judge over a predictions file.

    Uses the official prompt from the cached evaluate_qa.py when available,
    otherwise falls back to the verbatim re-implementation. Calls the Azure
    endpoint via the harness LLMClient (model gpt-4o, temperature 0).
    """
    get_prompt = _load_official_judge_prompt() or _fallback_judge_prompt
    if references is None:
        references = _load_longmemeval_references()

    preds = [
        json.loads(line)
        for line in predictions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results: list[dict[str, Any]] = []
    for pred in preds:
        qid = str(pred.get("question_id", ""))
        if qid not in references:
            continue
        ref = references[qid]
        qtype = str(ref.get("question_type", ""))
        question = str(ref.get("question", ""))
        answer = str(ref.get("answer", ""))
        hyp = str(pred.get("hypothesis", ""))
        prompt = get_prompt(qtype, question, answer, hyp, abstention="_abs" in qid)
        response = judge_client.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=10,
        )
        label = "yes" in response.content.strip().lower()
        results.append(
            {
                "question_id": qid,
                "question_type": qtype,
                "hypothesis": hyp,
                "answer": answer,
                "correct": label,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def aggregate_judge_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate LongMemEval judge correctness overall and per question_type."""
    if not results:
        return {"overall": 0.0, "per_question_type": {}, "count": 0}
    per_type: dict[str, list[int]] = defaultdict(list)
    for r in results:
        per_type[r["question_type"]].append(1 if r["correct"] else 0)
    return {
        "overall": round(_mean([1 if r["correct"] else 0 for r in results]), 4),
        "per_question_type": {
            qtype: round(_mean(scores), 4) for qtype, scores in per_type.items()
        },
        "count": len(results),
    }


def score_mab_predictions(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute SubEM per split and per coarse source group for MAB predictions."""
    if not predictions:
        return {
            "overall": 0.0,
            "per_split": {},
            "per_group": {},
            "count": 0,
        }
    per_split: dict[str, list[int]] = defaultdict(list)
    per_group: dict[str, list[int]] = defaultdict(list)
    all_scores: list[int] = []
    for pred in predictions:
        gold = pred.get("gold_answers", [])
        score = subem_score(pred.get("hypothesis", ""), gold)
        all_scores.append(score)
        split = str(pred.get("split", ""))
        source = str(pred.get("source", ""))
        group = _derive_coarse_group(source, split)
        per_split[split].append(score)
        per_group[group].append(score)
    return {
        "overall": round(_mean(all_scores), 4),
        "per_split": {k: round(_mean(v), 4) for k, v in per_split.items()},
        "per_group": {k: round(_mean(v), 4) for k, v in per_group.items()},
        "count": len(all_scores),
    }


def compute_recall_at_k(
    metadata: list[dict[str, Any]],
    references: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Compute Recall@k for LongMemEval from metadata retrieved_source_ids."""
    if references is None:
        references = _load_longmemeval_references()
    recalls: list[float] = []
    for row in metadata:
        qid = str(row.get("question_id", ""))
        ref = references.get(qid)
        if not ref:
            continue
        gold = set(str(x) for x in ref.get("answer_session_ids", []))
        if not gold:
            continue
        retrieved = set(str(x) for x in row.get("retrieved_source_ids", []))
        recalls.append(len(gold & retrieved) / len(gold))
    return {
        "mean": round(_mean(recalls), 4),
        "count": len(recalls),
    }


def aggregate_ops(metadata: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    """Aggregate operations metrics from metadata and summary."""
    if not metadata:
        return {
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "mean_latency_ms": 0.0,
            "total_tool_calls": 0,
            "truncation_rate": 0.0,
        }
    total_tokens = 0
    for row in metadata:
        usage = row.get("usage", {})
        total_tokens += int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))
    latencies = [float(row.get("latency_ms", 0.0)) for row in metadata]
    tool_calls = [int(row.get("tool_calls", 0)) for row in metadata]
    truncated = sum(1 for row in metadata if row.get("truncated", False))
    return {
        "total_tokens": total_tokens,
        "total_cost_usd": round(float(summary.get("total_cost_usd", 0.0)), 6),
        "mean_latency_ms": round(_mean(latencies), 2),
        "total_tool_calls": sum(tool_calls),
        "truncation_rate": round(truncated / len(metadata), 4),
    }


def aggregate_probes(probe_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate update-correctness over probe results."""
    if not probe_results:
        return {"count": 0, "accuracy": None}
    correct = sum(1 for p in probe_results if p.get("correct", False))
    return {
        "count": len(probe_results),
        "accuracy": round(correct / len(probe_results), 4),
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _render_metrics_md(metrics: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Scoring report: {metrics['dataset']} / {metrics['system']}")
    lines.append("")
    lines.append(f"- **Run directory:** `{metrics['run_dir']}`")
    lines.append(f"- **Instances:** {metrics['instances']}")
    lines.append(f"- **Questions:** {metrics['questions']}")
    lines.append("")

    lines.append("## Operations metrics")
    ops = metrics["ops"]
    lines.append(f"- Total tokens: {ops['total_tokens']:,}")
    lines.append(f"- Total cost USD: ${ops['total_cost_usd']:.6f}")
    lines.append(f"- Mean latency / question: {ops['mean_latency_ms']} ms")
    lines.append(f"- Total tool calls: {ops['total_tool_calls']}")
    lines.append(f"- Truncation rate: {ops['truncation_rate']:.2%}")
    lines.append("")

    probes = metrics.get("probes")
    if probes and probes["count"]:
        lines.append("## Update probes")
        lines.append(f"- Count: {probes['count']}")
        lines.append(f"- Accuracy: {probes['accuracy']:.2%}")
        lines.append("")

    if "subem" in metrics:
        lines.append("## MemoryAgentBench SubEM")
        subem = metrics["subem"]
        lines.append(f"- Overall: {subem['overall']:.2%} ({subem['count']} questions)")
        if subem.get("per_split"):
            lines.append("")
            lines.append("### Per split")
            for split, acc in sorted(subem["per_split"].items()):
                lines.append(f"- {split}: {acc:.2%}")
        if subem.get("per_group"):
            lines.append("")
            lines.append("### Per source group")
            for group, acc in sorted(subem["per_group"].items()):
                lines.append(f"- {group}: {acc:.2%}")
        lines.append("")

    if "judge" in metrics:
        lines.append("## LongMemEval official judge")
        judge = metrics["judge"]
        lines.append(f"- Overall: {judge['overall']:.2%} ({judge['count']} questions)")
        if judge.get("per_question_type"):
            lines.append("")
            lines.append("### Per question type")
            for qtype, acc in sorted(judge["per_question_type"].items()):
                lines.append(f"- {qtype}: {acc:.2%}")
        lines.append("")

    if "recall_at_k" in metrics:
        lines.append("## Recall@k")
        lines.append(f"- Mean: {metrics['recall_at_k']['mean']:.2%}")
        lines.append(f"- Count: {metrics['recall_at_k']['count']}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def score_run(
    run_dir: Path | str,
    judge_client: Optional[LLMClient] = None,
    references: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Score a single run directory and write metrics.json + metrics.md."""
    run_dir = Path(run_dir)
    if not run_dir.exists():
        raise FileNotFoundError(f"Run directory not found: {run_dir}")

    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    metadata = [
        json.loads(line)
        for line in (run_dir / "metadata.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    predictions = [
        json.loads(line)
        for line in (run_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    dataset = summary["dataset"]
    system = summary["system"]
    metrics: dict[str, Any] = {
        "run_dir": str(run_dir),
        "dataset": dataset,
        "system": system,
        "instances": summary["instances"],
        "questions": summary["questions"],
    }

    # Probe results (if present)
    probe_path = run_dir / "probe_results.json"
    if probe_path.exists():
        probe_results = json.loads(probe_path.read_text(encoding="utf-8"))
    else:
        probe_results = []
    metrics["probes"] = aggregate_probes(probe_results)

    # Operations metrics
    metrics["ops"] = aggregate_ops(metadata, summary)

    if dataset == "mab":
        metrics["subem"] = score_mab_predictions(predictions)
    elif dataset == "longmemeval":
        if judge_client is None:
            judge_client = LLMClient(
                model=JUDGE_MODEL,
                temperature=0,
                endpoint=AZURE_OPENAI_ENDPOINT,
                api_key=AZURE_OPENAI_API_KEY,
            )
        judge_results = run_longmemeval_judge(run_dir / "predictions.jsonl", judge_client, references=references)
        metrics["judge"] = aggregate_judge_results(judge_results)
        metrics["recall_at_k"] = compute_recall_at_k(metadata, references)
    else:
        raise ValueError(f"Unknown dataset: {dataset}")

    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / "metrics.md").write_text(_render_metrics_md(metrics), encoding="utf-8")
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score a benchmark run directory")
    parser.add_argument("--run-dir", required=True, help="Path to the run directory")
    parser.add_argument(
        "--judge-model",
        default=JUDGE_MODEL,
        help="Judge model for LongMemEval (default: gpt-4o)",
    )
    args = parser.parse_args(argv)

    judge_client = LLMClient(
        model=args.judge_model,
        temperature=0,
        endpoint=AZURE_OPENAI_ENDPOINT,
        api_key=AZURE_OPENAI_API_KEY,
    )
    metrics = score_run(args.run_dir, judge_client=judge_client)
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
