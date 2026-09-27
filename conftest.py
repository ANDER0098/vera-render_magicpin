"""Root pytest configuration.

``tests/test_contract.py`` is the frozen HTTP-contract suite owned by the
server lane (it imports the ``bot`` / ``app`` FastAPI module, which does not
exist in this worktree yet). This conftest skips that file until the server
module lands — and prints a LOUD warning every time it does, so a skip can
never silently read as a pass. ``tools/check_contract_ran.py`` is the server
lane's final gate against exactly that failure mode.
"""

import importlib.util
import os

_COLLECT_IGNORE = []

_here = os.path.dirname(__file__)
_contract = os.path.join(_here, "tests", "test_contract.py")
if os.path.exists(_contract):
    has_bot = importlib.util.find_spec("bot") is not None
    has_app = importlib.util.find_spec("app") is not None
    if not (has_bot or has_app):
        _COLLECT_IGNORE.append(_contract)
        print("=" * 78)
        print("SKIPPED tests/test_contract.py — no bot.py/app.py yet; "
              "this MUST run once the server lane lands")
        print("=" * 78)

collect_ignore = _COLLECT_IGNORE
