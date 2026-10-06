# Contributing

Code, documentation, data corrections, and reproducible research are welcome.
Use [GitHub issues](https://github.com/JarvanAI/agent-costbook/issues) for questions
and proposals, and pull requests for changes. The project is maintained through
the JarvanAI repository. Vulnerability reports follow [SECURITY.md](SECURITY.md).

## Issue templates

When reporting problems or proposing corrections, use the structured issue templates:
- **First-run failure**: For setup, demo, CLI execution, or installation errors.
- **Data correction**: For updating outdated or incorrect pricing, capability facts, or benchmarks with public sources.

> [!CAUTION]
> Never include authentication tokens (`ACB_ADMIN_TOKEN`, `ACB_READ_TOKEN`), account credentials, or private subscription screenshots in public issues or pull requests.

## Develop and verify

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required.

```sh
git clone https://github.com/JarvanAI/agent-costbook.git
cd agent-costbook
git switch -c your-change
uv sync --locked --extra dev
uv run pytest -q
uv build
uv run python scripts/check-docs.py
uv run python scripts/check-demo.py
```

Keep a pull request focused on one problem. Explain its observable behavior,
the checks you ran, and any compatibility or data-migration impact. Commit
subjects should describe the change; prefixes such as `docs:`, `fix:`, and
`feat:` are useful but not enforced. Maintainers review and merge pull requests.
Discuss a new schema or public API before implementing it. New behavior should
have a focused test; documentation changes should have working links and examples.
Keep English and Chinese README commands and support claims consistent.

## Contribute facts and research

Facts need a source, a retrieval time, an identity, units, and the conditions
under which they apply. For example, a rate may depend on the provider endpoint,
region, prompt length, cache write duration, plan, currency, or effort. An API
default, an evaluation setting, and a router recommendation are separate facts.

Use official documentation to confirm callable model identifiers and supported
settings. Forum discussions and measurements can provide additional observations;
label their origin and uncertainty. Preserve source precision and unknown values.
Do not turn an unknown price into zero, infer a success rate from a benchmark
score, or treat a coding harness score as a model-only score.

For public submissions, propose a correction with links and an explanation, or
submit a source adapter and synthetic fixture in a pull request. Check
[data rights](docs/data-sources.md) before attaching any third-party material.
Public research methods belong in `docs/research/` and should be linked from
[AGENTS.md](AGENTS.md) when useful for later Agents.

To update your own running instance, follow the
[catalog maintenance guide](docs/guides/catalog-maintenance.md) and
[initialization Skill](skills/costbook-initialize/SKILL.md). The
[contribution Skill](skills/costbook-contribute/SKILL.md) defines field shapes.
The existing sequence is `plan → validate → diff → review → backup → apply → verify`.
An Agent can carry out a previously authorized scope; the plan hash binds the
reviewed target, and repeated authorized updates do not need another permission
request. Check the receipt for deferred and unsupported rows as well as successes.

Private subscriptions, account identifiers, credentials, database copies, raw
restricted datasets, and operator receipts must stay out of public issues and
pull requests. Coding Agent combination records and private weekly-quota meters
are currently schema proposals, not supported batch categories.

## Licensing

Project code and original documentation are distributed under [MIT](LICENSE).
Submit only material you are entitled to contribute. Third-party data retains
its own terms; the code license does not grant redistribution rights to a dataset.
