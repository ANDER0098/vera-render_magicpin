"""Final merge gate: rerun the frozen contract suite and reject any skip."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.setdefault("BOT_MODULE", "bot")
    with tempfile.TemporaryDirectory(prefix="contract-pytest-", dir=root) as temp:
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                              f"--basetemp={temp}", "tests/test_contract.py"],
                             cwd=root, text=True, capture_output=True, env=env)
    output = run.stdout + run.stderr
    print(output, end="")
    summary = re.search(r"(\d+) passed(?:,\s*(\d+) skipped)?(?:,\s*(\d+) failed)?", output)
    if run.returncode or "SKIPPED tests/test_contract.py" in output:
        print("CONTRACT GATE FAILED: the contract suite skipped or failed.", file=sys.stderr)
        return 1
    if not summary or int(summary.group(1)) == 0 or int(summary.group(2) or 0) != 0:
        print("CONTRACT GATE FAILED: no passing contract tests were reported.", file=sys.stderr)
        return 1
    print(f"CONTRACT GATE PASSED: {summary.group(1)} tests ran; zero skips.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
