"""The model's engine requirement, `requiresEngine`.

A cost model may name the engine version it needs::

    version: "1.0"
    requiresEngine: ">=0.2.0"

Without that pin an engine too old to interpret the model prices it anyway.
The failure is quiet and the number looks reasonable: a metric using a SaaS
``shape:`` (issue #241) prices at $0 on an engine with no shape dispatch, the
catalog finds no row, and the run reports a total and exits 0. The model's
author has no way to tell the total lost a term.

The pin turns that into a refusal. The engine checks it in
:meth:`CostEngine.compute`, so every caller hits the same gate: the CLI,
the SDK, and any script that builds an engine. A mismatch raises
:class:`EngineRequirementError`, which subclasses ``ValueError`` because that
is what the CLI and the SDK already catch.

The check is a PEP 440 specifier evaluated with ``packaging``, the library pip
itself uses. It is a declared dependency of this package, so the engine and
the CLI can import it on a minimal install.

A model that declares no pin behaves exactly as it did before, which keeps
every existing model working.
"""

from __future__ import annotations

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


#: Top-level model key carrying the requirement. Spelled in the same
#: lowerCamelCase as the rest of the cost model representation.
ENGINE_REQUIREMENT_KEY = "requiresEngine"

#: Where the upgrade hint installs the engine from. The package isn't on
#: PyPI, so the hint gives the git repository rather than a bare name (#265).
INSTALL_SOURCE = "git+https://github.com/elecnix/infra-cost-model"


class EngineRequirementError(ValueError):
    """The running engine does not satisfy the model's `requiresEngine`."""


def engine_version() -> str:
    """Return the version of the running engine.

    Reads ``infra_cost_model.__version__`` rather than the installed
    distribution metadata, so an editable checkout reports the source it is
    actually running.
    """
    from infra_cost_model import __version__

    return __version__


def declared_requirement(model: dict) -> object | None:
    """Return the model's raw `requiresEngine` value, or None when absent."""
    if not isinstance(model, dict):
        return None
    return model.get(ENGINE_REQUIREMENT_KEY)


def _requirement_error(requirement: object, running: object, detail: str) -> str:
    """Build the refusal message for a pin the running engine cannot meet."""
    return (
        f"Model requires engine {requirement!r} but this engine is {running}. "
        f"{detail} Upgrade the engine with "
        f"`pip install -U \"{INSTALL_SOURCE}\"`, or lower the model's "
        f"`{ENGINE_REQUIREMENT_KEY}`."
    )


def check_engine_requirement(model: dict, running: str | None = None) -> str | None:
    """Check the model's `requiresEngine` against the running engine.

    Args:
        model: The cost model representation.
        running: Engine version to test against. Defaults to the running
            engine's own version; tests inject a value here.

    Returns:
        None when the model declares no pin or the pin holds. Otherwise a
        message naming the requirement, the running version, and the remedy.

    A malformed pin yields a message rather than an exception. A typo in the
    model is the model's problem, and a traceback from inside the engine hides
    which line of the model to fix.
    """
    requirement = declared_requirement(model)
    if requirement is None:
        return None

    running = engine_version() if running is None else running

    if not isinstance(requirement, str):
        return _requirement_error(
            requirement, running,
            f"`{ENGINE_REQUIREMENT_KEY}` must be a PEP 440 specifier string, "
            f"such as \">=0.2.0\".",
        )

    try:
        specifier = SpecifierSet(requirement)
    except InvalidSpecifier:
        return _requirement_error(
            requirement, running,
            f"`{ENGINE_REQUIREMENT_KEY}` is not a valid PEP 440 specifier.",
        )

    try:
        Version(running)
    except InvalidVersion:
        return _requirement_error(
            requirement, running,
            "The running engine's version is not a valid PEP 440 version, so "
            "the requirement cannot be checked.",
        )

    # `prereleases=True` so a 0.3.0.dev0 build satisfies `>=0.2.0`. The pin
    # names a feature set, and a pre-release of a newer version carries it.
    if not specifier.contains(running, prereleases=True):
        return _requirement_error(
            requirement, running,
            "This engine cannot price the model reliably: fields it does not "
            "know about are dropped or priced at $0 without a warning.",
        )

    return None


def require_engine(model: dict, running: str | None = None) -> None:
    """Raise :class:`EngineRequirementError` when the pin does not hold."""
    error = check_engine_requirement(model, running)
    if error is not None:
        raise EngineRequirementError(error)
