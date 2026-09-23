"""The TypeScript SDK and the Python engine carry the same version.

A release tags one commit for both, and `v0.2.0` went out with the TypeScript
package still at 0.1.0. This test fails when the two drift apart.
"""

import json
from pathlib import Path

from infra_cost_model import __version__

PACKAGE_JSON = Path(__file__).resolve().parent.parent / "sdk" / "ts" / "package.json"


def test_ts_sdk_version_matches_engine():
    ts_version = json.loads(PACKAGE_JSON.read_text())["version"]
    assert ts_version == __version__, (
        f"sdk/ts/package.json is {ts_version} but infra_cost_model.__version__ "
        f"is {__version__}. Set both to the same version."
    )
