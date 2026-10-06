"""Tests for the reconcile command (#444).

The command compares a cost model with an actuals file exported from AWS Cost
Explorer. Every test here drives one of the traps the issue names: a charge
that lands once a month, days the bill omits, a line that started billing
mid-window, several nodes sharing one bill line, a payload that failed or came
back truncated, and a bill line the model deliberately leaves out.
"""

import json

import pytest

from infra_cost_model.cli import main
from infra_cost_model.reconcile import (
    DAYS_PER_MONTH,
    ReconcileError,
    load_actuals,
    load_config,
    reconcile,
)


# --- fixtures ---------------------------------------------------------------

def ce_payload(days, key="Amazon Virtual Private Cloud/USW2-NatGateway-Hours",
               amounts=None, **extra):
    """A `get-cost-and-usage` payload with one group per day.

    A day with no amount in `amounts` reports an explicit `$0.00` row, which is
    what Cost Explorer does for a line that bills monthly (#444, trap 1).
    """
    results = []
    for day in days:
        amount = 1.0 if amounts is None else amounts.get(day, 0.0)
        results.append({
            "Time": {"Start": day, "End": day},
            "Groups": [{
                "Keys": [key],
                "Metrics": {"UnblendedCost": {"Amount": f"{amount:.6f}"}},
            }],
        })
    payload = {"ResultsByTime": results}
    payload.update(extra)
    return payload


def ce_sparse(days, amounts,
              key="Amazon Virtual Private Cloud/USW2-NatGateway-Hours", **extra):
    """A payload that omits the group on days the line didn't bill (#444, trap 2)."""
    results = []
    for day in days:
        groups = []
        if day in amounts:
            groups.append({"Keys": [key],
                           "Metrics": {"UnblendedCost": {"Amount": f"{amounts[day]:.6f}"}}})
        results.append({"Time": {"Start": day, "End": day}, "Groups": groups})
    payload = {"ResultsByTime": results}
    payload.update(extra)
    return payload


def month_days(count=30, start="2026-05-02"):
    """`count` consecutive ISO dates from `start`."""
    from datetime import date, timedelta
    first = date.fromisoformat(start)
    return [(first + timedelta(days=n)).isoformat() for n in range(count)]


def model_with(nodes):
    """A cost model representation whose nodes carry billing lines."""
    return {"workflow": {"name": "test", "entry": next(iter(nodes))},
            "nodes": nodes, "edges": []}


def node(service="Amazon Virtual Private Cloud",
         usage_type="USW2-NatGateway-Hours", provider="aws-cost-explorer"):
    return {"nodeType": "compute", "provider": "aws", "region": "us-west-2",
            "service": "AmazonVPC",
            "billingLines": {"natHours": {"provider": provider,
                                          "service": service,
                                          "usageType": usage_type}}}


def multi_line_node(lines):
    """A node whose metrics land on several bill lines, as a NAT gateway does."""
    return {"nodeType": "routing", "provider": "aws", "region": "us-west-2",
            "service": "AmazonVPC",
            "billingLines": {metric: {"provider": "aws-cost-explorer", **line}
                             for metric, line in lines.items()}}


HOURS_LINE = {"service": "Amazon Virtual Private Cloud",
              "usageType": "USW2-NatGateway-Hours"}
BYTES_LINE = {"service": "EC2 - Other", "usageType": "USW2-NatGateway-Bytes"}


def write_json(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return path


def run(model, costs, actuals, **kwargs):
    return reconcile(model, costs, actuals, **kwargs)


# --- trap 1: a charge that lands once a month --------------------------------

def test_a_short_window_still_misses_a_monthly_charge(tmp_path):
    """A Route 53 style charge bills once and reports $0.00 for the rest.

    Dividing by the one day it charged would project the charge at 30.4. The
    divisor runs over the window, so a 7-day window projects it at 30.4/7 and a
    30-day window brings the factor back to about 1.
    """
    days = month_days(7)
    amounts = {days[0]: 0.50}
    actuals = load_actuals(write_json(tmp_path, "actuals.json",
                                      ce_payload(days, amounts=amounts)))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 0.50},
                  actuals)

    group = report.groups[0]
    line = group.bill_lines[0]
    assert line.billed_days == 1
    assert line.zero_days == 6
    assert line.divisor_days == 7
    assert group.projected == pytest.approx(0.50 / 7 * DAYS_PER_MONTH)
    assert group.projected < 0.50 * DAYS_PER_MONTH


def test_a_30_day_window_keeps_the_monthly_charge_near_its_true_size(tmp_path):
    days = month_days(30)
    amounts = {days[0]: 0.50}
    actuals = load_actuals(write_json(tmp_path, "actuals.json",
                                      ce_payload(days, amounts=amounts)))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 0.50},
                  actuals)

    line = report.groups[0].bill_lines[0]
    assert line.billed_days == 1
    assert line.divisor_days == 30
    assert report.groups[0].projected == pytest.approx(0.50, rel=0.02)


# --- trap 2: days the bill omits --------------------------------------------

def test_absent_days_do_not_shrink_the_divisor(tmp_path):
    """A line billed on 10 of 30 days keeps the 30 days as its divisor.

    Dividing by the days the exporter happened to report would project the line
    at three times its real size.
    """
    days = month_days(30)
    amounts = {d: 2.0 for d in days[:10]}
    actuals = load_actuals(write_json(tmp_path, "actuals.json",
                                      ce_sparse(days, amounts)))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 20.0},
                  actuals)

    line = report.groups[0].bill_lines[0]
    assert line.billed_days == 10
    assert line.absent_days == 20
    assert line.divisor_days == 30
    assert line.projected == pytest.approx(20.0 / 30 * DAYS_PER_MONTH)


# --- trap 3: a line that started billing mid-window --------------------------

def test_a_new_line_projects_from_the_days_since_it_started(tmp_path):
    """A resource created mid-window is billed on the days it existed.

    Dividing by the whole window would read its half month as its monthly cost.
    """
    days = month_days(30)
    amounts = {d: 1.0 for d in days[15:]}
    actuals = load_actuals(write_json(tmp_path, "actuals.json",
                                      ce_sparse(days, amounts)))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 30.44},
                  actuals)

    line = report.groups[0].bill_lines[0]
    assert line.billed_days == 15
    assert line.divisor_days == 15
    assert line.first_billed_day == days[15]
    assert line.projected == pytest.approx(DAYS_PER_MONTH)


def test_a_longer_export_tells_a_new_line_from_an_idle_one(tmp_path):
    """The same new resource, read from a wider export.

    Export far enough back and the window opens on the day the line started,
    so the divisor reaches the whole window and the line projects at its real
    monthly rate instead of at half of it.
    """
    days = month_days(45)
    amounts = {d: 1.0 for d in days[15:]}
    actuals = load_actuals(write_json(tmp_path, "actuals.json",
                                      ce_sparse(days, amounts)))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 30.44},
                  actuals, window_days=30)

    line = report.groups[0].bill_lines[0]
    assert line.divisor_days == 30
    assert line.projected == pytest.approx(DAYS_PER_MONTH)


def test_a_line_that_never_billed_projects_zero(tmp_path):
    days = month_days(30)
    actuals = load_actuals(write_json(tmp_path, "actuals.json",
                                      ce_payload(days, amounts={})))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 5.0},
                  actuals)

    assert report.groups[0].bill_lines[0].divisor_days == 0
    assert report.groups[0].projected == 0.0


# --- trap 4: several nodes on one bill line ----------------------------------

def test_nodes_sharing_one_line_are_compared_once(tmp_path):
    """Four container services under one ECS line. Comparing each node with the
    whole line would count the line four times."""
    days = month_days(30)
    nodes = {f"aws_ecs_service.s{i}": node(service="AmazonECS", usage_type="USE1-Fargate-Hours")
             for i in range(4)}
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json",
        ce_payload(days, key="AmazonECS/USE1-Fargate-Hours")))
    costs = {address: 25.0 for address in nodes}

    report = run(model_with(nodes), costs, actuals)

    assert len(report.groups) == 1
    group = report.groups[0]
    assert group.modelled == pytest.approx(100.0)
    assert group.projected == pytest.approx(DAYS_PER_MONTH * 1.0)
    assert len(group.nodes) == 4


def test_a_line_shared_by_two_groups_merges_them(tmp_path):
    """One node spans two bill lines, another spans one of them.

    Both nodes and both lines belong to the same connected component, so the
    bill lines are compared against the two nodes together rather than counted
    once per node.
    """
    days = month_days(30)
    nodes = {
        "aws_nat_gateway.main": multi_line_node({"natHours": HOURS_LINE,
                                                "natGb": BYTES_LINE}),
        "aws_nat_gateway.dr": multi_line_node({"natHours": HOURS_LINE}),
    }
    payload = {"ResultsByTime": [
        {"Time": {"Start": d, "End": d}, "Groups": [
            {"Keys": ["Amazon Virtual Private Cloud/USW2-NatGateway-Hours"],
             "Metrics": {"UnblendedCost": {"Amount": "0.045"}}},
            {"Keys": ["EC2 - Other/USW2-NatGateway-Bytes"],
             "Metrics": {"UnblendedCost": {"Amount": "0.010"}}},
        ]} for d in days
    ]}
    actuals = load_actuals(write_json(tmp_path, "actuals.json", payload))
    costs = {"aws_nat_gateway.main": 20.0, "aws_nat_gateway.dr": 10.0}

    report = run(model_with(nodes), costs, actuals)

    assert len(report.groups) == 1
    group = report.groups[0]
    assert set(group.nodes) == set(nodes)
    assert len(group.lines) == 2
    assert group.modelled == pytest.approx(30.0)
    assert group.projected == pytest.approx(0.055 * DAYS_PER_MONTH)


# --- trap 5: a failed or truncated page --------------------------------------

def test_a_truncated_payload_reads_unreadable_not_zero(tmp_path):
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json",
        ce_payload(month_days(30), NextPageToken="page2")))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 10.0},
                  actuals)

    assert actuals.truncated is True
    assert report.groups[0].status == "unreadable"
    assert report.status == "unreadable"


def test_a_failed_payload_reads_unreadable(tmp_path):
    payload = {"__type": "AccessDeniedException",
               "message": "not authorised to perform ce:GetCostAndUsage"}
    actuals = load_actuals(write_json(tmp_path, "actuals.json", payload))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 10.0},
                  actuals)

    assert "AccessDeniedException" in actuals.error
    assert report.groups[0].status == "unreadable"


def test_an_unparseable_amount_reads_unreadable_not_zero(tmp_path):
    """A cost the reader cannot parse is missing data, not a zero.

    Treating it as $0.00 would report a fully costed model as failing drift
    against a bill that says nothing, which is the reading trap 5 forbids.
    """
    payload = {"ResultsByTime": [
        {"Time": {"Start": day, "End": day}, "Groups": [
            {"Keys": ["Amazon Virtual Private Cloud/USW2-NatGateway-Hours"],
             "Metrics": {"UnblendedCost": {"Amount": "N/A"}}}]}
        for day in month_days(30)
    ]}
    actuals = load_actuals(write_json(tmp_path, "actuals.json", payload))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 30.44},
                  actuals)

    assert actuals.readable is False
    assert "N/A" in actuals.error
    assert report.groups[0].status == "unreadable"
    assert report.status == "unreadable"


def test_an_empty_payload_reads_unreadable(tmp_path):
    actuals = load_actuals(write_json(tmp_path, "actuals.json", {}))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 10.0},
                  actuals)

    assert report.groups[0].status == "unreadable"


# --- trap 6: accepted gaps ---------------------------------------------------

def test_an_unmodelled_line_with_a_reason_is_excluded(tmp_path):
    """A tax line is real and deliberately left out of the model. It carries a
    written reason, and it leaves the totals rather than holding them red."""
    days = month_days(30)
    payload = {"ResultsByTime": [
        {"Time": {"Start": d, "End": d}, "Groups": [
            {"Keys": ["Amazon Virtual Private Cloud/USW2-NatGateway-Hours"],
             "Metrics": {"UnblendedCost": {"Amount": "0.50"}}},
            {"Keys": ["Tax/Tax"],
             "Metrics": {"UnblendedCost": {"Amount": "1.00"}}},
        ]} for d in days
    ]}
    actuals = load_actuals(write_json(tmp_path, "actuals.json", payload))
    config = load_config(_write_yaml(tmp_path, """
warnPct: 15
failPct: 30
unmodelled:
  - service: Tax
    reason: billed as its own line with no per-resource grain
"""))

    report = run(model_with({"aws_nat_gateway.main": node()}),
                 {"aws_nat_gateway.main": 0.50 * DAYS_PER_MONTH}, actuals, config=config)

    assert len(report.groups) == 1
    assert len(report.unmodelled) == 1
    assert report.unmodelled[0].service == "Tax"
    assert report.unmodelled[0].reason.startswith("billed as its own line")
    assert report.unmodelled[0].projected == pytest.approx(DAYS_PER_MONTH)
    # The accepted gap stays out of the totals, which carry the compared line only.
    assert report.totals["projected"] == pytest.approx(0.50 * DAYS_PER_MONTH)
    assert report.status == "ok"


def test_an_unmodelled_entry_without_a_reason_is_an_error(tmp_path):
    with pytest.raises(ReconcileError, match="reason"):
        load_config(_write_yaml(tmp_path, """
unmodelled:
  - service: Tax
"""))


def test_allowlisting_a_line_a_node_maps_to_is_an_error(tmp_path):
    """Allowlisting a compared line would delete the comparison, not accept a gap."""
    actuals = load_actuals(write_json(tmp_path, "actuals.json", ce_payload(month_days(30))))
    config = load_config(_write_yaml(tmp_path, """
unmodelled:
  - service: Amazon Virtual Private Cloud
    reason: pretending it is an accepted gap
"""))

    with pytest.raises(ReconcileError, match="Amazon Virtual Private Cloud"):
        run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 10.0},
            actuals, config=config)


def _write_yaml(tmp_path, text):
    path = tmp_path / "reconcile.yaml"
    path.write_text(text)
    return path


# --- drift and status -------------------------------------------------------

def _single_line_report(tmp_path, modelled, projected_per_day):
    days = month_days(30)
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json",
        ce_payload(days, amounts={d: projected_per_day for d in days})))
    return run(model_with({"aws_nat_gateway.main": node()}),
               {"aws_nat_gateway.main": modelled}, actuals)


def test_drift_inside_the_warn_band_is_ok(tmp_path):
    report = _single_line_report(tmp_path, modelled=31.0, projected_per_day=1.0)

    group = report.groups[0]
    assert group.projected == pytest.approx(DAYS_PER_MONTH)
    assert group.drift_usd == pytest.approx(31.0 - DAYS_PER_MONTH)
    assert group.drift_pct == pytest.approx((31.0 - DAYS_PER_MONTH) / DAYS_PER_MONTH * 100)
    assert group.status == "ok"


def test_drift_past_the_warn_band_is_warn(tmp_path):
    report = _single_line_report(tmp_path, modelled=38.0, projected_per_day=1.0)

    assert report.groups[0].status == "warn"


def test_drift_past_the_fail_band_is_fail(tmp_path):
    report = _single_line_report(tmp_path, modelled=50.0, projected_per_day=1.0)

    assert report.groups[0].status == "fail"
    assert report.status == "fail"


def test_drift_below_the_line_counts_against_the_model(tmp_path):
    """A model that predicts less than the bill spends is negative drift."""
    report = _single_line_report(tmp_path, modelled=28.0, projected_per_day=1.0)

    assert report.groups[0].drift_usd < 0
    assert report.groups[0].drift_pct < 0
    assert report.groups[0].status == "ok"


def test_a_line_missing_from_the_bill_fails(tmp_path):
    """The line a node maps to is absent from the payload: the model predicted
    spend the bill does not show."""
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json",
        ce_payload(month_days(30), key="AmazonEC2/BoxUsage:m5.large")))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 73.0},
                  actuals)

    group = report.groups[0]
    assert group.projected == 0.0
    assert group.drift_pct is None
    assert group.status == "fail"


def test_a_line_in_the_bill_no_node_maps_to_fails(tmp_path):
    days = month_days(30)
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json",
        ce_payload(days, key="AWSCloudTrail/EventRecording", amounts={d: 3.0 for d in days})))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 0.0},
                  actuals)

    unmodelled_group = [g for g in report.groups if g.nodes == []]
    assert len(unmodelled_group) == 1
    assert unmodelled_group[0].modelled == 0.0
    assert unmodelled_group[0].drift_pct == pytest.approx(-100.0)


def test_the_thresholds_come_from_the_config_file(tmp_path):
    days = month_days(30)
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json", ce_payload(days, amounts={d: 1.0 for d in days})))
    config = load_config(_write_yaml(tmp_path, """
warnPct: 5
failPct: 50
"""))

    report = run(model_with({"aws_nat_gateway.main": node()}),
                 {"aws_nat_gateway.main": 40.0}, actuals, config=config)

    assert report.warn_pct == 5.0
    assert report.fail_pct == 50.0
    assert report.groups[0].status == "warn"


# --- billing lines ----------------------------------------------------------

def test_a_usage_type_less_line_covers_the_whole_service(tmp_path):
    days = month_days(30)
    payload = ce_payload(days, key="EC2 - Other/USW2-NatGateway-Bytes",
                         amounts={d: 0.0 for d in days})
    payload["ResultsByTime"] = [
        {"Time": {"Start": d, "End": d}, "Groups": [
            {"Keys": ["EC2 - Other/USW2-NatGateway-Bytes"],
             "Metrics": {"UnblendedCost": {"Amount": "0.50"}}},
            {"Keys": ["EC2 - Other/USW2-NatGateway-Hours"],
             "Metrics": {"UnblendedCost": {"Amount": "1.00"}}},
        ]} for d in days
    ]
    actuals = load_actuals(write_json(tmp_path, "actuals.json", payload))
    model = model_with({"aws_nat_gateway.main": node(service="EC2 - Other",
                                                      usage_type=None)})

    report = run(model, {"aws_nat_gateway.main": 45.63}, actuals)

    assert len(report.groups) == 1
    assert len(report.groups[0].lines) == 2
    assert report.groups[0].projected == pytest.approx(1.5 * DAYS_PER_MONTH)


def test_a_line_naming_another_provider_is_refused(tmp_path):
    actuals = load_actuals(write_json(tmp_path, "actuals.json", ce_payload(month_days(30))))
    model = model_with({"aws_nat_gateway.main": node(provider="gcp-billing")})

    with pytest.raises(ReconcileError, match="gcp-billing"):
        run(model, {"aws_nat_gateway.main": 1.0}, actuals)


def test_a_node_without_billing_lines_still_reports_its_cost(tmp_path):
    """#442 adds the field. Before it lands, a node without one is unmodelled
    money the comparison cannot place, and the report says so."""
    actuals = load_actuals(write_json(tmp_path, "actuals.json", ce_payload(month_days(30))))
    model = model_with({"aws_nat_gateway.main": {
        "nodeType": "compute", "provider": "aws", "region": "us-west-2",
        "service": "AmazonVPC"}})

    report = run(model, {"aws_nat_gateway.main": 12.0}, actuals)

    assert report.unplaced == ["aws_nat_gateway.main"]


# --- the window -------------------------------------------------------------

def test_the_window_is_the_trailing_days_of_the_file(tmp_path):
    days = month_days(45)
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json", ce_payload(days, amounts={d: 1.0 for d in days})))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 30.0},
                 actuals, window_days=30)

    assert report.window_days == 30
    assert report.window_start == days[15]
    assert report.window_end == days[-1]
    assert report.groups[0].projected == pytest.approx(DAYS_PER_MONTH)


def test_a_short_file_uses_every_day_it_has(tmp_path):
    days = month_days(10)
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json", ce_payload(days, amounts={d: 1.0 for d in days})))

    report = run(model_with({"aws_nat_gateway.main": node()}), {"aws_nat_gateway.main": 10.0},
                 actuals, window_days=30)

    assert report.window_days == 10
    assert report.groups[0].projected == pytest.approx(DAYS_PER_MONTH)


# --- the command line -------------------------------------------------------

CLI_MODEL = """
version: "1.0"
workflow:
  name: reconcile-test
  entry: aws_nat_gateway.main
  frequency: { unit: perMinute, value: 10 }
nodes:
  aws_nat_gateway.main:
    nodeType: routing
    resourceAddress: aws_nat_gateway.main
    provider: aws
    region: us-east-1
    service: AmazonVPC
    pricingRates:
      natHours: 0.045
    usageMetrics:
      natHours: { unit: hours, value: 730, fixed: true }
    billingLines:
      natHours:
        provider: aws-cost-explorer
        service: "Amazon Virtual Private Cloud"
        usageType: "USE1-NatGateway-Hours"
"""

# 730 hours at the seeded $0.045 an hour. A bill line that projects to that
# amount, and one that projects to a twentieth of it.
ON_RATE = 1.08
OFF_RATE = 0.01


def cli_actuals(tmp_path, per_day):
    days = month_days(30)
    return write_json(tmp_path, "actuals.json",
                      ce_payload(days, key="Amazon Virtual Private Cloud/USE1-NatGateway-Hours",
                                 amounts={d: per_day for d in days}))


def test_cli_reconcile_reports_json(tmp_path, capsys):
    model_path = tmp_path / "model.yaml"
    model_path.write_text(CLI_MODEL)
    actuals_path = cli_actuals(tmp_path, 0.50)

    assert main(["reconcile", str(model_path), "--actuals", str(actuals_path), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["windowDays"] == 30
    assert report["groups"][0]["lines"] == [
        {"service": "Amazon Virtual Private Cloud", "usageType": "USE1-NatGateway-Hours"}]
    assert report["groups"][0]["projected"] == pytest.approx(15.22, abs=0.01)
    assert report["groups"][0]["status"] == "fail"


def test_cli_reconcile_prices_the_model_itself(tmp_path, capsys):
    """The modelled side comes from the engine on a monthly time basis."""
    model_path = tmp_path / "model.yaml"
    model_path.write_text(CLI_MODEL)
    actuals_path = cli_actuals(tmp_path, ON_RATE)

    assert main(["reconcile", str(model_path), "--actuals", str(actuals_path), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["groups"][0]["modelled"] == pytest.approx(32.85)
    assert report["groups"][0]["status"] == "ok"


def test_cli_reconcile_reads_a_reconcile_yaml_beside_the_model(tmp_path, capsys):
    model_path = tmp_path / "model.yaml"
    model_path.write_text(CLI_MODEL)
    _write_yaml(tmp_path, """
warnPct: 1
failPct: 2
unmodelled:
  - service: Tax
    reason: billed as its own line with no per-resource grain
""")
    actuals_path = cli_actuals(tmp_path, ON_RATE)

    assert main(["reconcile", str(model_path), "--actuals", str(actuals_path), "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["warnPct"] == 1.0
    assert report["failPct"] == 2.0


def test_cli_reconcile_fail_on_drift_exits_one(tmp_path, capsys):
    model_path = tmp_path / "model.yaml"
    model_path.write_text(CLI_MODEL)
    actuals_path = cli_actuals(tmp_path, OFF_RATE)

    code = main(["reconcile", str(model_path), "--actuals", str(actuals_path),
                 "--json", "--fail-on-drift"])

    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == "fail"


def test_cli_reconcile_fail_on_drift_passes_when_ok(tmp_path, capsys):
    model_path = tmp_path / "model.yaml"
    model_path.write_text(CLI_MODEL)
    actuals_path = cli_actuals(tmp_path, ON_RATE)

    assert main(["reconcile", str(model_path), "--actuals", str(actuals_path),
                 "--json", "--fail-on-drift"]) == 0


def test_cli_reconcile_text_output_names_each_group(tmp_path, capsys):
    model_path = tmp_path / "model.yaml"
    model_path.write_text(CLI_MODEL)
    actuals_path = cli_actuals(tmp_path, ON_RATE)

    assert main(["reconcile", str(model_path), "--actuals", str(actuals_path)]) == 0

    out = capsys.readouterr().out
    assert "Amazon Virtual Private Cloud/USE1-NatGateway-Hours" in out
    assert "Modelled" in out and "Projected" in out


def test_cli_reconcile_missing_actuals_file(tmp_path, capsys):
    model_path = tmp_path / "model.yaml"
    model_path.write_text(CLI_MODEL)

    assert main(["reconcile", str(model_path), "--actuals", str(tmp_path / "nope.json")]) == 1


def test_cli_reconcile_missing_model(tmp_path, capsys):
    assert main(["reconcile", str(tmp_path / "nope.yaml"),
                 "--actuals", str(tmp_path / "actuals.json")]) == 1


def test_cli_reconcile_without_actuals_is_an_error(tmp_path, capsys):
    model_path = tmp_path / "model.yaml"
    model_path.write_text(CLI_MODEL)

    assert main(["reconcile", str(model_path)]) == 1

# --- default lines (#461) ----------------------------------------------------

def test_a_default_nat_hours_line_reconciles_against_ec2_other(tmp_path):
    """The bill prints NAT gateway hours under `EC2 - Other`, beside the bytes.

    The default line for the hours metric comes from the known-names list. A
    model that declares it as it stands must find the rows the bill carries.
    """
    from infra_cost_model.billing import resolve_billing_lines

    days = month_days(30)
    nat = {"nodeType": "routing", "resourceAddress": "aws_nat_gateway.main",
           "provider": "aws", "service": "AmazonVPC", "region": "us-west-2",
           "usageMetrics": {"natHours": {"unit": "hours", "value": 730,
                                         "fixed": True}}}
    default = resolve_billing_lines(
        model_with({"aws_nat_gateway.main": nat}))[0]
    assert (default.service, default.usage_type) == (
        "EC2 - Other", "USW2-NatGateway-Hours")

    nat["billingLines"] = {"natHours": {"provider": default.provider,
                                        "service": default.service,
                                        "usageType": default.usage_type}}
    actuals = load_actuals(write_json(
        tmp_path, "actuals.json",
        ce_payload(days, key="EC2 - Other/USW2-NatGateway-Hours")))

    report = run(model_with({"aws_nat_gateway.main": nat}),
                 {"aws_nat_gateway.main": 12.34}, actuals)

    assert report.groups[0].projected == pytest.approx(1.0 * DAYS_PER_MONTH)
    assert report.groups[0].bill_lines[0].billed_days == 30

# --- the CLI's own payload layout (#458) ------------------------------------

from infra_cost_model.reconcile.actuals import parse_actuals  # noqa: E402


def cli_payload():
    """A `get-cost-and-usage` payload as the CLI writes it, with invented values."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Total": {},
                "Groups": [
                    {
                        "Keys": ["Amazon Simple Storage Service"],
                        "Metrics": {"UnblendedCost": {"Amount": "12.34", "Unit": "USD"}},
                    }
                ],
                "Estimated": False,
            }
        ],
        "DimensionValueAttributes": [],
    }


def test_parse_actuals_reads_the_cli_time_period_key():
    actuals = parse_actuals(cli_payload())

    assert actuals.error is None
    assert actuals.days == ["2026-01-01"]
    assert actuals.lines[("Amazon Simple Storage Service", None)] == {"2026-01-01": 12.34}


def test_parse_actuals_still_accepts_time_as_an_alias():
    payload = cli_payload()
    payload["ResultsByTime"][0]["Time"] = payload["ResultsByTime"][0].pop("TimePeriod")

    actuals = parse_actuals(payload)

    assert actuals.error is None
    assert actuals.days == ["2026-01-01"]


def test_parse_actuals_flags_rows_with_no_start_date_as_unreadable():
    payload = cli_payload()
    payload["ResultsByTime"][0].pop("TimePeriod")

    actuals = parse_actuals(payload)

    assert not actuals.readable
    assert "start date" in actuals.error



def test_parse_actuals_flags_a_start_that_is_not_a_date_as_unreadable():
    payload = cli_payload()
    payload["ResultsByTime"][0]["TimePeriod"]["Start"] = "not-a-date"

    actuals = parse_actuals(payload)

    assert not actuals.readable
    assert "not-a-date" in actuals.error
    assert actuals.days == []


def test_parse_actuals_flags_one_bad_start_among_good_rows():
    payload = cli_payload()
    bad = {"TimePeriod": {"Start": "2026-13-45", "End": "2026-13-46"}, "Groups": []}
    payload["ResultsByTime"].append(bad)

    actuals = parse_actuals(payload)

    assert not actuals.readable
    assert "2026-13-45" in actuals.error
    assert actuals.days == ["2026-01-01"]


# --- a group with two keys (#459) -------------------------------------------


def two_key_payload():
    """A query grouped by SERVICE then USAGE_TYPE, with invented values."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Groups": [
                    {
                        "Keys": ["EC2 - Other", "USW2-NatGateway-Bytes"],
                        "Metrics": {"UnblendedCost": {"Amount": "12.34", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["EC2 - Other", "USW2-NatGateway-Hours"],
                        "Metrics": {"UnblendedCost": {"Amount": "1.50", "Unit": "USD"}},
                    },
                ],
            }
        ]
    }


def test_two_keys_read_as_service_then_usage_type():
    actuals = parse_actuals(two_key_payload())

    assert actuals.error is None
    assert actuals.lines[("EC2 - Other", "USW2-NatGateway-Bytes")] == {"2026-01-01": 12.34}
    assert actuals.lines[("EC2 - Other", "USW2-NatGateway-Hours")] == {"2026-01-01": 1.5}


def test_two_key_usage_types_stay_separate_lines():
    from infra_cost_model.reconcile.actuals import BillLineKey

    actuals = parse_actuals(two_key_payload())

    assert actuals.daily(BillLineKey("EC2 - Other", "USW2-NatGateway-Bytes")) == {"2026-01-01": 12.34}
    assert actuals.daily(BillLineKey("EC2 - Other")) == {"2026-01-01": 13.84}


def test_joined_single_key_form_still_works():
    payload = two_key_payload()
    payload["ResultsByTime"][0]["Groups"] = [
        {
            "Keys": ["EC2 - Other/USW2-NatGateway-Bytes"],
            "Metrics": {"UnblendedCost": {"Amount": "12.34", "Unit": "USD"}},
        }
    ]

    actuals = parse_actuals(payload)

    assert list(actuals.lines) == [("EC2 - Other", "USW2-NatGateway-Bytes")]


def test_an_empty_second_key_is_the_service_wide_line():
    payload = two_key_payload()
    payload["ResultsByTime"][0]["Groups"] = [
        {
            "Keys": ["EC2 - Other", ""],
            "Metrics": {"UnblendedCost": {"Amount": "4.00", "Unit": "USD"}},
        }
    ]

    actuals = parse_actuals(payload)

    assert actuals.error is None
    assert list(actuals.lines) == [("EC2 - Other", None)]


# --- billing shape: which divisor rule a line takes (#462) -------------------

def _route53_actuals(tmp_path, days, amounts):
    key = "Amazon Route 53/DomainRegistration"
    return load_actuals(write_json(tmp_path, "actuals.json",
                                   ce_sparse(days, amounts, key=key)))


def _route53_report(actuals, **kwargs):
    model = model_with({"aws_route53_domain.main": node(
        service="Amazon Route 53", usage_type="DomainRegistration")})
    return run(model, {"aws_route53_domain.main": 1.03}, actuals, **kwargs)


def test_an_annual_charge_projects_at_its_share_of_the_window(tmp_path):
    """One charge in a 92-day export is not a resource that started that day."""
    days = month_days(92, start="2026-07-01")
    actuals = _route53_actuals(tmp_path, days, {"2026-08-15": 12.34})

    line = _route53_report(actuals, window_days=92).groups[0].bill_lines[0]

    assert line.rule == "zero-fill"
    assert line.divisor_days == 92
    assert line.projected == pytest.approx(12.34 / 92 * DAYS_PER_MONTH)


def test_a_daily_line_that_starts_mid_window_is_new(tmp_path):
    days = month_days(30)
    actuals = _route53_actuals(tmp_path, days, {d: 1.0 for d in days[15:]})

    line = _route53_report(actuals).groups[0].bill_lines[0]

    assert line.rule == "new"
    assert line.divisor_days == 15
    assert line.projected == pytest.approx(DAYS_PER_MONTH)


def test_a_daily_line_shorter_than_the_threshold_zero_fills(tmp_path):
    days = month_days(30)
    actuals = _route53_actuals(tmp_path, days, {d: 1.0 for d in days[-6:]})

    line = _route53_report(actuals).groups[0].bill_lines[0]

    assert line.rule == "zero-fill"
    assert line.divisor_days == 30


def test_a_charge_with_a_gap_after_its_first_day_zero_fills(tmp_path):
    days = month_days(30)
    amounts = {d: 1.0 for d in days[10:20]}
    actuals = _route53_actuals(tmp_path, days, amounts)

    line = _route53_report(actuals).groups[0].bill_lines[0]

    assert line.rule == "zero-fill"
    assert line.divisor_days == 30


def test_the_threshold_option_changes_the_outcome(tmp_path):
    days = month_days(30)
    actuals = _route53_actuals(tmp_path, days, {d: 1.0 for d in days[-5:]})

    default = _route53_report(actuals).groups[0].bill_lines[0]
    lowered = _route53_report(actuals, new_line_days=5).groups[0].bill_lines[0]

    assert (default.rule, default.divisor_days) == ("zero-fill", 30)
    assert (lowered.rule, lowered.divisor_days) == ("new", 5)


def test_the_threshold_must_be_positive(tmp_path):
    days = month_days(30)
    actuals = _route53_actuals(tmp_path, days, {days[0]: 1.0})

    with pytest.raises(ReconcileError):
        _route53_report(actuals, new_line_days=0)


def test_the_report_names_the_rule_in_json_and_text(tmp_path, capsys):
    days = month_days(30)
    actuals = _route53_actuals(tmp_path, days, {d: 1.0 for d in days[15:]})

    report = _route53_report(actuals)
    assert report.to_dict()["groups"][0]["billLines"][0]["divisorRule"] == "new"

    from infra_cost_model.cli import _print_reconciliation
    _print_reconciliation(report)
    assert "new, 15 days" in capsys.readouterr().out


def test_the_cli_exposes_the_threshold():
    from infra_cost_model.cli import _build_parser
    args = _build_parser().parse_args(
        ["reconcile", "m.yaml", "--actuals", "a.json", "--new-line-days", "3"])
    assert args.new_line_days == 3
