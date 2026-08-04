"""Update/contradiction probe extraction and probing."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from droid_brain.brain import Brain
from droid_brain.models import Entity

from bench.ir.types import BenchmarkInstance, Question
from bench.llm.client import LLMClient
from bench.prompts import probe_judge_prompt, probe_search_prompt, update_probe_extraction_prompt


@dataclass
class UpdateProbe:
    question_id: str
    subject: str
    attribute: str
    expected_value: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _extract_json_block(text: str) -> dict[str, Any]:
    """Parse a JSON object from a response that may contain markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("\n```", 1)[0]
    return json.loads(text)


def _normalize_value(value: str) -> str:
    return str(value).lower().strip().strip(".\"'")


def _value_fuzzy_match(probe_value: str, gold_answers: list[str]) -> bool:
    """Return True if the probe value is a substring of any gold answer (case-insensitive)."""
    probe = _normalize_value(probe_value)
    if not probe:
        return False
    for answer in gold_answers:
        if probe in _normalize_value(answer):
            return True
    return False


def _is_update_question(question: Question, instance_metadata: dict[str, Any]) -> bool:
    """Return True if the question tests a value that changes over time."""
    ability = question.ability
    coarse_group = instance_metadata.get("coarse_group", "")
    if ability == "knowledge-update":
        return True
    if "conflict_resolution" in coarse_group:
        return True
    return False


def extract_probes(
    instances: list[BenchmarkInstance],
    llm_client: LLMClient,
    cache_path: Path | None = None,
) -> list[UpdateProbe]:
    """Extract and validate update probes for contradiction-heavy questions.

    Uses an LLM to parse (subject, attribute, expected_value) from the question
    and gold answer, then discards probes whose expected_value does not fuzzy-match
    a gold answer. Caches the validated probes to `cache_path` when provided.
    """
    probes: list[UpdateProbe] = []
    for instance in instances:
        for question in instance.questions:
            if not _is_update_question(question, instance.metadata):
                continue
            prompt = update_probe_extraction_prompt(question.text, question.gold_answers)
            response = llm_client.chat(
                messages=[
                    {"role": "system", "content": "You extract structured update probes from QA pairs."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=512,
            )
            try:
                data = _extract_json_block(response.content)
            except (json.JSONDecodeError, KeyError):
                continue
            for p in data.get("probes", []):
                expected = p.get("expected_value", "")
                if not _value_fuzzy_match(expected, question.gold_answers):
                    continue
                probes.append(
                    UpdateProbe(
                        question_id=question.question_id,
                        subject=p.get("subject", ""),
                        attribute=p.get("attribute", ""),
                        expected_value=expected,
                    )
                )

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps([p.to_json() for p in probes], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    return probes


def load_probes(cache_path: Path) -> list[UpdateProbe]:
    """Load cached probes."""
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    return [UpdateProbe(**item) for item in raw]


# ---------------------------------------------------------------------------
# Probing implementations
# ---------------------------------------------------------------------------


def probe_structured(brain: Brain, probe: UpdateProbe) -> bool:
    """Directly inspect the structured brain for the expected current value."""
    entity_id = f"user_fact:{probe.subject.lower().strip()}-{probe.attribute.lower().strip()}"
    entity = brain.get_entity(entity_id)
    if entity is None:
        return False
    stored = entity.fields.get("value", "")
    return _value_fuzzy_match(probe.expected_value, [str(stored)])


def probe_flat(brain: Brain, probe: UpdateProbe, llm_client: LLMClient) -> bool:
    """Search the flat memory for the subject/attribute and LLM-judge the result."""
    query = probe_search_prompt(probe.subject, probe.attribute)
    results = brain.search(query=query, doc_types=["memory"], limit=1)
    if not results.results:
        return False
    raw = results.results[0].get("entity")
    if raw is None:
        return False
    entity = Entity.from_dict(raw)
    text = entity.fields.get("text", "")
    judge_prompt = probe_judge_prompt(text, probe.subject, probe.attribute, probe.expected_value)
    response = llm_client.chat(
        messages=[
            {"role": "system", "content": "You are a strict judge."},
            {"role": "user", "content": judge_prompt},
        ],
        max_tokens=32,
    )
    return response.content.strip().lower().startswith("yes")


def probe_long_context(
    events: list[Any],
    probe: UpdateProbe,
    llm_client: LLMClient,
) -> bool:
    """Ask an LLM to judge the current value from a chronological evidence list."""
    evidence = "\n\n".join(
        f"[{i}] {getattr(e, 'text', str(e))}" for i, e in enumerate(events)
    )
    prompt = (
        f"Evidence (oldest first):\n\n{evidence}\n\n"
        f"What is the current value of '{probe.attribute}' for '{probe.subject}'? "
        f"Return only the value (one phrase)."
    )
    response = llm_client.chat(
        messages=[
            {"role": "system", "content": "You answer based only on the evidence provided."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=128,
    )
    return _value_fuzzy_match(response.content, [probe.expected_value])
