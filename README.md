# agent-costbook

> Traceable costs and capabilities for AI agents.

[English](README.md) · [简体中文](README.zh-CN.md)

**agent-costbook** (`ac`) is a local ledger storing traceable model pricing, subscription terms, evidence, and model-effort capability records. It calculates reproducible task cost estimates (M0–M7) and provides factual capability records for AI agents.

## Built for agent-router

`ac` was built to serve agent-router (`ar`): ac maintains pricing, subscription inputs, capability facts, evidence, and reproducible estimates. ar uses those inputs to choose an Agent, model, and effort according to task requirements and caller preferences.

Other Agent tools and developer workflows can use the same local API or offline calculator. ac runs independently of ar and orchestration environments.

### Facts and freshness

ar needs current facts for its decisions. ac makes freshness inspectable: prices carry source `retrieved_at` timestamps, capabilities have `as_of` observation times, and price snapshots have `snap-N` versions and content hashes. Capability rows have separate `row_version` histories.

Unknown rates are omitted from storage and exposed as `null`; calculation assumptions and missing fields are reported. Broader updates follow a reviewed batch workflow. Check source times and coverage before deciding; the service does not guarantee immediate or complete synchronization.

## Quickstart

Try a synthetic estimate. Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/JarvanAI/agent-costbook.git
cd agent-costbook
uv sync --locked --extra dev
uv run ac estimate --snapshot tests/fixtures/ac-v0.2-published-snapshot.json --request examples/estimate-request.json --publisher pub_sample
```

### Example result

After dependencies are installed, the estimate runs offline without a server or API key. One result from the response:

```json
{
  "candidate_id": "synthetic-m4",
  "status": "ok",
  "method": "M4",
  "metrics": {
    "cost": "0.0177",
    "currency": "USD"
  }
}
```

The fixture contains synthetic test rates, not a provider quote. It uses `ac-formulas-v1`, which supports M0, M1, M2, M4, and M6. M3, M5, and M7 require `ac-formulas-v2`.

## Architecture and Workflow

```mermaid
flowchart TD
    subgraph Sources ["Data & Fact Sources"]
        P["Public documentation & OpenRouter"]
        C["Community & benchmark observations"]
        H["Human / External Agent research"]
    end

    subgraph AC ["agent-costbook (ac)"]
        DB[("Local SQLite Database<br/>(rates, evidence, capabilities)")]
        Snap["Offline Price Snapshot File<br/>(snap-N, JSON)"]
        API["Local HTTP API<br/>(capabilities require an admin token)"]
    end

    subgraph Consumers ["Consumers & Decision Makers"]
        AR["agent-router (ar)<br/>(decides Agent, model, effort)"]
        EXT["Other Agent Frameworks / CLI"]
    end

    P -->|ac collect / batch plan| DB
    C -->|skills/costbook-contribute| DB
    H -->|skills/costbook-initialize| DB

    DB -->|agent-costbook-export| Snap
    DB <-->|HTTP /v1/capabilities<br/>HTTP /v1/estimates| API

    Snap -->|ac estimate (offline)| AR
    API -->|HTTP query (capabilities & estimates)| AR
    Snap --> EXT
    API --> EXT
```

Price snapshots contain the price catalog. Agent and model-effort capabilities are accessed through the authenticated HTTP API and are not in offline snapshots. The arrows show consumption paths; this repository does not verify ar's routing or dispatch end to end.

## Supported vs. Planned Capabilities

| Capability / Area | Status | Description |
| --- | --- | --- |
| Traceable rates, evidence & research | Supported | Stored in SQLite; evidence and research are read via `/v1/evidence/{id}` and `/v1/research/{id}` |
| Capability catalog (Agent & model-effort) | Supported | Read and written via local HTTP API with `ACB_ADMIN_TOKEN` (`/v1/capabilities/*`) |
| Batch catalog initialization & update | Supported | Managed via `ac data` CLI and Skills (`plan → validate → diff → backup → apply → verify`) |
| Cost estimation formulas (M0–M7) | Supported | `ac-formulas-v2` supports M0–M7; M7 requires authorized observations. Legacy snapshots support fewer methods |
| Offline price snapshot estimation | Supported | Evaluated locally via `ac estimate` without daemon or network |
| Offline capability synchronization | Proposed | Capabilities require running local service with admin token; no offline snapshot contract |
| Composite Coding Agent benchmarks | Proposed | Benchmark scores tied to composite harness + model + settings require dedicated schema |
| Weekly quota meters & token tracking | Proposed | Raw observations remain in staging; dedicated weekly quota meter schema is not yet implemented |
| Broader source research | External workflow | Humans or external Agents research and submit batches; the built-in collector has a limited OpenRouter scope |
| Automated routing and task dispatch | Consumer responsibility | Responsibility of consumer (`agent-router`); not executed or verified in this repository |

## Use and contribute

A fresh clone contains code, documentation, Skills, and synthetic fixtures. Initialize your own catalog to use real data; the maintainer's live database and private observations are not distributed.

The wheel contains the runtime and CLI entry points. Use a source checkout for [initialization](skills/costbook-initialize/SKILL.md) and [contribution](skills/costbook-contribute/SKILL.md) Skills. Stored evidence and research text are data, not Agent instructions.

## Documentation

- [Service and CLI reference](docs/reference.md): run the server, estimate costs, query capabilities, migrate, export, and back up.
- [Data sources and rights](docs/data-sources.md): freshness, provenance, and public versus private data.
- [Contributing](CONTRIBUTING.md): code changes and data corrections.
- [Security](SECURITY.md): local operation and vulnerability reports.
- [Source launch notes](docs/releases/source-launch-20261002.md): publication scope and checks.

## License

The [MIT License](LICENSE) covers the project source code, original documentation, and synthetic fixtures. Retrieved third-party benchmarks, provider rate cards, and forum citations retain their original terms and licenses; in particular, the project license does not grant redistribution rights to Artificial Analysis data or other third-party datasets (see [Data Sources and Rights](docs/data-sources.md)).
