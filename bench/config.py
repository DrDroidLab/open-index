"""Bench configuration: paths, model constants, env loading, run config."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "bench"
CACHE_DIR = BENCH_DIR / "cache"
RESULTS_DIR = BENCH_DIR / "results"

# ---------------------------------------------------------------------------
# Model constants (owner decisions locked in plan §9)
# ---------------------------------------------------------------------------

AGENT_MODEL = "gpt-4o-mini"
JUDGE_MODEL = "gpt-4o"
TEMPERATURE = 0.0
AGENT_DEFAULT_MAX_TOKENS = 2048

# ---------------------------------------------------------------------------
# Env loading (reads /code/.secrets/azure-openai.env if env vars are unset)
# ---------------------------------------------------------------------------

SECRETS_ENV = Path("/code/.secrets/azure-openai.env")


def load_env_file(path: Path = SECRETS_ENV) -> None:
    """Load a simple KEY=VALUE env file into os.environ if it exists.

    Only sets variables that are not already present in the environment.
    Lines that are blank or start with '#' are ignored. Values are stripped of
    surrounding quotes.
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            if key and os.environ.get(key) is None:
                os.environ[key] = value


load_env_file()

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")


def ensure_llm_credentials() -> tuple[str, str]:
    """Return (endpoint, api_key) after loading the env file if needed.

    Raises RuntimeError if the credentials are not available.
    """
    if not AZURE_OPENAI_ENDPOINT or not AZURE_OPENAI_API_KEY:
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY must be set. "
            "Put them in /code/.secrets/azure-openai.env (outside the repo) or "
            "export them as environment variables."
        )
    return AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunConfig:
    """Configuration for one benchmark run."""

    dataset: str
    split: Optional[str] = None
    source_filter: Optional[str] = None
    system_name: str = "flat"
    max_instances: Optional[int] = None
    k: int = 5
    temperature: float = TEMPERATURE
    model: str = AGENT_MODEL
    judge: str = JUDGE_MODEL
    seed: int = 42
    keep_state: bool = False
    smoke: bool = False
    smoke_n: int = 5
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ValueError("k must be positive")
        if self.temperature < 0 or self.temperature > 2:
            raise ValueError("temperature must be in [0, 2]")


@dataclass(frozen=True)
class DatasetCacheConfig:
    """Cache paths for a dataset."""

    longmemeval_dir: Path = CACHE_DIR / "longmemeval"
    memoryagentbench_dir: Path = CACHE_DIR / "memoryagentbench"

    def ensure(self) -> None:
        self.longmemeval_dir.mkdir(parents=True, exist_ok=True)
        self.memoryagentbench_dir.mkdir(parents=True, exist_ok=True)
