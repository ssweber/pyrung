"""Regression tests for hook.py: recorded PreToolUse payloads in, exit codes and rewrites out.

Run: timeout 120 uv run pytest devtools/mergeq -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).with_name("hook.py")
TEMP = os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
REPO_FILE = str(Path(__file__).resolve().parents[2] / "src" / "pyrung" / "__init__.py")

MAIN = None
BUILDER = "bob-builder"
REVIEWER = "bob-reviewer"


def run_hook(tool: str, value: str, agent: str | None, env: dict[str, str] | None = None):
    key = "command" if tool in ("Bash", "PowerShell") else "file_path"
    event: dict = {"tool_name": tool, "tool_input": {key: value}, "cwd": "C:/x/pyrung-wt/drum"}
    if agent:
        event.update(agent_type=agent, agent_id="a1")
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, out, proc.stderr


ALLOW, REFUSE, WRAP = "allow", "refuse", "wrap"

CASES = [
    # main session
    ("Bash", "make test-pilot", MAIN, REFUSE),
    ("Bash", "make", MAIN, REFUSE),
    ("Bash", "make lint && make test", MAIN, REFUSE),
    (
        "Bash",
        "python -I devtools/mergeq/mergeq.py run --heavy --timeout 900 -- make test-pilot",
        MAIN,
        ALLOW,
    ),
    ("Bash", "python -I devtools/mergeq/mergeq.py next", MAIN, ALLOW),
    ("Bash", "make lint", MAIN, ALLOW),
    ("Bash", "make install", MAIN, ALLOW),
    ("Bash", "make install && timeout 300 uv run pytest tests/core -k test", MAIN, WRAP),
    ("Bash", "uv run pytest tests/core/test_x.py -q", MAIN, WRAP),
    ("Bash", "git status", MAIN, ALLOW),
    ("PowerShell", "uv run pytest tests/x.py", MAIN, REFUSE),
    ("Bash", "git commit -m 'docs: run make test before pytest'", MAIN, ALLOW),
    ("Bash", "cat > notes.md <<'EOF'\nthen make test-fold and pytest\nEOF", MAIN, ALLOW),
    ("Bash", "python - <<'EOF'\nprint('make test')\nEOF\nmake test-prove", MAIN, REFUSE),
    ("Edit", REPO_FILE, MAIN, ALLOW),
    # builder
    ("Bash", "make test", BUILDER, REFUSE),
    ("Bash", "python -I devtools/mergeq/mergeq.py run --heavy -- make test-fold", BUILDER, REFUSE),
    (
        "Bash",
        "timeout 600 uv run pytest -m conformance tests/conformance/test_modbus.py",
        BUILDER,
        REFUSE,
    ),
    ("Bash", "timeout 300 uv run pytest tests/conformance/test_drum.py -q", BUILDER, WRAP),
    ("Bash", "git commit -m 'fix(core): x'", BUILDER, ALLOW),
    ("Write", REPO_FILE, BUILDER, ALLOW),
    # reviewer: allowlist
    ("Bash", "git -C ../pyrung-wt/drum diff dev...bob/drum", REVIEWER, ALLOW),
    ("Bash", "git log --oneline -5 | head -3", REVIEWER, ALLOW),
    ("Bash", 'grep -nE "a|b" src/pyrung/core/rung.py', REVIEWER, ALLOW),
    ("Bash", "rg -n foo src 2>&1 | tail -5", REVIEWER, ALLOW),
    ("Bash", "cd ../pyrung-wt/drum && git show HEAD --stat", REVIEWER, ALLOW),
    ("Bash", "timeout 300 uv run pytest tests/core/test_drums.py -q", REVIEWER, WRAP),
    ("Bash", f"timeout 120 uv run python {TEMP}/probe.py", REVIEWER, ALLOW),
    ("Bash", "sed -n 1,40p src/pyrung/core/rung.py", REVIEWER, ALLOW),
    ("Write", f"{TEMP}/probe.py", REVIEWER, ALLOW),
    # reviewer: refusals
    ("Bash", "git commit -m x", REVIEWER, REFUSE),
    ("Bash", "git stash", REVIEWER, REFUSE),
    ("Bash", "git checkout -- src/x.py", REVIEWER, REFUSE),
    ("Bash", "sed -i s/a/b/ src/x.py", REVIEWER, REFUSE),
    ("Bash", "echo x > src/x.py", REVIEWER, REFUSE),
    ("Bash", "cat src/a.py >> src/b.py", REVIEWER, REFUSE),
    ("Bash", "python -c \"open('src/x.py','w')\"", REVIEWER, REFUSE),
    ("Bash", "uv run python src/pyrung/cli.py", REVIEWER, REFUSE),
    ("Bash", "python - <<'EOF'\nprint(1)\nEOF", REVIEWER, REFUSE),
    ("Bash", "cat $(git ls-files)", REVIEWER, REFUSE),
    ("Bash", "find . -name '*.pyc' -delete", REVIEWER, REFUSE),
    ("Bash", "rm -rf build", REVIEWER, REFUSE),
    ("Bash", "make test-prove", REVIEWER, REFUSE),
    ("PowerShell", "Get-Content src/x.py", REVIEWER, REFUSE),
    ("Write", REPO_FILE, REVIEWER, REFUSE),
    ("Edit", REPO_FILE, REVIEWER, REFUSE),
]


@pytest.mark.parametrize(("tool", "value", "agent", "want"), CASES)
def test_hook_decision(tool: str, value: str, agent: str | None, want: str) -> None:
    rc, out, err = run_hook(tool, value, agent)
    if want == REFUSE:
        assert rc == 2, f"expected refusal, got rc={rc} out={out}"
        assert err.startswith("mergeq"), err
    elif want == ALLOW:
        assert (rc, out) == (0, None), f"expected plain allow, got rc={rc} out={out} err={err}"
    else:
        assert rc == 0, err
        assert out is not None
        command = out["hookSpecificOutput"]["updatedInput"]["command"]
        assert "mergeq.py run --timeout" in command and " -- bash -c " in command
        assert "--name" in command and "@drum" in command


def test_wrap_takes_the_timeout_and_keeps_other_fields() -> None:
    event = {
        "tool_name": "Bash",
        "tool_input": {
            "command": "timeout 5m uv run pytest -q",
            "description": "d",
            "timeout": 1000,
        },
        "cwd": "C:/w/text",
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(event), text=True, capture_output=True
    )
    tool_input = json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]
    assert "--timeout 300 " in tool_input["command"] and "--name main@text" in tool_input["command"]
    assert (tool_input["description"], tool_input["timeout"]) == ("d", 1000)


def test_fails_closed_on_bad_input_and_bad_config() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input="not json", text=True, capture_output=True
    )
    assert proc.returncode == 2
    missing = {"MERGEQ_CONFIG": str(Path(TEMP) / "no-such-mergeq.toml")}
    rc, _, err = run_hook("Bash", "git status", MAIN, env=missing)
    assert rc == 2 and "broken" in err
    # ...but the main session can still edit, so the hook can be repaired.
    rc, _, _ = run_hook("Edit", REPO_FILE, MAIN, env=missing)
    assert rc == 0
