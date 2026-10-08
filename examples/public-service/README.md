# Actual public HTTP fixtures

These files were captured from the real 1.2.0 public application and its reviewed
packaged database on 2026-10-08. Each fixture includes the request, HTTP status,
headers and parsed response. `openapi.json` is the actual served OpenAPI document.
Synthetic privacy/body-limit inputs are deliberately planted test data, not user
account information. No model API was called.

`estimate.json` contains a priced GPT-4o mini result and a GPT-6.1 Sol price gap
with independently available high-effort capability facts. `missing_usage.json`
shows the real engine's missing-data result. The public profile reports reference
costs, not personal invoices or available subscription quota.

Regenerate from a fresh local or Docker instance (the service limits each instance
to 60 requests per minute):

```sh
python3 scripts/check-public-service.py http://127.0.0.1:8080 --fixtures examples/public-service
```

The checker uses only Python's standard library and HTTP. Use the real HTTPS base
URL for remote verification once deployment is authorized. The recorded local
responses are not evidence of a deployed cloud service.

See the [contract](../../docs/design/public-service-1.2/spec.md) and
[reviewed source coverage](../../docs/research/public-catalog-20261008.md).
