# README 与开源发布调研（2026-10-02）

状态：调研与候选方案，定位、宣传语、许可证、语言和发布范围待维护者确认。本文件不是批准后的发布计划。

本报告由协调 Agent 阅读现有实现、运行验证并检索公开一手资料后整理。用户指定的 Antigravity 调研 impler 已启动，但两次调用均在执行途中返回 `FAILED_PRECONDITION: User location is not supported for the API use`，未产出报告；不得将本文描述成该 impler 的成功交付。

## 已验证的现状

- GitHub 仓库当前为 private，尚未选择许可证。推送代码不等于更改可见性或正式发布。
- 当前 README 先解释版本、schema 与 M0–M7，缺少面向首次访问者的任务场景和最短成功路径；后面的 API、能力、备份和兼容说明有保留价值，可移入独立参考文档。
- `uv run pytest -q`：154 passed，1 个现有 Starlette/httpx 弃用警告；本轮没有产品代码变更。
- `uv build --out-dir /tmp/ac-oss-build-20261002` 成功生成 wheel 和 sdist。sdist 包含 Skill 文件，但没有 `docs/`；wheel 不含 Skill 与独立文档。克隆使用和包安装的可用文件范围不同，需要分别说明，不能宣称安装 wheel 就安装了 Skill。
- 本轮验证的离线示例无需服务、账号密钥或实时报价，结果为 `status: ok`、`metrics.cost: "0.0177"`、`currency: "USD"`。它是合成样例，不是厂商报价。
- 当前 `examples/data-scope.json` 与 `examples/estimate-request.json` 使用不同模型身份，不能直接把两个文件串成同一个上手流程。

```sh
uv sync --extra dev
uv run ac estimate \
  --snapshot tests/fixtures/ac-v0.2-published-snapshot.json \
  --request examples/estimate-request.json \
  --publisher pub_sample
```

上述路径属于源码 checkout。当前快照使用旧公式集 `ac-formulas-v1`；M4 在该集合受支持，示例不证明新公式集的所有方法。正式 README 可先使用这一真实可运行路径；以后另增同一身份、同一公式版本的专用 demo，不能只改文案使不匹配的样例看似可用。

## 一手资料与可借鉴做法

资料读取日期：2026-10-02。下表是项目维护者公布的入口及本报告的写作建议，不是对整个项目实现的独立审计。

| 来源 | 观察 | 对本项目的建议 |
| --- | --- | --- |
| [GitHub：About READMEs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes) | README 解释用途、价值、上手与求助；较长说明适合另设文档 | 首屏说明一个具体问题，紧接可运行示例，再链接完整 API 与公式 |
| [GitHub：Community profiles](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories) | README、许可证、贡献说明等属于社区健康文件；安全报告可以有独立说明 | 先保证贡献者有真实入口，不为勾选所有清单项制造空文档 |
| [Models.dev](https://github.com/anomalyco/models.dev) | README 将模型规格、价格和能力目录与 API 示例、TOML 贡献步骤联系起来，并区分基础模型事实与 provider 服务属性 | 借鉴直接展示接口和贡献一个模型的路径；其目录与本项目有重叠，不能声称价格／能力目录是独有能力 |
| [Portkey models](https://github.com/portkey-ai/models) | 以模型价格和配置数据服务成本归因为定位 | 数据贡献应明确计费条件、单位、缓存和 provider/channel，避免只有一个 input/output 数字 |
| [TokenCost](https://github.com/AgentOps-AI/tokencost) | 项目入口围绕 token 价格估算，配有使用示例 | 用一次具体估算解释价值；无需要求新用户先学完所有公式 |
| [LLM](https://github.com/simonw/llm) | 项目入口明确命令行用途并连接安装、文档与扩展生态 | CLI 的第一条成功命令比长端点列表更适合上手；详细参考放到文档 |
| [LiteLLM](https://github.com/BerriAI/litellm) | 项目定位涵盖模型调用接口和 Gateway，也包含成本相关能力 | 明确本项目负责事实、证据和估算；集成关系可以解释，但不能据此声称已实现 Gateway 或自动派遣 |

这些项目说明“又一个价格表”定位竞争明显。以下差异化建议是对现有实现及需求的推论：优先展示可追溯来源、可固定版本的估算、本地数据控制，以及现金、订阅摊销、API 等价金额和配额口径的区分。尚未支持的周额度资料和完整 Coding Agent 组合评测应标为计划。

## 定位候选

| 候选 | 用户与承诺 | 取舍 |
| --- | --- | --- |
| A：Agent 的成本与能力账本（推荐） | Agent 工具／router 开发者、维护多模型工作流的个人；提供带来源的事实与成本估算 | 覆盖现有价格、能力和证据，也保留订阅扩展空间；需解释“能力目录”不是路由决策 |
| B：AI 模型价格与订阅成本估算服务 | 主要面向调用成本服务的开发者，强调估算与可复现 | 更容易一眼理解，但弱化 Agent／model-effort 数据维护 |
| C：可贡献的 AI 价格与能力目录 | 主要面向数据贡献者和目录消费者 | 有利于数据社区，但与 Models.dev 等目录重叠；还可能被误读成已提供完整公共数据集 |

推荐目标文本：为 Agent 与开发者维护可追溯的模型价格、订阅输入和能力事实，通过本地 API、CLI 与版本化快照提供可复现的成本估算。

“本地运行”不要求用户部署一个推理模型。数据调研由人或外部 Agent 按贡献工作流完成；产品不依赖 AO、Orca 或某个特定 router。

### 宣传语候选

1. 推荐：**让 Agent 的成本与能力有据可查。** / **Traceable costs and capabilities for AI agents.**
2. 偏成本：**算清模型成本，保留每个依据。** / **Estimate model costs with evidence you can inspect.**
3. 偏行动价值：**为 Agent 选择提供可追溯的成本依据。** / **Cost evidence for informed agent choices.**

候选 3 可以表达服务消费者的价值，但正文必须说明：选择模型、Agent 和 effort 的策略属于消费者；本项目不承诺找到最优组合或自动执行任务。不得使用未经测量的“节省 X%”“全模型覆盖”“实时同步”宣传。

## 当前与计划能力

| 能力 | 当前状态与首版宣传口径 |
| --- | --- |
| 可追溯价格、证据、研究 Markdown | 已支持，SQLite 与 HTTP 接口 |
| M0–M7 | `ac-formulas-v2` 支持；旧公式集有方法限制，M7 依赖私有测量和授权 |
| Agent、model-effort 能力目录 | 已支持，读取与写入均需 admin 授权 |
| 初始化与更新 | 已有 `ac data`、初始化 Skill；人工／外部 Agent 准备批次后复核、备份、写入、验证 |
| 离线消费 | 已支持价格快照估算；能力目录没有正式离线同步合约 |
| 自动收集 | 仅现有 OpenRouter 来源与固定模型 endpoint 范围；不宣传通用 crawler |
| 论坛研究、完整 AA 自动导入 | 外部调研方法可复用，尚无产品内研究服务或完整同步 |
| Coding Agent 组合评测 | 独立资源提案，未实现 |
| 私有周配额／每种 token 额度观察 | 当前无对应完整资源；文件保存不等于入库 |
| 自动路由和派遣 | 消费端职责，当前仓库没有端到端验证 |

## README 建议结构

1. 名称、双语入口、选定的一句宣传语。
2. 三句话解释问题、消费者与输出：维护数据 → 查询估算 → 消费者自行选择。
3. “先跑一个例子”：源码克隆、Python 3.12+、uv、上面的离线命令及关键输出。
4. 两个真实场景：router 查询候选价格与能力；开发者复核来源并更新报价。
5. 一张小图：来源 → 证据与目录 → 估算/API/价格快照 → Agent 或开发者。标注授权能力目录与公共价格快照的差异。
6. 数据从哪里来、覆盖范围与时间；新 clone 默认没有个人维护的实时数据库。
7. 数据贡献入口：来源与计费条件 → scope → plan/validate/diff → review → backup/apply/verify；代码贡献另给测试与 PR 步骤。
8. 已支持／计划中的简表；公式、HTTP、能力目录、运行备份等详细说明链接到 `docs/`。
9. 许可证、第三方数据权利、维护者、问题与安全报告入口。

推荐英文 README 主入口＋`README.zh-CN.md`；替代方案是中文为主附英文简介。两者都会增加同步责任，不能只翻译宣传语而让安装步骤分叉。此次只提出候选，不改现有 README。

## 开源代码与数据边界

建议开放代码、维护者原创文档和合成示例；现有第三方数据和用户私有观察分别遵循其自身的授权／私有要求，不因仓库选了许可证而获得同一许可。当前没有选择 MIT 或 Apache，也没有完成第三方资料的逐项再分发审查，不能把现有本机库整体当作默认公开种子。

维护者选择代码许可证时可以比较：

- [MIT](https://choosealicense.com/licenses/mit/)：许可文本简短，适合希望广泛复用且维护手续少的项目。本报告推荐作为候选默认。
- [Apache-2.0](https://choosealicense.com/licenses/apache-2.0/)：同属宽松许可，包含明确专利授权以及相关条件；如果维护者更看重这一点可选择它。

该推荐不是许可证决定，也不授权仓库中不属于维护者的内容。首次开放前应写明代码许可覆盖范围与第三方数据来源说明，维护者再决定是否提供经过单独审查的数据发行物。

## 最小发布范围与后续事项

推荐第一步：GitHub 源码发布，附可运行合成 demo、API/CLI 文档和贡献流程；首轮不承诺 hosted 公共服务、不同时上线新 crawler 或补齐评测／配额 schema。产品版本已是 1.0.0，开源发布记录应解释已有版本背景，不为宣传重新标成一个相互矛盾的 v0.1。

确认定位之后，可实施的最小文档与工程清单：

- LICENSE 与 package license metadata；README 双语入口及独立 HTTP／公式／运行参考，去掉指向不随仓库提供的私有 `.docs/` 的指引。
- CONTRIBUTING：代码贡献与数据贡献两条路径；字段缺失、证据、时间、冲突、单位和 idempotency 的要求。
- 数据来源／权利说明：区分源码、合成示例、第三方数据、个人本地数据；禁止把私有回执或账号数据贴入 public issue。
- 最小 CI：仓库测试、构建、文档相对链接、可运行 demo；不得依赖维护者的本机数据库或凭据。
- 安全报告入口与首个发布说明；安全联系方式必须由维护者提供真实可用方式，不能捏造邮箱。

后续再按用户需求决定 PyPI 发行、文档站、Docker、更多来源适配器和经审查的数据发布。这些不是短期 README 修改的必要依赖。

## 需要维护者确认的五项内容

1. 定位：A／B／C；推荐 A，涵盖现有事实维护和估算能力。
2. 宣传语：上面的 1／2／3；推荐 1，简短且不暗示已经替消费者决策。
3. 语言：英文主 README＋中文版本，或中文主入口＋英文简介；推荐前者，方便外部贡献。
4. 代码许可证：MIT 或 Apache-2.0；推荐 MIT，若要求明确专利授权则选 Apache-2.0。
5. 首轮发布：GitHub 源码＋文档＋合成 demo，或同时提供包／托管数据服务；推荐先前者，范围明确且可验证。

仓库转 public 是上述内容落实后的一个单独发布动作；此次提交与推送不包含可见性变更。
