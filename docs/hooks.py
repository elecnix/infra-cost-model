"""MkDocs hook that builds the documentation site's pages (#243).

The site has no hand-copied pages. It publishes the Markdown files at the
repository root under their own names, so their links to each other work both
on GitHub and on the site. It generates the command reference from the
argparse parser in infra_cost_model/cli.py, and one page for each example
model and each vendor's pricing notes.

The functions above the MkDocs event handlers import nothing from MkDocs, so
the test suite can check the pages without the docs extra installed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

REPO_URL = "https://github.com/elecnix/infra-cost-model"

# Root documents, in navigation order, with their navigation titles.
ROOT_PAGES = {
    "README.md": "Home",
    "DESIGN_PRINCIPLES.md": "Design principles",
    "UBIQUITOUS_LANGUAGE.md": "Glossary",
}
CONTRIBUTING_PAGE = "CONTRIBUTING.md"
CLI_PAGE = "cli.md"

# A fixed width, so the reference reads the same whatever terminal builds it.
HELP_WIDTH = 88


def _fixed_width(parser: argparse.ArgumentParser) -> None:
    formatter_class = parser.formatter_class
    parser.formatter_class = lambda prog: formatter_class(prog, width=HELP_WIDTH)


def cli_reference_markdown() -> str:
    """Return the command reference page, built from the argparse parser."""
    from infra_cost_model.cli import _build_parser

    parser = _build_parser()
    _fixed_width(parser)
    lines = [
        "# Command reference",
        "",
        "The `infra-cost-model` command takes one subcommand. The text below is "
        "the `--help` output of each one, generated from the command's parser "
        "when the site is built.",
        "",
        "```text",
        parser.format_help().rstrip(),
        "```",
    ]
    for action in parser._subparsers._group_actions:
        for name, subparser in action.choices.items():
            _fixed_width(subparser)
            lines += [
                "",
                f"## `{name}`",
                "",
                "```text",
                subparser.format_help().rstrip(),
                "```",
            ]
    return "\n".join(lines) + "\n"


def _example_title(text: str, stem: str) -> str:
    first = next(iter(text.splitlines()), "")
    if not first.startswith("#"):
        return stem
    title = first.lstrip("#").strip()
    return title.removeprefix("Example:").strip() or stem


def example_page(path: Path, text: str | None = None) -> str:
    """Return the page for one example model."""
    if text is None:
        text = path.read_text()
    rel = f"examples/{path.name}"
    return "\n".join([
        f"# {_example_title(text, path.stem)}",
        "",
        f"Source: [`{rel}`]({REPO_URL}/blob/main/{rel})",
        "",
        "To check and price this model from a clone of the repository:",
        "",
        "```bash",
        f"infra-cost-model validate {rel}",
        f"infra-cost-model compute --time-basis monthly {rel}",
        "```",
        "",
        "````yaml",
        text.rstrip("\n"),
        "````",
        "",
    ])


def _examples(root: Path) -> dict[str, tuple[Path, str]]:
    """Return each example model's page path, file and text."""
    return {
        f"examples/{path.stem}.md": (path, path.read_text())
        for path in sorted((root / "examples").glob("*.yaml"))
    }


def _vendor_notes(root: Path) -> dict[str, Path]:
    """Return each vendor pricing note's page path and file."""
    notes = (root / "infra_cost_model" / "vendors").glob("*/pricing-notes.md")
    return {f"vendors/{path.parent.name}.md": path for path in sorted(notes)}


def generated_pages(root: Path) -> dict[str, str]:
    """Return every page of the site, keyed by its path in the site."""
    pages = {name: (root / name).read_text() for name in ROOT_PAGES}
    pages[CONTRIBUTING_PAGE] = (root / CONTRIBUTING_PAGE).read_text()
    pages[CLI_PAGE] = cli_reference_markdown()
    for uri, (path, text) in _examples(root).items():
        pages[uri] = example_page(path, text)
    for uri, path in _vendor_notes(root).items():
        pages[uri] = path.read_text()
    return pages


def navigation(root: Path) -> list:
    """Return the site navigation, which lists every page of generated_pages."""
    nav: list = [{title: uri} for uri, title in ROOT_PAGES.items()]
    nav.append({"Command reference": CLI_PAGE})
    nav.append({"Examples": [
        {_example_title(text, path.stem): uri}
        for uri, (path, text) in _examples(root).items()
    ]})
    nav.append({"Vendor pricing notes": [
        {Path(uri).stem: uri} for uri in _vendor_notes(root)
    ]})
    nav.append({"Contributing": CONTRIBUTING_PAGE})
    return nav


def _root(config) -> Path:
    return Path(config.config_file_path).resolve().parent


# MkDocs event handlers.


def on_config(config):
    config.nav = navigation(_root(config))
    return config


def on_files(files, config):
    from mkdocs.structure.files import File

    for uri, content in generated_pages(_root(config)).items():
        files.append(File.generated(config, uri, content=content))
    return files


def on_serve(server, config, builder):
    root = _root(config)
    for path in [*(root / name for name in [*ROOT_PAGES, CONTRIBUTING_PAGE]),
                 root / "examples", root / "infra_cost_model"]:
        server.watch(str(path))
    return server
