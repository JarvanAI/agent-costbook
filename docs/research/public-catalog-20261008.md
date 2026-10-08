# 2026-10-08 首份公开目录快照

这是 agent-costbook 的第一份公开数据快照。2026-10-08，维护者授权把已经核对的公开来源编成可随包分发的 SQLite 与 manifest。授权边界就是这一份发行物：官方页面、自写短摘要，以及 [OpenRouter models API](https://openrouter.ai/api/v1/models) 在 `2026-10-08T13:58:41+08:00` 返回的通道报价。私有额度文件、账号密钥、网页全文和 Artificial Analysis 数值不在这份快照里。审核通过只说明行可以进入公开包；它不授予厂商页面或第三方数据的再分发许可。权利边界见 [数据来源说明](../data-sources.md)。服务契约见 [公共服务规格](../design/public-service-1.2/spec.md) 与 [执行计划第 3 节](../design/public-service-1.2/exec-plan.md)。

构建读取已冻结的 [source.json](../../src/agent_costbook/public_catalog/source.json)，不在构建时重新抓取来源。`reviewed_at` 固定为 `2026-10-08T14:31:00+08:00`。

## 发行物

| 项 | 值 |
| --- | --- |
| 数据库 | [catalog.sqlite3](../../src/agent_costbook/public_catalog/catalog.sqlite3) |
| manifest | [manifest.json](../../src/agent_costbook/public_catalog/manifest.json) |
| `publisher_id` | `pub_be58824dab79454fa9760104ad277f01` |
| 公开 snapshot | `snap-1`（`data_version` 1，内部 id `snap_b393738772fe43aaaf0e8c5cfd8dfd2f`） |
| `database_sha256` | `40b6bb14a815e8f5ff792ec051aa026bb44658edb7fef8f246eaed50d0f9d6d2` |
| schema | SQLite `user_version` 2，`journal_mode=DELETE`，无 WAL/SHM |
| 覆盖 | 价格 17，Agent 5，model-effort 33 |
| 排除 | 24 条，其中 12 条是无法表达的分档价格 |
| 公式 | `ac-formulas-v2`，`export_generation` 2 |

价格身份是 `channel=openrouter`、`plan=payg`、`feature_scope=text`、`currency=USD`、`effort=""`。这些是 OpenRouter 通道标价，按原始每 token 价格用 `Decimal` 乘 `1000000` 得到每百万 token 的十进制字符串。未知费率保持 null。

`.gitignore` 继续忽略 `*.sqlite3`，只为这一条公开库增加例外 `!src/agent_costbook/public_catalog/catalog.sqlite3`。`/data/private/` 与其他 SQLite 仍被忽略。

首次 UUID 在这次编译时生成一次并随文件提交。运行时和镜像构建不新建 publisher 或行 id。以后更新必须从这份 SQLite 复制，再用 `scripts/build-public-catalog.py --update-from` 写出新文件；相同内容的从零重建会得到另一套 id，不能当作同一版本。目标文件已存在时构建直接失败。校验通过之后才把临时文件替换成发行物。

## 可计算的文本价格

下表单位是 USD / 1M tokens。`cache_write` 为空表示源里没有单一可写入的 cache write 价。

| model | context | input | output | cache read | cache write |
| --- | ---: | ---: | ---: | ---: | ---: |
| `openai/gpt-4o-mini` | 128000 | 0.15 | 0.6 | 0.075 | |
| `openai/gpt-5.4-mini` | 400000 | 0.75 | 4.5 | 0.075 | |
| `openai/gpt-5.2-codex` | 400000 | 1.75 | 14 | 0.175 | |
| `openai/gpt-5.1-codex` | 400000 | 1.25 | 10 | 0.13 | |
| `openai/gpt-5` | 400000 | 1.25 | 10 | 0.125 | |
| `openai/o3` | 200000 | 2 | 8 | 0.5 | |
| `openai/o4-mini` | 200000 | 1.1 | 4.4 | 0.275 | |
| `anthropic/claude-opus-5.5` | 1000000 | 4 | 20 | 0.2 | |
| `anthropic/claude-sonnet-5.5` | 1000000 | 2 | 10 | 0.1 | |
| `anthropic/claude-opus-5` | 1000000 | 5 | 25 | 0.5 | |
| `anthropic/claude-sonnet-5` | 1000000 | 2 | 10 | 0.2 | |
| `anthropic/claude-fable-5.1` | 1000000 | 10 | 50 | 0.25 | |
| `google/gemini-3.8-flash` | 1048576 | 0.75 | 3.75 | 0.075 | 0.0416666666666667 |
| `google/gemini-3.7-flash` | 1048576 | 0.75 | 3.75 | 0.075 | 0.0416666666666667 |
| `google/gemini-3.6-flash` | 1048576 | 0.75 | 3.75 | 0.075 | 0.0416666666666667 |
| `google/gemini-3.5-flash` | 1048576 | 1.5 | 9 | 0.15 | 0.0833333333333333 |
| `google/gemini-3.5-flash-lite` | 1048576 | 0.3 | 2.5 | 0.03 | 0.0833333333333333 |

`openai/gpt-4o-mini` 的 canonical slug 与 id 相同，缓存写入字段在该条 payload 中缺席，所以 `cache_write` 为 null。五条 Anthropic 价格同时给出 `input_cache_write` 与 `input_cache_write_1h`；一个字段无法表示 TTL，因此 `cache_write` 保持 null，cache read 仍按短摘要写入。`web_search`、`image`、`audio`、`input_audio_cache`、`internal_reasoning` 留在文本价卡之外。

与 id 不同的 dated `canonical_slug` 只记在 exclusion `aliases` 里，不另建价格行。`openai/gpt-4o-mini` 不在该别名清单中。

## 能力

五个 Agent 来自当次取回的官方页，`source=official`，`benchmarks` 与推荐档均不填写：

| agent | as_of | can_edit_files | can_use_tools | source |
| --- | --- | --- | --- | --- |
| `codex` | `2026-10-08T14:06:45+08:00` | true | true | [Codex CLI](https://developers.openai.com/codex/cli) |
| `claude-code` | `2026-10-08T14:01:06+08:00` | true | true | [Claude Code overview](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview) |
| `cursor` | `2026-10-08T14:02:02+08:00` | null | true | [Cursor Agent docs](https://docs.cursor.com/agent) |
| `gemini-cli` | `2026-10-08T14:03:10+08:00` | null | true | [Gemini CLI](https://github.com/google-gemini/gemini-cli) |
| `antigravity` | `2026-10-08T14:04:32+08:00` | true | true | [Antigravity overview](https://antigravity.google/docs/overview) |

Cursor 文档索引写了 Plan Mode、diff review 和 MCP，没有写文件编辑，所以 `can_edit_files` 为 null。Gemini CLI readme 列出文件操作，没有写明 write 或 edit，所以同一字段为 null。页面许可证正文没有抄入。`https://x.ai/build` 返回 HTTP 403，因此没有 `grok-build` Agent 行。

model-effort 共 33 行，`benchmarks` 与 `default_for_agents` 都是 null。28 行是空 effort：上下文取自上述 OpenRouter payload 的 `context_length`，`as_of` 为 `2026-10-08T13:58:41+08:00`。另外 5 行是 `openai/gpt-6.1-sol` 的 `low`、`medium`、`high`、`xhigh`、`max`，上下文 1050000，`as_of` 为 `2026-10-08T14:30:15+08:00`，来源是 [OpenAI 模型页](https://developers.openai.com/api/docs/models/gpt-6.1-sol)。该页写明 `reasoning.effort` 支持这五档，medium 是 API 默认，不支持 `none` 与 `minimal`。API 默认值没有写成 `default_for_agents`。这一模型没有平价行，也没有空 effort 行。同一档位没有复制到 `openai/gpt-6-astra`；Astra 只有空 effort 的上下文 1050000。OpenRouter 的 `supported_efforts` 没有变成命名档或分档价格。

检索到的 models payload 没有 `google/gemini-4`。`google/gemini-3.8-flash` 与 Gemma 是不同 id，快照只收录前者自己的报价和上下文。

## 显式价格缺口

记录身份不能按 prompt token 阈值切换费率。下列模型的 `pricing.overrides` 会在阈值之后改变 input 或 output，因此整段价格不入库，上下文能力行仍然保留：

| model | 阈值（prompt tokens） | 仍保留的 context |
| --- | ---: | ---: |
| `openai/gpt-6.1-sol` | 272000 | 1050000，且仅有上面的五档能力 |
| `openai/gpt-6-sol` | 272000 | 1050000 |
| `openai/gpt-6-astra` | 272000 | 1050000 |
| `openai/gpt-5.6-sol` | 272000 | 1050000 |
| `openai/gpt-5.4` | 272000 | 1050000 |
| `anthropic/claude-haiku-5.5` | 100000 | 1000000 |
| `google/gemini-3.1-pro-preview` | 200000 | 1048576 |
| `x-ai/grok-4.7` | 200000 | 500000 |
| `x-ai/grok-4.6` | 200000 | 500000 |
| `x-ai/grok-4.5` | 200000 | 500000 |
| `x-ai/grok-build-0.1` | 200000 | 256000 |
| `x-ai/grok-4.3` | 200000 | 1000000 |

Artificial Analysis 的数值分数没有入库。私有订阅、额度与账号观察没有入库。候选整理里把 Astra 与 Sol 的 effort 对调、补写 `default_for_agents` 或未核对的适用性说明的部分，没有进入 source。

## 校验

`validate_public_artifact(db_path, manifest_path)` 返回核对后的 manifest。失败时抛出 `PublicDataError`，只带安全的 `code` 和 `message`。它核对文件 SHA256、publisher、schema 2、能力表、空的 observations 与 collector_jobs、全部 contribution 已发布、DELETE 日志、无 WAL/SHM，以及来源行是已审核的公开摘要。`default_public_paths()` 返回包内数据库和 manifest 路径。

`uv run --locked pytest -q tests/test_public_data.py`：23 passed。
