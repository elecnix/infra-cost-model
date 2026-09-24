"""Tests for the pages the documentation site generates (#243).

The site's MkDocs hook, docs/hooks.py, builds the command reference from the
argparse parser and one page for each example model and vendor pricing note.
These tests load the hook without MkDocs, so the dev extra is enough to run
them.
"""

import importlib.util
from pathlib import Path

from infra_cost_model.cli import _build_parser

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_hooks():
    spec = importlib.util.spec_from_file_location(
        "docs_hooks", REPO_ROOT / "docs" / "hooks.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _subcommands():
    parser = _build_parser()
    for action in parser._subparsers._group_actions:
        return dict(action.choices)
    raise AssertionError("the parser has no subcommands")


def test_cli_reference_documents_every_subcommand():
    hooks = _load_hooks()
    page = hooks.cli_reference_markdown()
    for name, subparser in _subcommands().items():
        assert f"## `{name}`" in page
        for action in subparser._actions:
            for option in action.option_strings:
                assert option in page, f"{name}: {option} missing"


def test_cli_reference_does_not_depend_on_terminal_width(monkeypatch):
    hooks = _load_hooks()
    monkeypatch.setenv("COLUMNS", "40")
    narrow = hooks.cli_reference_markdown()
    monkeypatch.setenv("COLUMNS", "200")
    wide = hooks.cli_reference_markdown()
    assert narrow == wide


def test_every_example_model_gets_a_page():
    hooks = _load_hooks()
    pages = hooks.generated_pages(REPO_ROOT)
    for path in sorted((REPO_ROOT / "examples").glob("*.yaml")):
        uri = f"examples/{path.stem}.md"
        assert uri in pages
        assert path.read_text() in pages[uri]
        assert pages[uri].startswith("# ")


def test_example_title_comes_from_the_leading_comment():
    hooks = _load_hooks()
    pages = hooks.generated_pages(REPO_ROOT)
    first_line = pages["examples/serverless-api.md"].splitlines()[0]
    assert first_line == "# Serverless API (Terraform)"


def test_every_vendor_pricing_note_gets_a_page():
    hooks = _load_hooks()
    pages = hooks.generated_pages(REPO_ROOT)
    notes = sorted(
        (REPO_ROOT / "infra_cost_model" / "vendors").glob("*/pricing-notes.md")
    )
    assert notes
    for path in notes:
        assert pages[f"vendors/{path.parent.name}.md"] == path.read_text()


def test_root_documents_keep_their_names():
    hooks = _load_hooks()
    pages = hooks.generated_pages(REPO_ROOT)
    for name in (
        "README.md",
        "DESIGN_PRINCIPLES.md",
        "UBIQUITOUS_LANGUAGE.md",
        "CONTRIBUTING.md",
    ):
        assert pages[name] == (REPO_ROOT / name).read_text()
