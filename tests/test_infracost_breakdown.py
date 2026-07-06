"""Tests for the Infracost breakdown importer (Issue #237).

Fixtures mirror the `infracost breakdown --path <dir> --format json` output
(schema version 0.2): projects[].breakdown.resources[] with costComponents and
nested subresources, all money/quantity fields as strings.
"""
import pytest

from infra_cost_model.pricing.sources.infracost_breakdown import (
    import_breakdown,
    _provider_for,
)


def _breakdown(resources):
    return {
        "version": "0.2",
        "currency": "USD",
        "projects": [{"name": "proj", "breakdown": {"resources": resources}}],
    }


EC2 = {
    "name": "aws_instance.web",
    "resourceType": "aws_instance",
    "hourlyCost": "0.107",
    "monthlyCost": "78.91",
    "costComponents": [{
        "name": "Instance usage (Linux/UNIX, on-demand, m5.large)",
        "unit": "hours", "hourlyQuantity": "1", "monthlyQuantity": "730",
        "price": "0.107", "hourlyCost": "0.107", "monthlyCost": "78.11",
    }],
    "subresources": [{
        "name": "root_block_device",
        "costComponents": [{
            "name": "Storage (general purpose SSD, gp3)",
            "unit": "GB", "monthlyQuantity": "8", "price": "0.10",
            "monthlyCost": "0.80",
        }],
    }],
}

ELASTICACHE = {
    "name": "aws_elasticache_cluster.cache",
    "resourceType": "aws_elasticache_cluster",
    "monthlyCost": "153.30",
    "costComponents": [{
        "name": "ElastiCache (on-demand, cache.r6g.large)",
        "unit": "hours", "monthlyQuantity": "730", "price": "0.21",
        "monthlyCost": "153.30",
    }],
}

FREE = {
    "name": "aws_iam_role.x", "resourceType": "aws_iam_role",
    "monthlyCost": "0", "costComponents": [], "subresources": [],
}


class TestProviderInference:
    @pytest.mark.parametrize("rtype,provider", [
        ("aws_instance", "aws"),
        ("google_compute_instance", "gcp"),
        ("azurerm_linux_virtual_machine", "azure"),
        ("digitalocean_droplet", "unknown"),
    ])
    def test_provider_for(self, rtype, provider):
        assert _provider_for(rtype) == provider


class TestImportBreakdown:
    def test_one_node_per_costed_resource(self):
        nodes = import_breakdown(_breakdown([EC2, ELASTICACHE, FREE]))
        # FREE (no cost components) is skipped.
        assert set(nodes) == {"aws_instance.web", "aws_elasticache_cluster.cache"}

    def test_node_shape(self):
        nodes = import_breakdown(_breakdown([ELASTICACHE]))
        n = nodes["aws_elasticache_cluster.cache"]
        assert n["provider"] == "aws"
        assert n["service"] == "aws_elasticache_cluster"
        assert n["resourceAddress"] == "aws_elasticache_cluster.cache"
        assert n["flatOverride"] is True
        # single fixed metric valued at the component's monthly cost
        (metric,) = n["usageMetrics"].values()
        assert metric["fixed"] is True
        assert metric["value"] == pytest.approx(153.30)

    def test_total_cost_preserved_across_components_and_subresources(self):
        """The node's fixed metrics must sum to the resource's monthly cost —
        the instance component ($78.11) plus the root_block_device
        subresource ($0.80) = $78.91."""
        nodes = import_breakdown(_breakdown([EC2]))
        n = nodes["aws_instance.web"]
        total = sum(m["value"] for m in n["usageMetrics"].values())
        assert total == pytest.approx(78.91)
        # subresource component is present and namespaced (no key collision)
        assert any("root_block_device" in k for k in n["usageMetrics"])

    def test_priced_by_engine(self):
        """An imported node prices to its Infracost monthly cost through the
        engine (fixed flatOverride, no catalog needed)."""
        from infra_cost_model.engine import CostEngine
        nodes = import_breakdown(_breakdown([EC2, ELASTICACHE]))
        model = {
            "workflow": {"name": "t", "entry": "aws_instance.web",
                         "frequency": {"unit": "perMonth", "value": 0}},
            "nodes": nodes, "edges": [],
        }
        costs = CostEngine(model, time_basis="monthly").compute()
        assert costs["aws_instance.web"] == pytest.approx(78.91)
        assert costs["aws_elasticache_cluster.cache"] == pytest.approx(153.30)

    def test_empty_breakdown(self):
        assert import_breakdown(_breakdown([])) == {}

    def test_multiple_projects_merged(self):
        bd = {"version": "0.2", "projects": [
            {"name": "a", "breakdown": {"resources": [EC2]}},
            {"name": "b", "breakdown": {"resources": [ELASTICACHE]}},
        ]}
        nodes = import_breakdown(bd)
        assert set(nodes) == {"aws_instance.web", "aws_elasticache_cluster.cache"}
