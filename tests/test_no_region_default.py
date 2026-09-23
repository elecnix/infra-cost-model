"""Cost helpers must not default the region (issue #164, Design Principle 6).

The region comes from node metadata. A helper that falls back to a fixed
region prices the wrong place when a caller forgets to pass one, and the
result looks plausible. PR #171 removed these defaults, and later handlers
added them back. This test inspects every function so a new default fails CI.
"""
import importlib
import inspect
import pkgutil

import infra_cost_model.resources as resources_pkg

# aws_fallback_prices syncs the AWS Price List API into the local cache. Its
# region argument selects which price rows to download, like an API client
# setting. It does not price a node, so a default there cannot misprice one.
EXCLUDED = {
    "infra_cost_model.pricing.sources.aws_pricing.aws_fallback_prices",
}

EXTRA_MODULES = ["infra_cost_model.pricing.sources.aws_pricing"]


def _modules():
    names = [
        f"{resources_pkg.__name__}.{info.name}"
        for info in pkgutil.iter_modules(resources_pkg.__path__)
    ]
    return [importlib.import_module(n) for n in names + EXTRA_MODULES]


def _functions(module):
    for _, obj in inspect.getmembers(module):
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            yield f"{module.__name__}.{obj.__qualname__}", obj
        elif inspect.isclass(obj) and obj.__module__ == module.__name__:
            for _, member in inspect.getmembers(obj):
                func = getattr(member, "__func__", member)
                if inspect.isfunction(func) and func.__module__ == module.__name__:
                    yield f"{module.__name__}.{func.__qualname__}", func


def test_no_cost_helper_defaults_region():
    offenders = []
    for module in _modules():
        for name, func in _functions(module):
            if name in EXCLUDED:
                continue
            param = inspect.signature(func).parameters.get("region")
            if param is not None and param.default is not inspect.Parameter.empty:
                offenders.append(f"{name} (region={param.default!r})")
    assert not offenders, (
        "Region must come from node metadata, not a default (#164):\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_exclusions_still_exist():
    """An exclusion for a function that no longer exists should be removed."""
    for name in EXCLUDED:
        module_name, _, attr = name.rpartition(".")
        assert hasattr(importlib.import_module(module_name), attr), name
