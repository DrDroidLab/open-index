"""Prompt templates for the benchmark agents."""

from bench.prompts.templates import (
    answer_system_prompt,
    ingest_system_prompt,
    probe_judge_prompt,
    probe_search_prompt,
    update_probe_extraction_prompt,
)

__all__ = [
    "ingest_system_prompt",
    "answer_system_prompt",
    "update_probe_extraction_prompt",
    "probe_search_prompt",
    "probe_judge_prompt",
]
