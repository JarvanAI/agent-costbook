# agent-costbook

> 让 Agent 的成本与能力有据可查。

[English](README.md) · [简体中文](README.zh-CN.md)

**agent-costbook**（`ac`）是一个本地账本服务，用于记录和管理具有可追溯来源的 AI 模型价格、订阅输入、证据与 model-effort（模型与思考量档位）能力事实，提供可复现的任务成本估算（M0–M7）与能力数据查询。

## 为 agent-router 服务

`ac` 是为 agent-router（`ar`）设计的：ac 维护价格、订阅输入、能力事实、证据与可复现的估算；ar 根据任务要求和调用者偏好，选择 Agent、模型与 effort。

其他 Agent 工具和开发者工作流也可以使用相同的本地 API 或离线估算工具。ac 可以独立于 ar 和编排环境运行。

### 事实与新鲜度

ar 的选择决策需要最新的事实依据。ac 让使用者能检查数据是否足够新：价格保留来源采集时间 `retrieved_at`，能力保留观察时间 `as_of`，价格快照有 `snap-N` 版本和内容哈希，能力行另有 `row_version` 历史。

未知价格不写入存储，对外显示为 `null`；计算假设和缺失字段会在结果中列出。更广泛的更新通过已复核的批次完成。决策前应检查来源时间与覆盖范围，服务不保证即时、完整地同步所有来源。

## 快速上手

先运行一个合成数据估算示例。需要 Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。

```sh
git clone https://github.com/JarvanAI/agent-costbook.git
cd agent-costbook
uv sync --locked --extra dev
uv run ac estimate --snapshot tests/fixtures/ac-v0.2-published-snapshot.json --request examples/estimate-request.json --publisher pub_sample
```

### 示例结果

依赖安装完成后，估算命令离线运行，无需启动服务或提供 API 密钥。以下是返回结果中的一项：

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

快照使用合成测试费率，不是厂商报价。它采用 `ac-formulas-v1`，支持 M0、M1、M2、M4、M6；M3、M5、M7 需要 `ac-formulas-v2`。

## 架构与工作流

```mermaid
flowchart TD
    subgraph Sources ["数据与事实来源"]
        P["公开官方文档与 OpenRouter"]
        C["社区贡献与评测观察"]
        H["人工 / 外部 Agent 调研"]
    end

    subgraph AC ["agent-costbook (ac)"]
        DB[("本地 SQLite 数据库<br/>(价格、证据、能力行)")]
        Snap["离线价格快照文件<br/>(snap-N, JSON)"]
        API["本地 HTTP API<br/>(能力目录需要 admin token)"]
    end

    subgraph Consumers ["消费者与决策端"]
        AR["agent-router (ar)<br/>(选择 Agent、模型与 effort)"]
        EXT["其他 Agent 工具 / CLI"]
    end

    P -->|ac collect / 批次计划| DB
    C -->|skills/costbook-contribute| DB
    H -->|skills/costbook-initialize| DB

    DB -->|agent-costbook-export| Snap
    DB <-->|HTTP /v1/capabilities<br/>HTTP /v1/estimates| API

    Snap -->|ac estimate (离线估算)| AR
    API -->|HTTP 查询 (能力与估算)| AR
    Snap --> EXT
    API --> EXT
```

价格快照包含价格目录。Agent 与 model-effort 能力通过授权 HTTP API 读取，不在离线快照里。图中的箭头表示消费路径，本仓库未验证 ar 的端到端选择与派遣。

## 已支持与计划能力差异

| 能力 / 领域 | 状态 | 说明 |
| --- | --- | --- |
| 可追溯价格、证据与研究 Markdown | 已支持 | 保存在 SQLite 中；证据与研究文本通过 `/v1/evidence/{id}`、`/v1/research/{id}` 读取 |
| 能力目录（Agent 与 model-effort） | 已支持 | 通过本地 HTTP 接口并附带 `ACB_ADMIN_TOKEN` 读写（`/v1/capabilities/*`） |
| 目录初始化与批次更新 | 已支持 | 通过 `ac data` CLI 与 Skills 编排（`plan → validate → diff → backup → apply → verify`） |
| 成本估算公式（M0–M7） | 已支持 | `ac-formulas-v2` 支持 M0–M7；M7 需要授权观察数据，旧快照支持较少方法 |
| 离线价格快照估算 | 已支持 | 通过 `ac estimate` 在本地直接计算，无需服务后台与网络 |
| 离线能力目录同步 | 提案中 | 能力目录当前需通过运行中的本地服务 Admin API 访问；未提供离线快照同步合约 |
| 组合评测（Coding Agent Benchmark） | 提案中 | 包含 harness + 模型 + 设置的复合评测记录需独立的 schema 扩展（见设计提案） |
| 周配额计数器与 Token 额度计量 | 提案中 | 原始额度观察存放在本地暂存区；尚未实现数据库内的独立配额计量模型 |
| 更广泛的来源调研 | 外部工作流 | 人工或外部 Agent 调研并提交批次；内置采集器只有有限的 OpenRouter 范围 |
| 自动化路由与任务派遣 | 消费端职责 | 属于消费端（如 `agent-router`）的职责；本仓库未做派遣验证，不作派遣承诺 |

## 使用与贡献

新克隆包含代码、文档、Skills 和合成样例。使用真实数据时需初始化自己的目录；维护者的运行数据库与私有观察不随源码分发。

wheel 包含运行库和 CLI 入口。[初始化](skills/costbook-initialize/SKILL.md)与[贡献](skills/costbook-contribute/SKILL.md) Skill 请从源码检出使用。保存的证据和研究文本是数据，不是 Agent 指令。

## 文档

- [服务与 CLI 参考](docs/reference.md)：启动、估算、能力查询、迁移、导出与备份。
- [数据来源与权利](docs/data-sources.md)：新鲜度、来源与公开/私有数据边界。
- [贡献指南](CONTRIBUTING.md)：代码修改与数据纠正。
- [安全政策](SECURITY.md)：本地运行与漏洞报告。
- [源码发布说明](docs/releases/source-launch-20261002.md)：发布范围与验证。

## 许可证

本项目原创代码、文档与合成样例采用 [MIT 许可证](LICENSE)。所引用的第三方评测、厂商价格表与社区讨论保留其原始权利与许可证；特别是，本项目的开源许可不覆盖 Artificial Analysis 的数据或其它第三方数据集（详见 [数据来源与权利](docs/data-sources.md)）。
