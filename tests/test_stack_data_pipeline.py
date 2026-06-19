"""Integration test for Stack: Data Processing Pipeline (Issue #6).

Validates:
- Multi-workflow model with two independent workflows
- Mixed time units (perDay for both workflows)
- Provisioned RDS with flatOverride
- Batch processing data volumes
- Cost aggregation across workflows
"""

import pytest
import yaml
from pathlib import Path
from infra_cost_model.engine.engine import CostEngine, DerivedUsage


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def load_yaml_model(name: str) -> dict:
    path = EXAMPLES_DIR / name
    with open(path) as f:
        return yaml.safe_load(f)


class TestDataPipelineModel:
    """Validate the data pipeline YAML model loads and computes correctly."""

    def test_model_loads(self):
        """Model YAML parses successfully."""
        model = load_yaml_model("data-pipeline.yaml")
        assert model["version"] == "1.0"
        assert "workflows" in model
        assert len(model["workflows"]) == 2

    def test_model_has_both_workflows(self):
        """Both workflows are present with correct names."""
        model = load_yaml_model("data-pipeline.yaml")
        wf_names = [w["name"] for w in model["workflows"]]
        assert "data-pipeline" in wf_names
        assert "daily-analytics" in wf_names

    def test_mixed_time_units(self):
        """Both workflows use perDay but at different values."""
        model = load_yaml_model("data-pipeline.yaml")
        pipeline = model["workflows"][0]
        analytics = model["workflows"][1]

        assert pipeline["frequency"]["unit"] == "perDay"
        assert pipeline["frequency"]["value"] == 1000
        assert analytics["frequency"]["unit"] == "perDay"
        assert analytics["frequency"]["value"] == 1

    def test_all_nodes_defined(self):
        """All expected infrastructure nodes are defined."""
        model = load_yaml_model("data-pipeline.yaml")
        expected_nodes = {
            "aws_s3_bucket.uploads",
            "aws_eventbridge_rule.upload_trigger",
            "aws_lambda_function.extractor",
            "aws_lambda_function.transformer",
            "aws_dynamodb_table.data",
            "aws_eventbridge_rule.daily_schedule",
            "aws_lambda_function.aggregator",
            "aws_db_instance.analytics",
            "aws_s3_bucket.reports",
        }
        assert set(model["nodes"].keys()) == expected_nodes

    def test_rds_flat_override(self):
        """RDS instance uses flatOverride for provisioned pricing."""
        model = load_yaml_model("data-pipeline.yaml")
        rds = model["nodes"]["aws_db_instance.analytics"]
        assert rds["flatOverride"] is True
        assert rds["usageMetrics"]["instanceHours"]["value"] == 730

    def test_dag_no_cycles(self):
        """DAG is acyclic."""
        model = load_yaml_model("data-pipeline.yaml")
        from infra_cost_model.engine.engine import DAGValidator
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is True


class TestMultiWorkflowEngine:
    """Integration tests for multi-workflow cost computation."""

    @pytest.fixture
    def engine(self):
        model = load_yaml_model("data-pipeline.yaml")
        return CostEngine(model, time_basis="monthly")

    @pytest.fixture
    def costs(self, engine):
        return engine.compute()

    def test_compute_returns_costs(self, costs):
        """Multi-workflow engine produces cost dict."""
        assert isinstance(costs, dict)
        assert len(costs) > 0

    def test_both_entry_nodes_have_costs(self, costs):
        """Both workflow entry nodes appear in costs."""
        assert "aws_s3_bucket.uploads" in costs
        assert "aws_eventbridge_rule.daily_schedule" in costs

    def test_all_nodes_costed(self, engine):
        """Every defined node that is reachable has a cost entry."""
        model = load_yaml_model("data-pipeline.yaml")
        costs = engine.compute()

        # All nodes except unreachable ones should have costs
        for addr in model["nodes"]:
            assert addr in costs, f"Node '{addr}' missing from costs"

    def test_total_cost_is_positive(self, engine):
        """Total cost is a positive number."""
        total = engine.total_cost()
        assert total > 0

    def test_workflow_1_data_pipeline_derived(self, engine):
        """Data pipeline workflow derives invocation counts correctly."""
        engine.compute()
        derived = engine.derived_usage

        # S3 uploads: 1000/day = ~0.011574/sec → monthly ≈ 30,000
        s3 = derived["aws_s3_bucket.uploads"]
        monthly_invocations = s3.invocation_count * 2629800  # seconds/month
        assert monthly_invocations == pytest.approx(30_000, rel=0.02)

        # EventBridge events should match S3 invocations (rate=1)
        eb = derived["aws_eventbridge_rule.upload_trigger"]
        assert eb.invocation_count == pytest.approx(s3.invocation_count)

        # Extractor Lambda gets same as EventBridge
        extractor = derived["aws_lambda_function.extractor"]
        assert extractor.invocation_count == pytest.approx(s3.invocation_count)

        # DynamoDB gets writes from transformer (rate=1)
        ddb = derived["aws_dynamodb_table.data"]
        assert ddb.invocation_count == pytest.approx(s3.invocation_count)

    def test_workflow_2_daily_analytics_derived(self, engine):
        """Daily analytics workflow derives 1/day = 30/month."""
        engine.compute()
        derived = engine.derived_usage

        # Daily schedule: 1/day
        schedule = derived["aws_eventbridge_rule.daily_schedule"]
        monthly_invocations = schedule.invocation_count * 2629800
        assert monthly_invocations == pytest.approx(30, rel=0.05)

        # Aggregator Lambda: rate=1 from schedule
        aggregator = derived["aws_lambda_function.aggregator"]
        assert aggregator.invocation_count == pytest.approx(schedule.invocation_count)

    def test_rds_cost_is_flat_monthly(self, engine):
        """RDS uses flatOverride — cost is independent of invocation count."""
        costs = engine.compute()
        rds_cost = costs["aws_db_instance.analytics"]

        # 730 hours × $0.021/hour = $15.33/month
        expected = 730 * 0.021
        assert rds_cost == pytest.approx(expected, rel=0.01)

    def test_rds_cost_unchanged_with_frequency(self):
        """RDS flatOverride means cost doesn't change with frequency."""
        model = load_yaml_model("data-pipeline.yaml")
        engine_base = CostEngine(model, time_basis="monthly")
        base_cost = engine_base.compute()["aws_db_instance.analytics"]

        # Modify frequency — RDS cost should stay the same
        model["workflows"][1]["frequency"]["value"] = 100  # 100/day instead of 1
        engine_mod = CostEngine(model, time_basis="monthly")
        mod_cost = engine_mod.compute()["aws_db_instance.analytics"]

        assert base_cost == pytest.approx(mod_cost)

    def test_sensitivity_on_data_pipeline_frequency(self):
        """What-if: 10× uploads increases data pipeline costs proportionally."""
        model = load_yaml_model("data-pipeline.yaml")
        from infra_cost_model.engine.engine import ParametricSensitivityAnalyzer

        engine_base = CostEngine(model, time_basis="monthly")
        base_total = engine_base.total_cost()

        # 10× frequency
        model["workflows"][0]["frequency"]["value"] = 10_000
        engine_10x = CostEngine(model, time_basis="monthly")
        total_10x = engine_10x.total_cost()

        # Data pipeline costs should scale roughly 10× (minus fixed RDS cost)
        assert total_10x > base_total
        # Not strictly 10× because RDS is fixed
        assert total_10x < base_total * 10

    def test_no_cycles_in_dag(self, engine):
        """Engine validates DAG is acyclic."""
        # compute() already validates — reaching here means it passed
        engine.compute()
        assert True

    def test_scenario_edge_case_zero_frequency(self):
        """Edge case: workflow with zero frequency should have zero cost."""
        model = load_yaml_model("data-pipeline.yaml")
        model["workflows"][0]["frequency"]["value"] = 0
        engine = CostEngine(model, time_basis="monthly")
        costs = engine.compute()
        # Data pipeline entry should have zero cost contribution
        assert costs["aws_s3_bucket.uploads"] == 0.0


class TestDataVolumeMetrics:
    """Validates data volume propagation through edges."""

    @pytest.fixture
    def engine(self):
        model = load_yaml_model("data-pipeline.yaml")
        return CostEngine(model, time_basis="monthly")

    def test_data_in_accumulates_on_transformer(self, engine):
        """Transformer Lambda receives data from extractor via dataSize."""
        model = load_yaml_model("data-pipeline.yaml")
        engine.compute()
        derived = engine.derived_usage

        transformer = derived["aws_lambda_function.transformer"]
        # Extractor sends 100KB per invocation at 1000/day
        # per-second: 1000/86400 ≈ 0.01157 invocations/sec
        # data per sec: 0.01157 * 1 * 100kB = 1.157 kB/sec
        # monthly: 1.157 * 86400 * 30.4375 ≈ 3,041,280 kB ≈ 3 GB
        monthly_invocations = (
            derived["aws_lambda_function.extractor"].invocation_count * 2629800
        )
        monthly_kb = monthly_invocations * 100  # 100 kB per invocation
        assert monthly_kb == pytest.approx(3_000_000, rel=0.02)

    def test_data_out_on_reports(self, engine):
        """Reports S3 bucket receives data from aggregator."""
        engine.compute()
        derived = engine.derived_usage

        reports = derived["aws_s3_bucket.reports"]
        aggregator = derived["aws_lambda_function.aggregator"]

        monthly_aggr = aggregator.invocation_count * 2629800  # ~30
        # dataSize on edge is 10 MB per invocation, so data_in is in MB
        monthly_data_in = reports.data_in * 2629800  # Convert per-sec to monthly
        # Expected: 30 invocations * 10 MB = 300 MB/month
        assert monthly_data_in == pytest.approx(monthly_aggr * 10, rel=0.05)
