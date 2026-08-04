# droid-brain benchmark harness (`bench/`)

This directory contains the evaluation harness used to compare memory-system
arms (structured brain, flat brain, long-context baseline) on long-context and
update-heavy QA benchmarks. It imports the core `droid_brain` package as a
library and does not change production code.

## Status

Phase 2 (tasks 5–7) is implemented on top of Phase 1: LLM client wrapper,
memory-system arms (structured, flat, long-context), runner, and
update/contradiction probes. The harness writes official-format prediction
JSONL files and per-instance metadata.

## Prerequisites

- Python 3.12+ (already installed in this environment)
- `droid_brain` installed from the repo root: `pip install -e .`
- Bench-specific packages already installed: `openai`, `datasets`, `pytest`,
  `pandas`, `tiktoken`, `PyYAML`, `requests`
- LLM credentials: the harness expects Azure OpenAI credentials in
  `/code/.secrets/azure-openai.env` (outside the repo, mode 600) as:
  - `AZURE_OPENAI_ENDPOINT` — base URL ending in `/openai/v1`
  - `AZURE_OPENAI_API_KEY`

If the environment variables are already exported, the env file is skipped. The
harness never writes the key into the repository and never logs it. If the env
file is missing, the harness fails early with a clear message.

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

Results are written under `bench/results/<dataset>/<system>/`:

- `predictions.jsonl` — official-format predictions.
- `metadata.jsonl` — per-question usage, latency, tool calls, truncation flag.
- `summary.json` — aggregate cost and instance/question counts.

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
│   └── probes.py          # update/contradiction probe extraction + validation
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
