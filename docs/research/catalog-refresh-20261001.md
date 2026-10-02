# 2026-10-01 价格与能力目录更新

本文记录增量调研的范围、身份映射、写入方法和限制。原始 AA 数据、用户提供的订阅观察、批次及回执保存在本机私有工作区；本文不发布 AA 数据集。

## 更新方法

1. 从当前数据库读取 Agent 与 `(provider, model, effort)`，保存带版本的基线。
2. 打开 [AA 模型榜](https://artificialanalysis.ai/leaderboards/models)、各 release/model 页和 [Coding Agent 比较入口](https://artificialanalysis.ai/agents/coding-agents/comparisons)。对比已捕获的 2026-09-27 身份；同一模型的分数变化也属于更新候选。
3. 用厂商资料确认 API 身份、支持的 effort、上下文与价格。论坛保留为单独观察，不用主观评价生成分数、配额倍率或默认推荐。
4. 用 [初始化 Skill](../../skills/costbook-initialize/SKILL.md) 的 `plan → validate → diff → backup → apply → verify`。先在数据库副本上执行；重复写入应不新增版本。消费端用 publisher 和具体 snapshot 校验价格文件。

本轮并行调研使用三个 GPT-6 Luna 子代理，覆盖 OpenAI、Anthropic、Google/Agent。主代理复核身份、价格条件、映射和回执。旧数据在来源缺失时保留。

## 本轮确认与保留

| 对象 | 能力目录处理 | 来源及限制 |
| --- | --- | --- |
| GPT-6.1 Sol | `openai/gpt-6.1-sol`，low/medium/high/xhigh/max | [OpenAI 模型页](https://developers.openai.com/api/docs/models/gpt-6.1-sol)；不支持 none/minimal；官方上下文 1,050,000。AA 五档只取得显示整数，指标名与单位明确标成 display / rounded_points。 |
| Claude Sonnet 5.5 | `anthropic/claude-sonnet-5-5`，五档 | [Claude effort](https://platform.claude.com/docs/en/build-with-claude/effort)、[模型概览](https://platform.claude.com/docs/en/models/overview)。AA 的 Default Fallback 测分存为变体证据，未写到普通模型 benchmark。 |
| Claude Opus 5.5 | `anthropic/claude-opus-5-5`，五档 | 同上；API default medium，adaptive thinking 始终开启。 |
| Gemini 4 Argon | `google/aa/gemini-4-argon`，AA 观察的 high | [AA 模型页](https://artificialanalysis.ai/models/gemini-4-argon)。`aa/` 是本目录的来源命名空间，不是 Google API model ID；厂商 effort 合约和调用者账户可用性未确认，不自动派遣。 |
| 五个已捕获的 AA harness | Antigravity SDK、Devin Fusion CLI、Muse Code、Kimi Code CLI、Opencode | 补录 2026-09-27 证据中已经确认的身份；as_of 保留原日，未知本机安装、工具和编辑能力留空。 |

`default_for_agents` 仍为空。厂商的 API 默认值、Claude Code 默认值、测分档位与 router 推荐档是不同事实。`ultracode` 是 Claude Code 工作流设置，也不能直接当作一个新 effort。

## 价格条件

价格来自 [OpenAI 模型页](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 和 [Anthropic 定价页](https://platform.claude.com/docs/en/about-claude/pricing)。新价格覆盖 GPT-6.1 Sol 两个上下文区间、Sonnet 5.5 与 Opus 5.5 的 uncached / cache 5m / cache 1h；每档分别匹配身份。Fable 5.1 既有价格重新核对，内容相同则不写新版本。

GPT-6.1 Sol 的输入超过 272K 时，全请求输入/缓存按 2 倍、输出按 1.5 倍；记录的 scope 分开表达。Claude 新模型在完整 1M 上下文使用标准价。Fast、Batch/Flex、区域溢价和工具费用未折入这些标准价；不得跨 scope 当同一价格。

Argon 的公布价格包含 introductory 阶段和后续阶段，公开公告未给明确结束日期；官方 API 身份仍待确认。本轮没有写它的可执行 API 报价，也未把 AA 的 task cost 写成 token 单价。

## 两类字段缺口

- Coding Agent 评测是 harness＋模型＋设置。当前 [Claude Code/Codex 比较页](https://artificialanalysis.ai/agents/coding-agents/comparisons/claude-code-vs-codex) 已出现新组合，另有 Antigravity CLI/Argon；组合分、API task cost、token mix、wall time、fallback 与 harness 版本需独立保存。当前 `coding_agent` 批次仍 refused，扩展见 [benchmark-records 提案](../design/benchmark-records-extension.md)。
- 用户订阅观察需要私有 quota meter、7 天周期、每种 token 的额度百分点、点值/区间、任务与 effort、来源和实测时间。当前模型不能完整表达；先存私有 Markdown/JSON，不能塞进 token 价格或 M1 的倍率。API 等价金额与现金仍分别报告。

## 本轮获取限制

本轮 shell 的网络策略禁止远程和 loopback socket；网页工具可读取公开文本，却没有取到新版原始 HTML/flight 载荷。新分数仅保留来源实际显示的精度，缺失的 estimated/deprecated 标志和评测日期保持未知。旧的小数与版本未被低精度显示值覆盖。这是一轮明确范围的增量更新，不能宣称已枚举整个 AA 当前数据库。

AA 比较页与公告的直链可能失败，而搜索结果或其他语言页面可打开；分别记录成功入口和失败路径。不同抓取时刻的 chart 选择数不是完整行数，也不能和旧的 20 条 raw 组合直接相减。

本轮操作脚本在允许写入的数据库上调用现有 ASGI API，使用真实鉴权、请求校验、发布与乐观版本检查。同步路由在单进程串行执行，避开当前环境的线程唤醒阻塞；这不是产品新增的 CLI 功能，也不证明 8080 端口连通。真实写入前在副本上验证无 token 的 PUT 返回 401、非法版本返回 422，并检查备份、回读和重复执行。

## 验证记录

真实写入已完成：4 → 9 个 Agent，10 → 14 个模型，42 → 58 条 model-effort，72 → 112 条价格。新增 61 项（5 Agent、16 model-effort、40 价格），15 项相同；2 类字段缺口仍 refused。第一次及重复执行均回读匹配；重复执行 76 项 unchanged，没有新增价格或能力版本。SQLite integrity / foreign keys 检查通过。原有 42 条能力内容与版本逐条保持，未替换旧的小数分数。副本和真实库的流程均保留备份与回执。

### 更新后清单

Agent：`antigravity-cli`、`antigravity-sdk`、`claude-code`、`codex-cli`、`devin-fusion-cli`、`grok-build`、`kimi-code-cli`、`muse-code`、`opencode`。这些是本目录已收录的身份，不表示全部已在本机安装。

| provider / model | 收录 effort |
| --- | --- |
| anthropic / claude-fable-5-1 | low, medium, high, xhigh, max |
| anthropic / claude-opus-5-5 | low, medium, high, xhigh, max |
| anthropic / claude-sonnet-5-5 | low, medium, high, xhigh, max |
| google / gemini-3.8-flash | low, medium, high |
| google / aa/gemini-4-argon | high，AA 观察，调用合约未确认 |
| openai / gpt-6-astra | low, medium, high, xhigh, max, ultra（保留本地观察，官方 API 页只确认前五档） |
| openai / gpt-6-sol | none, low, medium, high, xhigh, max |
| openai / gpt-6-luna | none, low, medium, high, xhigh, max |
| openai / gpt-6.1-sol | low, medium, high, xhigh, max |
| opencode / jev-1.13-free | null |
| xai / grok-4.5 | low, medium, high |
| xai / grok-4.6 | low, medium, high, xhigh |
| xai / grok-4.7 | low, medium, high, xhigh |
| xai / grok-4.7-build-fast | low, medium, high, xhigh |

完整清单和真实数据库回执以本机 `.worktree/catalog-refresh-20261001/apply-result.json` 及 `root-verification.json` 为准。22 条 Coding Agent 显示读数另存为私有组合证据，未入数据库。用户观察文档与 JSON 另保存在 Git 忽略的 `data/private/observations/20261001-pro-weekly-quota/`，避免和临时执行文件一起清理。离线正式价格文件为 `.worktree/ac-live/exports/prices-snap-45.json`；能力目录另存 `capabilities-20261001.json` 作为读取副本，尚未成为 `ac estimate` 的离线合约。公开文档不携带私有套餐数据。
