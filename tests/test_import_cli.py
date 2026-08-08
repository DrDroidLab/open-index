"""`open-index import` — the bulk path from the terminal."""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_index.brain import Brain
from open_index.cli import app

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"
runner = CliRunner()


@pytest.fixture
def brain_dir(tmp_path):
    dst = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, dst)
    runner.invoke(app, ["index", "--brain", str(dst)])
    return dst


def run(*args):
    return runner.invoke(app, [str(a) for a in args])


def test_import_json_array(brain_dir, tmp_path):
    src = tmp_path / "e.json"
    src.write_text(json.dumps([
        {"doc_type": "issue", "id": "issue:i1", "name": "I1"},
        {"doc_type": "issue", "id": "issue:i2", "name": "I2"},
    ]))
    result = run("import", src, "--brain", brain_dir)
    assert result.exit_code == 0
    assert "imported 2 entities" in result.stdout
    assert Brain.open(brain_dir).get_entity("issue:i2") is not None


def test_import_csv(brain_dir, tmp_path):
    src = tmp_path / "e.csv"
    src.write_text("id,name,severity\nc1,C1,high\nc2,C2,low\n")
    result = run("import", src, "--brain", brain_dir, "-t", "issue")
    assert result.exit_code == 0
    assert Brain.open(brain_dir).get_entity("issue:c1").fields["severity"] == "high"


def test_import_jsonl(brain_dir, tmp_path):
    src = tmp_path / "e.jsonl"
    src.write_text('{"doc_type":"issue","id":"issue:j1","name":"J1"}\n')
    assert run("import", src, "--brain", brain_dir).exit_code == 0


def test_import_reports_bad_rows_and_still_writes_the_rest(brain_dir, tmp_path):
    src = tmp_path / "e.jsonl"
    src.write_text(
        '{"doc_type":"issue","id":"issue:good","name":"Good"}\n'
        "{broken}\n"
        '{"doc_type":"ghost","id":"ghost:x","name":"Ghost"}\n'
    )
    result = run("import", src, "--brain", brain_dir)
    assert result.exit_code == 1, "a partially-failed import must not report success"
    assert "imported 1 entities" in result.stdout
    assert "2 skipped" in result.stdout
    assert Brain.open(brain_dir).get_entity("issue:good") is not None


def test_import_reports_rows_that_parse_but_are_not_valid_entities(brain_dir, tmp_path):
    """Well-formed JSON, unusable id — reported per row, not as a crash."""
    src = tmp_path / "e.csv"
    src.write_text("id,name\nhas space,Bad Id\nfine,Fine\n")
    result = run("import", src, "--brain", brain_dir, "-t", "issue")
    assert result.exit_code == 1
    assert "row 1" in result.output
    assert "imported 1 entities" in result.stdout
    assert Brain.open(brain_dir).get_entity("issue:fine") is not None


def test_import_attaches_batch_provenance(brain_dir, tmp_path):
    src = tmp_path / "e.csv"
    src.write_text("id,name\np1,P1\n")
    run("import", src, "--brain", brain_dir, "-t", "issue",
        "--asserted-by", "import:crm", "--confidence", "0.7")

    stored = Brain.open(brain_dir).get_entity("issue:p1")
    assert stored.provenance.asserted_by == "import:crm"
    assert stored.provenance.confidence == 0.7
    assert stored.provenance.asserted_at, "asserted_at should be stamped"


def test_import_confidence_alone_is_enough(brain_dir, tmp_path):
    src = tmp_path / "e.csv"
    src.write_text("id,name\np2,P2\n")
    run("import", src, "--brain", brain_dir, "-t", "issue", "--confidence", "0.4")
    assert Brain.open(brain_dir).get_entity("issue:p2").provenance.confidence == 0.4


def test_import_rejects_out_of_range_confidence(brain_dir, tmp_path):
    src = tmp_path / "e.csv"
    src.write_text("id,name\np3,P3\n")
    result = run("import", src, "--brain", brain_dir, "-t", "issue",
                 "--confidence", "5")
    assert result.exit_code != 0


def test_dry_run_writes_nothing(brain_dir, tmp_path):
    src = tmp_path / "e.json"
    src.write_text(json.dumps([{"doc_type": "issue", "id": "issue:dry", "name": "Dry"}]))
    result = run("import", src, "--brain", brain_dir, "--dry-run")
    assert result.exit_code == 0
    assert "would be written" in result.stdout
    assert Brain.open(brain_dir).get_entity("issue:dry") is None


def test_dry_run_reports_problems_and_exits_nonzero(brain_dir, tmp_path):
    src = tmp_path / "e.json"
    src.write_text(json.dumps([{"doc_type": "ghost", "id": "ghost:x"}]))
    result = run("import", src, "--brain", brain_dir, "--dry-run")
    assert result.exit_code == 1
    assert "unknown doc_type" in result.output


def test_import_custom_id_field(brain_dir, tmp_path):
    src = tmp_path / "e.csv"
    src.write_text("slug,name\nfromslug,From Slug\n")
    run("import", src, "--brain", brain_dir, "-t", "issue", "--id-field", "slug")
    assert Brain.open(brain_dir).get_entity("issue:fromslug") is not None


def test_import_missing_file(brain_dir, tmp_path):
    result = run("import", tmp_path / "nope.json", "--brain", brain_dir)
    assert result.exit_code == 1
    assert "no such file" in result.output


def test_import_unsupported_format(brain_dir, tmp_path):
    src = tmp_path / "e.xlsx"
    src.write_text("")
    result = run("import", src, "--brain", brain_dir)
    assert result.exit_code == 1
    assert "unsupported file type" in result.output


def test_import_csv_without_doc_type_explains_why(brain_dir, tmp_path):
    src = tmp_path / "e.csv"
    src.write_text("id,name\na,A\n")
    result = run("import", src, "--brain", brain_dir)
    assert result.exit_code == 1
    assert "--doc-type is required for CSV" in result.output


def test_import_writes_files_for_file_backed_types(brain_dir, tmp_path):
    src = tmp_path / "e.csv"
    src.write_text("id,name\nfiled,Filed\n")
    result = run("import", src, "--brain", brain_dir, "-t", "issue")
    assert "file(s) written" in result.stdout
    assert (brain_dir / "entities" / "issue" / "filed.json").exists()
