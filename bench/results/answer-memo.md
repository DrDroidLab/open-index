# Benchmark answer memo: does structured memory help?

This memo is the final judgment document for the droid-brain benchmark harness.
It is filled with placeholders and instructions after the smoke / full runs are
complete. Replace the bracketed placeholders with observed numbers and cite the
relevant tables in `bench/results/report.md`.

## Hypothesis

Structured memory (typed entities, stable IDs, explicit relationships) improves
retrieval, updating, and contradiction resolution compared to a flat memory
baseline and a long-context baseline, when all three use the same LLM and the
same backend.

## Capability verdicts

### 1. Retrieval

- **Verdict:** [better / worse / inconclusive — placeholder]
- **Evidence:**
  - LongMemEval overall accuracy: structured [X%] vs flat [Y%] vs long-context [Z%].
  - LongMemEval per-ability accuracy: single-session-user [X%] vs [Y%]; multi-session [X%] vs [Y%]; temporal-reasoning [X%] vs [Y%].
  - MemoryAgentBench Accurate_Retrieval SubEM: structured [X%] vs flat [Y%] vs long-context [Z%].
  - Recall@k (LongMemEval): structured [X%] vs flat [Y%].
- **Caveats:**
  - LongMemEval is persona-flavored chat history, not organizational knowledge.
  - MemoryAgentBench Accurate_Retrieval overlaps with LongMemEval-S; exclude `longmemeval_s*` sub-sources when directly comparing the two datasets.

### 2. Updating

- **Verdict:** [better / worse / inconclusive — placeholder]
- **Evidence:**
  - LongMemEval knowledge-update accuracy: structured [X%] vs flat [Y%] vs long-context [Z%].
  - Update-probe correctness: structured [X%] vs flat [Y%] (long-context skipped for probes).
- **Caveats:**
  - The agent uses `gpt-4o-mini`; schema-write failures in the structured arm would understate its advantage. If smoke results show such failures, re-run the structured arm with `gpt-4o` as a sensitivity check.

### 3. Contradiction resolution

- **Verdict:** [better / worse / inconclusive — placeholder]
- **Evidence:**
  - MemoryAgentBench Conflict_Resolution SubEM (overall): structured [X%] vs flat [Y%].
  - Conflict_Resolution SH split: structured [X%] vs flat [Y%].
  - Conflict_Resolution MH split: structured [X%] vs flat [Y%].
  - Update-probe correctness on conflict-heavy questions: structured [X%] vs flat [Y%].
- **Caveats:**
  - MH rows exercise multi-hop contradiction, SH rows exercise single-hop.
  - Long-context is not probed for store-state correctness because it does not persist a memory store.

## Evidence table references

| Capability | Primary table | Supporting metric |
| --- | --- | --- |
| Retrieval | LongMemEval per-ability accuracy | MAB Accurate_Retrieval SubEM, Recall@k |
| Updating | LongMemEval knowledge-update accuracy | Update-probe correctness |
| Contradiction resolution | MAB Conflict_Resolution SubEM | Update-probe correctness (SH/MH) |

## Follow-ups

1. [Fill with any sensitivity checks, e.g., re-run structured arm with `gpt-4o` if mini underperforms.]
2. [Fill with any backend or embedding baseline comparisons.]
3. [Fill with any custom org-knowledge benchmark proposal.]
