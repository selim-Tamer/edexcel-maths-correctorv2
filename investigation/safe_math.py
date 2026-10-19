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

PARSER_SCHEMA = "investigation.safe_math/2"

MAX_EXPR_CHARS = 500
MAX_EXPR_NODES = 60
MAX_EXPR_DEPTH = 40
MAX_INTEGER_DIGITS = 12
MAX_FLOAT_MAGNITUDE = 1e6
MAX_EXPONENT_MAGNITUDE = 50
MAX_EXPANDED_TERMS = 4096
EXPAND_POWER_BOUND = 20
# Historical denominator degree cap, superseded by the class-complete
# factory rule in _resolved_zero_set (degree <= 2, or degree 3-4 that
# factors into degree <= 2 irreducible factors). Retained for reference.
MAX_ZERO_DEGREE = 4

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

REFUSAL_DIVISION_BY_ZERO = "division_by_zero"

_KNOWN_REFUSALS = frozenset({
    REFUSAL_UNSUPPORTED_SYNTAX, REFUSAL_UNSUPPORTED_FUNCTION,
    REFUSAL_UNKNOWN_SYMBOL, REFUSAL_TOO_LONG, REFUSAL_TOO_COMPLEX,
    REFUSAL_NUMBER_TOO_LARGE, REFUSAL_EXPONENT_TOO_LARGE,
    REFUSAL_NOT_A_STRING, REFUSAL_UNEXPECTED_EQUALS,
    REFUSAL_DIVISION_BY_ZERO,
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
    """Validated-AST Sympy builder. The input never executes anywhere.

    While building the expression the parser independently records the
    ORIGINAL domain exclusions BEFORE Sympy's automatic simplification
    can erase them: every division (``/``) and every negative power
    (``**-n``) contributes the exact zero set of its denominator.
    Later simplification may collapse the resulting symbol, but the
    recorded excluded values still describe the original expression's
    undefined points.
    """

    def __init__(self, whitelist):
        self._whitelist = frozenset(whitelist)
        self._depth = 0
        self.referenced = set()        # variable NAMES seen in the raw input
        self.denominators = []         # provenance of every original divisor
        self.excluded = set()          # canonical zero texts of denominators
        self.domain_complete = True    # False when analysis was not exact

    def _note_denominator_zeros(self, denominator):
        """Record one ORIGINAL denominator with variable-specific provenance.

        Each entry keeps the denominator text, the variables it depends
        on, its zero set (only when PROVABLY class-complete; see
        :func:`_resolved_zero_set`) and the real/non-real partition of
        those zeros. Radical, multivariate, over-cap and irreducible
        degree-3/4 denominators are recorded but marked unresolvable:
        the parser never guesses their restrictions, and
        ``domain_analysis_complete`` stays False. This provenance is
        preserved before any Sympy auto-cancellation. The engine's
        declared domain is the complex algebraic domain, so EVERY zero
        (real or non-real) excludes a point; the partition makes the
        real-domain restriction explicit for evidence without changing
        that decision.
        """
        if denominator.is_number:
            # Nonzero constant denominators impose no restriction;
            # exact zero denominators are refused at the operator site.
            return
        generators = tuple(sorted(denominator.free_symbols, key=str))
        record = {
            "denominator": canonical_text(denominator),
            "variables": [generator.name for generator in generators],
            "zeros": [],
            "real_zeros": [],
            "non_real_zeros": [],
            "resolvable": False,
        }
        self.denominators.append(record)
        if len(generators) != 1:
            # Multivariate denominators have no enumerated exact zero
            # set in this stage: unresolvable, never guessed.
            self.domain_complete = False
            return
        roots_list, complete = _resolved_zero_set(denominator, generators[0])
        if not complete:
            self.domain_complete = False
            return
        real_zeros = []
        non_real_zeros = []
        for root in roots_list:
            if root.is_real:
                real_zeros.append(canonical_text(root))
            elif root.is_real is False:
                non_real_zeros.append(canonical_text(root))
            else:
                # Realness undecidable: no guessed restrictions.
                self.domain_complete = False
                return
        record["zeros"] = sorted(canonical_text(root) for root in roots_list)
        record["real_zeros"] = sorted(real_zeros)
        record["non_real_zeros"] = sorted(non_real_zeros)
        record["resolvable"] = True
        self.excluded.update(record["zeros"])

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
        self.referenced.add(identifier)
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
            if right == 0:
                # Division by an exact zero denominator (constant or
                # symbolic-constant such as x - x): the quotient has no
                # value anywhere, so the expression is refused outright.
                raise ExpressionRefused(REFUSAL_DIVISION_BY_ZERO)
            # The ORIGINAL denominator's zeros are excluded values of the
            # original expression, even if Sympy cancels the fraction later.
            self._note_denominator_zeros(right)
            return Mul(left, Pow(right, Integer(-1)))
        if isinstance(node.op, ast.Pow):
            _check_power(right)
            if right.is_number and right < 0:
                if left == 0:
                    raise ExpressionRefused(REFUSAL_DIVISION_BY_ZERO)
                # A negative power is a division by the base: the base's
                # zeros are excluded from the original domain.
                self._note_denominator_zeros(left)
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


def _resolved_zero_set(denominator, generator):
    """Exact zero set of a univariate polynomial denominator.

    Returns ``(roots, complete)``. ``complete`` is True only when the
    zero set is PROVABLY exhaustive over the declared (complex)
    mathematical domain:

    - degree <= 2: the closed-form linear/quadratic solution classes
      enumerate every root (real and complex); the returned
      multiplicity count is cross-checked against the degree.
    - degree 3-4: the polynomial must factor over its exact
      coefficient field into irreducible factors of degree <= 2.
      Irreducible cubic/quartic factors are conservatively LEFT
      UNRESOLVED: Sympy's ``roots()`` output is NOT guaranteed to
      enumerate every radical root for them, so their absence is never
      silently claimed complete.

    Anything else (radical shapes, factoring failures) is unresolved.
    """
    from sympy import Poly, PolynomialError, factor_list, roots as sympy_roots
    try:
        poly = Poly(denominator, generator)
        degree = poly.total_degree()
        if degree == 0:
            return [], True              # nonzero constant: no restriction
        if degree <= 2:
            found = dict(sympy_roots(poly))
            if sum(found.values()) != degree:
                return [], False         # solver sanity net
            return list(found), True
        _content, factors = factor_list(denominator, generator)
        zeros = []
        for factor, _multiplicity in factors:
            factor_poly = Poly(factor, generator)
            factor_degree = factor_poly.total_degree()
            if factor_degree == 0:
                continue                 # exact numeric factor: no zeros
            if factor_degree > 2:
                return [], False         # irreducible degree >= 3
            zeros.extend(sympy_roots(factor_poly))
        return zeros, True
    except Exception:
        # Fail closed: any resolution failure leaves the restriction
        # unresolved rather than guessed.
        return [], False


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

    Returns ``{"schema", "variables", "has_floats", "original_denominators",
    "referenced_variables", "excluded_values", "domain_analysis_complete",
    "lhs", "rhs"}`` where ``rhs`` is None unless an equation was
    requested. ``original_denominators`` carries the original denominator
    subtrees with variable-specific provenance (exact zero-sets only for
    univariate resolvable denominators), recorded BEFORE Sympy's
    automatic simplification; ``domain_analysis_complete`` is False when
    a denominator's restrictions could not be resolved exactly (radical,
    multivariate or over-cap denominators), never guessed. Raises
    :class:`ExpressionRefused` for every refusal; reason codes are
    explicit and the raw input is never echoed back.
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
    parsers = []
    has_floats = False
    free_symbols = set()
    for part in (left_text, right_text):
        if part is None:
            parts.append(None)
            continue
        parser = _BoundedParser(whitelist)
        parsers.append(parser)
        expression = parser.parse(part)
        free = expression.free_symbols
        free_symbols.update(free)
        from sympy import Float as SympyFloat
        if expression.atoms(SympyFloat):
            has_floats = True
        parts.append(expression)
    referenced_variables = set()
    excluded_values = set()
    domain_analysis_complete = True
    denominators = []
    for parser in parsers:
        referenced_variables.update(parser.referenced)
        excluded_values.update(parser.excluded)
        denominators.extend(parser.denominators)
        domain_analysis_complete = domain_analysis_complete \
            and parser.domain_complete
    return {
        "schema": PARSER_SCHEMA,
        "variables": sorted(name.name for name in free_symbols),
        "has_floats": has_floats,
        # Variable names present in the ORIGINAL text, even when Sympy
        # cancels them away (x/x -> 1); distinguishes genuinely constant
        # equations from cancelled-away ones.
        "referenced_variables": sorted(referenced_variables),
        # ORIGINAL-domain restrictions with variable-specific provenance:
        # every original denominator, its own variables and (only when
        # exactly resolvable) its zero set. Recorded before any Sympy
        # auto-cancellation; restrictions are never pooled across
        # variables.
        "original_denominators": denominators,
        # Legacy pooled union of resolvable zeros (evidence/display only;
        # verification decisions use original_denominators provenance).
        "excluded_values": sorted(excluded_values),
        "domain_analysis_complete": domain_analysis_complete,
        "lhs": parts[0],
        "rhs": parts[1],
    }


def canonical_text(sympy_expression):
    """Deterministic bounded rendering of a Sympy expression."""
    from sympy import srepr
    return srepr(sympy_expression)
