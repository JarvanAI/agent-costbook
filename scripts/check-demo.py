"""Verify the published synthetic example without local data or credentials."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
run = subprocess.run(
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
result = json.loads(run.stdout)
assert result["publisher_id"] == "pub_sample", result
assert len(result["results"]) == 1, result
estimate = result["results"][0]
assert estimate["candidate_id"] == "synthetic-m4", estimate
assert estimate["status"] == "ok", estimate
assert estimate["metrics"] == {"cost": "0.0177", "currency": "USD"}, estimate
print("Synthetic demo verified: synthetic-m4 costs 0.0177 USD")
