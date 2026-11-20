"""Validate CSV operand syntax before it becomes executable Python source.

This validates data accepted by the importer; it is not a sandbox for user
programs. Only the expression forms used by Click instructions are accepted.
"""

from __future__ import annotations

import ast
import json
import re


def _string_literal(value: str) -> str:
    """Render a Python string while retaining the generator's double quotes."""
    return json.dumps(value, ensure_ascii=False)


def _read_csv_string(source: str, start: int = 0) -> tuple[str, int]:
    """Decode Click's doubled quotes; backslashes are ordinary characters."""
    chars: list[str] = []
    index = start + 1
    while index < len(source):
        char = source[index]
        if char != '"':
            chars.append(char)
        elif index + 1 < len(source) and source[index + 1] == '"':
            chars.append('"')
            index += 1
        else:
            return "".join(chars), index + 1
        index += 1
    raise ValueError(f"Malformed CSV string literal: {source!r}")


def _csv_string_value(source: str) -> str:
    value, end = _read_csv_string(source)
    if end != len(source):
        raise ValueError(f"Expected CSV string literal: {source!r}")
    return value


_OPERATORS = ("==", "!=", ">=", "<=", ">", "<")

_CALL_CELL = re.compile(r"^(\.?[A-Za-z_][A-Za-z0-9_]*)\((.*)\)$", re.DOTALL)


def _double_inner_quotes(text: str) -> str:
    """Rewrite the quoted *text*, whose quotes are as typed, with each inner quote written twice."""
    return '"' + text[1:-1].replace('"', '""') + '"'


def _kwarg_eq_index(part: str) -> int:
    """Return the index of the ``=`` of a keyword argument in *part*, or -1.

    An ``=`` in ``==``, ``!=``, ``<=`` or ``>=`` and an ``=`` inside quotes do not count.
    """
    in_quote = False
    for index, char in enumerate(part):
        if char == '"':
            in_quote = not in_quote
        elif char == "=" and not in_quote:
            before = part[index - 1] if index else ""
            after = part[index + 1] if index + 1 < len(part) else ""
            if before not in "=!<>" and after != "=":
                return index
    return -1


def _split_paired(args: str) -> list[str]:
    """Split *args* at each comma outside paired quotes and brackets.

    Raise ValueError for a quote with no partner.
    """
    parts: list[str] = []
    start = depth = index = 0
    in_quote = False
    while index < len(args):
        char = args[index]
        if in_quote:
            if char == '"':
                if args.startswith('""', index):
                    index += 1
                else:
                    in_quote = False
        elif char == '"':
            in_quote = True
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == "," and depth == 0:
            parts.append(args[start:index].strip())
            start = index + 1
        index += 1
    if in_quote:
        raise ValueError(f"Unmatched quote in CSV cell arguments: {args!r}")
    return [*parts, args[start:].strip()]


def _refuse_quoted_keyword(call: str, args: str) -> None:
    """Raise ValueError for a keyword value in quotes in *args*, as laddercodec does."""
    for part in _split_paired(args):
        eq = _kwarg_eq_index(part)
        if eq >= 0 and part[eq + 1 :].strip().startswith('"'):
            raise ValueError(f"{call}(): the keyword value in {part!r} must have no quotes")


def _text_holds_the_rest(call: str, args: str, last: int) -> bool:
    """Return whether the arguments after the text still hold what *call* needs.

    A Copy needs a destination. A Search needs ``result=`` and ``found=``.
    """
    rest = [part.strip() for part in args[last + 1 :].split(",")][1:]
    if call == "copy":
        return any(part and _kwarg_eq_index(part) < 0 for part in rest)
    keys = {part[: _kwarg_eq_index(part)].strip() for part in rest if _kwarg_eq_index(part) >= 0}
    return {"result", "found"} <= keys


def _typed_text_cell(cell: str) -> str:
    """Return *cell* with each text that holds a quote rewritten with each inner quote written twice.

    CLICK keeps the text of a Copy, a Search and a compare as typed and has no escape for
    a quote. So ``copy("A""B",TXT1)`` copies the four characters ``A""B``, and
    ``copy("A"B",TXT1)`` copies three. The rest of the importer reads a quote written
    twice as one quote, so this function rewrites the text that way. A keyword value in a call
    has no quotes, so the text of a Copy or a Search runs from the first quote in the
    arguments to the last. A compare cell reads as laddercodec reads it: the text runs
    from the first quote to the last when only one side is text, and each side that starts
    and ends with a quote is text when both are.

    Raise ValueError for a Copy or Search cell with a keyword value in quotes, such as
    ``oneshot="1"``. laddercodec refuses to read it.
    """
    if '"' not in cell:
        return cell
    call = _CALL_CELL.fullmatch(cell)
    if call is not None:
        name, args = call.group(1), call.group(2)
        if name not in ("copy", "search"):
            return cell
        first, last = args.find('"'), args.rfind('"')
        if first == last:
            return cell
        before = args[:first]
        opens = before.strip() == "" if name == "copy" else "," not in before
        if opens and _text_holds_the_rest(name, args, last):
            if '"' not in args[first + 1 : last]:
                return cell
            text = _double_inner_quotes(args[first : last + 1])
            return f"{name}({args[:first]}{text}{args[last + 1 :]})"
        _refuse_quoted_keyword(name, args)
        return cell
    return _typed_compare_cell(cell)


def _text_pair_splits(token: str) -> list[tuple[str, str, str]]:
    """Return each way to read *token* as a compare of two quoted texts.

    Each way is an operator with a text of at least one character on each side. A token
    such as ``"A"=="B"`` has one way. A token such as ``"A"=="B"=="C"`` has two, and
    laddercodec refuses to read it.
    """
    first, last = token.find('"'), token.rfind('"')
    found = []
    for index in range(first + 1, last):
        for op in _OPERATORS:
            if not token.startswith(op, index):
                continue
            left, right = token[:index].strip(), token[index + len(op) :].strip()
            if len(left) >= 3 and left[-1] == '"' and len(right) >= 3 and right[0] == '"':
                found.append((left, op, right))
    return found


def _typed_compare_cell(token: str) -> str:
    first, last = token.find('"'), token.rfind('"')
    if first >= last:
        return token
    left_text = token[:first].strip() == ""
    right_text = token[last + 1 :].strip() == ""
    if left_text != right_text:
        text = _double_inner_quotes(token[first : last + 1])
        return token[:first] + text + token[last + 1 :]
    if not left_text:
        return token
    found = _text_pair_splits(token)
    if len(found) != 1:
        return token
    left, op, right = found[0]
    return f"{_double_inner_quotes(left)}{op}{_double_inner_quotes(right)}"


def _pythonize_csv_strings(source: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(source):
        if source[index] == '"':
            value, index = _read_csv_string(source, index)
            parts.append(_string_literal(value))
        elif source[index] in "'#":
            raise ValueError(f"Unsupported CSV operand expression: {source!r}")
        else:
            parts.append(source[index])
            index += 1
    return "".join(parts)


def _validate_expression(
    source: str,
    *,
    functions: set[str] | frozenset[str],
    constants: set[str] | frozenset[str],
    operand_pattern: re.Pattern[str],
    blocks: set[str] | frozenset[str],
) -> ast.expr:
    """Accept literals, addresses, arithmetic, comparisons and known calls.

    The caller normalizes Click operators and range syntax first. Validation
    deliberately precedes nickname substitution, so imported names cannot add
    Python expression capabilities.
    """

    def reject() -> None:
        raise ValueError(f"Unsupported CSV operand expression: {source!r}")

    try:
        root = ast.parse(source, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise ValueError(f"Unsupported CSV operand expression: {source!r}") from None

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Constant):
            if type(node.value) not in (str, int, float, bool, type(None)):
                reject()
        elif isinstance(node, ast.Name):
            if node.id not in constants and not operand_pattern.fullmatch(node.id):
                reject()
        elif isinstance(node, ast.BinOp):
            if not isinstance(
                node.op,
                (
                    ast.Add,
                    ast.Sub,
                    ast.Mult,
                    ast.Div,
                    ast.FloorDiv,
                    ast.Mod,
                    ast.Pow,
                    ast.BitAnd,
                    ast.BitOr,
                    ast.BitXor,
                    ast.LShift,
                    ast.RShift,
                ),
            ):
                reject()
            visit(node.left)
            visit(node.right)
        elif isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.UAdd, ast.USub, ast.Invert)):
                reject()
            visit(node.operand)
        elif isinstance(node, ast.Compare):
            if any(
                not isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                for op in node.ops
            ):
                reject()
            visit(node.left)
            for comparator in node.comparators:
                visit(comparator)
        elif isinstance(node, ast.List):
            for item in node.elts:
                visit(item)
        elif isinstance(node, ast.Subscript):
            if not isinstance(node.value, ast.Name) or node.value.id not in blocks:
                reject()
            visit(node.slice)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in functions:
                reject()
            for arg in node.args:
                visit(arg)
            for kwarg in node.keywords:
                if kwarg.arg is None:
                    reject()
                visit(kwarg.value)
        else:
            reject()

    try:
        visit(root.body)
    except RecursionError:
        reject()
    return root.body
