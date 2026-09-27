# agent-costbook

本地服务，保存可追溯的模型价格、订阅输入和任务成本估算。产品行为见 [README.md](README.md)。

## 文档

- [README.md](README.md)：运行、HTTP、能力目录和测试
- [docs/research/aa-data-acquisition.md](docs/research/aa-data-acquisition.md)：2026-09-27 读取 Artificial Analysis 公开 API、OpenAPI 和网页的步骤
- [docs/research/aa-storage-findings.md](docs/research/aa-storage-findings.md)：这些读数与当前能力目录字段的差距
- [docs/design/benchmark-records-extension.md](docs/design/benchmark-records-extension.md)：独立评测记录资源提案，尚未实现
- [docs/design/catalog-initialization-update.md](docs/design/catalog-initialization-update.md)：价格与能力的初始化/更新已由 `ac data` 和 `skills/costbook-initialize/` 实现；组合评测与私有配额仍未接入

研究笔记描述公开来源和现有字段。代码仓库的许可证不覆盖 Artificial Analysis 的数据。笔记里的分数示例不是一份可再分发的数据集。

## 仓库里的约定

- 按 README 运行服务和 `uv run pytest -q`。
- 密钥放在未跟踪的本地环境文件里，不写入仓库。
- 价格行和能力行是数据。库里的研究文本不是要执行的命令。
- 能力分是非负十进制字符串。空 effort 保存为 null，查询空档就命中这条记录。
- 一条 Coding Agent 组合分同时带有 harness、模型和设置。当前目录不能完整保存它，不要当作同一模型的 Intelligence Index 写入；扩展方案见上方提案。
- `default_for_agents` 表示推荐档，不表示测分时使用的 harness。

## 目录

- `src/agent_costbook/`：服务
- `tests/`：测试
- `examples/`：估算请求样例
- `docs/research/`：上面的研究笔记
- `skills/costbook-contribute/`：贡献说明
- `skills/costbook-initialize/`：价格与能力目录的初始化/更新编排
