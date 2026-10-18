"""Strict, bounded offline mathematical expression parsing (Stage C4).

Investigation-only. This module is NOT imported by the production app and
imports nothing from it. No network, no AI calls, no disk writes, no eval,
no exec, no ``sympy.sympify`` and no ``sympy.parse_expr``: untrusted text is
NEVER passed to an unrestricted parser.

Security model
==============

Input is a plain bounded ``str``. It is parsed once with Python's ``ast``
module in expression mode and validated node by node against an explicit
whitelist:

- ``BinOp`` with ``+ - * / **``
- ``UnaryOp`` with ``+ -``
- ``Constant``: bounded integers; floats are kept as *approximate data*
- ``Name``: single short identifiers from an explicit whitelist
- ``Call``: only whitelisted mathematical functions by plain name and
  position (no attributes, no keywords, no starred arguments, no lambdas,
  no comprehensions, no comparisons, no f-strings)

Everything else is refused. The Sympy expression is built node-by-node from
Sympy objects; the input string is never handed to Sympy or evaluated.

Refusals are explicit values with fixed reason codes
(``unsupported_syntax``, ``unsupported_function``, ``unknown_symbol``,
``expression_too_long``, ``expression_too_complex``, ``number_too_large``,
``exponent_too_large``, ``not_a_string``, ``unexpected_equals``) and never
echo raw input.

Resource bounds
===============

Length, node-count and depth caps; integer magnitude caps; integer exponent
magnitude caps; and a combinatorial expand bound computed WITHOUT expanding,
so no downstream check can drive Sympy into exponential work.
"""

import ast
from math import comb

PARSER_SCHEMA = "investigation.safe_math/1"

MAX_EXPR_CHARS = 500
MAX_EXPR_NODES = 60
MAX_EXPR_DEPTH = 40
MAX_INTEGER_DIGITS = 12
MAX_FLOAT_MAGNITUDE = 1e6
MAX_EXPONENT_MAGNITUDE = 50
MAX_EXPANDED_TERMS = 4096
EXPAND_POWER_BOUND = 20

DEFAULT_VARIABLES = ("x", "y", "z", "a", "b", "c", "k", "m", "n", "t")
WHITELISTED_FUNCTIONS = ("sqrt", "abs")

REFUSAL_UNSUPPORTED_SYNTAX = "unsupported_syntax"
REFUSAL_UNSUPPORTED_FUNCTION = "unsupported_function"
REFUSAL_UNKNOWN_SYMBOL = "unknown_symbol"
REFUSAL_TOO_LONG = "expression_too_long"
REFUSAL_TOO_COMPLEX = "expression_too_complex"
REFUSAL_NUMBER_TOO_LARGE = "number_too_large"
REFUSAL_EXPONENT_TOO_LARGE = "exponent_too_large"
REFUSAL_NOT_A_STRING = "not_a_string"
REFUSAL_UNEXPECTED_EQUALS = "unexpected_equals"

_KNOWN_REFUSALS = frozenset({
    REFUSAL_UNSUPPORTED_SYNTAX, REFUSAL_UNSUPPORTED_FUNCTION,
    REFUSAL_UNKNOWN_SYMBOL, REFUSAL_TOO_LONG, REFUSAL_TOO_COMPLEX,
    REFUSAL_NUMBER_TOO_LARGE, REFUSAL_EXPONENT_TOO_LARGE,
    REFUSAL_NOT_A_STRING, REFUSAL_UNEXPECTED_EQUALS,
})


class ExpressionRefused(ValueError):
    """Bounded refusal: fixed reason code; never echoes input content."""

    def __init__(self, reason, detail=""):
        if reason not in _KNOWN_REFUSALS:
            raise AssertionError("unknown refusal reason")
        self.reason = reason
        keep = "".join(
            character if character.isalnum() or character in "-_.=()" else ""
            for character in str(detail))[:60]
        self.detail = keep
        super().__init__(f"{reason}: {keep}" if keep else reason)


def validate_variables(variables):
    """Validate a caller-supplied whitelist of short identifiers."""
    if not isinstance(variables, (tuple, list)) or not variables \
            or not all(isinstance(item, str) and item and len(item) <= 4
                       and item.isidentifier() and item.isalpha()
                       for item in variables):
        raise ValueError("variables must be short alphabetic identifiers")
    if len(set(variables)) != len(variables):
        raise ValueError("variable whitelist repeats")
    return tuple(variables)


def count_ast_nodes(tree):
    return sum(1 for _ in ast.walk(tree))


def _safe_ast_parse(text):
    """One audited boundary: Syntax/Memory/Recursion errors are refusals."""
    try:
        return ast.parse(text, mode="eval")
    except ExpressionRefused:
        raise
    except (SyntaxError, ValueError):
        raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX) from None
    except (RecursionError, MemoryError):
        raise ExpressionRefused(REFUSAL_TOO_COMPLEX) from None


class _BoundedParser:
    """Validated-AST Sympy builder. The input never executes anywhere."""

    def __init__(self, whitelist):
        self._whitelist = frozenset(whitelist)
        self._depth = 0

    def parse(self, text):
        tree = _safe_ast_parse(text)
        return self._visit(tree.body)

    def _visit(self, node):
        self._depth += 1
        if self._depth > MAX_EXPR_DEPTH:
            raise ExpressionRefused(REFUSAL_TOO_COMPLEX)
        try:
            return self._dispatch(node)
        finally:
            self._depth -= 1

    def _dispatch(self, node):
        if isinstance(node, ast.Constant):
            return self._constant(node.value)
        if isinstance(node, ast.Name):
            return self._name(node.id)
        if isinstance(node, ast.UnaryOp) \
                and isinstance(node.op, (ast.USub, ast.UAdd)):
            operand = self._visit(node.operand)
            return operand if isinstance(node.op, ast.UAdd) else -operand
        if isinstance(node, ast.BinOp):
            return self._binary(node)
        if isinstance(node, ast.Call):
            return self._call(node)
        raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX)

    @staticmethod
    def _constant(value):
        from sympy import Integer, Float, Rational
        if isinstance(value, bool):
            raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX)
        if isinstance(value, int):
            if len(str(abs(value))) > MAX_INTEGER_DIGITS:
                raise ExpressionRefused(REFUSAL_NUMBER_TOO_LARGE)
            return Integer(value)
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX)
            if abs(value) > MAX_FLOAT_MAGNITUDE:
                raise ExpressionRefused(REFUSAL_NUMBER_TOO_LARGE)
            # Floats stay approximate data, never silently exact rationals;
            # str() keeps the decimal digits exactly as written/evaluated.
            return Float(str(value))
        raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX)

    def _name(self, identifier):
        if identifier not in self._whitelist:
            raise ExpressionRefused(REFUSAL_UNKNOWN_SYMBOL, identifier)
        from sympy import Symbol
        return Symbol(identifier)

    def _binary(self, node):
        from sympy import Add, Mul, Pow, Integer
        left = self._visit(node.left)
        right = self._visit(node.right)
        if isinstance(node.op, ast.Add):
            return Add(left, right)
        if isinstance(node.op, ast.Sub):
            return Add(left, -right)
        if isinstance(node.op, ast.Mult):
            return Mul(left, right)
        if isinstance(node.op, ast.Div):
            return Mul(left, Pow(right, Integer(-1)))
        if isinstance(node.op, ast.Pow):
            _check_power(right)
            return Pow(left, right)
        raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX)

    def _call(self, node):
        if node.keywords or node.args and any(
                isinstance(argument, ast.Starred)
                for argument in node.args):
            raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX)
        if not isinstance(node.func, ast.Name):
            raise ExpressionRefused(REFUSAL_UNSUPPORTED_FUNCTION)
        if node.func.id not in WHITELISTED_FUNCTIONS or len(node.args) != 1:
            raise ExpressionRefused(REFUSAL_UNSUPPORTED_FUNCTION,
                                    node.func.id)
        argument = self._visit(node.args[0])
        from sympy import sqrt, Abs
        return sqrt(argument) if node.func.id == "sqrt" else Abs(argument)


def _check_power(exponent):
    from sympy import Integer
    if isinstance(exponent, Integer) and \
            abs(int(exponent)) > MAX_EXPONENT_MAGNITUDE:
        raise ExpressionRefused(REFUSAL_EXPONENT_TOO_LARGE)


def expand_is_bounded(expression):
    """True when expand-like work is bounded; computed WITHOUT expanding."""
    from sympy import Add, Mul, Pow

    def bound(node):
        if isinstance(node, Add):
            return sum(bound(argument) for argument in Add.make_args(node))
        if isinstance(node, Mul):
            total = 1
            for factor in Mul.make_args(node):
                total *= bound(factor)
                if total > MAX_EXPANDED_TERMS:
                    return MAX_EXPANDED_TERMS + 1
                if total < 0:
                    return MAX_EXPANDED_TERMS + 1
            return total
        if isinstance(node, Pow):
            base, exponent = node.args
            if bound(base) > MAX_EXPANDED_TERMS:
                return MAX_EXPANDED_TERMS + 1
            if exponent.is_number is False:
                return MAX_EXPANDED_TERMS + 1   # symbolic power: refuse expand
            addlen = len(Add.make_args(base))
            if addlen == 0:
                return 1
            if exponent.is_Integer:
                power = int(exponent)
            else:
                return 1                        # sqrt-like radicals: not additive blowup
            if power == 0:
                return 1
            repeat = abs(power) if power < 0 else power
            if repeat > EXPAND_POWER_BOUND:
                return MAX_EXPANDED_TERMS + 1
            terms = comb(addlen + repeat - 1, repeat)
            return min(terms, MAX_EXPANDED_TERMS) \
                if terms <= MAX_EXPANDED_TERMS else MAX_EXPANDED_TERMS + 1
        return 1

    return bound(expression) <= MAX_EXPANDED_TERMS


def _split_equation(text, allow_equation):
    if text.count("=") == 0:
        return text, None
    if not allow_equation or text.count("=") != 1:
        raise ExpressionRefused(REFUSAL_UNEXPECTED_EQUALS,
                                "expected at most one '='")
    left, right = text.split("=")
    if not left.strip() or not right.strip():
        raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX,
                                "empty side of equation")
    return left.strip(), right.strip()


def parse_bounded_expression(text, variables=None, *, allow_equation=False):
    """Parse one bounded expression, or ``lhs = rhs`` when allowed.

    Returns ``{"schema", "variables", "has_floats", "lhs", "rhs"}`` where
    ``rhs`` is None unless an equation was requested. Raises
    :class:`ExpressionRefused` for every refusal; reason codes are explicit
    and the raw input is never echoed back.
    """
    if not isinstance(text, str):
        raise ExpressionRefused(REFUSAL_NOT_A_STRING)
    if len(text) > MAX_EXPR_CHARS:
        raise ExpressionRefused(REFUSAL_TOO_LONG)
    if not text.strip():
        raise ExpressionRefused(REFUSAL_UNSUPPORTED_SYNTAX)
    left_text, right_text = _split_equation(text, allow_equation)
    whitelist = validate_variables(DEFAULT_VARIABLES if variables is None
                                   else variables)
    for part in (left_text, right_text):
        if part is None:
            continue
        plus_tree = _safe_ast_parse(part)
        nodes = count_ast_nodes(plus_tree)
        if nodes > MAX_EXPR_NODES:
            raise ExpressionRefused(REFUSAL_TOO_COMPLEX)
    parts = []
    has_floats = False
    free_symbols = set()
    for part in (left_text, right_text):
        if part is None:
            parts.append(None)
            continue
        parser = _BoundedParser(whitelist)
        expression = parser.parse(part)
        free = expression.free_symbols
        free_symbols.update(free)
        from sympy import Float as SympyFloat
        if expression.atoms(SympyFloat):
            has_floats = True
        parts.append(expression)
    return {
        "schema": PARSER_SCHEMA,
        "variables": sorted(name.name for name in free_symbols),
        "has_floats": has_floats,
        "lhs": parts[0],
        "rhs": parts[1],
    }


def canonical_text(sympy_expression):
    """Deterministic bounded rendering of a Sympy expression."""
    from sympy import srepr
    return srepr(sympy_expression)
