# Data sources, freshness, and rights

[README](../README.md) · [中文首页](../README.zh-CN.md)

agent-costbook stores facts with evidence so that a consumer such as
agent-router can inspect the basis of
an Agent, model, or effort decision. A fresh clone includes synthetic examples;
it does not include the maintainer's running database or a complete live catalog.

## Sources and updates

Sources include provider documentation, OpenRouter data, community contributions,
third-party benchmark pages, and user observations. Official documentation is the
basis for callable model identifiers, supported effort settings, and billing
conditions. Community and forum material is separately attributed; it does not
silently become an official capability, quota multiplier, or recommendation.

The built-in collector has a limited OpenRouter scope documented in the
[reference](reference.md). Broader research is performed by a human or external
Agent using the [initialization workflow](../skills/costbook-initialize/SKILL.md).
That workflow checks a reviewed batch, backs up the database, applies changes,
and verifies them. It does not run a general web crawler or guarantee complete
provider coverage.

Price freshness follows source `retrieved_at`; capability observations have
`as_of`. Consumers should inspect those times, source references, known gaps,
and evaluation conditions. Republishing an old source does not refresh it.
Offline `stale` needs both a source timestamp and an explicit `--now` value.
Keep publisher, snapshot version, and content hash when comparing estimates.
Capability rows have their own history and versions; they are outside the public
price snapshot and have no supported offline synchronization contract.

## Cost meanings

Report added cash, subscription quota use, and completion time separately when
those observations exist. Subscription amortization and API-equivalent amounts
are distinct from added cash. The service does not assign them an automatic
exchange rate. Benchmark-based capability weights are optional linear proxies,
not measured success probabilities. Task-result observations are a separate
private input; missing observations stay missing.

## What the license covers

The [MIT license](../LICENSE) covers project code, original documentation, and
project-authored synthetic examples. Retrieved third-party content and datasets
retain their own terms. Source labels and citations do not grant redistribution
permission, and public accessibility alone is not a project license grant.

In particular, this repository's license does not cover Artificial Analysis's
data. Public research notes explain acquisition methods and field limitations;
their small examples are not a redistributable AA catalog. Raw datasets and the
maintainer's live database are not bundled with the source release. Before
publishing a data export, review that export's sources and applicable terms
independently of the code license.

Private account and quota observations belong in a local instance or the ignored
`data/private/observations/` staging folder. Current weekly-quota observations and
Coding Agent combinations need dedicated storage resources; staging files must
not be described as database records. Never submit private data as a public
fixture, issue attachment, snapshot, or research example.

## Research references

- [AA acquisition methods](research/aa-data-acquisition.md)
- [Storage findings](research/aa-storage-findings.md)
- [Catalog refresh methods](research/catalog-refresh-20261001.md)
- [Combination benchmark proposal](design/benchmark-records-extension.md)
- [Initialization and update design](design/catalog-initialization-update.md)
