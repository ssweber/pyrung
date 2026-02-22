"""Parser and compiler for DAP breakpoint condition expressions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pyrung.core.state import SystemState


@dataclass(frozen=True)
class TagRef:
    name: str


@dataclass(frozen=True)
class Literal:
    value: int | float | bool | str


@dataclass(frozen=True)
class Compare:
    tag: TagRef
    op: str | None
    right: Literal | None


@dataclass(frozen=True)
class Not:
    child: TagRef


@dataclass(frozen=True)
class And:
    children: list[Expr]


@dataclass(frozen=True)
class Or:
    children: list[Expr]


Expr = Compare | Not | And | Or


class ExpressionParseError(ValueError):
    """Raised when a condition expression is invalid."""

    def __init__(self, message: str, position: int) -> None:
        super().__init__(f"{message} at position {position}")
        self.position = position
        self.message = message


def parse(source: str) -> Expr:
    """Parse a condition expression into an AST."""
    parser = _Parser(source)
    return parser.parse()


def validate(source: str) -> list[str]:
    """Validate an expression and return parse errors (empty if valid)."""
    try:
        parse(source)
    except ExpressionParseError as exc:
        return [str(exc)]
    return []


def compile(expr: Expr) -> Callable[[SystemState], bool]:
    """Compile a parsed expression into a predicate callable."""

    def _eval(node: Expr, state: SystemState) -> bool:
        if isinstance(node, Compare):
            left = _tag_value(state, node.tag.name)
            if node.op is None:
                return bool(left)
            assert node.right is not None
            right = node.right.value
            try:
                if node.op == "==":
                    return left == right
                if node.op == "!=":
                    return left != right
                if node.op == "<":
                    return left < right  # type: ignore[operator]
                if node.op == "<=":
                    return left <= right  # type: ignore[operator]
                if node.op == ">":
                    return left > right  # type: ignore[operator]
                if node.op == ">=":
                    return left >= right  # type: ignore[operator]
            except TypeError:
                return False
            return False
        if isinstance(node, Not):
            return not bool(_tag_value(state, node.child.name))
        if isinstance(node, And):
            return all(_eval(child, state) for child in node.children)
        if isinstance(node, Or):
            return any(_eval(child, state) for child in node.children)
        return False

    return lambda state: _eval(expr, state)


def _tag_value(state: SystemState, name: str) -> Any:
    if name in state.tags:
        return state.tags.get(name)
    if name in state.memory:
        return state.memory.get(name)
    return None


class _Parser:
    _COMP_OPS = ("==", "!=", "<=", ">=", "<", ">")

    def __init__(self, source: str) -> None:
        self._source = source
        self._pos = 0

    def parse(self) -> Expr:
        self._skip_ws()
        if self._eof():
            self._error("Expression cannot be empty")
        expr = self._parse_expr()
        self._skip_ws()
        if not self._eof():
            self._error(
                f"Expected operator or end of expression, got {self._peek_snippet()!r}"
            )
        return expr

    def _parse_expr(self) -> Expr:
        items = [self._parse_or().expr]
        while True:
            self._skip_ws()
            if not self._consume_if(","):
                break
            items.append(self._parse_or().expr)
        if len(items) == 1:
            return items[0]
        return And(children=items)

    @dataclass
    class _ParsedNode:
        expr: Expr
        bare_comparison: bool = False

    def _parse_or(self) -> _ParsedNode:
        items = [self._parse_and()]
        while True:
            self._skip_ws()
            if not self._consume_if("|"):
                break
            self._reject_bare_comparison_in_boolean_operator(items[-1], "|")
            next_item = self._parse_and()
            self._reject_bare_comparison_in_boolean_operator(next_item, "|")
            items.append(next_item)
        if len(items) == 1:
            return items[0]
        return self._ParsedNode(expr=Or(children=[item.expr for item in items]))

    def _parse_and(self) -> _ParsedNode:
        items = [self._parse_atom()]
        while True:
            self._skip_ws()
            if not self._consume_if("&"):
                break
            self._reject_bare_comparison_in_boolean_operator(items[-1], "&")
            next_item = self._parse_atom()
            self._reject_bare_comparison_in_boolean_operator(next_item, "&")
            items.append(next_item)
        if len(items) == 1:
            return items[0]
        return self._ParsedNode(expr=And(children=[item.expr for item in items]))

    def _parse_atom(self) -> _ParsedNode:
        self._skip_ws()
        if self._eof():
            self._error("Expected expression")

        if self._consume_if("("):
            nested = self._parse_expr()
            self._skip_ws()
            self._expect(")")
            return self._ParsedNode(expr=nested)

        if self._consume_if("~"):
            self._skip_ws()
            if self._peek() == "(":
                self._error("~ only supports single tag negation")
            tag_name = self._parse_tag()
            return self._ParsedNode(expr=Not(child=TagRef(name=tag_name)))

        tag_name = self._parse_tag()
        self._skip_ws()
        if tag_name in {"all_of", "any_of"} and self._consume_if("("):
            args = self._parse_call_args(tag_name)
            if tag_name == "all_of":
                return self._ParsedNode(expr=And(children=args))
            return self._ParsedNode(expr=Or(children=args))

        op = self._parse_comp_op()
        if op is None:
            return self._ParsedNode(expr=Compare(tag=TagRef(name=tag_name), op=None, right=None))

        literal = self._parse_value()
        return self._ParsedNode(
            expr=Compare(tag=TagRef(name=tag_name), op=op, right=Literal(value=literal)),
            bare_comparison=True,
        )

    def _parse_call_args(self, fn_name: str) -> list[Expr]:
        self._skip_ws()
        if self._consume_if(")"):
            self._error(f"{fn_name} requires at least one argument")

        args: list[Expr] = [self._parse_or().expr]
        while True:
            self._skip_ws()
            if self._consume_if(")"):
                return args
            self._expect(",")
            args.append(self._parse_or().expr)

    def _reject_bare_comparison_in_boolean_operator(
        self, node: _ParsedNode, operator: str
    ) -> None:
        if not node.bare_comparison:
            return
        self._error(
            f"Comparisons used with '{operator}' must be parenthesized, e.g. A {operator} (B > 1)"
        )

    def _parse_comp_op(self) -> str | None:
        self._skip_ws()
        for op in self._COMP_OPS:
            if self._source.startswith(op, self._pos):
                self._pos += len(op)
                return op
        return None

    def _parse_value(self) -> int | float | bool | str:
        self._skip_ws()
        if self._eof():
            self._error("Expected comparison value")
        quote = self._peek()
        if quote in {"'", '"'}:
            return self._parse_string()

        token = self._parse_bare_token()
        lowered = token.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        try:
            return int(token)
        except ValueError:
            pass
        try:
            return float(token)
        except ValueError:
            self._error(f"Expected literal value, got {token!r}")
        raise AssertionError("unreachable")

    def _parse_string(self) -> str:
        quote = self._peek()
        assert quote in {"'", '"'}
        self._pos += 1
        parts: list[str] = []
        while not self._eof():
            ch = self._peek()
            if ch == quote:
                self._pos += 1
                return "".join(parts)
            if ch == "\\":
                self._pos += 1
                if self._eof():
                    self._error("Unterminated escape sequence")
                parts.append(self._peek())
                self._pos += 1
                continue
            parts.append(ch)
            self._pos += 1
        self._error("Unterminated string literal")
        raise AssertionError("unreachable")

    def _parse_tag(self) -> str:
        self._skip_ws()
        start = self._pos
        if self._eof():
            self._error("Expected tag reference")
        first = self._peek()
        if not (first.isalpha() or first == "_"):
            self._error(f"Expected tag reference, got {self._peek_snippet()!r}")

        bracket_depth = 0
        while not self._eof():
            ch = self._peek()
            if ch == "[":
                bracket_depth += 1
                self._pos += 1
                continue
            if ch == "]":
                if bracket_depth == 0:
                    self._error("Unexpected ']'")
                bracket_depth -= 1
                self._pos += 1
                continue
            if bracket_depth == 0 and (
                ch.isspace() or ch in {",", "&", "|", "(", ")", "=", "!", "<", ">", "~"}
            ):
                break
            self._pos += 1

        if bracket_depth != 0:
            self._error("Unterminated '[' in tag reference")

        name = self._source[start : self._pos]
        if not name:
            self._error("Expected tag reference")
        return name

    def _parse_bare_token(self) -> str:
        self._skip_ws()
        start = self._pos
        while not self._eof():
            ch = self._peek()
            if ch.isspace() or ch in {",", "&", "|", ")"}:
                break
            self._pos += 1
        token = self._source[start : self._pos]
        if not token:
            self._error("Expected value")
        return token

    def _skip_ws(self) -> None:
        while not self._eof() and self._source[self._pos].isspace():
            self._pos += 1

    def _peek(self) -> str:
        return self._source[self._pos]

    def _consume_if(self, token: str) -> bool:
        if self._source.startswith(token, self._pos):
            self._pos += len(token)
            return True
        return False

    def _expect(self, token: str) -> None:
        if not self._consume_if(token):
            self._error(f"Expected {token!r}, got {self._peek_snippet()!r}")

    def _peek_snippet(self) -> str:
        if self._eof():
            return "<end>"
        return self._source[self._pos : self._pos + 8]

    def _eof(self) -> bool:
        return self._pos >= len(self._source)

    def _error(self, message: str) -> None:
        raise ExpressionParseError(message, self._pos + 1)
