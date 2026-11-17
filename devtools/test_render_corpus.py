"""Check that render_corpus.py renders the same bytes twice, and that compare finds a change.

Run: timeout 300 uv run pytest devtools/test_render_corpus.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import render_corpus  # noqa: E402

ONLY = "programs/conditions"


def test_two_renders_compare_equal_and_a_change_is_found(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    render_corpus.render(first, ONLY)
    render_corpus.render(second, ONLY)

    assert render_corpus.compare(first, second) == []
    assert (first / render_corpus.SUMS).read_bytes() == (second / render_corpus.SUMS).read_bytes()
    rendered = sorted(path.name for path in (first / ONLY).iterdir())
    assert "code.py" in rendered
    assert sum(name.startswith("kernel_") and name.endswith(".py") for name in rendered) == 8
    assert sum(name.endswith(".meta") for name in rendered) == 8

    changed = second / ONLY / "kernel_b1_p0_f0.py"
    changed.write_bytes(changed.read_bytes() + b"\n")
    (second / ONLY / "code.py").unlink()
    assert render_corpus.compare(first, second) == [
        f"only in {first}: {ONLY}/code.py",
        f"differs: {ONLY}/kernel_b1_p0_f0.py",
    ]


def test_every_label_has_a_file_name_of_its_own() -> None:
    names = [rel for rel, _build in render_corpus._corpus(None)]
    assert len(names) == len(set(names))
    assert render_corpus._slug("a % b -> c") != render_corpus._slug("a / b -> c")
