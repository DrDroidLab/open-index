"""Reading entity records out of the file formats people actually have.

`open-index import` accepts three shapes, because the data you want in a brain
usually arrives as one of them:

    .json   a single object, or an array of objects
    .jsonl  one JSON object per line (streaming exports)
    .csv    a header row plus rows (spreadsheet / CRM / SQL export)

Everything is normalized to the same loose dict that `Entity.from_dict` takes:
reserved keys (`id`, `doc_type`, `name`, `related_to`, `provenance`,
`valid_from`, `valid_to`) stay top-level, and every other column becomes a
schema field. Parsing never raises on a single bad row — it collects the problem
and continues, so one malformed line doesn't cost you the import.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Optional

# Keys that mean something to Entity itself; everything else is a schema field.
RESERVED = {"id", "doc_type", "name", "related_to", "provenance",
            "valid_from", "valid_to", "fields"}

# Values a CSV cell may use to mean "no value". CSV has no null, so an empty
# cell would otherwise be stored as the empty string and defeat required-field
# checks and `search: none` handling alike.
_CSV_NULLS = {"", "null", "none", "n/a", "na", "-"}


def load_entity_records(
    path: Path,
    doc_type: Optional[str] = None,
    id_field: str = "id",
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (records, errors) read from `path`.

    `doc_type` supplies the type for rows that don't name one (and is required
    for CSV, which has no place to put it otherwise). `id_field` names the
    column holding the id; a bare slug is prefixed with the doc_type so
    `checkout` becomes `product:checkout`.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        if not doc_type:
            raise ValueError(
                "--doc-type is required for CSV (a CSV has nowhere to declare it)"
            )
        raw_rows, errors = _read_csv(path)
    elif suffix == ".jsonl":
        raw_rows, errors = _read_jsonl(path)
    elif suffix == ".json":
        raw_rows, errors = _read_json(path)
    else:
        raise ValueError(
            f"unsupported file type '{suffix or path.name}' — use .json, .jsonl, or .csv"
        )

    records: list[dict[str, Any]] = []
    for i, row in enumerate(raw_rows):
        try:
            records.append(_normalize(row, doc_type, id_field, line=i + 1))
        except ValueError as exc:
            errors.append(str(exc))
    return records, errors


# -- per-format readers -------------------------------------------------------


def _read_json(path: Path) -> tuple[list[dict], list[str]]:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [], [f"{path.name}: invalid JSON ({exc})"]
    if isinstance(data, dict):
        return [data], []
    if isinstance(data, list):
        return data, []
    return [], [f"{path.name}: expected an object or array, got {type(data).__name__}"]


def _read_jsonl(path: Path) -> tuple[list[dict], list[str]]:
    rows, errors = [], []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append(f"line {i}: invalid JSON ({exc})")
    return rows, errors


def _read_csv(path: Path) -> tuple[list[dict], list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return [], [f"{path.name}: empty file (no header row)"]
        rows = []
        for row in reader:
            # Drop empty cells so they don't become empty-string field values.
            rows.append({
                k: _coerce_csv(v)
                for k, v in row.items()
                if k is not None and str(v).strip().lower() not in _CSV_NULLS
            })
    return rows, []


def _coerce_csv(value: Any) -> Any:
    """CSV is all strings; recover the obvious scalars so number/boolean fields
    validate. Anything ambiguous stays a string — guessing too hard is worse
    than leaving it alone."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    lowered = text.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


# -- normalization ------------------------------------------------------------


def _normalize(
    row: Any, doc_type: Optional[str], id_field: str, line: int
) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"row {line}: expected an object, got {type(row).__name__}")

    record = dict(row)
    resolved_type = record.get("doc_type") or doc_type
    if not resolved_type:
        raise ValueError(
            f"row {line}: no doc_type on the row and no --doc-type given"
        )
    record["doc_type"] = resolved_type

    # The id may live under a different column, and may be a bare slug.
    raw_id = record.pop(id_field, None) if id_field != "id" else record.get("id")
    if raw_id in (None, ""):
        raise ValueError(f"row {line}: no '{id_field}' value to build an id from")
    record["id"] = _qualify_id(str(raw_id), resolved_type)

    if isinstance(record.get("related_to"), str):
        record["related_to"] = _parse_edges(record["related_to"])
    return record


def _qualify_id(raw: str, doc_type: str) -> str:
    """Accept both `product:checkout` and a bare `checkout`."""
    return raw if ":" in raw else f"{doc_type}:{raw}"


def _parse_edges(cell: str) -> list[dict[str, str]]:
    """Parse a CSV `related_to` cell.

    Format: semicolon-separated edges, each `target|meaning` (meaning optional):

        service:api|depends on; datastore:pg|writes to
    """
    edges = []
    for part in cell.split(";"):
        part = part.strip()
        if not part:
            continue
        target, _, meaning = part.partition("|")
        edges.append({
            "target": target.strip(),
            "relationship_edge_meaning": meaning.strip(),
        })
    return edges
