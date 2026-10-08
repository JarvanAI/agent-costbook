# agent-costbook

> 让 Agent 的成本与能力有据可查。

[English](README.md) · [简体中文](README.zh-CN.md)

**agent-costbook**（`ac`）提供公开参考服务与本地账本，用于记录和管理具有可追溯来源的 AI 模型价格、订阅输入、证据与 model-effort（模型与思考量档位）能力事实，提供可复现的任务成本估算（M0–M7）与能力数据查询。

`ac` 最初为 [agent-router](https://github.com/JarvanAI/agent-router)（`ar`）设计，用于提供可验证的价格与能力事实依据：由 `ac` 维护来源证据、时间戳与估算公式，由 `ar` 做出路由决策；`ac` 亦可独立于 `ar` 供任何开发者工作流或 Agent 框架使用。所有事实均包含明确的新鲜度元数据（`retrieved_at`、`as_of`、`row_version`），未知费率显式呈现为 `null` 而非零，计算过程坦诚汇报缺失字段与假设，不作无依据推断。

## 公共 HTTP 服务（1.2）

AR 可以直接通过 HTTP 读取事实，无需安装 AC、CLI 或消费 key。管理员在本地调研与审核；线上只读，不执行 AI，不存模型 API key。私人实付价与剩余额度保留在用户的 AR。

公网 URL 尚未部署。可从源码运行同一公开入口：

```sh
uv sync --locked
uv run uvicorn agent_costbook.public_api:create_public_app --factory --host 127.0.0.1 --port 8080 --no-access-log
curl --fail http://127.0.0.1:8080/v1/info
curl --fail http://127.0.0.1:8080/v1/capabilities
```

公开库覆盖 17 条 OpenRouter 通道报价、5 个 Agent、33 条 model-effort。返回 `public-reference`，不代表个人实付；缺 usage 不猜测。12 个模型的条件价暂未纳入计算，AA 数值与私有配额不随包公开。见[公开契约](docs/design/public-service-1.2/spec.md)、[真实来源与缺项](docs/research/public-catalog-20261008.md)、[Docker 与部署](docs/deployment.md)。本地管理服务仍支持原有鉴权与 M0–M7；公共服务只开放 M0–M6。

## 安装

`agent-costbook` 需要 Python 3.12+ 环境（可通过 [uv](https://docs.astral.sh/uv/) 自动管理与下载）。

### 独立 CLI 工具安装（推荐）

通过 Git 直接安装 `ac` 工具：

```sh
uv tool install git+https://github.com/JarvanAI/agent-costbook.git@v1.1.0
```

> [!NOTE]
> Git 安装方式已固定至验证过的发布标签 `@v1.1.0`。PyPI 待发布；当前使用上述 Git 安装命令。

安装后同时提供短命令 `ac` 与长命令别名 `agent-costbook`：

```sh
ac --version
```

### 源码检出安装（开发与数据维护）

用于代码贡献、运行测试或制作目录批次：

```sh
git clone https://github.com/JarvanAI/agent-costbook.git
cd agent-costbook
uv sync --locked --extra dev
uv run pytest -q
```

## 1 分钟上手试玩

安装后无需任何配置、无需启动服务、无需数据库连接、无需网络与 API 密钥，直接在终端中运行一条合成估算命令：

```sh
# 方式 A：通过 uvx 直接试玩（无需先安装到系统）
uvx --from git+https://github.com/JarvanAI/agent-costbook.git@v1.1.0 ac demo

# 方式 B：使用已安装的 ac CLI
ac demo
```

### 预期结果

命令使用包内自带的合成数据资源，输出 M4 方法的估算金额（结果摘录）：

```json
{
  "publisher_id": "pub_sample",
  "synthetic": true,
  "results": [
    {
      "candidate_id": "synthetic-m4",
      "status": "ok",
      "method": "M4",
      "metrics": {
        "cost": "0.0177",
        "currency": "USD"
      }
    }
  ]
}
```

## 三个使用入口

### 1. 开发者入口：本地服务与命令行

通过单条命令完成初始化并启动前台回环服务（`127.0.0.1:8080`）：

```sh
# 单命令生成随机凭据配置、空数据库并在前台启动服务
ac serve --setup

# 检查本地配置、数据库连接与目录覆盖状态（只读诊断）
ac doctor

# 对运行中的本地服务发起在线估算
ac estimate --server http://127.0.0.1:8080 --request request.json
```

详见 [快速上手指南](docs/getting-started.md) 与 [服务与 CLI 参考](docs/reference.md)。

### 2. 查询 Agent 与 Router 入口：只读事实与能力查询

外部 Agent 可以通过 CLI、本地 HTTP 接口或标准 Skill 查询价格和能力事实：

```sh
# 查询模型价格（支持按 provider/model 过滤）
ac query prices --provider openai --model openai/gpt-4o-mini

# 查询模型与 effort 档位的能力与评测分
ac query model-efforts --provider openai --model openai/gpt-4o-mini

# 查询已记录的 Agent 能力
ac query agents --agent-id agent-1
```

也可将只读查询 Skill 安装到 Codex 或 Cursor 环境：

```sh
# 用于 Codex
npx skills add JarvanAI/agent-costbook --skill costbook-query --agent codex --global

# 用于 Cursor
npx skills add JarvanAI/agent-costbook --skill costbook-query --agent cursor --global
```

详见 [Agent 接入指南](docs/guides/agent-integration.md) 与 [costbook-query Skill](skills/costbook-query/SKILL.md)。

### 3. 数据维护者入口：受审批次更新

通过严格受审的六步工作流维护与更新真实价格和能力目录：

```sh
# 计划 -> 验证 -> 比对 -> 备份并应用 -> 核验
ac data plan --mode init --scope scope.json --out batch-dir
ac data validate --batch batch-dir
ac data diff --batch batch-dir --server http://127.0.0.1:8080
ac data apply --batch batch-dir --approved-diff-sha256 PLAN_SHA256 --server http://127.0.0.1:8080 \
  --backup-source "$ACB_DB" --backup-destination batch-dir/backup.sqlite3
ac data verify --receipt batch-dir/receipt.json --server http://127.0.0.1:8080
```

详见 [目录维护指南](docs/guides/catalog-maintenance.md) 与 [costbook-initialize Skill](skills/costbook-initialize/SKILL.md)。

---

## 架构与工作流

```mermaid
flowchart LR
    S["Official sources / external research"] --> A["Local administrator review"]
    A --> P["Reviewed SQLite + manifest"]
    P --> H["Anonymous public HTTP / Docker"]
    H --> R["AR: Agent / model / effort decisions"]
    R --> L["Private prices, quota and assets stay local"]
    P --> O["Other HTTP consumers"]
```

---

## 已支持与计划能力差异

| 能力 / 领域 | 状态 | 说明 |
| --- | --- | --- |
| 可追溯价格、证据与研究 Markdown | 已支持 | 保存在 SQLite 中；证据与研究文本通过 `/v1/evidence/{id}`、`/v1/research/{id}` 读取 |
| 能力目录（Agent 与 model-effort；公共服务匿名读，本地管理服务鉴权） | 已支持 | 通过 `ACB_READ_TOKEN` 或 `ACB_ADMIN_TOKEN` 读取；通过 `ACB_ADMIN_TOKEN` 写入 |
| 本地 CLI 查询命令 | 已支持 | `ac query prices`、`agents`、`model-efforts`、`evidence`、`research` 支持 JSON 与表格展示 |
| 成本估算公式（M0–M7） | 已支持 | `ac-formulas-v2` 支持 M0–M7；M7 需要授权观察数据，旧快照支持较少方法 |
| 离线价格快照估算 | 已支持 | 通过 `ac estimate --snapshot` 在本地直接计算，无需服务后台与网络 |
| 在线服务估算 | 已支持 | 通过 `ac estimate --server` 直接连接本地 HTTP 服务计算并附带能力数据 |
| 独立合成试玩 | 已支持 | `ac demo` 无需任何配置与写库，直接运行包内合成用例 |
| 目录初始化与批次更新 | 已支持 | 通过 `ac data` CLI 与 Skills 编排（`plan → validate → diff → backup → apply → verify`） |
| 只读消费 Skill | 已支持 | 为 Codex / Cursor 准备的标准 `costbook-query` Skill |
| 离线能力目录同步 | 提案中 | 能力目录当前需通过运行中的本地服务 API 访问；未提供离线快照同步合约 |
| 组合评测（Coding Agent Benchmark） | 提案中 | 包含 harness + 模型 + 设置的复合评测记录需独立的 schema 扩展（见设计提案） |
| 周配额计数器与 Token 额度计量 | 提案中 | 原始额度观察存放在本地暂存区；尚未实现数据库内的独立配额计量模型 |
| 更广泛的来源调研 | 外部工作流 | 人工或外部 Agent 调研并提交批次；内置采集器只有有限的 OpenRouter 范围 |
| 自动化路由与任务派遣 | 消费端职责 | 属于消费端（如 `agent-router`）的职责；本仓库未做派遣验证，不作派遣承诺 |

---

## 文档

- [快速上手指南](docs/getting-started.md)：合成试玩、本地服务搭建与基础查询。
- [Agent 接入指南](docs/guides/agent-integration.md)：外部 Agent 与 Router 查询价格、能力与估算的接入方式。
- [目录维护指南](docs/guides/catalog-maintenance.md)：真实数据的批次计划、比对、安全备份与应用流程。
- [服务与 CLI 参考](docs/reference.md)：完整服务运行、公式计算、数据库迁移与命令行参数参考。
- [数据来源与权利](docs/data-sources.md)：新鲜度、来源与公开/私有数据边界。
- [贡献指南](CONTRIBUTING.md)：代码修改与数据纠正流程。
- [安全政策](SECURITY.md)：本地运行与漏洞报告。
- [1.1.0 开发者体验发布说明](docs/releases/developer-experience-1.1.md)：1.1.0 GitHub 已发布；PyPI 待配置。
- [源码发布说明](docs/releases/source-launch-20261002.md)：初始开源发布范围与验证记录。

---

## 许可证与支持环境

- 本项目原创代码、文档与合成样例采用 [MIT 许可证](LICENSE)。
- 所引用的第三方评测、厂商价格表与社区讨论保留其原始权利与许可证；本项目的开源许可不覆盖 Artificial Analysis 的数据或其它第三方数据集（详见 [数据来源与权利](docs/data-sources.md)）。
- 平台支持：已在 Linux 环境实测验证（Linux 默认配置路径为 `~/.config/agent-costbook/config.json`；其他操作系统尚未验证，需通过 `ACB_CONFIG` 或 `--config` 显式指定配置路径）。新建的缺失目录使用权限 `0700`，既有父目录保留其原始权限；配置文件与数据库文件使用权限 `0600`。
