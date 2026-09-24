"""Tests for the model's engine requirement (`requiresEngine`).

A cost model may declare the engine version it needs. Without that pin an old
engine prices the model anyway: a metric using a SaaS `shape:` (issue #241)
prices at $0 on an engine with no shape dispatch, and the run exits 0. The
total reads as plausible, so nothing signals the loss.

These tests cover the three places the pin has to hold:
- the parser keeps the key instead of dropping it,
- the engine refuses to price a model it cannot interpret,
- the CLI surfaces the refusal on every command that prices.
"""

from pathlib import Path

import pytest

from infra_cost_model import __version__ as running_engine_version
from infra_cost_model.cli import main
from infra_cost_model.engine import CostEngine
from infra_cost_model.schema import validate_cost_model
from infra_cost_model.sdk import parse_yaml_dsl
from infra_cost_model.version_requirement import (
    ENGINE_REQUIREMENT_KEY,
    EngineRequirementError,
    check_engine_requirement,
    declared_requirement,
    engine_version,
)


REPO_ROOT = Path(__file__).resolve().parent.parent


def make_model(requirement=None, **node_overrides):
    """Build a minimal valid model, optionally declaring an engine pin."""
    node = {
        "nodeType": "compute",
        "resourceAddress": "n",
        "provider": "aws",
        "service": "AWSLambda",
        "region": "us-east-1",
        "usageMetrics": {"invocations": {"unit": "requests", "value": 1}},
        "pricingRates": {"invocations": 0.20e-6},
    }
    node.update(node_overrides)
    model = {
        "version": "1.0",
        "workflow": {
            "name": "requirement-test",
            "entry": "n",
            "frequency": {"unit": "perMinute", "value": 60},
        },
        "nodes": {"n": node},
        "edges": [],
    }
    if requirement is not None:
        model[ENGINE_REQUIREMENT_KEY] = requirement
    return model


def write_model(path, requirement=None, **node_overrides):
    """Write a model to disk as YAML and return the path."""
    import yaml

    path.write_text(yaml.safe_dump(make_model(requirement, **node_overrides)))
    return path


class TestDeclaredRequirement:
    """Reading the pin off the model."""

    def test_absent_key_reads_as_none(self):
        """A model without the key asks for nothing."""
        assert declared_requirement(make_model()) is None

    def test_present_key_reads_back(self):
        """The declared specifier comes back verbatim."""
        model = make_model(">=0.2.0")
        assert declared_requirement(model) == ">=0.2.0"

    def test_key_name_is_camel_case(self):
        """The schema-visible key matches the documented spelling."""
        assert ENGINE_REQUIREMENT_KEY == "requiresEngine"


class TestEngineVersion:
    """The engine reports the version it enforces."""

    def test_reports_the_package_version(self):
        """`engine_version` is the package's own `__version__`."""
        assert engine_version() == running_engine_version


class TestCheckEngineRequirement:
    """The compatibility decision, checked against an injected version."""

    def test_no_requirement_passes(self):
        """No pin means no constraint, whatever the engine version."""
        assert check_engine_requirement(make_model(), "0.0.1") is None

    def test_equal_version_satisfies_a_minimum(self):
        """`>=0.2.0` holds on exactly 0.2.0."""
        assert check_engine_requirement(make_model(">=0.2.0"), "0.2.0") is None

    def test_newer_version_satisfies_a_minimum(self):
        """`>=0.2.0` holds on 0.3.1."""
        assert check_engine_requirement(make_model(">=0.2.0"), "0.3.1") is None

    def test_older_version_fails_a_minimum(self):
        """`>=0.2.0` fails on 0.1.0."""
        error = check_engine_requirement(make_model(">=0.2.0"), "0.1.0")
        assert error is not None
        assert ">=0.2.0" in error
        assert "0.1.0" in error

    def test_error_names_the_remedy(self):
        """The refusal tells the reader how to get a usable engine."""
        error = check_engine_requirement(make_model(">=0.2.0"), "0.1.0")
        assert "pip install" in error

    def test_remedy_installs_from_the_repository(self):
        """The upgrade command points at the git repository (#265).

        The package isn't on PyPI, so `pip install -U infra-cost-model`
        can't find it.
        """
        error = check_engine_requirement(make_model(">=0.2.0"), "0.1.0")
        assert "git+https://github.com/elecnix/infra-cost-model" in error

    def test_upper_bound_is_enforced(self):
        """An excluded range fails on a version above it."""
        model = make_model(">=0.2.0,<0.3")
        assert check_engine_requirement(model, "0.3.0") is not None
        assert check_engine_requirement(model, "0.2.5") is None

    def test_equality_pin_is_enforced(self):
        """`==0.2.0` fails on any other version."""
        model = make_model("==0.2.0")
        assert check_engine_requirement(model, "0.2.0") is None
        assert check_engine_requirement(model, "0.3.0") is not None

    def test_loose_specifier_is_accepted(self):
        """`>=0.2` is a valid PEP 440 specifier and passes on 0.3."""
        assert check_engine_requirement(make_model(">=0.2"), "0.3") is None

    def test_invalid_specifier_is_reported_not_raised(self):
        """A typo in the pin yields a message, not a traceback."""
        error = check_engine_requirement(make_model("banana"), "0.1.0")
        assert error is not None
        assert "banana" in error

    def test_non_string_requirement_is_reported(self):
        """A numeric pin is a model error, reported plainly."""
        error = check_engine_requirement(make_model(0.2), "0.1.0")
        assert error is not None
        assert "string" in error

    def test_prerelease_of_a_newer_version_satisfies_a_minimum(self):
        """0.3.0.dev0 is a 0.3 engine, so `>=0.2.0` holds."""
        assert check_engine_requirement(make_model(">=0.2.0"), "0.3.0.dev0") is None

    def test_unparseable_running_version_is_reported(self):
        """A broken build version yields a message, not a traceback."""
        error = check_engine_requirement(make_model(">=0.2.0"), "not-a-version")
        assert error is not None


class TestParserKeepsThePin:
    """`parse_yaml_dsl` rebuilds the model, so it must carry the pin across."""

    def test_requires_engine_survives_the_round_trip(self):
        """A dropped key would disable the check without a word."""
        import yaml

        model = make_model(">=0.2.0")
        parsed = parse_yaml_dsl(yaml.safe_dump(model))
        assert parsed.get(ENGINE_REQUIREMENT_KEY) == ">=0.2.0"

    def test_model_without_the_pin_gains_no_key(self):
        """The parser does not invent a requirement."""
        import yaml

        parsed = parse_yaml_dsl(yaml.safe_dump(make_model()))
        assert ENGINE_REQUIREMENT_KEY not in parsed

    def test_parsed_model_still_validates(self):
        """Carrying the pin across does not break schema validation."""
        import yaml

        parsed = parse_yaml_dsl(yaml.safe_dump(make_model(">=0.2.0")))
        assert validate_cost_model(parsed) == []


class TestSchemaAcceptsThePin:
    """The schema is the single source of truth for the field's shape."""

    def test_valid_specifier_passes(self):
        """A PEP 440 specifier string validates."""
        assert validate_cost_model(make_model(">=0.2.0")) == []

    def test_absent_pin_passes(self):
        """The field is optional."""
        assert validate_cost_model(make_model()) == []

    def test_non_string_pin_is_rejected(self):
        """The schema requires a string, so a bare number fails early."""
        errors = validate_cost_model(make_model(0.2))
        assert any(ENGINE_REQUIREMENT_KEY in e for e in errors)

    def test_empty_string_is_rejected(self):
        """An empty pin states nothing, so it fails rather than passing."""
        errors = validate_cost_model(make_model(""))
        assert any(ENGINE_REQUIREMENT_KEY in e for e in errors)


class TestEngineRefuses:
    """The engine is the chokepoint: no pricing happens on a mismatch."""

    def test_satisfied_pin_prices_normally(self):
        """A pin the engine meets changes nothing."""
        engine = CostEngine(make_model(">=0.1.0"), catalog=None)
        assert sum(engine.compute().values()) > 0

    def test_absent_pin_prices_normally(self):
        """Models written before the pin still price."""
        engine = CostEngine(make_model(), catalog=None)
        assert sum(engine.compute().values()) > 0

    def test_unsatisfied_pin_refuses_to_price(self):
        """A model needing a newer engine raises instead of reporting $0."""
        engine = CostEngine(make_model(">=99.0.0"), catalog=None)
        with pytest.raises(EngineRequirementError) as excinfo:
            engine.compute()
        assert "99.0.0" in str(excinfo.value)

    def test_refusal_is_a_value_error(self):
        """Existing callers catch ValueError, so the refusal subclasses it."""
        assert issubclass(EngineRequirementError, ValueError)

    def test_total_cost_refuses_too(self):
        """`total_cost` routes through `compute`, so it refuses as well."""
        engine = CostEngine(make_model(">=99.0.0"), catalog=None)
        with pytest.raises(EngineRequirementError):
            engine.total_cost()

    def test_refusal_leaves_no_partial_costs(self):
        """A refused run reports nothing rather than a misleading zero."""
        engine = CostEngine(make_model(">=99.0.0"), catalog=None)
        with pytest.raises(EngineRequirementError):
            engine.compute()
        assert engine.costs == {}


class TestCliSurfacesTheRefusal:
    """Every command that prices refuses, and `validate` reports the pin."""

    def test_compute_exits_nonzero(self, tmp_path, capsys):
        path = write_model(tmp_path / "m.yaml", ">=99.0.0")
        assert main(["compute", str(path)]) == 1
        assert "99.0.0" in capsys.readouterr().err

    def test_analyze_exits_nonzero(self, tmp_path, capsys):
        path = write_model(tmp_path / "m.yaml", ">=99.0.0")
        assert main(["analyze", str(path)]) == 1
        assert "99.0.0" in capsys.readouterr().err

    def test_what_if_exits_nonzero(self, tmp_path, capsys):
        path = write_model(tmp_path / "m.yaml", ">=99.0.0")
        code = main(["what-if", str(path), "--param", "frequency", "--values", "1,2"])
        assert code == 1
        assert "99.0.0" in capsys.readouterr().err

    def test_sensitivity_exits_nonzero(self, tmp_path, capsys):
        path = write_model(tmp_path / "m.yaml", ">=99.0.0")
        code = main(["sensitivity", str(path), "--parameter", "frequency"])
        assert code == 1
        assert "99.0.0" in capsys.readouterr().err

    def test_compute_names_the_running_version(self, tmp_path, capsys):
        path = write_model(tmp_path / "m.yaml", ">=99.0.0")
        main(["compute", str(path)])
        err = capsys.readouterr().err
        assert running_engine_version in err

    def test_validate_reports_the_mismatch(self, tmp_path, capsys):
        """`validate` is where a reader looks first, so it reports the pin."""
        path = write_model(tmp_path / "m.yaml", ">=99.0.0")
        assert main(["validate", str(path)]) == 1
        assert "99.0.0" in capsys.readouterr().out

    def test_validate_passes_a_met_pin(self, tmp_path, capsys):
        path = write_model(tmp_path / "m.yaml", ">=0.1.0")
        assert main(["validate", str(path)]) == 0
        assert "Valid cost model" in capsys.readouterr().out

    def test_validate_rejects_an_invalid_specifier(self, tmp_path, capsys):
        path = write_model(tmp_path / "m.yaml", "banana")
        assert main(["validate", str(path)]) == 1
        assert "banana" in capsys.readouterr().out

    def test_compute_succeeds_on_a_met_pin(self, tmp_path):
        path = write_model(tmp_path / "m.yaml", ">=0.1.0")
        assert main(["compute", str(path), "--no-catalog"]) == 0


class TestUnpinnedModelStillWorks:
    """The field is opt-in: existing models keep their behaviour."""

    def test_existing_examples_validate(self):
        """Every bundled example still validates."""
        for example in sorted((REPO_ROOT / "examples").glob("*.yaml")):
            assert main(["validate", str(example)]) == 0, example.name


class TestVersionHasOneSource:
    """A load-bearing version cannot be duplicated, or the two will drift."""

    def test_pyproject_declares_the_version_dynamically(self):
        """pyproject reads `__version__` instead of repeating the number."""
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        assert 'dynamic = ["version"]' in pyproject

    def test_installed_metadata_matches_the_module(self):
        """The installed distribution reports the module's version."""
        from importlib.metadata import version as dist_version

        try:
            installed = dist_version("infra-cost-model")
        except Exception:  # pragma: no cover - package not installed
            pytest.skip("infra-cost-model is not installed")
        assert installed == running_engine_version


class TestThePinOnTheBundledExample:
    """`examples/saas-subscription-api.yaml` is the worked case for the pin."""

    EXAMPLE = REPO_ROOT / "examples" / "saas-subscription-api.yaml"

    def load(self):
        import yaml

        return yaml.safe_load(self.EXAMPLE.read_text())

    def test_example_declares_the_requirement(self):
        """The example needs the vendor rows that ship with 0.3.0, and says so."""
        assert declared_requirement(self.load()) == ">=0.3.0"

    def test_example_requirement_holds_today(self):
        """The shipped engine satisfies its own example."""
        assert check_engine_requirement(self.load()) is None

    def test_vendor_nodes_price_above_zero(self, seed_catalog):
        """The example's whole point: the SaaS nodes cost something."""
        engine = CostEngine(self.load(), catalog=seed_catalog, time_basis="monthly")
        costs = engine.compute()
        assert costs["workos_identity"] == pytest.approx(349.0)
        assert costs["datadog_observability"] == pytest.approx(138.0)

    def test_example_validates(self):
        assert main(["validate", str(self.EXAMPLE)]) == 0

    def test_example_computes(self):
        assert main(["compute", str(self.EXAMPLE), "--no-catalog"]) == 0


class TestPinIsCheckedBeforePricing:
    """The refusal must not depend on which branch of compute runs."""

    def test_multi_workflow_model_is_also_gated(self):
        """The `workflows` array path checks the pin too."""
        model = make_model(">=99.0.0")
        model.pop("workflow")
        model["workflows"] = [
            {
                "name": "wf",
                "entry": "n",
                "frequency": {"unit": "perMinute", "value": 60},
            }
        ]
        engine = CostEngine(model, catalog=None)
        with pytest.raises(EngineRequirementError):
            engine.compute()

    def test_pin_is_checked_even_when_the_dag_is_invalid(self):
        """A bad DAG must not mask the version mismatch.

        The pin is about which engine can read the model at all, so it comes
        first. Reporting a DAG error here would send the reader to fix
        topology in a model their engine cannot interpret.
        """
        model = make_model(">=99.0.0")
        model["edges"] = [{"from": "n", "to": "missing", "rate": 1}]
        engine = CostEngine(model, catalog=None)
        with pytest.raises(EngineRequirementError):
            engine.compute()


class TestDependenciesAreDeclared:
    """The pin's dependency must be declared, not inherited from a test extra.

    `packaging` is imported at engine import time. A minimal install that
    lacks it fails to `import infra_cost_model` at all, so relying on it
    arriving with pytest or jsonschema breaks every user who installs the
    package on its own.
    """

    def test_packaging_is_a_declared_dependency(self):
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        dependencies = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
        assert "packaging" in dependencies

    def test_engine_imports_without_test_extras(self):
        """Importing the engine must not need pytest to be installed."""
        import infra_cost_model.version_requirement as module

        assert module.SpecifierSet is not None
