"""PreToolUse hook for mergeq: heavy suites stay in the queue, test runs stay timed and
killable, and the reviewer agent stays read-only.

Wired once in .claude/settings.json for Bash, PowerShell, Write, Edit and NotebookEdit.
Reads the hook JSON on stdin. Exit 2 + stderr refuses the call (the reason goes back to the
model); exit 0 allows it, optionally with JSON on stdout that rewrites the command.

It fails CLOSED: if the hook itself breaks (bad JSON, bad mergeq.toml), shell calls are
refused with the error, so enforcement can't silently lapse. File edits by anyone but the
reviewer are never checked, so the Edit tool can always repair hook.py or mergeq.toml.

Rules (mergeq.toml [hook]):
- heavy: every non-light [stages] command plus heavy_extra. Refused for subagents; the main
  session must run it through `mergeq.py run --heavy` or the queue. heavy_exempt takes
  a main-session command back out, for one whose work happens off this machine.
- needs_timeout (e.g. pytest): a Bash command is rewritten to run under
  `mergeq.py run --timeout N --name <agent>@<worktree> -- bash -c '<command>'`, so it gets a
  hard timeout and a named, killable process group. PowerShell is refused with the fix.
- reviewer (agent_type == reviewer_agent): every shell segment must match reviewer_allow,
  redirects may only target /dev/null or reviewer_write_roots, heredocs and command
  substitution are refused, PowerShell is refused, and Write/Edit only reach the write roots.
  Scratch scripts (reviewer_script) and reviewer_bounded commands (node --test) run only as
  `python -I devtools/mergeq/mergeq.py run --timeout N -- <command>`, whose Job Object applies
  the job_memory_mb cap; the same command bare has no memory bound and is refused.
- readonly (agent_type == readonly_agent): the reviewer's rules, minus everything that runs
  code: no scripts, no mergeq run, no test commands (readonly_refuse). For investigations that
  must not execute tests.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
MERGEQ = HERE / "mergeq.py"
EDIT_TOOLS = ("Write", "Edit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
TIMEOUT_PREFIX = re.compile(r"(^|[;&|(]\s*)timeout\s+(-\S+\s+)*(\d+)([smhd]?)\s")
MERGEQ_RUN = re.compile(r"mergeq\.py[\"']?\s+run\b")
MERGEQ_HEAVY_OK = re.compile(r"mergeq\.py[\"']?\s+(run\s+(\S+\s+)*--heavy\b|next\b)")
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&"}
REDIRECTS = {">", ">>", ">&", "&>", ">|", "&>>"}


HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.S)
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")


class Refuse(Exception):
    pass


def _code_only(cmd: str) -> str:
    """The command with heredoc bodies and quoted strings blanked: what the shell will run,
    not the text it passes around (commit messages, docs written through a heredoc)."""
    return QUOTED.sub("''", HEREDOC.sub("<<''", cmd))


def _config() -> dict:
    path = Path(os.environ.get("MERGEQ_CONFIG") or HERE / "mergeq.toml")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _heavy_patterns(cfg: dict) -> list[re.Pattern[str]]:
    pats = []
    for stage in cfg.get("stages", {}).values():
        if stage.get("light"):
            continue
        words = stage["cmd"][:2]
        pats.append(r"\b" + r"\s+([^\s;&|]+\s+)*".join(re.escape(w) for w in words) + r"\b")
    pats += cfg.get("hook", {}).get("heavy_extra", [])
    return [re.compile(p) for p in pats]


def _roots(cfg: dict) -> list[Path]:
    roots = []
    for root in cfg.get("hook", {}).get("reviewer_write_roots", []):
        expanded = os.path.expandvars(root)
        if expanded and "$" not in expanded:
            roots.append(Path(expanded).resolve())
    return roots


def _under(path: str, roots: list[Path]) -> bool:
    if not path:
        return False
    if re.match(r"^/[a-zA-Z]/", path):  # Git Bash /c/... -> C:/...
        path = f"{path[1]}:{path[2:]}"
    target = Path(path).resolve()
    return any(target == r or r in target.parents for r in roots)


# --------------------------------------------------------------------------- reviewer

# Environment assignments a reviewer command may carry: they change output, not what runs.
ENV_OK = ("PYTHONUTF8", "PYTHONIOENCODING", "NO_COLOR", "FORCE_COLOR", "TERM", "LC_ALL", "LANG")
PYTHON = re.compile(r"python[0-9.]*")
# Flags that make an otherwise read-only command run code or write a file, by command.
FORBIDDEN_FLAGS = {
    "git": re.compile(r"--output.*|--open-files-in-pager.*|-O.*|--ext-diff|--exec-path.*"),
    "rg": re.compile(r"--pre(=.*)?|--hostname-bin.*|-z|--search-zip"),
    "sort": re.compile(r"--compress-program.*|-o.*|--output.*"),
    "tree": re.compile(r"-o.*"),
    "file": re.compile(r"-C|--compile"),
    "date": re.compile(r"-s|--set.*"),
}
SED_SCRIPT = re.compile(r"(\d+|\$|/[^/]*/)(,(\d+|\$|/[^/]*/))?p")
AWK_DENY = re.compile(r"system|getline|[|>@]|ENVIRON|close\(|fflush")
RUFF_FLAGS = (
    r"--(select|ignore|extend-select|extend-ignore|per-file-ignores|output-format|target-version"
    r"|line-length|exclude|extend-exclude|preview|no-preview|no-cache|isolated|no-fix|statistics"
    r"|show-files|show-settings|quiet|verbose|exit-zero|exit-non-zero-on-fix|diff|force-exclude"
    r"|respect-gitignore|no-respect-gitignore|stdin-filename|unfixable)(=\S*)?|-q|-v"
)
RUFF_FORMAT_FLAGS = (
    r"--(check|diff|no-cache|isolated|quiet|verbose|line-length|target-version|preview"
    r"|no-preview|exclude|extend-exclude|force-exclude|respect-gitignore|no-respect-gitignore"
    r"|stdin-filename)(=\S*)?|-q|-v"
)
TY_FLAGS = (
    r"--(project|error|warn|ignore|python-version|python|platform|output-format|no-progress|quiet"
    r"|verbose|color|exit-zero|extra-search-path|typeshed|exclude|force-exclude|config-file"
    r"|respect-ignore-files|no-respect-ignore-files|error-on-warning)(=\S*)?|-q|-v"
)
NODE_FLAGS = (
    r"--test|--test-only|--test-isolation=(none|process)|--test-reporter=(spec|tap|dot)"
    r"|--test-name-pattern=\S+|--test-skip-pattern=\S+|--test-timeout=\d+"
    r"|--test-concurrency=\d+|--test-shard=\d+/\d+"
)


def _scan_newlines(cmd: str) -> str:
    """An unquoted newline ends a command like `;` does (shlex would read it as a space)."""
    out, quote = [], ""
    for ch in cmd:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            out.append(" ; ")
            continue
        out.append(ch)
    return "".join(out)


def _prepare(cmd: str) -> str:
    """The command text the shell will see, as far as the check can tell, or Refuse for the
    forms it cannot judge: heredocs, substitution, process substitution, brace and variable
    expansion (only $TEMP and $TMP, which are expanded here)."""
    if "<<" in cmd:
        raise Refuse(
            "heredocs are not allowed for this agent: Write the probe script to a temp dir and run it"
        )
    if "`" in cmd or "$(" in cmd:
        raise Refuse(
            "command substitution is not allowed for this agent: run the inner command on its own"
        )
    if re.search(r"[<>]\(", cmd):
        raise Refuse("process substitution is not allowed for this agent")
    if re.search(r"\$['\"]", QUOTED.sub("''", cmd)):
        raise Refuse("$'...' quoting is not allowed for this agent")
    cmd = cmd.replace("\\", "/")

    def temp(m: re.Match[str]) -> str:
        value = os.environ.get(m.group(1) or m.group(2), "")
        if not value:
            raise Refuse(f"${m.group(1) or m.group(2)} is not set; spell the path out")
        return shlex.quote(value.replace("\\", "/"))

    cmd = re.sub(r"\$\{(TEMP|TMP)\}|\$(TEMP|TMP)\b", temp, cmd)
    if re.search(r"\$[{\w]", re.sub(r"'[^']*'", "''", cmd)):
        raise Refuse("variable expansion is not allowed for this agent (only $TEMP and $TMP)")
    if re.search(r"[{}]", QUOTED.sub("''", cmd)):
        raise Refuse("brace expansion is not allowed for this agent")
    return _scan_newlines(cmd)


def _segments(cmd: str) -> list[list[str]]:
    lexer = shlex.shlex(_prepare(cmd), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""  # `#` is an ordinary character in the middle of a word
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise Refuse(f"could not parse the command ({exc})") from None
    segs, cur = [], []
    for tok in tokens:
        if tok in SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segs.append(cur)
    return segs


def _words(seg: list[str], roots: list[Path]) -> list[str]:
    """The segment's command words: redirects checked and dropped, harmless VAR=value prefixes
    dropped. Any other assignment is refused, alone or as a prefix: it can change what runs
    (MERGEQ_CONFIG, PATH, GIT_EXTERNAL_DIFF) or build an argument the check never saw."""
    words: list[str] = []
    i = 0
    while i < len(seg):
        tok = seg[i]
        if re.fullmatch(r"\d", tok) and i + 1 < len(seg) and seg[i + 1] in REDIRECTS:
            i += 1  # the fd of `2>&1` / `2>/dev/null`
            continue
        if tok in REDIRECTS or tok == "<":
            target = seg[i + 1] if i + 1 < len(seg) else ""
            i += 2
            if tok == "<" or target in ("/dev/null", "1", "2") or _under(target, roots):
                continue
            raise Refuse(f"redirect to {target!r} is outside the scratch roots")
        words.append(tok)
        i += 1
    assigned = False
    while words and (m := re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=\S*", words[0])):
        if m.group(1) not in ENV_OK:
            raise Refuse(
                f"{m.group(1)}= is not allowed for this agent (it can change what runs); "
                f"only {', '.join(ENV_OK)}"
            )
        words = words[1:]
        assigned = True
    if assigned and not words:
        raise Refuse("a bare assignment is not allowed for this agent")
    return words


class _Ctx:
    """What the check knows about the command so far."""

    def __init__(self, cfg: dict, cwd: str, readonly: bool) -> None:
        hook = cfg.get("hook", {})
        self.roots = _roots(cfg)
        self.cwd = cwd
        self.readonly = readonly
        self.cd_seen = False
        self.allow = [re.compile(p) for p in hook.get("reviewer_allow", [])]
        self.refuse = [re.compile(p) for p in hook.get("readonly_refuse", [])]
        self.script_pat = re.compile(hook.get("reviewer_script", r"$^"))
        self.max_timeout = int(hook.get("reviewer_max_timeout", 1800))

    def resolve(self, path: str) -> Path:
        if re.match(r"^/[a-zA-Z]/", path):  # Git Bash /c/... -> C:/...
            path = f"{path[1]}:{path[2:]}"
        p = Path(path)
        return (p if p.is_absolute() else Path(self.cwd) / p).resolve()

    def under_roots(self, path: str) -> bool:
        return any(self.resolve(path) == r or r in self.resolve(path).parents for r in self.roots)


def _mergeq_path_ok(token: str, ctx: _Ctx) -> bool:
    """Is this the repo's own devtools/mergeq/mergeq.py (not a copy the reviewer wrote)?"""
    if "$" in token or "~" in token:
        return False
    if not Path(token).is_absolute() and not re.match(r"^/[a-zA-Z]/", token) and ctx.cd_seen:
        return False  # a relative path after `cd` points somewhere the check did not see
    path = ctx.resolve(token)
    tail = path.parts[-3:]
    return tail == ("devtools", "mergeq", "mergeq.py") and not ctx.under_roots(token)


def _bounded_inner(words: list[str], ctx: _Ctx) -> list[str] | None:
    """The command of `python -I .../mergeq.py run --timeout N [--name X] [--cwd D] -- CMD`, or
    None if the segment is not that form. Raises Refuse for a malformed one: only --timeout
    (required, at most max_timeout), --name and --cwd are accepted, so --heavy stays out."""
    if not (
        len(words) > 4
        and PYTHON.fullmatch(words[0])
        and words[1] == "-I"
        and words[2].endswith("mergeq.py")
        and words[3] == "run"
    ):
        return None
    if not _mergeq_path_ok(words[2], ctx):
        raise Refuse(
            "mergeq.py must be this repo's devtools/mergeq/mergeq.py, spelled without $ or ~ "
            "and, after a `cd`, as an absolute path"
        )
    timeout = None
    i = 4
    while i < len(words) and words[i] != "--":
        value = words[i + 1] if i + 1 < len(words) else ""
        if words[i] == "--timeout" and re.fullmatch(r"\d+", value):
            timeout = int(value)
        elif words[i] == "--name" and re.fullmatch(r"[\w.@-]+", value):
            pass
        elif words[i] == "--cwd" and value and not value.startswith("-"):
            pass
        else:
            raise Refuse(
                "mergeq run for this agent takes only --timeout N, --name X and --cwd D, "
                f"not {words[i]!r}"
            )
        i += 2
    if i >= len(words) - 1:
        raise Refuse("mergeq run needs `-- <command>` after its options")
    if timeout is None or not 0 < timeout <= ctx.max_timeout:
        raise Refuse(f"mergeq run needs --timeout N with N in 1..{ctx.max_timeout}")
    return words[i + 1 :]


def _script_of(ctx: _Ctx, text: str) -> str | None:
    """The script a `python <script>.py` command runs, or None. The repo's own mergeq.py is not
    a scratch script (`mergeq.py status` has its own allow rule); a mergeq.py under the scratch
    roots is."""
    m = ctx.script_pat.search(text)
    if not m:
        return None
    script = m.group("script")
    if Path(script).name == "mergeq.py" and not ctx.under_roots(script):
        return None
    return script


def _only_flags(rest: list[str], flag_pat: str, what: str) -> None:
    """Every token is a listed flag or a positional (a path or a flag's value)."""
    pat = re.compile(flag_pat)
    for tok in rest:
        if tok.startswith("-") and tok != "-" and not pat.fullmatch(tok):
            raise Refuse(
                f"{tok!r} is not an allowed {what} option for this agent (read-only forms)"
            )


def _judge(words: list[str], ctx: _Ctx, bounded: bool) -> None:
    """Allow one command, or raise Refuse. `bounded`: it runs under `mergeq run --timeout`."""
    who = "the read-only agent" if ctx.readonly else "the reviewer"
    fix = "python -I devtools/mergeq/mergeq.py run --timeout 300 -- "
    core = words
    if core[:2] == ["uv", "run"]:
        core = core[2:]
        while core and re.fullmatch(r"--[a-z-]+", core[0]):
            core = core[1:]
    head = core[0] if core else ""
    text = " ".join(words)
    unbounded = (
        f"run this bounded (a bare run has no memory limit): `{fix}<command>`; "
        "--timeout N sets the time limit and the Job Object caps memory"
    )
    # repo lint, read-only
    if core in (["devtools/lint.py", "--check"],) or (
        len(core) == 3 and PYTHON.fullmatch(core[0]) and core[1:] == ["devtools/lint.py", "--check"]
    ):
        if ctx.readonly:
            raise Refuse("the read-only agent runs no code; devtools/lint.py is refused")
        return
    # scratch scripts
    script = _script_of(ctx, text)
    if script:
        if ctx.readonly:
            raise Refuse("the read-only agent runs no scripts")
        if not bounded:
            raise Refuse(unbounded)
        if not (ctx.under_roots(script) and (not ctx.cd_seen or Path(script).is_absolute())):
            raise Refuse(f"probe scripts must live under the scratch roots, not {script!r}")
        return
    if head == "node" and core[1:2] == ["--test"]:
        if ctx.readonly:
            raise Refuse("the read-only agent runs no tests")
        if not bounded:
            raise Refuse(unbounded)
        pat = re.compile(NODE_FLAGS)
        for tok in core[1:]:
            if tok.startswith("-") and not pat.fullmatch(tok):
                raise Refuse(f"{tok!r} is not an allowed node --test option for this agent")
        return
    if head == "ruff" and core[1:2] == ["check"]:
        _only_flags(core[2:], RUFF_FLAGS, "ruff check")
        return
    if head == "ruff" and core[1:2] == ["format"]:
        _only_flags(core[2:], RUFF_FORMAT_FLAGS, "ruff format")
        if not {"--check", "--diff"} & set(core[2:]):
            raise Refuse("ruff format needs --check or --diff for this agent (it writes files)")
        return
    if head == "ty" and core[1:2] == ["check"]:
        _only_flags(core[2:], TY_FLAGS, "ty check")
        return
    if head in ("ruff", "ty"):
        raise Refuse(f"only `{head} check{' and ruff format --check' if head == 'ruff' else ''}`")
    # read-only tools: no flag that runs a program or writes a file
    if words[0] in FORBIDDEN_FLAGS:
        for tok in words[1:]:
            if FORBIDDEN_FLAGS[words[0]].fullmatch(tok):
                raise Refuse(f"{words[0]} {tok} runs code or writes a file; refused")
    if words[0] == "sed":
        i = 1
        while i < len(words) and re.fullmatch(r"-[nEr]+", words[i]):
            i += 1
        if i >= len(words) or not SED_SCRIPT.fullmatch(words[i]):
            raise Refuse("sed is allowed only as `sed -n 'N,Mp' file` or `sed -n '/re/p' file`")
        if any(t.startswith("-") for t in words[i + 1 :]):
            raise Refuse("sed options after the script are not allowed")
    if words[0] == "awk":
        for tok in words[1:]:
            if AWK_DENY.search(tok) or (tok.startswith("-") and not re.fullmatch(r"-[Fv].*", tok)):
                raise Refuse(f"awk may not run commands or write files ({tok[:40]!r})")
    if not any(p.search(text) for p in ctx.allow):
        raise Refuse(f"{words[0]!r} is not on {who}'s read-only allowlist ({text[:80]!r})")


def _check_reviewer_shell(
    cfg: dict, tool: str, cmd: str, readonly: bool = False, cwd: str = ""
) -> None:
    who = "the read-only agent" if readonly else "the reviewer"
    if tool == "PowerShell":
        raise Refuse(f"{who} uses Bash only (read-only allowlist); PowerShell is refused")
    ctx = _Ctx(cfg, cwd or os.getcwd(), readonly)
    for seg in _segments(cmd):
        words = _words(seg, ctx.roots)
        if not words:
            continue
        text = " ".join(words)
        if readonly and any(p.search(text) for p in ctx.refuse):
            raise Refuse("the read-only agent runs no tests or code")
        if words[0] == "cd":
            if len(words) > 1 and ctx.under_roots(words[1]):
                raise Refuse("cd into the scratch roots is not allowed for this agent")
            ctx.cd_seen = True
            continue
        inner = _bounded_inner(words, ctx)
        if inner is not None:
            if readonly:
                raise Refuse("the read-only agent runs no mergeq run")
            _judge(inner, ctx, bounded=True)
        else:
            _judge(words, ctx, bounded=False)


# --------------------------------------------------------------------------- everyone


def _wrap(cfg: dict, event: dict, cmd: str, capped: bool = False) -> dict:
    m = TIMEOUT_PREFIX.search(cmd)
    seconds = int(cfg.get("hook", {}).get("default_timeout", 600))
    if m:
        seconds = int(m.group(3)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(4)]
    if capped:  # a reviewer's tests get the same ceiling as its scripts
        seconds = min(seconds, int(cfg.get("hook", {}).get("reviewer_max_timeout", 1800)))
    who = event.get("agent_type") or "main"
    where = Path(event.get("cwd") or os.getcwd()).name
    name = re.sub(r"[^A-Za-z0-9_.@-]", "_", f"{who}@{where}")
    mergeq = shlex.quote(MERGEQ.as_posix())
    wrapped = (
        f"python -I {mergeq} run --timeout {seconds} --name {name} -- bash -c {shlex.quote(cmd)}"
    )
    tool_input = dict(event.get("tool_input") or {})
    tool_input["command"] = wrapped
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": tool_input}}


def check(event: dict, cfg: dict) -> dict | None:
    """Raise Refuse to block; return a hook JSON object to rewrite; None to allow."""
    tool = event.get("tool_name", "")
    tool_input = event.get("tool_input") or {}
    hook = cfg.get("hook", {})
    agent = event.get("agent_type")
    readonly = bool(agent) and agent == hook.get("readonly_agent", "bob-investigator")
    reviewer = readonly or agent == hook.get("reviewer_agent", "bob-reviewer")
    subagent = bool(event.get("agent_id") or event.get("agent_type"))
    if tool in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if reviewer and not _under(path, _roots(cfg)):
            raise Refuse(
                f"this agent is read-only; {path} is outside the scratch roots. Report the fix instead"
            )
        return None
    if tool not in SHELL_TOOLS:
        return None
    cmd = tool_input.get("command", "")
    if reviewer:
        _check_reviewer_shell(cfg, tool, cmd, readonly, event.get("cwd") or "")
    code = _code_only(cmd)
    if not subagent:
        for pattern in hook.get("heavy_exempt", []):
            code = re.sub(pattern, "", code)
    heavy = next((p.pattern for p in _heavy_patterns(cfg) if p.search(code)), None)
    if heavy:
        if subagent:
            raise Refuse(
                f"heavy suites run in the merge queue, not in an agent (matched {heavy!r}). Run "
                "targeted tests (single files or -k) instead; the orchestrator gates your branch."
            )
        if not MERGEQ_HEAVY_OK.search(cmd):
            raise Refuse(
                f"this is a heavy suite (matched {heavy!r}). Run it under the machine-wide lock: "
                "`python -I devtools/mergeq/mergeq.py run --heavy --timeout <s> -- <command>`, "
                "or submit it to the queue."
            )
    # A segment that runs a test command and is not itself a `mergeq run` needs the wrap, even
    # when another segment of the command calls mergeq.
    timed = hook.get("needs_timeout", [])
    if any(
        any(re.search(p, seg) for p in timed) and not MERGEQ_RUN.search(seg)
        for seg in re.split(r"[;&|\n]+", code)
    ):
        if tool == "PowerShell":
            raise Refuse(
                "test runs need a hard timeout: use `python -I devtools/mergeq/mergeq.py run "
                "--timeout 600 -- <command>` (timeout.exe in PowerShell is a sleep), or use Bash."
            )
        return _wrap(cfg, event, cmd, reviewer)
    return None


def main() -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw)
    except ValueError as exc:
        print(f"mergeq hook: unreadable hook input ({exc}); refusing", file=sys.stderr)
        return 2
    if event.get("tool_name", "") in EDIT_TOOLS and not event.get("agent_type"):
        return 0  # never block the main session's edits: they are how a broken hook gets fixed
    try:
        out = check(event, _config())
    except Refuse as why:
        print(f"mergeq: {why}", file=sys.stderr)
        return 2
    except Exception as exc:  # fail closed: a broken hook must not silently allow
        print(
            f"mergeq hook is broken ({exc!r}); refusing until it is fixed. Repair "
            "devtools/mergeq/hook.py or mergeq.toml (the Edit tool is not blocked).",
            file=sys.stderr,
        )
        return 2
    if out is not None:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
