"""Test that the provider field accepts any lowercase vendor id instead of a closed enum,
and rejects unrecognized ids with a clear load-time error message."""

from infra_cost_model.schema.cost_model_schema import KNOWN_PROVIDERS, validate_cost_model


def _model(provider):
    return {
        "version": "1.0",
        "workflow": {"name": "t", "entry": "n1", "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": {"n1": {"nodeType": "external", "resourceAddress": "github.copilot.api",
                          "provider": provider, "service": "Copilot", "region": "global"}},
    }


def test_provider_registry_skips_template_and_includes_aliases():
    assert "example-vendor" not in KNOWN_PROVIDERS
    assert "github-copilot" in KNOWN_PROVIDERS
    assert "github" in KNOWN_PROVIDERS


def test_provider_accepts_known_github():
    """A registered vendor id like 'aws' passes validation."""
    errors = validate_cost_model(_model("aws"))
    assert errors == [], f"aws provider rejected: {errors}"


def test_provider_rejects_uppercase_pattern():
    """An invalid provider pattern fails schema validation."""
    errors = validate_cost_model(_model("Unknown!"))
    assert len(errors) > 0, "Invalid provider pattern accepted"


def test_unknown_shape_emits_helpful_error():
    model = _model("aws")
    model["nodes"]["n1"]["usageMetrics"] = {
        "requests": {"unit": "requests", "shape": "not_a_shape"}
    }

    errors = validate_cost_model(model)

    assert any(
        "Unknown shape 'not_a_shape' on node 'n1'. Known shapes:" in error
        for error in errors
    )


def test_unknown_lowercase_id_emits_helpful_error():
    """A well-formed but unregistered id is rejected with a clear message listing known providers."""
    errors = validate_cost_model(_model("my-fake-vendor"))
    joined = "\n".join(errors)
    assert any('Unknown provider "my-fake-vendor"' in e for e in errors), \
        f"Expected helpful unknown-provider error, got: {errors}"
    assert "Known providers:" in joined and "aws" in joined, \
        "Error should enumerate known providers including aws"