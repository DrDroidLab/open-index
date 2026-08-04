"""Download and cache benchmark datasets from Hugging Face."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from datasets import load_dataset
from huggingface_hub import hf_hub_download

from bench.config import CACHE_DIR, DatasetCacheConfig
from bench.data.longmemeval import LONGMEMEVAL_FILE
from bench.data.memoryagentbench import MEMORYAGENTBENCH_SPLITS as _MAB_SPLIT_NAMES

LONGMEMEVAL_REPO = "xiaowu0162/longmemeval-cleaned"
MEMORYAGENTBENCH_REPO = "ai-hyz/MemoryAgentBench"
MEMORYAGENTBENCH_SPLITS = list(_MAB_SPLIT_NAMES)


def _longmemeval_cache_path(cfg: DatasetCacheConfig) -> Path:
    return cfg.longmemeval_dir / LONGMEMEVAL_FILE


def _memoryagentbench_split_path(cfg: DatasetCacheConfig, split: str) -> Path:
    return cfg.memoryagentbench_dir / f"{split}.jsonl"


def fetch_longmemeval(cfg: DatasetCacheConfig) -> Path:
    """Download and cache the LongMemEval-S cleaned JSON file.

    Idempotent: if the file already exists, return its path without re-downloading.
    """
    cfg.longmemeval_dir.mkdir(parents=True, exist_ok=True)
    dest = _longmemeval_cache_path(cfg)
    if dest.exists():
        return dest
    print(f"Downloading {LONGMEMEVAL_REPO}/{LONGMEMEVAL_FILE} ...", file=sys.stderr)
    downloaded = hf_hub_download(
        repo_id=LONGMEMEVAL_REPO,
        filename=LONGMEMEVAL_FILE,
        repo_type="dataset",
        local_dir=cfg.longmemeval_dir,
    )
    # hf_hub_download may create a symlink; copy to the canonical name if needed.
    downloaded_path = Path(downloaded)
    if downloaded_path != dest and not dest.exists():
        dest.write_bytes(downloaded_path.read_bytes())
    return dest


def fetch_memoryagentbench(cfg: DatasetCacheConfig) -> dict[str, Path]:
    """Download and cache the MemoryAgentBench splits as JSONL files.

    Idempotent: if the split JSONL file already exists, skip re-downloading.
    """
    cfg.memoryagentbench_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for split in MEMORYAGENTBENCH_SPLITS:
        split_file = _memoryagentbench_split_path(cfg, split)
        if split_file.exists():
            paths[split] = split_file
            continue
        print(f"Downloading {MEMORYAGENTBENCH_REPO}/{split} ...", file=sys.stderr)
        ds = load_dataset(MEMORYAGENTBENCH_REPO, split=split)
        with split_file.open("w", encoding="utf-8") as f:
            for row in ds:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        paths[split] = split_file
    return paths


def fetch_all(cfg: DatasetCacheConfig | None = None) -> dict[str, Any]:
    """Fetch all Phase 1 datasets."""
    if cfg is None:
        cfg = DatasetCacheConfig()
    cfg.ensure()
    longmemeval_path = fetch_longmemeval(cfg)
    memoryagentbench_paths = fetch_memoryagentbench(cfg)
    return {
        "longmemeval": longmemeval_path,
        "memoryagentbench": memoryagentbench_paths,
    }


def _count_longmemeval_rows(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        # Some HF JSON files are keyed by split; take the first list value.
        for v in data.values():
            if isinstance(v, list):
                return len(v)
    return 0


def _count_memoryagentbench_rows(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def verify(cfg: DatasetCacheConfig | None = None) -> None:
    """Print cached dataset row counts."""
    if cfg is None:
        cfg = DatasetCacheConfig()
    longmemeval_path = _longmemeval_cache_path(cfg)
    print("Cache directory:", cfg.longmemeval_dir.parent)
    if longmemeval_path.exists():
        count = _count_longmemeval_rows(longmemeval_path)
        print(f"LongMemEval-S cleaned: {count} rows ({longmemeval_path})")
    else:
        print(f"LongMemEval-S cleaned: NOT CACHED ({longmemeval_path})")
    for split in MEMORYAGENTBENCH_SPLITS:
        split_dir = _memoryagentbench_split_path(cfg, split)
        if split_dir.exists():
            count = _count_memoryagentbench_rows(split_dir)
            print(f"MemoryAgentBench/{split}: {count} rows ({split_dir})")
        else:
            print(f"MemoryAgentBench/{split}: NOT CACHED ({split_dir})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and verify benchmark datasets")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Only print cached counts; do not download.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Override the cache directory (default: bench/cache).",
    )
    args = parser.parse_args()
    cfg = DatasetCacheConfig(
        longmemeval_dir=(args.cache_dir or CACHE_DIR) / "longmemeval",
        memoryagentbench_dir=(args.cache_dir or CACHE_DIR) / "memoryagentbench",
    )
    if args.verify:
        verify(cfg)
    else:
        fetch_all(cfg)
        verify(cfg)


if __name__ == "__main__":
    main()
