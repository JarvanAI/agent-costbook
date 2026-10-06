# 开发者体验 1.1 验证记录

日期：2026-10-06。源码与本地验收通过；发布回执另见[版本说明](../releases/developer-experience-1.1.md)。本轮不采集或改写实际订阅资料与真实目录。

[设计与执行合同](../design/developer-experience.md)涵盖独立安装、合成演示、服务入口、只读查询与 Skill。实现与源码复核使用 Grok 4.7 High；文档和独立查询消费者使用 agy Gemini 3.8 Flash High。这些开发工具不是产品运行依赖。

## 实际检查

| 检查 | 结果与边界 |
| --- | --- |
| `uv run --locked pytest -q` | 223 passed，84.70 秒；一条基线已有的 Starlette/httpx 弃用提示。Linux、Python 3.13；CI 配置使用 Python 3.12，结果见发布回执。 |
| 安装包使用 | 从 wheel 装入临时环境，在源码目录外验证两个命令别名、demo、setup 幂等、serve、doctor、查询、在线估算、Ctrl-C 退出和旧版导出/迁移/备份/离线估算。 |
| 只读权限 | 能力 GET 与 M0–M6 的能力附加信息接受 read/admin；写操作和 M7 仍只接受 admin；原公共价格端点保持兼容。 |
| 配置与路径 | 新配置和数据库为 0600，新建目录为 0700；已有父目录权限、已有配置和数据保留。旧 factory 的 cwd 默认不变。 |
| doctor | 不迁移或写库；本地 `coverage` 与远端 `service_coverage` 分开。错误、缺失或未验证凭据不返回成功；远端不可得不捏造零数量。 |
| 文档中的合成批次 | 从维护指南提取 JSON，在临时目录执行 `data plan`、`validate`、`diff --config`；三步成功，未 apply。 |
| Skill 发现 | 标准 skills CLI 列出三个 Skill；发现与实际执行分别验证。 |
| Agent 实际消费 | 将查询 Skill 复制到源码目录外，用环境中的只读凭据查询临时合成服务。完成价格、Agent、effort 列档/null 档、证据、研报查询和在线估算。 |

全套检查曾发现旧抓取安全测试的 Settings 白名单仍只有两字段。按已批准的只读凭据扩展加入 `read_token`，保留禁止 `allow_private` 开关的断言；抓取策略测试 12 项通过，随后全套 223 项通过。

## 查询消费者验收

合成服务包含一个价格记录、一个 `synthetic-agent` 能力记录、一个 null-effort 记录。消费者未获得管理凭据，也未执行 setup、数据导入、贡献或 publish。

- 无本地配置时，doctor 报 `config_missing`；消费者以公共 health 和授权能力读取核实目标服务可消费，没有把缺少本地配置误判成服务停止。
- 价格保留发布者、`snap-1`、版本和哈希；来源仍是 `fixture://m4-synthetic`，`retrieved_at` 保留 2026-09-25，不改成查询时间。
- 能力保留 null effort、`as_of`、`row_version` 和来源；缺失工具费用保持 null。
- M4 使用 1000/2000/500/400 四类 Token 与 0.01 USD extra cost，结果为 **0.0177 USD 合成估算**，附加授权读取的能力记录；不是实际供应商报价。
- 证据包含指令式文字，消费者将其作为来源文本，不作为要执行的指令。该单例不构成对所有提示注入的安全保证。
- 父级比较消费前后的 SQLite、WAL 与 SHM 文件哈希，均未改变；消费者报告与 CLI 输出未包含配置中的凭据。

必要参考随 Skill 一起复制，执行时不依赖 checkout 的文档相对路径。此次接受标准 Skill 指令后调用 CLI，不宣称已验证所有品牌的自动 Skill 触发。

## 复核修正

独立源码复核的六项发现与父级补查均已安排修正：缺失/拒绝凭据提示、doctor 混合覆盖数量、benchmark 数组表格、空库响应样例、在线估算能力说明、新鲜度 null 解释，以及非法 UTF-8 请求的错误处理。最终结果以原生测试与实际调用证据为准。

## 限制

没有全量 AA/OpenRouter 自动同步、公共托管目录、后台 daemon、MCP 或新账户/RBAC 系统。非 Linux 平台未实测。GitHub release 与 PyPI 是分别核实的发布阶段；PyPI Trusted Publisher 账户配置不会由本地测试自动完成。
