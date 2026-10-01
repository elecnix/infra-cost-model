"""The usageMetric whitelist must cover every parameter the handlers read.

`additionalProperties: false` on the `usageMetric` definition rejects any key
the definition does not name. The built-in shape handlers read more parameters
than the whitelist declared at first, so a model using one of them failed
validation for a parameter the engine would have honoured.

These tests take the parameter names from the handlers themselves rather than
from a list repeated here, so a handler that starts reading a new parameter
fails this test until the schema declares it.
"""

import re
from pathlib import Path

import pytest

from infra_cost_model.schema import validate_cost_model
from infra_cost_model.saas import pricing_shapes


REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "infra_cost_model" / "schema" / "cost-model.schema.json"
HANDLERS_PATH = Path(pricing_shapes.__file__)


def declared_metric_keys() -> set[str]:
    """Parameter names the schema allows on a usage metric."""
    import json

    schema = json.loads(SCHEMA_PATH.read_text())
    return set(schema["definitions"]["usageMetric"]["properties"])


def handler_param_names() -> set[str]:
    """Parameter names the shape handlers read out of the model.

    Read from the source rather than a list kept beside it: a handler that
    starts reading a new key is then caught here instead of at a user's model.
    """
    text = HANDLERS_PATH.read_text()
    return set(re.findall(r'params\.get\(\s*"([a-z_]+)"', text))


def model_with_metric(metric: dict) -> dict:
    return {
        "version": "1.0",
        "workflow": {
            "name": "m",
            "entry": "n",
            "frequency": {"unit": "perMinute", "value": 1},
        },
        "nodes": {
            "n": {
                "nodeType": "external",
                "resourceAddress": "n",
                "service": "Vendor",
                "region": "global",
                "usageMetrics": {"M": metric},
            }
        },
        "edges": [],
    }


class TestWhitelistCoversHandlers:
    """Every parameter a handler reads is one the schema allows."""

    def test_every_handler_param_is_declared(self):
        missing = handler_param_names() - declared_metric_keys()
        assert not missing, (
            f"the shape handlers read {sorted(missing)}, which "
            f"`additionalProperties: false` rejects. Add them to the "
            f"usageMetric definition in cost-model.schema.json."
        )

    def test_the_handlers_read_something(self):
        """Guard against the regex silently matching nothing."""
        assert handler_param_names() >= {"percentage_rate", "per_call", "volume", "fixed_per_transaction"}


class TestKnownParamsValidate:
    """Each parameter a handler reads passes validation in a real model."""

    @pytest.mark.parametrize("param", ["volume", "fixed_per_transaction"])
    def test_previously_rejected_numeric_params_validate(self, param):
        """These two were read by handlers and refused by the schema."""
        metric = {"unit": "u", "value": 1, "shape": "transactional", param: 1}
        assert validate_cost_model(model_with_metric(metric)) == []

    def test_unknown_key_still_rejected(self):
        """The restriction the whitelist exists for still holds."""
        metric = {"unit": "u", "value": 1, "shape": "transactional", "per_cal": 0.01}
        errors = validate_cost_model(model_with_metric(metric))
        assert any("per_cal" in e for e in errors)


def test_shape_description_examples_are_registered_shapes():
    """The schema's ``shape`` description gives only shapes the engine knows.

    An author who copies an example name from the description, or from the
    TypeScript types generated from it, must get a shape that prices. An
    unknown shape raises an error at compute time.
    """
    import json

    from infra_cost_model.saas import SaaSPricingRegistry

    schema = json.loads(SCHEMA_PATH.read_text())
    description = schema["definitions"]["usageMetric"]["properties"]["shape"]["description"]
    match = re.search(r"\(e\.g\.,\s*([^)]*)\)", description)
    assert match, f"shape description lists no examples: {description!r}"
    examples = {name.strip() for name in match.group(1).split(",")}
    unknown = examples - SaaSPricingRegistry.known_shapes()
    assert not unknown, (
        f"shape description gives unregistered shapes as examples: {sorted(unknown)}"
    )
