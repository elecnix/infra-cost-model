"""Tests for matching IaC addresses against a cost model that declares `covers`.

A node's `resourceAddress` names one logical address. Terraform's address
carries the module path, and one node often stands for several resources on
purpose, so a node lists the extra addresses it covers (#449).
"""

from infra_cost_model.coverage import StalePattern, match_coverage
from infra_cost_model.schema import validate_cost_model


def test_schema_accepts_a_covers_list():
    """The model validates with `covers` on a node, so all three surfaces take it."""
    model = {
        "version": "1.0",
        "workflow": {
            "name": "m",
            "entry": "aws_lb.main",
            "frequency": {"unit": "perMinute", "value": 1},
        },
        "nodes": {
            "aws_lb.main": {
                "nodeType": "routing",
                "resourceAddress": "aws_lb.main",
                "covers": ["module.app.aws_lb.public", "module.app.aws_lb.internal"],
            }
        },
    }
    assert validate_cost_model(model) == []


def test_schema_rejects_a_covers_entry_that_is_not_a_string():
    model = {
        "version": "1.0",
        "workflow": {
            "name": "m",
            "entry": "aws_lb.main",
            "frequency": {"unit": "perMinute", "value": 1},
        },
        "nodes": {
            "aws_lb.main": {
                "nodeType": "routing",
                "resourceAddress": "aws_lb.main",
                "covers": [{"address": "module.app.aws_lb.public"}],
            }
        },
    }
    assert validate_cost_model(model)


def _node(**fields) -> dict:
    return {"nodeType": "compute", **fields}


def test_exact_resource_address_still_matches():
    result = match_coverage(
        {"aws_s3_bucket.data": _node(resourceAddress="aws_s3_bucket.data")},
        {"aws_s3_bucket.data"},
    )
    assert result.matched == {"aws_s3_bucket.data"}
    assert result.uncosted == set()
    assert result.orphaned == set()
    assert result.stale_patterns == []


def test_covers_entry_matches_the_full_module_path():
    nodes = {
        "aws_ecs_service.api": _node(
            resourceAddress="aws_ecs_service.api",
            covers=["module.app.module.ecs.aws_ecs_service.api"],
        )
    }
    result = match_coverage(nodes, {"module.app.module.ecs.aws_ecs_service.api"})
    assert result.matched == {"module.app.module.ecs.aws_ecs_service.api"}
    assert result.uncosted == set()
    assert result.orphaned == set()
    assert result.stale_patterns == []


def test_glob_pattern_covers_every_module_it_reaches():
    nodes = {
        "aws_lb.main": _node(
            resourceAddress="aws_lb.main",
            covers=["module.*.aws_lb.public"],
        )
    }
    result = match_coverage(
        nodes,
        {
            "module.app.aws_lb.public",
            "module.partner.aws_lb.public",
            "module.app.aws_lb.internal",
        },
    )
    assert result.matched == {"module.app.aws_lb.public", "module.partner.aws_lb.public"}
    assert result.uncosted == {"module.app.aws_lb.internal"}
    assert result.orphaned == set()


def test_aggregate_node_is_not_orphaned():
    """A node that covers four load balancers is costed, so it is not orphaned."""
    nodes = {
        "aws_lb.main": _node(
            resourceAddress="aws_lb.main",
            covers=[
                "module.a.aws_lb.public",
                "module.b.aws_lb.public",
                "module.c.aws_lb.public",
                "module.d.aws_lb.public",
            ],
        )
    }
    result = match_coverage(
        nodes,
        {
            "module.a.aws_lb.public",
            "module.b.aws_lb.public",
            "module.c.aws_lb.public",
            "module.d.aws_lb.public",
        },
    )
    assert result.orphaned == set()
    assert len(result.matched) == 4


def test_pattern_matching_nothing_is_stale():
    nodes = {
        "aws_lb.main": _node(
            resourceAddress="aws_lb.main",
            covers=["module.*.aws_lb.public"],
        )
    }
    result = match_coverage(nodes, {"module.app.aws_s3_bucket.logs"})
    assert [(s.node, s.pattern) for s in result.stale_patterns] == [
        ("aws_lb.main", "module.*.aws_lb.public")
    ]
    # A stale pattern claims nothing, so it leaves the node unaccounted for.
    assert result.orphaned == {"aws_lb.main"}
    assert result.uncosted == {"module.app.aws_s3_bucket.logs"}


def test_one_matching_pattern_clears_a_node_whose_address_is_a_logical_name():
    nodes = {
        "aws_lb.main": _node(
            resourceAddress="aws_lb.main",
            covers=["module.app.aws_lb.public", "module.app.aws_lb.removed"],
        )
    }
    result = match_coverage(nodes, {"module.app.aws_lb.public"})
    assert result.orphaned == set()
    assert [s.pattern for s in result.stale_patterns] == ["module.app.aws_lb.removed"]


def test_stale_patterns_are_sorted_for_deterministic_output():
    nodes = {
        "b": _node(resourceAddress="b", covers=["nope.*.b", "absent"]),
        "a": _node(resourceAddress="a", covers=["gone.*.a"]),
    }
    result = match_coverage(nodes, set())
    assert [(s.node, s.pattern) for s in result.stale_patterns] == [
        ("a", "gone.*.a"),
        ("b", "absent"),
        ("b", "nope.*.b"),
    ]


def test_a_node_whose_address_the_export_has_is_not_orphaned():
    """A node naming an address a `covers` glob would also reach is not orphaned."""
    nodes = {
        "aws_lb.main": _node(resourceAddress="aws_lb.public", covers=["aws_lb.*"])
    }
    result = match_coverage(nodes, {"aws_lb.public"})
    assert result.matched == {"aws_lb.public"}
    assert result.orphaned == set()
    assert result.uncosted == set()
    assert result.stale_patterns == []


def test_stale_pattern_does_not_orphan_a_node_the_export_has():
    """A node the export has stays matched when another covers entry is stale."""
    nodes = {
        "aws_lb.main": _node(
            resourceAddress="aws_lb.public", covers=["aws_lb.gone"]
        )
    }
    result = match_coverage(nodes, {"aws_lb.public"})
    assert result.matched == {"aws_lb.public"}
    assert result.orphaned == set()
    assert result.uncosted == set()
    assert result.stale_patterns == [
        StalePattern(node="aws_lb.main", pattern="aws_lb.gone")
    ]


def test_glob_without_a_metacharacter_matches_one_exact_address():
    nodes = {
        "aws_lb.main": _node(
            resourceAddress="aws_lb.main",
            covers=["module.app.aws_lb.public"],
        )
    }
    result = match_coverage(
        nodes,
        {"module.app.aws_lb.public", "module.app2.aws_lb.public"},
    )
    assert result.matched == {"module.app.aws_lb.public"}
    assert result.uncosted == {"module.app2.aws_lb.public"}


def test_matching_is_case_sensitive():
    nodes = {"n": _node(resourceAddress="n", covers=["Module.app.aws_lb.public"])}
    result = match_coverage(nodes, {"module.app.aws_lb.public"})
    assert result.uncosted == {"module.app.aws_lb.public"}
    assert [s.pattern for s in result.stale_patterns] == ["Module.app.aws_lb.public"]


def test_two_nodes_may_cover_the_same_address_without_double_counting():
    """A shared address counts once as costed, and neither node is orphaned."""
    nodes = {
        "aws_lb.main": _node(
            resourceAddress="aws_lb.main",
            covers=["module.app.aws_lb.public"],
        ),
        "aws_lb.audit": _node(
            resourceAddress="aws_lb.audit",
            covers=["module.app.aws_lb.public"],
        ),
    }
    result = match_coverage(nodes, {"module.app.aws_lb.public"})
    assert result.matched == {"module.app.aws_lb.public"}
    assert result.uncosted == set()
    assert result.orphaned == set()


def test_non_dict_node_is_ignored():
    result = match_coverage({"broken": "not-a-node"}, {"aws_s3_bucket.data"})
    assert result.matched == set()
    assert result.uncosted == {"aws_s3_bucket.data"}
    assert result.orphaned == set()


def test_covers_entries_that_are_not_strings_are_ignored():
    nodes = {"n": _node(resourceAddress="n", covers=[17, None])}
    result = match_coverage(nodes, set())
    assert result.stale_patterns == []


def test_a_covered_address_is_not_also_reported_uncosted():
    nodes = {
        "aws_lb.main": _node(
            resourceAddress="module.app.aws_lb.public",
            covers=["module.app.aws_lb.internal"],
        )
    }
    result = match_coverage(
        nodes,
        {"module.app.aws_lb.public", "module.app.aws_lb.internal"},
    )
    assert result.matched == {
        "module.app.aws_lb.public",
        "module.app.aws_lb.internal",
    }
    assert result.uncosted == set()
    assert result.orphaned == set()