"""Verify a real public HTTP deployment using only Python's standard library.

Run against a fresh instance (below its 60-request window). --fixtures records
actual requests/responses; it does not synthesize service responses.
"""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--fixtures", type=Path)
    args = parser.parse_args()
    fixtures = {}

    def request(name, path, body=None, expected=200, method=None, headers=None):
        verb = method or ("POST" if body is not None else "GET")
        data = canonical(body) if body is not None else None
        sent_headers = {"Content-Type": "application/json"} if data is not None else {}
        sent_headers.update(headers or {})
        req = Request(args.base_url.rstrip("/") + path, data=data, method=verb, headers=sent_headers)
        try:
            response = urlopen(req, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            status = response.status
            response_headers = dict(response.headers)
        assert status == expected, (name, status, raw[:500])
        payload = json.loads(raw) if raw else None
        fixtures[name] = {"request": {"method": verb, "path": path, "body": body},
                          "status": status, "headers": response_headers, "response": payload}
        etag = response_headers.get("etag") or response_headers.get("ETag")
        if verb == "GET" and status == 200:
            assert etag == '"' + hashlib.sha256(raw).hexdigest() + '"', (name, "ETag")
        return payload, response_headers

    request("health", "/health")
    request("ready", "/ready")
    info, _ = request("info", "/v1/info")
    assert info["profile"] == "public-reference" and info["api_version"] == 1
    assert info["service_version"] == "1.2.0" and info["schema_version"] == 1
    assert info["access"]["anonymous_read"] and not info["access"]["remote_write"]
    assert info["usage_assumptions"] == {"supported": False}
    catalog, headers = request("catalog", "/v1/catalog")
    assert catalog["publisher_id"] == info["publisher_id"] != "pub_sample"
    assert catalog["content_sha256"] == hashlib.sha256(canonical(catalog["records"])).hexdigest()
    assert catalog["content_sha256"] == info["catalog"]["content_sha256"]
    assert catalog["freshness"] is None and catalog["records"]
    tag = headers.get("etag") or headers["ETag"]
    request("not_modified", "/v1/catalog", expected=304, headers={"If-None-Match": "W/" + tag})
    pinned, pinned_headers = request("pinned", "/v1/catalog?" + urlencode({"snapshot_id": catalog["snapshot_id"]}))
    assert pinned == catalog and "immutable" in (pinned_headers.get("cache-control") or pinned_headers["Cache-Control"])
    request("unknown_snapshot", "/v1/catalog?snapshot_id=snap-999999999", expected=404)
    request("internal_snapshot", "/v1/catalog?snapshot_id=snap_not_public", expected=404)
    request("oversized_snapshot", "/v1/catalog?snapshot_id=snap-" + "9" * 4301, expected=404)
    caps, _ = request("capabilities", "/v1/capabilities")
    assert caps["publisher_id"] == info["publisher_id"]
    assert caps["content_sha256"] == hashlib.sha256(canonical({"agents": caps["agents"], "model_efforts": caps["model_efforts"]})).hexdigest()
    assert caps["content_sha256"] == info["capabilities"]["content_sha256"]
    agent, _ = request("agent", "/v1/capabilities/agents?agent_id=codex")
    assert agent["can_edit_files"] is True and agent["row_version"] >= 1
    effort, _ = request("effort", "/v1/capabilities/model-efforts?" + urlencode({"provider": "openai", "model": "openai/gpt-6.1-sol", "effort": "high"}))
    assert effort["effort"] == "high" and effort["source"] == "official"
    quote = next(row for row in catalog["records"] if row["model"] == "openai/gpt-4o-mini")
    candidate = {"candidate_id": "priced", "provider": quote["provider"], "channel": quote["channel"], "model": quote["model"], "plan": quote["plan"], "feature_scope": quote["scope"]["function"], "agent_id": "codex"}
    gap = dict(candidate, candidate_id="price-gap", model="openai/gpt-6.1-sol", effort="high")
    body = {"method": "M4", "currency": "USD", "usage": {"uncached_input": "1000", "cache_read": "0", "cache_write": "0", "billed_output": "400"}, "extra_cost": "0", "candidates": [candidate, gap]}
    result, _ = request("estimate", "/v1/estimates", body)
    assert result["cost_basis"] == "public-reference"
    assert result["publisher_id"] == info["publisher_id"]
    priced, missing = result["results"]
    assert priced["status"] == "ok" and Decimal(priced["metrics"]["cost"]) == Decimal("0.00039")
    assert missing["status"] == "missing_data" and missing["capabilities"]["model_effort"]["effort"] == "high"
    assert all(row["cost_basis"] == "public-reference" and "record_snapshot_id" not in row for row in result["results"])
    no_usage = {key: value for key, value in body.items() if key != "usage"}
    missing_usage, _ = request("missing_usage", "/v1/estimates", no_usage)
    assert missing_usage["results"][0]["status"] == "missing_data"
    assert missing_usage["results"][0]["capabilities"]["agent"]["agent_id"] == "codex"
    request("m7_forbidden", "/v1/estimates", dict(body, method="M7"), expected=403)
    request("unknown_method", "/v1/estimates", dict(body, method="M99"), expected=422)
    request("write_forbidden", "/v1/contributions", {}, expected=403)
    secret = "PRIVATE_INPUT_MUST_NOT_ECHO"
    invalid, _ = request("private_rejected", "/v1/estimates", dict(body, private_rates=secret), expected=422)
    assert secret not in json.dumps(invalid)
    request("currency_required", "/v1/estimates", {k: v for k, v in body.items() if k != "currency"}, expected=422)
    request("body_limit", "/v1/estimates", {"padding": "x" * 65537}, expected=413)
    evidence, _ = request("evidence", "/v1/evidence/" + quote["sources"][0]["id"])
    assert evidence["source_url"].startswith("https://")
    request("research", "/v1/research/" + quote["research_id"])
    openapi, _ = request("openapi", "/openapi.json")
    assert "private_rates" not in json.dumps(openapi) and "securitySchemes" not in openapi.get("components", {})
    assert "/v1/contributions" not in openapi["paths"]
    for path, method in (("/v1/info", "get"), ("/v1/catalog", "get"), ("/v1/capabilities", "get"), ("/v1/estimates", "post")):
        responses = openapi["paths"][path][method]["responses"]
        assert responses["200"]["content"]["application/json"]["schema"]
        assert "500" in responses
    if args.fixtures:
        args.fixtures.mkdir(parents=True, exist_ok=True)
        for name, fixture in fixtures.items():
            (args.fixtures / (name + ".json")).write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
        (args.fixtures / "openapi.json").write_text(json.dumps(openapi, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "passed", "base_url": args.base_url, "publisher_id": info["publisher_id"], "catalog": info["catalog"], "capabilities": info["capabilities"], "checks": len(fixtures)}, indent=2))


if __name__ == "__main__":
    main()
