# droid-brain benchmark harness (`bench/`)

This directory contains the evaluation harness used to compare memory-system
arms (structured brain, flat brain, long-context baseline) on long-context and
update-heavy QA benchmarks. It imports the core `droid_brain` package as a
library and does not change production code.

## Status

Phase 3 (tasks 8–10) is implemented on top of Phases 1–2. The runner now also
records `retrieved_source_ids` and update-probe results. The scorer computes
SubEM for MemoryAgentBench, runs the official LongMemEval LLM judge against our
Azure endpoint, computes Recall@k, aggregates operations metrics, and produces
`metrics.json` + `metrics.md`. The report generator combines scored runs into a
single markdown report with per-ability tables, probe correctness, and a findings
skeleton. The smoke runner executes the full validation matrix with a hard cost
cap and prints the total cost.

### LongMemEval judge provenance

The official LongMemEval evaluation script (`src/evaluation/evaluate_qa.py`) and
the `print_qa_metrics.py` helper were fetched from
<https://github.com/xiaowu0162/LongMemEval> into `bench/cache/eval_assets/`.
The harness uses the official per-question-type rubrics from that script. When
the script's extra dependencies (`tqdm`, `backoff`) are available, the harness
imports the `get_anscheck_prompt` function directly; otherwise it falls back to
a verbatim re-implementation of the same prompts.

## Prerequisites

- Python 3.12+ (already installed in this environment)
- `droid_brain` installed from the repo root: `pip install -e .`
- Bench-specific packages already installed: `openai`, `datasets`, `pytest`,
  `pandas`, `tiktoken`, `PyYAML`, `requests`
- LLM credentials: the harness supports either Azure OpenAI or a standard
  OpenAI account.
  - Azure OpenAI (preferred): set `AZURE_OPENAI_ENDPOINT` (base URL ending in
    `/openai/v1`) and `AZURE_OPENAI_API_KEY`.
  - Standard OpenAI: set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL`).
  - Credentials can be placed in `/code/.secrets/azure-openai.env` (outside the
    repo, mode 600) or exported as environment variables. If the env file is
    missing, the harness falls back to the OpenAI credentials when available.

The harness never writes the key into the repository and never logs it.

## Dataset licenses

- **LongMemEval-S cleaned** (`xiaowu0162/longmemeval-cleaned`): MIT license.
- **MemoryAgentBench** (`ai-hyz/MemoryAgentBench`): MIT license.

See the Hugging Face dataset cards for full license text and attribution.

## How to run

Fetch and verify the datasets:

```bash
python3 -m bench.data.fetch_eval_assets --verify
```

Run the unit tests:

```bash
python3 -m pytest bench/tests -q
```

Run one system on one instance of LongMemEval (micro-test):

```bash
python3 -m bench.harness.runner --dataset longmemeval --max-instances 1 --system structured
python3 -m bench.harness.runner --dataset longmemeval --max-instances 1 --system flat
python3 -m bench.harness.runner --dataset longmemeval --max-instances 1 --system longctx
```

Run on MemoryAgentBench:

```bash
python3 -m bench.harness.runner --dataset mab --split Accurate_Retrieval --max-instances 10 --system structured
python3 -m bench.harness.runner --dataset mab --split Conflict_Resolution --max-instances 10 --system flat
```

Score a run:

```bash
python3 -m bench.harness.scoring --run-dir bench/results/<dataset>/<system>
```

Generate a combined report from scored runs:

```bash
python3 -m bench.harness.report --runs bench/results/<dataset>/<system> [...] --out bench/results/report.md
```

Run the full smoke matrix (default n=5, all 3 systems, includes MAB):

```bash
python3 -m bench.run_smoke --n 5 --systems structured,flat,longctx
```

Validate the wiring with one cheap instance (use `gpt-4o-mini` as the judge if
the Azure deployment does not expose `gpt-4o`):

```bash
python3 -m bench.run_smoke --n 1 --systems flat --skip-mab --judge-model gpt-4o-mini
```

Results are written under `bench/results/<dataset>/<system>/`:

- `predictions.jsonl` — official-format predictions.
- `metadata.jsonl` — per-question usage, latency, tool calls, retrieved source ids, truncation flag.
- `probe_results.json` — update/contradiction probe results (brain-backed systems only).
- `summary.json` — aggregate cost and instance/question counts.
- `metrics.json` / `metrics.md` — scored metrics and a human-readable summary.

## Layout

```
bench/
├── config.py              # paths, model constants, env-file loading, RunConfig
├── README.md              # this file
├── cache/                 # downloaded datasets and extracted probes (gitignored)
├── configs/
│   ├── structured/        # droid-brain config + doc_types for the structured arm
│   └── flat/              # droid-brain config + doc_type for the flat arm
├── data/
│   ├── fetch_eval_assets.py   # download/cache datasets from Hugging Face
│   ├── longmemeval.py         # LongMemEval-S cleaned adapter
│   └── memoryagentbench.py    # MemoryAgentBench adapter
├── harness/
│   ├── runner.py          # dataset x system matrix executor
│   ├── probes.py          # update/contradiction probe extraction + validation
│   ├── scoring.py         # SubEM, official judge, Recall@k, ops/probe metrics
│   └── report.py          # combined markdown report generator
├── run_smoke.py           # smoke matrix runner with cost cap
├── ir/
│   └── types.py           # EvidenceEvent, Question, BenchmarkInstance
├── llm/
│   └── client.py          # OpenAI client wrapper + FakeLLMClient for tests
├── prompts/
│   └── templates.py       # agent + probe prompts
├── systems/
│   ├── base.py            # MemorySystem abstract base + Answer
│   ├── structured_brain.py   # typed droid-brain arm
│   ├── flat_memory.py        # single unstructured doc_type baseline
│   └── long_context.py       # sliding-window prompt-only baseline
├── tests/                 # pytest unit tests + optional integration smoke
└── results/               # generated reports (raw logs are gitignored)
```

## Design notes

- **Temp brain per instance:** Each benchmark instance gets its own temporary
  brain directory seeded from `bench/configs/structured/` or `bench/configs/flat/`,
  with a unique SQLite path. The directory is removed after the instance unless
  `--keep-state` is enabled. This is the zero-core-change strategy: every instance
  starts fresh and there is no cross-contamination between questions.

- **Same backend for both brain arms:** Both `StructuredBrainMemory` and
  `FlatMemoryBaseline` use `droid_brain`'s SQLite+FTS5 backend. Structure is the
  only intentional difference.

- **Question-date filtering:** LongMemEval instances are guaranteed to have all
  haystack sessions precede the question date. The harness therefore ingests
  every event for an instance and records the invariant; it does not filter at
  answer time by default. For MemoryAgentBench there is no date bound.

- **Cost accounting:** The LLM client keeps a cumulative cost ledger using the
  plan's pricing constants (`gpt-4o-mini` $0.15/$0.60 per 1M tokens, `gpt-4o`
  $2.50/$10.00 per 1M tokens). Every call's prompt/completion token counts are
  recorded in the per-answer metadata.

- **Determinism:** The runner fixes temperature to 0, processes events in
  chronological order, and does not depend on wall-clock time for outputs.
