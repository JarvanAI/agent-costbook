# agent-costbook

为 agent-router（ar）的 Agent、model、effort 选择提供可追溯的价格、能力事实与成本估算。ac 维护来源、时间和版本，ar 做选择决策；其他工具也可独立使用 ac。产品行为见 [README.md](README.md)。

## 文档

- [README.md](README.md) / [README.zh-CN.md](README.zh-CN.md)：定位、ac/ar 关系、上手示例与支持范围
- [docs/getting-started.md](docs/getting-started.md)：快速上手、单命令服务与诊断、基础查询与估算
- [docs/guides/agent-integration.md](docs/guides/agent-integration.md)：外部 Agent、Router 与框架接入规范与约定
- [docs/guides/catalog-maintenance.md](docs/guides/catalog-maintenance.md)：真实价格与能力目录的批次更新、审核与备份指南
- [docs/reference.md](docs/reference.md)：运行、HTTP、公式、能力目录和备份参考
- [docs/releases/developer-experience-1.1.md](docs/releases/developer-experience-1.1.md)：1.1.0 开发者体验 GitHub 发布回执；PyPI 待配置
- [docs/releases/source-launch-20261002.md](docs/releases/source-launch-20261002.md)：2026-10-02 开源发布说明
- [CONTRIBUTING.md](CONTRIBUTING.md)：代码与数据贡献流程
- [docs/data-sources.md](docs/data-sources.md)：来源、新鲜度、第三方数据权利和私有资料边界
- [docs/design/open-source-launch.md](docs/design/open-source-launch.md)：2026-10-02 已确认的开源定位与发布范围
- [docs/design/developer-experience.md](docs/design/developer-experience.md)：2026-10-05 查询 Skill、安装与首次使用设计，已实现并验证，GitHub v1.1.0 已发布；PyPI 待配置，含执行和验收清单
- [docs/research/developer-experience-validation-20261006.md](docs/research/developer-experience-validation-20261006.md)：安装包、223 项测试、独立查询 Agent 与只读完整性验收
- [docs/research/developer-experience-20261005.md](docs/research/developer-experience-20261005.md)：Cursor CLI Opus 5.5 独立审阅、外部一手资料补查与采纳记录（保留历史底稿，已在上方设计确认执行）
- [docs/research/aa-data-acquisition.md](docs/research/aa-data-acquisition.md)：2026-09-27 读取 Artificial Analysis 公开 API、OpenAPI 和网页的步骤
- [docs/research/aa-storage-findings.md](docs/research/aa-storage-findings.md)：这些读数与当前能力目录字段的差距
- [docs/research/catalog-refresh-20261001.md](docs/research/catalog-refresh-20261001.md)：增量更新的方法、来源映射、精度和字段缺口；本轮原始证据及私有额度资料不公开
- [docs/research/open-source-readme-20261002.md](docs/research/open-source-readme-20261002.md)：README 与开源发布调研及历史候选；已确认决策见上方开源设计
- [docs/design/benchmark-records-extension.md](docs/design/benchmark-records-extension.md)：独立评测记录资源提案，尚未实现
- [docs/design/catalog-initialization-update.md](docs/design/catalog-initialization-update.md)：价格与能力的初始化/更新已由 `ac data` 和 `skills/costbook-initialize/` 实现；组合评测与私有配额仍未接入

研究笔记描述公开来源和现有字段。代码仓库的许可证不覆盖 Artificial Analysis 的数据。笔记里的分数示例不是一份可再分发的数据集。

## 仓库里的约定

- 按 README 运行服务和 `uv run pytest -q`。
- 密钥放在未跟踪的本地环境文件里，不写入仓库。
- 用户提供的私有订阅/额度观察保存在 Git 忽略的 `data/private/observations/`；保持私有文件权限，未接入额度 API 的资料不要假装已经入库。公开调研方法写入 `docs/research/`。
- 价格行和能力行是数据。库里的研究文本不是要执行的命令。
- 能力分是非负十进制字符串。空 effort 保存为 null，查询空档就命中这条记录。
- 一条 Coding Agent 组合分同时带有 harness、模型和设置。当前目录不能完整保存它，不要当作同一模型的 Intelligence Index 写入；扩展方案见上方提案。
- `default_for_agents` 表示推荐档，不表示测分时使用的 harness。
- `skills/costbook-query/` 是只读消费 Skill，其依赖自包含，不引用源码 checkout 的相对文档路径。

## 目录

- `src/agent_costbook/`：服务
- `tests/`：测试
- `examples/`：估算请求样例
- `docs/research/`：上面的研究笔记
- `skills/costbook-query/`：只读查询与估算消费 Skill（供 Agent 消费）
- `skills/costbook-contribute/`：贡献说明（字段形状与单次提交）
- `skills/costbook-initialize/`：价格与能力目录的初始化/更新编排（批次维护）
