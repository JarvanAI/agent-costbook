# Open-source source launch

Human-approved on 2026-10-02. This is a documentation, packaging, and publication
change; the service's runtime behavior and version stay unchanged.

## Approved core

- Positioning: an Agent cost and capability book.
- Purpose: provide current, traceable facts for agent-router's Agent/model
  selection decisions; ar also decides effort. ac is independently consumable.
- Tagline: “Traceable costs and capabilities for AI agents.” /
  “让 Agent 的成本与能力有据可查。”
- English README plus a Chinese README; MIT for project code and original
  documentation, with third-party data rights explained separately.
- First publication: GitHub source, documentation, contribution workflow,
  and synthetic demo. No runtime version change or hosted-service launch.

Freshness is a property consumers inspect through source times, versions, and
known gaps. Neither the tagline nor “current facts” guarantees full coverage,
immediate synchronization, or verified consumer dispatch.

## Execution and checks

1. Preserve the existing detailed README as `docs/reference.md` and write the
   short bilingual entry points, including the ac/ar relationship.
2. Add LICENSE, contribution/security/data-source guides and source-launch notes.
3. Add license metadata, public source-distribution files, packaging checks,
   and minimal CI for tests, build, relative links, and the synthetic demo.
4. Review the diff and reachable history for private runtime artifacts and
   credential material, run the checks, commit and push.
5. Publish the GitHub repository and verify its visibility and reporting route.

The short documentation task uses the existing checkout. Simple work prefers
agy; unavailable agy falls back to Codex 6-Luna MAX. Packaging/CI work uses
Codex 6.1-Sol. Worker files have separate owners; root validates the combined
result and publishes it. No new product testing framework or AO dependency is
introduced.

Background and alternatives: [README/open-source research](../research/open-source-readme-20261002.md).
