"""End-to-end CLI coverage, driven through Typer's runner.

The CLI is the surface most users touch first, so these assert on what is
actually printed and on exit codes — a command that "works" but reports a
success message on failure is a real bug.
"""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from open_index.cli import app

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"
runner = CliRunner()


@pytest.fixture
def brain_dir(tmp_path):
    dst = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, dst)
    return dst


def run(*args, **kwargs):
    return runner.invoke(app, [str(a) for a in args], **kwargs)


# -- init ---------------------------------------------------------------------


def test_init_scaffolds_a_runnable_brain(tmp_path):
    target = tmp_path / "fresh"
    result = run("init", "fresh", target)
    assert result.exit_code == 0
    assert "created brain" in result.stdout
    for expected in ("brain.yaml", ".mcp.json", "CLAUDE.md", ".gitignore",
                     "doc_types/note.yaml", "entities/note/welcome.json",
                     ".claude/skills/edit-brain/SKILL.md"):
        assert (target / expected).exists(), expected


def test_init_defaults_directory_to_the_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("init", "mybrain").exit_code == 0
    assert (tmp_path / "mybrain" / "brain.yaml").exists()


def test_init_refuses_to_clobber(tmp_path):
    target = tmp_path / "fresh"
    run("init", "fresh", target)
    result = run("init", "fresh", target)
    assert result.exit_code == 1
    assert "already exists" in result.output


# -- add-doc-type -------------------------------------------------------------

def test_add_doc_type_writes_a_stub(brain_dir):
    result = run("add-doc-type", "ticket", "--brain", brain_dir)
    assert result.exit_code == 0
    written = (brain_dir / "doc_types" / "ticket.yaml").read_text()
    assert "doc_type: ticket" in written
    assert "storage: index" in written


def test_add_doc_type_storage_file(brain_dir):
    run("add-doc-type", "ticket", "--brain", brain_dir, "--storage", "file")
    assert "storage: file" in (brain_dir / "doc_types" / "ticket.yaml").read_text()


def test_add_doc_type_custom_color(brain_dir):
    run("add-doc-type", "ticket", "--brain", brain_dir, "--color", "#123456")
    assert "#123456" in (brain_dir / "doc_types" / "ticket.yaml").read_text()


def test_add_doc_type_rejects_bad_storage(brain_dir):
    result = run("add-doc-type", "ticket", "--brain", brain_dir, "--storage", "nope")
    assert result.exit_code == 1
    assert "must be 'index' or 'file'" in result.output


def test_add_doc_type_refuses_to_clobber(brain_dir):
    result = run("add-doc-type", "issue", "--brain", brain_dir)
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_add_doc_type_creates_missing_brain_dir(tmp_path):
    """The command works on a directory that has no doc_types/ yet."""
    target = tmp_path / "bare"
    assert run("add-doc-type", "thing", "--brain", target).exit_code == 0
    assert (target / "doc_types" / "thing.yaml").exists()


# -- add-entity ---------------------------------------------------------------


def test_add_entity_single(brain_dir, tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps({"doc_type": "issue", "id": "issue:new", "name": "New"}))
    result = run("add-entity", f, "--brain", brain_dir)
    assert result.exit_code == 0
    assert "stored 1" in result.stdout


def test_add_entity_accepts_a_list(brain_dir, tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps([
        {"doc_type": "issue", "id": "issue:a", "name": "A"},
        {"doc_type": "issue", "id": "issue:b", "name": "B"},
    ]))
    result = run("add-entity", f, "--brain", brain_dir)
    assert "stored 2" in result.stdout


def test_add_entity_rejects_invalid(brain_dir, tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps({"doc_type": "ghost", "id": "ghost:x", "name": "X"}))
    result = run("add-entity", f, "--brain", brain_dir)
    assert result.exit_code == 1
    assert "unknown doc_type" in result.output


# -- index / validate / search ------------------------------------------------


def test_index(brain_dir):
    result = run("index", "--brain", brain_dir)
    assert result.exit_code == 0
    assert "indexed" in result.stdout


def test_index_with_reembed(brain_dir):
    result = run("index", "--brain", brain_dir, "--reembed")
    assert result.exit_code == 0
    assert "recomputed embeddings" in result.stdout


def test_index_reports_skipped_entity_files(brain_dir):
    """A partial load must not look complete — one bad file, still reported."""
    (brain_dir / "entities" / "issue" / "broken.json").write_text("{not json")
    result = run("index", "--brain", brain_dir)
    assert result.exit_code == 0
    assert "1 entity file(s) skipped" in result.stdout
    assert "broken.json" in result.stdout


def test_index_truncates_a_long_skip_list(brain_dir):
    """More than ten failures collapse to a count rather than a wall of text."""
    for i in range(12):
        (brain_dir / "entities" / "issue" / f"bad{i}.json").write_text("{nope")
    result = run("index", "--brain", brain_dir)
    assert "12 entity file(s) skipped" in result.stdout
    assert "and 2 more" in result.stdout


def test_validate_clean_brain(brain_dir):
    run("index", "--brain", brain_dir)
    result = run("validate", "--brain", brain_dir)
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_validate_reports_bad_json(brain_dir):
    (brain_dir / "entities" / "issue" / "broken.json").write_text("{not json")
    result = run("validate", "--brain", brain_dir)
    assert result.exit_code == 1
    assert "invalid JSON" in result.output


def test_validate_reports_unparseable_entity(brain_dir):
    """Valid JSON, invalid entity — e.g. an id that isn't <doc_type>:<slug>."""
    (brain_dir / "entities" / "issue" / "bad.json").write_text(
        json.dumps({"doc_type": "issue", "id": "no-colon", "name": "X"})
    )
    result = run("validate", "--brain", brain_dir)
    assert result.exit_code == 1
    assert "problem" in result.output


def test_validate_reports_schema_errors(brain_dir):
    (brain_dir / "doc_types" / "task.yaml").write_text(
        "doc_type: task\nstorage: file\nschema:\n  fields:\n"
        "    - {name: owner, required: true}\n"
    )
    (brain_dir / "entities" / "task").mkdir(parents=True)
    (brain_dir / "entities" / "task" / "t.json").write_text(
        json.dumps({"doc_type": "task", "id": "task:t", "name": "T"})
    )
    result = run("validate", "--brain", brain_dir)
    assert result.exit_code == 1
    assert "missing required field 'owner'" in result.output


def test_search(brain_dir):
    run("index", "--brain", brain_dir)
    result = run("search", "payment", "--brain", brain_dir)
    assert result.exit_code == 0
    assert "match(es)" in result.stdout


def test_search_with_doc_type_filter(brain_dir):
    run("index", "--brain", brain_dir)
    result = run("search", "checkout", "--brain", brain_dir, "-t", "product")
    assert result.exit_code == 0
    assert "product:" in result.stdout


def test_search_empty_query_lists_everything(brain_dir):
    run("index", "--brain", brain_dir)
    result = run("search", "--brain", brain_dir)
    assert result.exit_code == 0


# -- brain resolution errors --------------------------------------------------


def test_missing_brain_is_a_clean_error(tmp_path):
    result = run("index", "--brain", tmp_path / "nope")
    assert result.exit_code == 1
    assert "no brain.yaml" in result.output
    assert "Traceback" not in result.output


def test_bad_env_backend_is_a_clean_error(brain_dir, monkeypatch):
    """A ValueError from config must not surface as a traceback."""
    monkeypatch.setenv("OPEN_INDEX_SEARCH_BACKEND", "postgres")
    result = run("search", "x", "--brain", brain_dir)
    assert result.exit_code == 1
    assert "unknown search backend" in result.output
    assert "Traceback" not in result.output


# -- connectors ---------------------------------------------------------------


def test_list_connectors_empty(brain_dir):
    shutil.rmtree(brain_dir / "connectors")
    result = run("list-connectors", "--brain", brain_dir)
    assert "(no connectors" in result.stdout


def test_list_connectors_finds_the_bundled_example(brain_dir):
    result = run("list-connectors", "--brain", brain_dir)
    assert "example-issues" in result.stdout


def test_list_connectors_shows_name_and_url(brain_dir):
    conn = brain_dir / "connectors"
    conn.mkdir(exist_ok=True)
    (conn / "c.py").write_text(
        "from open_index.connectors import Connector, EntitySpec\n"
        "class C(Connector):\n"
        "    name = 'demo'\n"
        "    mcp_url = 'https://example.com/mcp'\n"
        "    def extract_x(self):\n"
        "        return []\n"
    )
    result = run("list-connectors", "--brain", brain_dir)
    assert "demo" in result.stdout
    assert "https://example.com/mcp" in result.stdout


def _write_connector(brain_dir, schedule="manual"):
    conn = brain_dir / "connectors"
    conn.mkdir(exist_ok=True)
    (conn / "c.py").write_text(
        "from open_index.connectors import Connector, EntitySpec\n"
        "class C(Connector):\n"
        "    name = 'demo'\n"
        f"    schedule = '{schedule}'\n"
        "    def extract_x(self):\n"
        "        yield EntitySpec(doc_type='issue', id='issue:pulled', name='Pulled')\n"
    )


def test_ingest_runs_a_connector(brain_dir):
    _write_connector(brain_dir)
    result = run("ingest", "demo", "--brain", brain_dir)
    assert result.exit_code == 0
    assert "created/updated 1 entities" in result.stdout


def test_ingest_unknown_connector(brain_dir):
    result = run("ingest", "ghost", "--brain", brain_dir)
    assert result.exit_code == 1
    assert "no connector named" in result.output


def test_ingest_reports_per_entity_errors(brain_dir):
    conn = brain_dir / "connectors"
    conn.mkdir(exist_ok=True)
    (conn / "c.py").write_text(
        "from open_index.connectors import Connector, EntitySpec\n"
        "class C(Connector):\n"
        "    name = 'demo'\n"
        "    def extract_x(self):\n"
        "        yield EntitySpec(doc_type='ghost', id='ghost:x', name='X')\n"
    )
    result = run("ingest", "demo", "--brain", brain_dir)
    assert result.exit_code == 0
    assert "unknown doc_type" in result.output


def test_run_with_no_connectors(brain_dir):
    shutil.rmtree(brain_dir / "connectors")
    result = run("run", "--brain", brain_dir)
    assert "(no connectors)" in result.stdout


def test_run_skips_when_not_due(brain_dir):
    shutil.rmtree(brain_dir / "connectors")
    _write_connector(brain_dir, schedule="manual")
    result = run("run", "--brain", brain_dir)
    assert "not due: demo" in result.stdout


def test_run_force_ignores_schedule(brain_dir):
    shutil.rmtree(brain_dir / "connectors")
    _write_connector(brain_dir, schedule="manual")
    result = run("run", "--brain", brain_dir, "--force")
    assert "demo: 1 entities" in result.stdout


def test_run_reports_error_count(brain_dir):
    shutil.rmtree(brain_dir / "connectors")
    conn = brain_dir / "connectors"
    conn.mkdir(exist_ok=True)
    (conn / "c.py").write_text(
        "from open_index.connectors import Connector, EntitySpec\n"
        "class C(Connector):\n"
        "    name = 'demo'\n"
        "    schedule = 'daily'\n"
        "    def extract_x(self):\n"
        "        yield EntitySpec(doc_type='ghost', id='ghost:x', name='X')\n"
    )
    result = run("run", "--brain", brain_dir)
    assert "errors" in result.stdout


def test_run_loop_repeats_until_interrupted(brain_dir, monkeypatch):
    """--loop runs a pass, sleeps, and runs again. Break on the second sleep so
    the in-loop pass is actually exercised."""
    shutil.rmtree(brain_dir / "connectors")
    _write_connector(brain_dir, schedule="always")
    sleeps = {"n": 0}

    def fake_sleep(seconds):
        assert seconds == 1
        sleeps["n"] += 1
        if sleeps["n"] == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", fake_sleep)
    result = run("run", "--brain", brain_dir, "--loop", "1")

    assert sleeps["n"] == 2
    # One pass before the loop plus one inside it.
    assert result.stdout.count("demo: 1 entities") == 2


# -- ui / mcp (subprocess + server entry points are stubbed) ------------------


def test_ui_invokes_streamlit(brain_dir, monkeypatch):
    captured = {}

    def fake_run(cmd, env=None, check=None):
        captured["cmd"] = cmd
        captured["dir"] = env["OPEN_INDEX_DIR"]

    monkeypatch.setattr("subprocess.run", fake_run)
    result = run("ui", "--brain", brain_dir, "--port", "9999")
    assert result.exit_code == 0
    assert "streamlit" in captured["cmd"]
    assert "9999" in captured["cmd"]
    assert captured["dir"] == str(brain_dir.resolve())


def test_ui_reports_missing_streamlit(brain_dir, monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr("subprocess.run", boom)
    result = run("ui", "--brain", brain_dir)
    assert result.exit_code == 1
    assert "Streamlit not installed" in result.output


def test_mcp_stdio_entry_point(brain_dir, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "open_index.mcp_server.serve",
        lambda d, read_only=False: captured.update(dir=d, read_only=read_only),
    )
    assert run("mcp", "--brain", brain_dir, "--read-only").exit_code == 0
    assert captured["read_only"] is True
    assert captured["dir"] == str(brain_dir.resolve())
