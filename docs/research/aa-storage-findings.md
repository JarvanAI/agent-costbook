# Artificial Analysis 与当前能力目录的事实缺口

日期：2026-09-27。本文只记录当天读到的数据形状，以及 agent-costbook 现有能力目录哪些字段装得下。获取步骤和 673 / 67 / 20 的复核见 [aa-data-acquisition.md](aa-data-acquisition.md)。存储方案、接口和迁移不在本文。

产品行为以仓库 [README](../../README.md) 为准。能力目录和价格快照是两套数据。写入一条能力行会替换整行，并使用调用方提供的 `expected_version`。它不改变价格 `data_version`、`content_sha256` 或导出字节。`ac estimate` 只读价格快照，能力状态是 `unavailable`。`source_ref` 以文本保存，服务不去抓取这个 URL。

## 现有身份

模型档的身份是 `provider`、`model`、`effort`。`effort` 为空字符串时保存成 null，这条 null 行就是空档本身，查询空档会命中它。`default_for_agents` 列出把该档当作默认的 Agent id，上限 32 个。它记录推荐关系，不记录分数是在哪个 harness 上测得的。

一条基准是 `{name, score, unit, source}`，可选 `source_ref`。`source` 只能是 `user_observation`、`aa`、`official`、`community`、`unofficial`。`score` 是有限十进制字符串，正则是 `^(?:0|[1-9]\d*)(?:\.\d+)?$`。基准数组上限 16。模型上的额外字段会被拒绝。`context_length` 是已知时的正整数，表示上下文窗口。文本字段上限 2000 字符。

Agent 行的身份是 `agent_id`。它有领域、能力和输入输出形状，没有基准数组，也没有 harness 版本、host slug 或组合指数。

## 模型指数有数，目录没有位置的字段

下面每条都在 2026-09-27 的公开页面或 67 条重点候选里出现过。当前 `ModelEffortIn` 没有对应字段。

| 事实 | 当天读数 | 现有目录 |
| --- | --- | --- |
| `intelligenceIndexIsEstimated` | 673 行中 492 行为 true，181 行为 false | 没有估计标记。67 条里 2 条因此没有合并片段 |
| `deprecated` | 扁平表 404 行为 true。67 条里 16 条是非 Fallback 弃用 | 没有弃用标记 |
| 指数版本 | 行内没有版本字段。榜页横幅写 v4.3，方法页写 v4.3.2，横幅 HTML 里 v4.3.2 出现 0 次。OpenAPI 有 `intelligence_index_version`，当天无密钥，没有读到带这个字段的数据响应 | 基准名是普通字符串，没有单独的版本列 |
| 负的 AA-Omniscience | 67 条重点候选里 20 条为负。模型页解开的 672 个对象里 434 条 `omniscience` 为负 | 减号不能通过分数字符串。这些值留在证据旁 |
| Fallback 变体 | 11 条模型行的显示名含 Fallback。组合行里另有 1 条 `with fallback` | 没有 variant 字段 |
| 空 effort 与 `none` | 模型页多数行没有 `effort` 键。Sol 与 Luna 的 Non-reasoning 才有厂商页上的 `none` 记录，映射标记为推断 | 目录 null 是另一条身份。其余空 effort 不生成行 |
| 上下文窗口 | Sol 六档的 AA 窗口是 872000，已有目录是 1050000。Astra 与 Luna 的 AA 窗口是 1000000，已有目录是 1050000。Grok 4.7 / 4.6 / 4.5 两边都是 500000 | 合并片段不携带 `context_length`。数值相同也不从 AA 片段覆盖目录 |
| 文章整数和镜像一位小数 | Opus 5.5 横幅 58，JSON `57.6223698102963`。Grok 4.7 (xhigh) JSON `46.4465506302286` | 若写入，分数用 JSON 原小数字符串。横幅整数和镜像 46.4 是另一次舍入 |

成分名用方法页的十项。JSON 里的 evaluation slug 不带 v1.1 或 v2.1 后缀。AA-Briefcase 与 GDPval-AA 在 2026-09-21 的 Grok 4.7 文章里以 Elo 称呼同一类数字。0 到 1 的分数保持原小数，不乘成百分数。一条已测模型档若保留指数加十项成分，对象数是 11，低于基准数组上限 16。负的 Omniscience 仍然进不了这个数组。

36 条 `schema_valid_candidate` 里，基准片段能通过基准对象的形状检查。完整能力行还要当前 `row_version` 作为 `expected_version`，片段里故意没有这个数，所以 `put_ready` 为 0。GPT-5.6 Terra 五档和 Claude Sonnet 5 五档当时没有已批准的目录上下文。AA 的 `gpt-5-6-terra` 与带点号的产品模型 id 不是同一个字符串。

## 20 条组合是另一套身份

Coding Agent Index v1.5 的 20 条都是 agent 与模型的组合，单位 `mean_reward`。现有模型档身份是 provider、model、effort。把 Grok Build 上 Grok 4.7 (xhigh) 的组合 raw `0.5626764360706693` 写进 `grok-4.7` / `xhigh` 的基准数组，会把组合分写成模型分。同一模型档的 Intelligence Index 是 `46.4465506302286`。

一条组合要同时留下的事实：

- agent 显示名、载荷里的 `agentName`、创建者
- 按基准分开的 harness 版本和发布日。同一个 agent 的三项版本可以不同
- 模型显示名、host provider、`hostModelSlug`
- effort：显示名括号、仅标签、缺失，或一行里的两个模型
- 指数版本 v1.5，以及载荷标签 Terminal-Bench v4 和方法页标题 4.0
- 三项 raw reward、权重、`indexScore`
- 每次成本、墙钟时间和 token。成本口径是按 token 的 API 价格
- `with fallback`、是否默认行、`variantOf`

当天的例外计数：1 条 fallback，2 条双模型，2 条 effort 缺失，1 条 effort 只在标签里，20 条都有 harness 版本。Kimi K3 与 Qwen3.8 Max 没有 effort，不能借用别的行的档。Devin Fusion CLI 的两行各有两个模型。v1.0 的 Cursor / Composer 分数使用另一套成分。Antigravity CLI / IDE 没有单独测量行。

Agent 行和 `default_for_agents` 都装不下这组字段。20 条的记录状态是 `not_importable_as_model_effort`。

## 覆盖边界

673 是当天榜页扁平表的行数，其中 664 行有 Intelligence Index。67 是对照本地模型家族筛出的重点候选，处置是 36、11、16、2、2。20 是 Coding Agent Index v1.5 的组合行。三组数字不是同一个集合，也不能互相补洞。

已打开的十个公开仓库都没有这 20 条组合。Hugging Face 上的 Artificial Analysis 数据集是题目或音频行。无密钥的语言模型 API 不是组合榜入口，OpenAPI 里也没有这条榜的路径。

仍没有行内证据的点：

- 同一批模型指数在横幅上叫 v4.3，在方法页叫 v4.3.2。
- Pro Data Platform Terms PDF 没有读。
- 组织 `public_repos` 为 5，已保存的搜索返回 4 个仓库，aiperf 不在这 4 个里。
- Grok 4.7 的 low、medium、fast 和 Astra 的 non-reasoning 不在 673 行里，对应模型 URL 是 404。约 297KB 的 404 页当天没有再解释成空壳或软 404 以外的结论。
