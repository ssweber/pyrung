"""Hook cases from the review of the reviewer allowlist: shell tricks that slipped past it, and
the nearest legitimate form of each that must keep working.

Run: timeout 120 uv run pytest devtools/mergeq -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from test_hook import ALLOW, HOOK, MAIN, MQ, READER, REFUSE, REVIEWER, TEMP, WRAP, run_hook

T = TEMP.replace("\\", "/")
BOUND = f"{MQ} run --timeout 60 --"

CASES = [
    # B1: a newline, a mid-word #, and <( are command boundaries the old split did not see
    ("git status\npython T/p.py", REVIEWER, REFUSE),
    ("cd src\npython T/p.py", REVIEWER, REFUSE),
    ("echo hi\nuv run python T/p.py", REVIEWER, REFUSE),
    (f"{BOUND} python T/a.py\npython T/b.py", REVIEWER, REFUSE),
    ("git status\nrm -rf build", REVIEWER, REFUSE),
    ("ruff check src\nruff format src", REVIEWER, REFUSE),
    ("git log --format=%h#%s; python T/p.py", REVIEWER, REFUSE),
    ("git status #x\npython T/p.py", REVIEWER, REFUSE),
    ("cat <(python T/p.py)", REVIEWER, REFUSE),
    ("echo x >(tee src/x.py)", REVIEWER, REFUSE),
    ("git status\nuv run pytest tests/x.py", READER, REFUSE),
    ("echo a#b; pytest tests", READER, REFUSE),
    ("cat <(pytest tests)", READER, REFUSE),
    ("git status\ngit log --oneline -3", REVIEWER, ALLOW),
    ("git log --format=%h#%s -3", REVIEWER, ALLOW),
    ("git status # why\ngit diff --stat", REVIEWER, ALLOW),
    ("git diff --stat \\\n  dev...HEAD", REVIEWER, REFUSE),  # a continuation: judged as two
    ("grep -n 'a\nb' src/x.py", REVIEWER, ALLOW),  # a newline inside quotes is data
    # B2: assignments, and mergeq.py paths the check could not see
    (f"MERGEQ_CONFIG={T}/nocap.toml {BOUND} python {T}/p.py", REVIEWER, REFUSE),
    (f"PATH={T} git status", REVIEWER, REFUSE),
    ("GIT_EXTERNAL_DIFF=x git diff", REVIEWER, REFUSE),
    ("F=--fix; ruff check src $F", REVIEWER, REFUSE),
    ("PYTHONPATH=x git status", REVIEWER, REFUSE),
    ("NO_COLOR=1 git diff --stat", REVIEWER, ALLOW),
    ("PYTHONIOENCODING=utf-8 git log -1", REVIEWER, ALLOW),
    ("python -I $TEMP/mergeq.py run --timeout 60 -- cat x", REVIEWER, REFUSE),
    ("python -I ~/AppData/Local/Temp/mergeq.py run --timeout 60 -- cat x", REVIEWER, REFUSE),
    (f"cd {T} && python -I mergeq.py run --timeout 60 -- cat x", REVIEWER, REFUSE),
    (f"cd {T} && python -I devtools/mergeq/mergeq.py run --timeout 60 -- cat x", REVIEWER, REFUSE),
    (f"python -I {T}/devtools/mergeq/mergeq.py run --timeout 60 -- cat x", REVIEWER, REFUSE),
    (f"python -I {T}/mergeq.py status", REVIEWER, REFUSE),
    (
        "cd ../pyrung-wt/drum && python -I devtools/mergeq/mergeq.py run --timeout 60 -- cat x",
        REVIEWER,
        REFUSE,
    ),
    ("cd src && python -I /x/devtools/mergeq/mergeq.py run --timeout 60 -- cat x", REVIEWER, ALLOW),
    (f"{BOUND} cat x", REVIEWER, ALLOW),
    ("python -I devtools/mergeq/mergeq.py status", REVIEWER, ALLOW),
    # N2: ruff and ty are judged on tokens
    ("ruff check -o src/x.txt src", REVIEWER, REFUSE),
    ("ruff check {--fix,src}", REVIEWER, REFUSE),
    ("ruff check src $F", REVIEWER, REFUSE),
    ("ruff format src 'x --check'", REVIEWER, REFUSE),
    ("ruff format src --check --fix", REVIEWER, REFUSE),
    ("ty check -W src", REVIEWER, REFUSE),
    ("ty check --watch src", REVIEWER, REFUSE),
    ("ruff check --fix-only src", REVIEWER, REFUSE),
    ("ruff check src --select F401,E --output-format=concise", REVIEWER, ALLOW),
    ("uv run --no-sync ruff check src tests", REVIEWER, ALLOW),
    ("ruff format --check --diff src", REVIEWER, ALLOW),
    ("ty check --project . --error unused-ignore-comment src", REVIEWER, ALLOW),
    # N3: node options are an exact list
    (f"{BOUND} node --test --test-reporter={T}/evil.mjs", REVIEWER, REFUSE),
    (f"{BOUND} node --test --test-global-setup={T}/x.mjs", REVIEWER, REFUSE),
    (f"{BOUND} node --test --test-reporter-destination=src/x.py", REVIEWER, REFUSE),
    (f"{BOUND} node --test --test-update-snapshots", REVIEWER, REFUSE),
    (f"S=--eval; {BOUND} node --test $S 1", REVIEWER, REFUSE),
    (f"{BOUND} node --test --test-reporter=spec --test-name-pattern=foo", REVIEWER, ALLOW),
    (f"{BOUND} node --test --test-isolation=none test/a.test.js", REVIEWER, ALLOW),
    # N4: a mergeq call elsewhere in the command does not exempt a pytest
    ("python -I devtools/mergeq/mergeq.py status && uv run pytest tests/x.py", REVIEWER, WRAP),
    ("python -I devtools/mergeq/mergeq.py status && uv run pytest tests/x.py", MAIN, WRAP),
    (
        "python -I devtools/mergeq/mergeq.py run --timeout 60 -- uv run pytest tests/x.py",
        MAIN,
        ALLOW,
    ),
    (
        "python -I devtools/mergeq/mergeq.py run --timeout 60 -- uv run pytest t.py; uv run pytest u.py",
        MAIN,
        WRAP,
    ),
    # N5: the fold-new script runs pytest unbounded; agents and the main session go through the queue
    ("uv run python devtools/fold_new_tests.py", "bob-builder", REFUSE),
    ("uv run python devtools/fold_new_tests.py", MAIN, REFUSE),
    ("make test-fold-new", "bob-builder", REFUSE),
    (
        "python -I devtools/mergeq/mergeq.py run --heavy --timeout 900 -- uv run python devtools/fold_new_tests.py",
        MAIN,
        ALLOW,
    ),
    # N9: natural forms
    ("python devtools/lint.py --check", REVIEWER, ALLOW),
    ("uv run python devtools/lint.py --check", REVIEWER, ALLOW),
    ("uv run devtools/lint.py --check", REVIEWER, ALLOW),
    ("uv run devtools/lint.py --check --fix", REVIEWER, REFUSE),
    ("python devtools/lint.py", REVIEWER, REFUSE),
    ("python devtools/lint.py --check", READER, REFUSE),
    ("uv run devtools/lint.py --check", READER, REFUSE),
    (f"{BOUND} uv run python $TEMP/probe.py", REVIEWER, ALLOW),
    (f"{BOUND} uv run python ${{TEMP}}/probe.py", REVIEWER, ALLOW),
    (f"{BOUND} uv run python $TMP/probe.py", REVIEWER, ALLOW),
    (f"{BOUND} uv run python $HOME/probe.py", REVIEWER, REFUSE),
    (f"{BOUND} uv run python $OTHER/probe.py", REVIEWER, REFUSE),
    ("echo $'\\x2d\\x2dfix'", REVIEWER, REFUSE),
    ("grep -n 'x$' src/a.py", REVIEWER, ALLOW),
    ("awk '{print $1}' src/a.txt", REVIEWER, ALLOW),
    ("cat $TEMP/notes.txt", REVIEWER, ALLOW),
    # N10: forms that run code or write files through an allowlisted command
    ("awk 'BEGIN{system(\"pytest\")}'", REVIEWER, REFUSE),
    ("awk 'BEGIN{system(\"pytest\")}'", READER, REFUSE),
    ("awk '{print | \"sh\"}' src/a.txt", REVIEWER, REFUSE),
    ("awk '{ \"date\" | getline x }' src/a.txt", REVIEWER, REFUSE),
    ("awk -f T/prog.awk src/a.txt", REVIEWER, REFUSE),
    ("awk -F: '{print $1}' src/a.txt", REVIEWER, ALLOW),
    ("sed -n '1e pytest' src/a.py", READER, REFUSE),
    ("sed -n '1w src/x.py' src/a.py", REVIEWER, REFUSE),
    ("sed -n -e 1p src/a.py", REVIEWER, REFUSE),
    ("sed -n 1,40p src/a.py", REVIEWER, ALLOW),
    ("sed -n '/foo/,/bar/p' src/a.py", REVIEWER, ALLOW),
    ("sed -n '$p' src/a.py", REVIEWER, ALLOW),
    ("git grep -O'pytest' foo", READER, REFUSE),
    ("git grep --open-files-in-pager=sh foo", REVIEWER, REFUSE),
    ("git log --output=src/x.txt", REVIEWER, REFUSE),
    ("git diff --ext-diff", REVIEWER, REFUSE),
    ("git grep -n foo -- src", REVIEWER, ALLOW),
    ("rg --pre pytest foo", READER, REFUSE),
    ("rg --pre=sh foo", REVIEWER, REFUSE),
    ("rg -z foo", REVIEWER, REFUSE),
    ("rg -n -o foo src", REVIEWER, ALLOW),
    ("sort --compress-program=sh src/a.txt", READER, REFUSE),
    ("sort -o src/x.txt src/a.txt", REVIEWER, REFUSE),
    ("sort -u src/a.txt", REVIEWER, ALLOW),
    ("tree -o src/x.txt", REVIEWER, REFUSE),
    ("date -s 2020-01-01", REVIEWER, REFUSE),
    ("date +%Y", REVIEWER, ALLOW),
]


@pytest.mark.parametrize(("value", "agent", "want"), CASES)
def test_review_cases(value: str, agent: str | None, want: str) -> None:
    value = value.replace("T/", f"{T}/")
    rc, out, err = run_hook("Bash", value, agent)
    if want == REFUSE:
        assert rc == 2, f"expected refusal, got rc={rc} out={out}"
        assert err.startswith("mergeq"), err
    elif want == ALLOW:
        assert (rc, out) == (0, None), f"expected plain allow, got rc={rc} out={out} err={err}"
    else:
        assert rc == 0, err
        assert out is not None
        assert " -- bash -c " in out["hookSpecificOutput"]["updatedInput"]["command"]


def test_reviewer_test_run_timeout_is_capped() -> None:
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "timeout 9000 uv run pytest tests/x.py"},
        "cwd": "C:/x/pyrung-wt/drum",
        "agent_type": REVIEWER,
        "agent_id": "a1",
    }
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(event), text=True, capture_output=True
    )
    command = json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    assert "--timeout 1800 " in command
    event.pop("agent_type")
    event.pop("agent_id")
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(event), text=True, capture_output=True
    )
    assert (
        "--timeout 9000 "
        in json.loads(proc.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    )


def test_env_dir_is_not_assumed() -> None:
    assert os.path.isabs(TEMP)
