# GitHub source launch — 2026-10-02

agent-costbook provides traceable costs and capabilities for AI agents. It was
built to serve agent-router: ac maintains pricing, subscription inputs,
capability facts and evidence; ar uses those inputs to choose an Agent, model,
and effort according to a caller's preferences. Other tools can use the same
local HTTP API and CLI without depending on ar or an orchestration environment.

This source launch documents the existing 1.0.0 package. It does not create a
new runtime version or a package-registry release. The release scope is the
GitHub source, original documentation, Skills, and synthetic examples.

The English and Chinese README introduce a runnable offline estimate before
the detailed [service reference](../reference.md). Code uses the MIT license;
[source and data rights](../data-sources.md) describe the separate treatment of
third-party evidence and private observations. [Contributing](../../CONTRIBUTING.md)
covers code changes and research-backed data corrections.

The source build includes public documentation and Skills. The wheel contains
the Python runtime, command entry points, and license; install Skills from the
source checkout. Local databases, environment files, and operator records stay
outside these distributions.

## Validation

Run from the source checkout:

```sh
uv sync --locked --extra dev
uv run pytest -q
uv build
uv run python scripts/check-docs.py
uv run python scripts/check-demo.py
```

The synthetic demo returns `status: ok` with an M4 estimate of `0.0177 USD`.
Its fixture is a recorded synthetic price snapshot using `ac-formulas-v1`;
it is not a current provider quote.

## Follow-up scope

Independent Coding Agent evaluations, private weekly-quota meters, broader
source adapters, and offline capability synchronization remain separate work.
Automatic routing and dispatch are consumer responsibilities and are not
verified by this repository's tests. Review source times and missing fields
before using a catalog for a live decision.
