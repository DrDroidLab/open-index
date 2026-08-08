"""Environment overrides for backend selection.

These exist so one brain directory (and one container image) can run against
different backends without editing brain.yaml — the compose-profile case.
"""

import shutil
from pathlib import Path

import pytest

from open_index.config import load_brain_config

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"


@pytest.fixture
def brain_dir(tmp_path):
    dst = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, dst)
    return dst


def test_defaults_come_from_brain_yaml(brain_dir):
    config = load_brain_config(brain_dir)
    assert config.search.backend == "sqlite"


def test_search_backend_override(brain_dir, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_SEARCH_BACKEND", "opensearch")
    assert load_brain_config(brain_dir).search.backend == "opensearch"


def test_opensearch_hosts_override_splits_on_commas(brain_dir, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_SEARCH_BACKEND", "opensearch")
    monkeypatch.setenv(
        "OPEN_INDEX_OPENSEARCH_HOSTS", "http://a:9200, http://b:9200 ,"
    )
    assert load_brain_config(brain_dir).search.hosts == [
        "http://a:9200",
        "http://b:9200",
    ]


def test_index_and_db_path_overrides(brain_dir, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_OPENSEARCH_INDEX", "open_index_prod")
    monkeypatch.setenv("OPEN_INDEX_DB_PATH", "/data/brain.db")
    config = load_brain_config(brain_dir)
    assert config.search.index == "open_index_prod"
    assert config.db_path() == Path("/data/brain.db")


def test_empty_env_var_does_not_override(brain_dir, monkeypatch):
    """docker compose passes unset variables through as empty strings."""
    monkeypatch.setenv("OPEN_INDEX_SEARCH_BACKEND", "")
    monkeypatch.setenv("OPEN_INDEX_OPENSEARCH_HOSTS", "")
    config = load_brain_config(brain_dir)
    assert config.search.backend == "sqlite"
    assert config.search.hosts == ["http://localhost:9200"]


def test_unknown_backend_is_rejected_with_a_clear_message(brain_dir, monkeypatch):
    monkeypatch.setenv("OPEN_INDEX_SEARCH_BACKEND", "postgres")
    with pytest.raises(ValueError, match="unknown search backend"):
        load_brain_config(brain_dir)


def test_a_backend_typo_in_brain_yaml_is_caught_too(brain_dir):
    """The same guard covers the file, not just the environment."""
    path = brain_dir / "brain.yaml"
    path.write_text(path.read_text().replace("backend: sqlite", "backend: sqlight"))
    with pytest.raises(ValueError, match="unknown search backend"):
        load_brain_config(brain_dir)


def test_legacy_storage_backend_still_loads_but_warns(brain_dir, caplog):
    """It never did anything; failing on it would break existing brains, and
    ignoring it silently is what made two 'backend' keys so confusing."""
    path = brain_dir / "brain.yaml"
    path.write_text(path.read_text().replace(
        "storage:\n  path:", "storage:\n  backend: opensearch\n  path:"))

    with caplog.at_level("WARNING", logger="open_index.config"):
        config = load_brain_config(brain_dir)

    assert config.search.backend == "sqlite", "storage.backend must not select the engine"
    assert "storage.backend" in caplog.text
    assert "search.backend" in caplog.text


def test_no_warning_when_storage_backend_is_absent(brain_dir, caplog):
    with caplog.at_level("WARNING", logger="open_index.config"):
        load_brain_config(brain_dir)
    assert "storage.backend" not in caplog.text


def test_missing_brain_yaml_names_the_fix(tmp_path):
    with pytest.raises(FileNotFoundError, match="open-index init"):
        load_brain_config(tmp_path)


def test_duplicate_doc_type_is_rejected(brain_dir):
    shutil.copy(
        brain_dir / "doc_types" / "issue.yaml",
        brain_dir / "doc_types" / "issue-copy.yaml",
    )
    with pytest.raises(ValueError, match="duplicate doc_type 'issue'"):
        load_brain_config(brain_dir)


def test_relative_db_path_resolves_against_the_brain_root(brain_dir):
    config = load_brain_config(brain_dir)
    assert config.db_path() == brain_dir / "brain.db"


# -- ${ENV} expansion (used for OpenSearch credentials) ----------------------


def test_expand_env_handles_strings_lists_dicts_and_scalars(monkeypatch):
    from open_index.config import expand_env

    monkeypatch.setenv("OI_USER", "admin")
    assert expand_env("${OI_USER}") == "admin"
    assert expand_env(["${OI_USER}", "x"]) == ["admin", "x"]
    assert expand_env({"u": "${OI_USER}"}) == {"u": "admin"}
    # Non-string scalars pass through untouched.
    assert expand_env(7) == 7
    assert expand_env(None) is None


def test_expand_env_blanks_unset_variables(monkeypatch):
    from open_index.config import expand_env

    monkeypatch.delenv("OI_ABSENT", raising=False)
    assert expand_env("${OI_ABSENT}") == ""


def test_doc_type_round_trips_through_yaml_with_all_optional_keys(tmp_path):
    """write_doc_type must preserve required/description/relationships."""
    from open_index.config import load_brain_config as load
    from open_index.config import write_doc_type
    from open_index.schema import DocType, FieldSpec, RelationshipSpec

    root = tmp_path / "b"
    root.mkdir()
    (root / "brain.yaml").write_text("name: b\n")
    dt = DocType(
        doc_type="task",
        description="A task.",
        storage="file",
        fields=[
            FieldSpec(name="name", boost=5),
            FieldSpec(name="owner", required=True, description="Who owns it."),
        ],
        relationships=[RelationshipSpec(name="blocks", target_doc_type="task")],
    )
    write_doc_type(root, dt)

    reloaded = load(root).doc_type("task")
    assert reloaded.field("owner").required is True
    assert reloaded.field("owner").description == "Who owns it."
    assert reloaded.field("name").boost == 5
    assert reloaded.relationship("blocks").target_doc_type == "task"
