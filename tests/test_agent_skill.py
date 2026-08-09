from pathlib import Path

import yaml


def test_setup_skill_is_portable_and_vendor_neutral():
    path = Path(__file__).parents[1] / "skills" / "setup-open-index" / "SKILL.md"
    text = path.read_text()
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)

    assert metadata["name"] == "setup-open-index"
    assert "domain-specialized agent" in metadata["description"]
    assert "open-index mcp --brain" in body
    assert "Do not add it by default" in body
    assert all(tool in body for tool in (
        "navigation_guidelines", "search_brain", "get_entity",
        "put_entity", "create_doc_type",
    ))
