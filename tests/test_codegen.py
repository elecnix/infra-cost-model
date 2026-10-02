"""Regression guard: the dead IaC codegen printer is gone (#430).

`infra_cost_model/codegen/` generated resource handler classes from
`terraform providers schema -json`, but nothing consumed the result: the CLI
printed it to stdout, no file was written, nothing was checked in, and no CI
job ran it. Where its output was inspected it was also wrong -- it derived
usage metrics from Terraform *attributes* (`memorySize`, `runtime`) instead of
consumption metrics, guessed `node_type`/`service` from resource type names, and
omitted `catalog_metrics`, `catalog_metrics_for`, `derive_catalog_usage` and
`extract_arm`.

DP#10 therefore remains unimplemented. Deleting the printer keeps that honest.
The one generation seam that works -- cost-model.schema.json ->
sdk/ts/src/types.generated.ts -- is unaffected and is drift-checked in CI.

This guard fails if the module or the CLI subcommand is ever reintroduced without
being made load-bearing (written to disk, registered, drift-checked).
"""

import subprocess
import sys
from pathlib import Path

import pytest

import infra_cost_model


PACKAGE_DIR = Path(infra_cost_model.__file__).parent


def test_codegen_package_directory_is_absent():
    assert not (PACKAGE_DIR / "codegen").exists()


@pytest.mark.parametrize("module_name", ["__init__.py", "generator.py", "schema_reader.py"])
def test_codegen_module_files_are_absent(module_name):
    assert not (PACKAGE_DIR / "codegen" / module_name).exists()


def test_cli_help_has_no_codegen_subcommand():
    result = subprocess.run(
        [sys.executable, "-m", "infra_cost_model.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "codegen" not in result.stdout


def test_cli_rejects_codegen_subcommand():
    result = subprocess.run(
        [sys.executable, "-m", "infra_cost_model.cli", "codegen", "schema.json"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0