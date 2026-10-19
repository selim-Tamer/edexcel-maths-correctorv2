"""Offline regression tests for the Stage C4 strict bounded parser.

No network, no AI calls, no student data. Everything here runs against the
local ``investigation.safe_math`` module only.
"""
import unittest

from investigation import safe_math
from investigation.safe_math import (
    DEFAULT_VARIABLES, ExpressionRefused, canonical_text,
    expand_is_bounded, parse_bounded_expression, validate_variables,
)


class GrammarWhitelistTests(unittest.TestCase):

    def test_simple_expression_parses_to_sympy(self):
        parsed = parse_bounded_expression("(x + 2)**2")
        self.assertEqual(parsed["schema"], safe_math.PARSER_SCHEMA)
        self.assertEqual(canonical_text(parsed["lhs"]).count("Symbol('x')"), 1)
        self.assertEqual(parsed["variables"], ["x"])
        self.assertFalse(parsed["has_floats"])
        self.assertIsNone(parsed["rhs"])

    def test_all_default_variables_are_accepted(self):
        for name in DEFAULT_VARIABLES:
            parsed = parse_bounded_expression(name)
            self.assertEqual(str(parsed["lhs"]), name)

    def test_unknown_symbol_is_refused_with_reason(self):
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression("w + 1")
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_UNKNOWN_SYMBOL)
        self.assertIn("w", caught.exception.detail)

    def test_function_whitelist_sqrt_abs(self):
        self.assertEqual(str(parse_bounded_expression("sqrt(16)")["lhs"]), "4")
        self.assertEqual(str(parse_bounded_expression("abs(-3)")["lhs"]), "3")
        self.assertEqual(
            str(parse_bounded_expression("sqrt(sqrt(16))")["lhs"]), "2")

    def test_unknown_function_is_refused(self):
        for text in ("f(x)", "Q(x)", "sin(x)", "log(x)", "exp(x)",
                     '__import__("os")'):
            with self.assertRaises(ExpressionRefused) as caught:
                parse_bounded_expression(text)
            self.assertEqual(caught.exception.reason,
                             safe_math.REFUSAL_UNSUPPORTED_FUNCTION)

    def test_arbitrary_python_syntax_is_refused(self):
        cases = {
            "2x": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
            "x > 2": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
            "[1, 2]": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
            "lambda x: x": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
            "x.real": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
            "x if y else z": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
            "False": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
            "{1: 2}": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
            "import os": safe_math.REFUSAL_UNSUPPORTED_SYNTAX,
        }
        for text, expected in cases.items():
            with self.assertRaises(ExpressionRefused) as caught:
                parse_bounded_expression(text)
            self.assertEqual(caught.exception.reason, expected, text)

    def test_attribute_and_call_chains_cannot_execute(self):
        for text in ("x.__class__", "x.real", "sqrt(x).__doc__"):
            with self.assertRaises(ExpressionRefused):
                parse_bounded_expression(text)

    def test_keyword_and_starred_call_arguments_are_refused(self):
        # A keyword argument contains '=' so the equation guard fires first;
        # either way the input is refused and nothing is executed.
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression("sqrt(x=1)")
        self.assertIn(caught.exception.reason,
                      (safe_math.REFUSAL_UNEXPECTED_EQUALS,
                       safe_math.REFUSAL_UNSUPPORTED_SYNTAX))
        with self.assertRaises(ExpressionRefused) as starred:
            parse_bounded_expression("sqrt(*[x])")
        self.assertEqual(starred.exception.reason,
                         safe_math.REFUSAL_UNSUPPORTED_SYNTAX)

    def test_equation_splitting_requires_the_flag(self):
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression("x**2 = 0")
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_UNEXPECTED_EQUALS)
        parsed = parse_bounded_expression("x**2 = 0", allow_equation=True)
        self.assertEqual(str(parsed["lhs"]), "x**2")
        self.assertEqual(str(parsed["rhs"]), "0")

    def test_double_equals_and_empty_sides_are_refused(self):
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression("x = 3 = 4", allow_equation=True)
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_UNEXPECTED_EQUALS)
        with self.assertRaises(ExpressionRefused):
            parse_bounded_expression("  =  4", allow_equation=True)

    def test_negative_and_fractional_numbers(self):
        parsed = parse_bounded_expression("-3 + 1/4")
        self.assertEqual(str(parsed["lhs"]), "-11/4")


class ExactnessAndFloatTests(unittest.TestCase):

    def test_floats_are_kept_as_approximate_data(self):
        parsed = parse_bounded_expression("1.4142135")
        self.assertTrue(parsed["has_floats"])
        self.assertEqual(str(parsed["lhs"]).rstrip("0"), "1.4142135")
        self.assertEqual(float(parsed["lhs"]), 1.4142135)

    def test_integers_stay_exact(self):
        parsed = parse_bounded_expression("123456789012")
        self.assertEqual(str(parsed["lhs"]), "123456789012")
        self.assertFalse(parsed["has_floats"])

    def test_oversized_integer_is_refused(self):
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression("12345678901234")
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_NUMBER_TOO_LARGE)

    def test_nan_and_infinity_are_refused(self):
        for text in ("float('nan')",):
            with self.assertRaises(ExpressionRefused):
                parse_bounded_expression(text)


class ResourceBoundTests(unittest.TestCase):

    def test_expression_length_cap(self):
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression("1 + 1" * 300)
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_TOO_LONG)

    def test_node_count_cap(self):
        text = " + ".join("(1 + 1)" for _ in range(16))
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression(text)
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_TOO_COMPLEX)

    def test_exponent_cap(self):
        for text in ("x**99999", "10**100000000", "x**-999"):
            with self.assertRaises(ExpressionRefused) as caught:
                parse_bounded_expression(text)
            self.assertEqual(caught.exception.reason,
                             safe_math.REFUSAL_EXPONENT_TOO_LARGE, text)

    def test_non_string_input_refused(self):
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression(42)
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_NOT_A_STRING)
        with self.assertRaises(ExpressionRefused):
            parse_bounded_expression(None)

    def test_empty_and_whitespace_refused(self):
        for text in ("", "   ", "\n"):
            with self.assertRaises(ExpressionRefused):
                parse_bounded_expression(text)

    def test_expand_bound_accepts_polynomials_and_fractions(self):
        simple = parse_bounded_expression("(x + 1)*(x + 2)")["lhs"]
        self.assertTrue(expand_is_bounded(simple))
        fraction = parse_bounded_expression("(3*x - 3)/9")["lhs"]
        self.assertTrue(expand_is_bounded(fraction))
        radical = parse_bounded_expression("sqrt(50)")["lhs"]
        self.assertTrue(expand_is_bounded(radical))

    def test_expand_bound_refuses_exponential_product_chains(self):
        # Five trilinear factors stay under the parse node cap but their
        # expanded product (10^5 terms potential) trips the combinatorial
        # expand bound; nothing exponential is ever handed to Sympy.
        chain = "*".join(f"(x + y + {i})**3" for i in range(1, 5))
        self.assertFalse(expand_is_bounded(parse_bounded_expression(
            chain)["lhs"]))

    def test_wider_chains_are_refused_at_parse_time(self):
        # Defence in depth: a yet larger chain is refused by the AST node
        # cap before the expand bound is even consulted.
        chain = "*".join(f"(x + y + {i})**3" for i in range(1, 9))
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression(chain)
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_TOO_COMPLEX)

    def test_deeply_nested_unary_chains_refused(self):
        text = "-" * 45 + "1"
        with self.assertRaises(ExpressionRefused) as caught:
            parse_bounded_expression(text)
        self.assertEqual(caught.exception.reason,
                         safe_math.REFUSAL_TOO_COMPLEX)


class VariableWhitelistTests(unittest.TestCase):

    def test_custom_whitelist_is_honoured(self):
        parsed = parse_bounded_expression("t**2", variables=("t",))
        self.assertEqual(parsed["variables"], ["t"])
        with self.assertRaises(ExpressionRefused):
            parse_bounded_expression("t**2", variables=("u",))

    def test_whitelist_validation(self):
        with self.assertRaises(ValueError):
            validate_variables([])
        with self.assertRaises(ValueError):
            validate_variables(("x", "x"))
        with self.assertRaises(ValueError):
            validate_variables(("1x",))
        with self.assertRaises(ValueError):
            validate_variables(("toolong",))
        with self.assertRaises(ValueError):
            validate_variables((("x",),))
        self.assertEqual(validate_variables(("t", "v")), ("t", "v"))


class DeterminismTests(unittest.TestCase):

    def test_repeated_parsing_is_deterministic(self):
        texts = ("(x + 2)**2", "sqrt(50)", "x/y - 3/4", "1.5*x")
        for text in texts:
            first = parse_bounded_expression(text)
            second = parse_bounded_expression(text)
            self.assertEqual(canonical_text(first["lhs"]),
                             canonical_text(second["lhs"]))
            self.assertEqual(first["variables"], second["variables"])
            self.assertEqual(first["has_floats"], second["has_floats"])

    def test_canonical_text_uses_srepr(self):
        parsed = parse_bounded_expression("x + 2")
        self.assertEqual(canonical_text(parsed["lhs"]),
                         "Add(Symbol('x'), Integer(2))")

    def test_module_exposes_no_eval_helpers(self):
        public = [name for name in dir(safe_math)
                  if not name.startswith("_")]
        self.assertNotIn("eval", public)
        self.assertNotIn("exec", public)
        self.assertNotIn("sympify", public)
        self.assertNotIn("parse_expr", public)


if __name__ == "__main__":
    unittest.main()
