"""Render the codegen's output for a fixed corpus, so a refactor can show it changed no byte.

    uv run python devtools/render_corpus.py render OUT [--only SUBSTRING] [--base REF]
    uv run python devtools/render_corpus.py compare A B

To check a branch against its base, render both and compare them:

    uv run python devtools/render_corpus.py render build/rc-base --base origin/main
    uv run python devtools/render_corpus.py render build/rc-head
    uv run python devtools/render_corpus.py compare build/rc-base build/rc-head

``--base REF`` exports REF's ``src`` and ``tests`` with ``git archive`` into
``build/render-corpus-trees``.  It renders that tree with this tree's two corpus scripts,
so the base needs no copy of them.  A render stops unless it imports pyrung from the
``src`` beside its own ``devtools``.  So a base render can never render the head instead.

``render`` writes one file per program and mode under OUT, and ``OUT/SHA256SUMS``:

    kernel_b{0,1}_p{0,1}_f{0,1}.py   compile_kernel's source, for blockless, proof_metadata
                                     and force_rung_enable off (0) and on (1)
    kernel_b{0,1}_p{0,1}_f{0,1}.meta the kernel's other fields, such as timer_unit_ns
    kernel_b0_p0_f0_s1.py, .meta     the same with ``split_after=1``, which also writes
                                     ``pre_step_fn``, for the ``programs/`` entries only
    code.py, pyrung_rt.py            generate_circuitpy's output on a P1AM with one module

A mode that refuses the program writes ``<mode>.err`` with the exception's type and
message instead, so a refusal must stay the same too.  The corpus has three parts:

- ``render_corpus_programs``, which reaches every emitter branch three ways
- the facts idiom corpus, ``tests/facts/corpus.py``
- the conformance suite, built as one program and as its groups apart (``build_apart``)

The run prints which pyrung it imported.  It also prints the seconds ``compile_kernel``
spent writing source (``timings["source"]``) and the seconds ``generate_circuitpy`` took.

``compare`` lists every file that differs or is in only one render, and exits 1 if any is.
Time the base and the branch in the same interpreter.  The source does not depend on it,
but the times do.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
TREES = ROOT / "build" / "render-corpus-trees"

SUMS = "SHA256SUMS"
_UNSAFE = re.compile(r"[^A-Za-z0-9.+-]+")
_WORDS = (
    (" -> ", " to "),
    ("**", " pow "),
    ("==", " eq "),
    ("!=", " ne "),
    ("<=", " le "),
    (">=", " ge "),
    ("<", " lt "),
    (">", " gt "),
    ("*", " mul "),
    ("/", " div "),
    ("%", " mod "),
    ("|", " or "),
)


def _slug(label: str) -> str:
    """*label* as a file name.  Each operator becomes a word, so two labels never share one."""
    for text, word in _WORDS:
        label = label.replace(text, word)
    return _UNSAFE.sub("_", label).strip("_")


def _corpus(only: str | None) -> Iterator[tuple[str, Callable[[], Any]]]:
    """``(relative directory, build)`` for each program.

    *build* returns a Program, or a conformance build, which carries its own TagMap.
    """
    import render_corpus_programs as rcp

    entries: list[tuple[str, Callable[[], Any]]] = []
    for flavor in rcp.FLAVORS:
        for label, body in rcp.variants():
            entries.append(
                (
                    f"variants/{flavor}/{_slug(label)}",
                    lambda b=body, f=flavor: rcp.build_variant(b, f),
                )
            )
    for label, build in rcp.programs():
        entries.append((f"programs/{_slug(label)}", build))

    from tests.facts.corpus import CORPUS

    for name in sorted(CORPUS):
        entries.append((f"facts/{name}", CORPUS[name]))

    from tests.conformance import suite

    entries.append(("conformance/suite", suite.build))
    entries.append(("conformance/apart", suite.build_apart))

    seen: set[str] = set()
    for rel, build in entries:
        if rel in seen:
            raise ValueError(f"Two corpus entries render to {rel}")
        seen.add(rel)
        if only is None or only in rel:
            yield rel, build


def _error_text(exc: BaseException) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc))


def _uses_modbus_client(program: Any) -> bool:
    from pyrung.core.instruction.send_receive import (
        ModbusReceiveInstruction,
        ModbusSendInstruction,
    )
    from pyrung.core.validation._common import walk_instructions

    return any(
        isinstance(instr, ModbusSendInstruction | ModbusReceiveInstruction)
        for instr in walk_instructions(program)
    )


def _code_py(built: Any) -> Any:
    from pyrung.circuitpy import P1AM, generate_circuitpy
    from pyrung.circuitpy.modbus import ModbusClientConfig
    from pyrung.click import ModbusTcpTarget, TagMap
    from pyrung.conformance._build import ConformanceProgram

    if isinstance(built, ConformanceProgram):
        from pyrung.conformance._circuitpy import generate

        return generate(built)
    hw = P1AM()
    hw.slot(1, "P1-08SIM")
    if _uses_modbus_client(built):
        target = ModbusTcpTarget(name="peer", ip="192.168.1.50", port=1502, device_id=17)
        return generate_circuitpy(
            built,
            hw,
            target_scan_ms=10.0,
            modbus_client=ModbusClientConfig(targets=(target,)),
            tag_map=TagMap(),
        )
    return generate_circuitpy(built, hw, target_scan_ms=10.0)


def _meta(kernel: Any) -> str:
    """The fields of *kernel* that a kernel run reads besides its source, one line each.

    The sets and ``timer_unit_ns`` are sorted, since their order means nothing.
    ``referenced_tags`` and ``block_specs`` keep their order.
    """
    fields = {
        "timer_unit_ns": sorted(kernel.timer_unit_ns.items()),
        "referenced_tags": list(kernel.referenced_tags),
        "block_specs": [
            (
                name,
                spec.symbol,
                spec.size,
                spec.default,
                spec.tag_type.name,
                spec.tag_names,
                spec.tag_indices,
            )
            for name, spec in kernel.block_specs.items()
        ],
        "materialized_tag_names": sorted(kernel.materialized_tag_names),
        "indirect_block_info": sorted(
            (symbol, block, start, end, sorted(addresses))
            for symbol, (block, start, end, addresses) in kernel.indirect_block_info.items()
        ),
        "edge_tags": sorted(kernel.edge_tags),
        "has_io_gaps": kernel.has_io_gaps,
        "calls_functions": kernel.calls_functions,
    }
    return "".join(f"{name}: {value!r}\n" for name, value in fields.items())


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _pyrung_here() -> Path:
    """Where pyrung was imported from.  It must be this tree's ``src``, or the render stops."""
    import pyrung

    found = Path(pyrung.__file__).resolve().parent
    if not found.is_relative_to((ROOT / "src").resolve()):
        raise SystemExit(f"pyrung comes from {found}, not from {ROOT / 'src'}. Nothing rendered.")
    return found


def render(out: Path, only: str | None = None) -> dict[str, float]:
    """Render the corpus into *out* (which must not exist yet) and return the times."""
    from pyrung.circuitpy.codegen.render_kernel import compile_kernel
    from pyrung.conformance._build import ConformanceProgram

    _pyrung_here()
    if out.exists():
        raise SystemExit(f"{out} exists. Render into a new directory.")
    times = {"kernel source": 0.0, "code.py": 0.0, "programs": 0}

    def kernel(folder: Path, program: Any, mode: str, **options: Any) -> None:
        timings: dict[str, float] = {}
        try:
            compiled = compile_kernel(program, timings=timings, **options)
        except Exception as exc:  # noqa: BLE001
            _write(folder / f"{mode}.err", _error_text(exc))
            return
        times["kernel source"] += timings["source"]
        _write(folder / f"{mode}.py", compiled.source)
        _write(folder / f"{mode}.meta", _meta(compiled))

    for rel, build in _corpus(only):
        folder = out / rel
        try:
            built = build()
        except Exception as exc:  # noqa: BLE001 - a refusal is part of the render
            _write(folder / "build.err", _error_text(exc))
            continue
        times["programs"] += 1
        program = built.program if isinstance(built, ConformanceProgram) else built
        for blockless in (False, True):
            for proof in (False, True):
                for force in (False, True):
                    mode = f"kernel_b{int(blockless)}_p{int(proof)}_f{int(force)}"
                    options = {"blockless": blockless, "proof_metadata": proof}
                    kernel(folder, program, mode, force_rung_enable=force, **options)
        if rel.startswith("programs/"):
            kernel(folder, program, "kernel_b0_p0_f0_s1", split_after=1)
        started = time.perf_counter()
        try:
            output = _code_py(built)
        except Exception as exc:  # noqa: BLE001
            _write(folder / "code.err", _error_text(exc))
            continue
        times["code.py"] += time.perf_counter() - started
        _write(folder / "code.py", output.code)
        if output.runtime:
            _write(folder / "pyrung_rt.py", output.runtime)
    _write(out / SUMS, _sums(out))
    return times


def _export(ref: str) -> Path:
    """REF's ``src`` and ``tests``, exported once per commit, plus this tree's corpus scripts."""
    found = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if found.returncode:
        raise SystemExit(f"{ref!r} is not a git ref: {found.stderr.strip()}")
    sha = found.stdout.strip()
    tree = TREES / sha
    if not (tree / ".complete").exists():
        archive = subprocess.run(
            ["git", "-C", str(ROOT), "archive", "--format=tar", sha, "src", "tests"],
            capture_output=True,
            check=True,
        )
        shutil.rmtree(tree, ignore_errors=True)  # a half-extracted earlier try
        tree.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(tree, filter="data")
        (tree / ".complete").write_text(sha)  # last, so a crash never looks done
    (tree / "devtools").mkdir(exist_ok=True)
    for name in ("render_corpus.py", "render_corpus_programs.py"):
        shutil.copyfile(ROOT / "devtools" / name, tree / "devtools" / name)
    return tree


def _files(root: Path) -> dict[str, Path]:
    return {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path.name != SUMS
    }


def _sums(root: Path) -> str:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {rel}"
        for rel, path in sorted(_files(root).items())
    ]
    return "\n".join(lines) + "\n"


def compare(a: Path, b: Path) -> list[str]:
    """The files that differ between renders *a* and *b*, or are in only one."""
    files_a, files_b = _files(a), _files(b)
    problems = [f"only in {a}: {rel}" for rel in sorted(files_a.keys() - files_b.keys())]
    problems += [f"only in {b}: {rel}" for rel in sorted(files_b.keys() - files_a.keys())]
    problems += [
        f"differs: {rel}"
        for rel in sorted(files_a.keys() & files_b.keys())
        if files_a[rel].read_bytes() != files_b[rel].read_bytes()
    ]
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    render_cmd = commands.add_parser("render", help="render the corpus into a new directory")
    render_cmd.add_argument("out", type=Path)
    render_cmd.add_argument("--only", help="render only the entries whose path holds this text")
    render_cmd.add_argument("--base", help="render this git ref's tree instead of this one")
    compare_cmd = commands.add_parser("compare", help="compare two renders")
    compare_cmd.add_argument("a", type=Path)
    compare_cmd.add_argument("b", type=Path)
    args = parser.parse_args(argv)

    if args.command == "render" and args.base:
        script = _export(args.base) / "devtools" / "render_corpus.py"
        command = [sys.executable, str(script), "render", str(args.out.resolve())]
        return subprocess.run(command + (["--only", args.only] if args.only else [])).returncode
    if args.command == "render":
        print(f"pyrung from {_pyrung_here()}")
        started = time.perf_counter()
        times = render(args.out, args.only)
        files = len(_files(args.out))
        print(
            f"{times['programs']} programs, {files} files in {args.out}"
            f" ({time.perf_counter() - started:.1f} s)"
        )
        print(f"kernel source {times['kernel source']:.3f} s, code.py {times['code.py']:.3f} s")
        return 0

    problems = compare(args.a, args.b)
    for line in problems:
        print(line)
    if problems:
        print(f"{len(problems)} files differ")
        return 1
    print(f"same: {len(_files(args.a))} files")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv[1:]))
