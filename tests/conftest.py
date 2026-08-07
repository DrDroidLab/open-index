import shutil
from pathlib import Path

import pytest

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "support-brain"


@pytest.fixture
def brain(tmp_path):
    """An indexed copy of the example support-brain in a temp dir."""
    from open_index.brain import Brain

    dst = tmp_path / "support-brain"
    shutil.copytree(EXAMPLE, dst)
    b = Brain.open(dst)
    b.index()
    return b
