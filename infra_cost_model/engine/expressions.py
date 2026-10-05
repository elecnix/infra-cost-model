"""Evaluate a usage metric's `value` as arithmetic over parameters.

A usage metric's `value` states a quantity: how many requests an invocation
makes, how many active users a tenant bills for. The quantity is often a
product, such as `customers * 40` monthly active users per customer, or a
bare parameter such as `customers` (#448). Before this module, `value`
accepted one number or one parameter name, and a user who needed a product
wrote the result into the model per scenario.

The grammar is arithmetic: numbers, parameter names, `+ - * /` and
parentheses, with arithmetic precedence. Nothing else is a value. In
particular a call, a conditional, an exponent and a bitwise operator are
refused: the expression states a quantity, and it is never Python. The
parser therefore reads the text into an arithmetic tree and walks only the
node types below, rather than handing the text to an interpreter.
"""

import ast
import operator


# The whole grammar: the four arithmetic operators. Any other operator node
# the parser produces falls through to a refusal below.
_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}

_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

# A flat sum of N terms nests N deep in the tree, and the walk below recurses
# with it, so the text's length bounds how deep the walk goes. Real quantities
# are a handful of terms: `customers * 40`, `(seats + extra) / 2`. A 500-
# character floor is far beyond any of those and still leaves the walk well
# clear of the interpreter's recursion limit, so a pathological expression is
# refused by name instead of exhausting the stack mid-walk.
_MAXIMUM_LENGTH = 500


def _evaluate(node: ast.AST, parameters: dict, text: str) -> float:
    """Value of one parsed node, or a refusal that names what is wrong."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, parameters, text)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"'{text}' is not a quantity")
        return float(node.value)

    if isinstance(node, ast.Name):
        if node.id not in parameters:
            available = ", ".join(sorted(parameters)) or "none declared"
            raise ValueError(
                f"Unrecognized parameter reference '{node.id}' in '{text}'. "
                f"Available parameters: {available}"
            )
        try:
            return float(parameters[node.id])
        except (TypeError, ValueError):
            raise ValueError(
                f"Parameter '{node.id}' in '{text}' is not a number: "
                f"{parameters[node.id]!r}"
            ) from None

    if isinstance(node, ast.BinOp):
        operation = _BINARY_OPERATORS.get(type(node.op))
        if operation is None:
            raise ValueError(f"'{text}' uses an operator outside + - * /")
        left = _evaluate(node.left, parameters, text)
        right = _evaluate(node.right, parameters, text)
        try:
            return operation(left, right)
        except ZeroDivisionError:
            raise ValueError(f"'{text}' divides by zero") from None

    if isinstance(node, ast.UnaryOp):
        operation = _UNARY_OPERATORS.get(type(node.op))
        if operation is None:
            raise ValueError(f"'{text}' uses a unary operator outside + and -")
        return operation(_evaluate(node.operand, parameters, text))

    raise ValueError(
        f"'{text}' is not a quantity: a value is a number, a parameter name, "
        f"or arithmetic over them with + - * /"
    )


def evaluate_metric_expression(text: str, parameters: dict) -> float:
    """Resolve a usage metric ``value`` written as text.

    Args:
        text: The metric's ``value``, such as ``customers * 40``.
        parameters: The workflow's parameters, by name.

    Returns:
        The resolved quantity.

    Raises:
        ValueError: The text is not a string, or is not arithmetic over
            declared parameters. The message names the parameter or the
            operator at fault.
    """
    if not isinstance(text, str):
        raise ValueError(
            f"A metric value is a number or a string, not {type(text).__name__}: "
            f"{text!r}"
        )

    if len(text) > _MAXIMUM_LENGTH:
        raise ValueError(
            f"'{text[:60]}…' is too long or too deeply nested to read as a "
            f"quantity: {len(text)} characters"
        )

    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as exc:
        raise ValueError(
            f"Cannot read '{text}' as a quantity: {exc.msg}"
        ) from None

    return _evaluate(tree, parameters, text)