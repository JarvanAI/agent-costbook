"""Verify synthetic demo evaluation without local data or credentials."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 1. Compatibility check: verify offline evaluation against legacy snapshot fixture
run_legacy = subprocess.run(
    [
        "uv", "run", "--locked", "ac", "estimate",
        "--snapshot", "tests/fixtures/ac-v0.2-published-snapshot.json",
        "--request", "examples/estimate-request.json",
        "--publisher", "pub_sample",
    ],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=True,
)
result_legacy = json.loads(run_legacy.stdout)
assert result_legacy["publisher_id"] == "pub_sample", result_legacy
assert len(result_legacy["results"]) == 1, result_legacy
estimate_legacy = result_legacy["results"][0]
assert estimate_legacy["candidate_id"] == "synthetic-m4", estimate_legacy
assert estimate_legacy["status"] == "ok", estimate_legacy
assert estimate_legacy["metrics"] == {"cost": "0.0177", "currency": "USD"}, estimate_legacy
print("Legacy snapshot estimate verified: synthetic-m4 costs 0.0177 USD")

# 2. Built-in demo command check: verify zero-config ac demo
run_demo = subprocess.run(
    ["uv", "run", "--locked", "ac", "demo"],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=True,
)
result_demo = json.loads(run_demo.stdout)
assert result_demo.get("publisher_id") == "pub_sample", result_demo
assert result_demo.get("synthetic") is True, result_demo
assert len(result_demo.get("results", [])) == 1, result_demo
estimate_demo = result_demo["results"][0]
assert estimate_demo.get("candidate_id") == "synthetic-m4", estimate_demo
assert estimate_demo.get("status") == "ok", estimate_demo
assert estimate_demo.get("metrics") == {"cost": "0.0177", "currency": "USD"}, estimate_demo
print("Built-in ac demo verified: synthetic-m4 costs 0.0177 USD")
