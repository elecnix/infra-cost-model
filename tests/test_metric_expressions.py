"""A usage metric `value` states an expression over parameters, not one number.

Identity providers bill per monthly active user and per organization, and a
SaaS seat bills per customer. Those quantities are `customers * 40` or plain
`customers`, so a model has to multiply a parameter by a constant. Before
expressions, `value` took one parameter name or one number, and a user who
needed the product wrote the result into the model per scenario, or pre-
processed the YAML outside the engine.

The reading these tests pin: `value` is arithmetic over parameter names and
numbers, evaluated where every other parameter reference is resolved, so
what-if and sensitivity over the parameter move the expression without a
wrapper. Precedence is arithmetic precedence, and nothing outside
`+ - * /`, numbers, parameter names and parentheses is a value.

The alternative reading, that a string `value` stays a single parameter name
and an expression needs a wrapper, is the pre-processing step this replaces.
"""

import pytest

from infra_cost_model.engine.engine import CostEngine, SensitivityAnalyzer
from infra_cost_model.engine.expressions import evaluate_metric_expression
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.schema import validate_cost_model
from infra_cost_model.sdk.workflow import NodeUsage, Workflow


@pytest.fixture()
def catalog(tmp_path):
    return PricingCatalog(str(tmp_path / "pricing.db"))


def identity_model(customers=300, mau="customers * 40",
                   organizations="customers") -> dict:
    """The model in the issue: a Kinde tenant sized by a customer count.

    Kinde prices 10,500 monthly active users free, then $0.01 a user, and
    $25 for each SSO connection from the fifth. The metrics are `fixed`, so
    their quantity is a flat monthly total rather than a per-invocation
    multiplier (DP#9).
    """
    return {
        "version": "1.0",
        "workflow": {
            "name": "saas-identity",
            "entry": "identity_provider",
            "frequency": {"unit": "perMonth", "value": 1},
            "parameters": {"customers": customers},
        },
        "nodes": {
            "identity_provider": {
                "nodeType": "external",
                "resourceAddress": "kinde.tenant",
                "provider": "kinde",
                "service": "Kinde",
                "region": "global",
                "usageMetrics": {
                    "MAU": {"unit": "users", "value": mau, "fixed": True},
                    "SSO-Connection": {
                        "unit": "connections", "value": organizations, "fixed": True,
                    },
                },
            }
        },
        "edges": [],
    }


class TestExpressionPricesTheModel:
    """An expression resolves to the quantity the price rows read."""

    def test_a_parameter_times_a_number(self, catalog):
        """300 customers at 40 monthly active users each.

        12,000 active users are 1,500 past the free 10,500, so $15. The 300
        SSO connections are 295 past the five Kinde includes, so $7,375.
        """
        engine = CostEngine(identity_model(), catalog, time_basis="monthly")
        assert engine.total_cost() == pytest.approx(7390.0)

    @pytest.mark.parametrize("value", ["customers", "10000"])
    def test_a_single_name_or_number_still_works(self, catalog, value):
        """The two readings a string `value` had before expressions."""
        engine = CostEngine(
            identity_model(mau=value, customers=300), catalog,
            time_basis="monthly",
        )
        # 10,000 active users sit inside the free band, so only the connections.
        assert engine.total_cost() == pytest.approx(7375.0)


class TestWhatIfMovesTheExpression:
    """The point of the change: no pre-processor to re-run per scenario."""

    def test_what_if_scales_with_the_parameter(self, catalog):
        analyzer = SensitivityAnalyzer(identity_model(), catalog, time_basis="monthly")
        # 600 customers: 24,000 users ($135) and 595 billed connections
        # ($14,875).
        assert analyzer.what_if("customers", 600) == pytest.approx(15010.0)

    def test_sensitivity_is_monotonic_in_the_parameter(self, catalog):
        analyzer = SensitivityAnalyzer(identity_model(), catalog, time_basis="monthly")
        costs = [cost for _, cost in analyzer.sensitivity("customers", steps=4)]
        assert costs == sorted(costs)


class TestArithmetic:
    """Precedence and grouping, pinned so the grammar cannot drift."""

    @pytest.mark.parametrize("text,parameters,expected", [
        ("2 + 3 * 4", {}, 14.0),               # * binds tighter than +
        ("(2 + 3) * 4", {}, 20.0),             # parentheses group
        ("10 - 4 - 3", {}, 3.0),               # - is left-associative
        ("100 / 4 / 5", {}, 5.0),
        ("1.5 * 4", {}, 6.0),
        ("-3 + 10", {}, 7.0),                  # unary minus
        ("customers * 40", {"customers": 25}, 1000.0),
        ("seats + extra", {"seats": 25, "extra": 5}, 30.0),
        ("1e3", {}, 1000.0),
    ])
    def test_value(self, text, parameters, expected):
        assert evaluate_metric_expression(text, parameters) == pytest.approx(expected)


class TestExpressionsAreNotCode:
    """`value` is arithmetic. It is not Python, and it never was."""

    @pytest.mark.parametrize("text", [
        "customers ** 2",                       # power is outside the grammar
        "__import__('os').getcwd()",            # calls are outside the grammar
        "customers if customers else 0",        # conditionals too
        "customers & 1",                        # and bitwise operators too
    ])
    def test_refused(self, text):
        with pytest.raises(ValueError):
            evaluate_metric_expression(text, {"customers": 25})


class TestExpressionErrors:
    """Every refusal says which name or which expression is at fault."""

    def test_an_unknown_parameter_names_itself_and_lists_the_known_ones(self):
        with pytest.raises(ValueError) as caught:
            evaluate_metric_expression("tenants * 40", {"customers": 25})
        assert str(caught.value) == (
            "Unrecognized parameter reference 'tenants' in 'tenants * 40'. "
            "Available parameters: customers"
        )

    def test_an_unknown_parameter_in_a_parameterless_model(self):
        with pytest.raises(ValueError, match="Available parameters: none declared"):
            evaluate_metric_expression("tenants * 40", {})

    def test_a_parameter_that_is_not_a_number_names_itself(self):
        """The schema states a parameter is a number; a model can still say otherwise."""
        for value in ("many", True, None, [1]):
            with pytest.raises(ValueError, match="customers"):
                evaluate_metric_expression("customers * 40", {"customers": value})

    def test_a_long_operator_chain_is_refused_rather_than_exhausting_the_stack(self):
        """A 5,000-term sum parses, so the walk itself has to stay bounded."""
        with pytest.raises(ValueError, match="nested"):
            evaluate_metric_expression(" + ".join(["1"] * 5000), {})

    def test_division_by_zero_is_refused(self):
        with pytest.raises(ValueError, match="zero"):
            evaluate_metric_expression("customers / seats", {"customers": 10, "seats": 0})

    def test_an_unknown_name_keeps_the_single_name_message(self):
        """A bare name still reads as a parameter reference, not as arithmetic."""
        with pytest.raises(ValueError, match="Unrecognized parameter reference"):
            evaluate_metric_expression("tenants", {"customers": 25})

    def test_a_non_string_is_refused_rather_than_crashing_the_caller(self):
        """The caller catches ValueError; the contract says it gets one."""
        for value in (None, ["customers"], {"a": 1}, 10 ** 400):
            with pytest.raises(ValueError, match="number or a string"):
                evaluate_metric_expression(value, {"customers": 25})

    @pytest.mark.parametrize("text,parameters", [
        ("1e999", {}),                     # a literal that overflows to inf
        ("-1e999", {}),
        ("1e999 - 1e999", {}),             # inf - inf is nan
        ("huge", {"huge": float("inf")}),  # a parameter that is inf
    ])
    def test_a_quantity_that_is_not_finite_is_refused(self, text, parameters):
        """An infinite or NaN quantity is not a bill."""
        with pytest.raises(ValueError, match="finite"):
            evaluate_metric_expression(text, parameters)


class TestSchemaAcceptsAnExpression:
    """One schema, three interfaces (Principle 11)."""

    def test_an_expression_validates(self):
        model = identity_model()
        assert validate_cost_model(model) == []

    @pytest.mark.parametrize("value", [
        "customers * 40", "(customers + 1) * 40", "customers", "1000", "1.5 * seats",
    ])
    def test_the_grammar_validates(self, value):
        model = identity_model(mau=value)
        assert validate_cost_model(model) == []

    @pytest.mark.parametrize("value", [
        "customers % 2", "customers & 1", "customers | 1", "customers ^ 1",
        "~customers", "!customers", "customers; import os",
        "__import__('os')", "customers, 2", "customers: 2", "",
    ])
    def test_a_character_outside_the_grammar_is_refused(self, value):
        """The pattern is a character filter, and it does filter these."""
        model = identity_model(mau=value)
        assert validate_cost_model(model) != []

    @pytest.mark.parametrize("value", ["customers ** 2", "f(customers)"])
    def test_the_engine_refuses_what_the_class_cannot(self, catalog, value):
        """Two admitted characters can still spell an exponent or a call.

        A character class cannot forbid either, so the model validates and the
        engine refuses it. Splitting the two layers is the same call `per`
        makes: the schema states what it can state, and the engine refuses the
        rest.
        """
        model = identity_model(mau=value)
        assert validate_cost_model(model) == []
        with pytest.raises(ValueError, match="not a quantity|operator"):
            CostEngine(model, catalog, time_basis="monthly").total_cost()

    def test_a_number_still_validates(self):
        model = identity_model(mau=12000)
        assert validate_cost_model(model) == []


class TestSdkAcceptsAnExpression:
    """The Python interface shares the schema, so it accepts the same value."""

    def test_with_metric_takes_an_expression(self):
        usage = NodeUsage().with_metric("MAU", "customers * 40", unit="users")
        workflow = Workflow("saas-identity").parameter("customers", 25)
        workflow.usage("kinde.tenant", usage)
        model = workflow.assemble()
        assert model["kinde.tenant"]["usageMetrics"]["MAU"]["value"] == "customers * 40"

    def test_a_metric_with_no_unit_keeps_the_expression(self):
        """Without a unit or edge type, `with_metric` stores the value bare.

        That is the form the widened parameter has to accept as well, not
        only the wrapped one.
        """
        usage = NodeUsage().with_metric("SSO-Connection", "customers * 40")
        workflow = Workflow("saas-identity").parameter("customers", 25)
        workflow.usage("kinde.tenant", usage)
        assert workflow.assemble()["kinde.tenant"]["usageMetrics"] == {
            "SSO-Connection": "customers * 40"
        }