# 首次使用与开源体验调研（2026-10-05）

状态：独立审阅与候选建议，保留作为 2026-10-05 历史调研底稿；推荐方案已在 [开发者体验设计](../design/developer-experience.md) 确认并执行中。批准状态与执行以该设计为准。

## 调研来源

用户指定 Cursor CLI + Opus 5.5。本轮先通过 `cursor-agent models` 确认账户可用 ID，再实际执行 `cursor-agent --print --mode ask --model claude-opus-5-5-high --trust --output-format text`。调用正常退出（exit 0），未修改产品代码。任务限定读取仓库公开文件，不读取私有运行数据或凭据。

Cursor 的网页抓取和搜索工具被拒绝，其外部链接不能当作已完成的网页调研。下列外部来源由协调 Agent 独立浏览核实；Cursor 本仓文件观察与其候选意见在后半部保留。

## 协调 Agent 补查的一手来源

读取日期：2026-10-05。这些是发布者文档与 README，不是对其完整实现的审计，也不能据此承诺获星效果。

| 来源 | 本次核实 | 采用建议 |
| --- | --- | --- |
| [uv 工具指南](https://docs.astral.sh/uv/guides/tools/) | `uvx --from` 支持包名与 Git 来源；`uv tool install` 可将工具安装到独立环境并提供 CLI | 试玩与长期安装分开；固定已存在的 release tag，不要求新用户 clone |
| [Agent Skills 规范](https://agentskills.io/specification) | Skill 是带 SKILL.md 的目录，可带 scripts/references/assets；必要依赖应有说明 | 新查询 Skill 自带关键参考，安装后不依赖源码目录里的文档 |
| [skills CLI](https://github.com/vercel-labs/skills) | 支持仓库来源、具体 Skill、项目与全局安装、列出与更新 | 延续通用 Skill 安装方式；Skill 安装与运行服务分别说明 |
| [LLM README](https://github.com/simonw/llm) | README 提供 pip/Homebrew/pipx/uv 安装、首条执行与详细文档入口 | 把安装和第一条可运行命令放前面，开发环境说明移到 CONTRIBUTING |
| [FastMCP README](https://github.com/PrefectHQ/fastmcp) | 安装、quickstart、Server/Client 用途与详细文档分别有入口 | 借鉴旅程清晰度，不能据此把本轮变成 MCP 项目 |
| [Models.dev README](https://github.com/anomalyco/models.dev) | 直接提供 API 示例，区分基础模型事实与 provider 服务数据，并说明贡献路径 | 展示查询结果和数据来源；解释 ac 的证据/版本/估算价值，避免仅宣传另一个价格表 |
| [GitHub topics 指南](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics) | topics 让用户查找相关仓库 | 使用符合实际功能的主题；不把设置 topics 当作传播效果的保证 |
| [PyPI Trusted Publishers](https://docs.pypi.org/trusted-publishers/) | 可以配置受信任的发布工作流 | 若维护者同意 PyPI，发布配置与权限准备单独验证，不在文件里存长期发布 token |

本轮匿名读取 `https://pypi.org/pypi/agent-costbook/json` 返回 404。这只表示该公开接口没有对应项目，不能证明名称必然可注册，也不证明已有发布权限。

## 采纳与调整

- 采纳薄入口、wheel 内合成资源、默认用户数据目录、旧 factory 默认行为兼容、前台服务和打包后端到端验证。
- 采纳初始化命名问题：用拟议 `ac setup` 表达建立配置和空库；真实目录填充继续用既有 `ac data ... --mode init`。不将空库创建报告为资料初始化完成。
- 不采纳把能力查询延期到 P1 的排序。用户本轮要求消费入口，Agent/model-effort 查询必须一起验收；推荐沿用 HTTP，并增加可选只读凭据，避免增加一套直接 SQLite 消费合同。
- Cursor 候选例子再次要求用户从源码复制 examples、手动生成 token，与“不 clone 即可使用”目标不一致。推荐 setup 管理本地受限配置，并继续接受已有环境变量；查询结果和 doctor 不显示 token。
- 不采用复制演示文件后测试字节相等的长期双维护方案。包内资源应成为演示的单一来源；已有历史兼容快照继续留在测试中，不需要把它们变成新演示数据的永久镜像。
- `agent-costbook` 长命令别名可兼容增加；Cursor 对 GNU acct 命令冲突的说法尚未本轮验证，不能据此声称本机发生冲突。
- PyPI 本轮是否发布、公共数据是否成为长期维护产品，由维护者确认。平台支持按实际 CI/实测声明，不因为采用跨平台目录库就宣称全部系统可用。
- 宣传素材、渠道和 star 趋势属于发布后的传播工作；本轮验收以安装、查询、兼容性与数据语义为准。

## Cursor 原始独立审阅

以下保留完整输出。里面的命令、P0/P1 与决策是 Cursor 候选，不是项目已批准或已执行的内容；外部事实的证据限制以开头说明为准。

我没有修改任何文件。我的建议是这轮只补一个"薄入口层"：查询 Skill、`serve`、数据库初始化、`doctor`、`demo` 这几个 CLI 子命令，加上随 wheel 分发的合成样例。不新增框架、数据库、托管服务或调度器，现有 CLI 和 HTTP 合同保持不变。

需要先说明的限制：Web 抓取和网页搜索都被拒绝了，所以外部项目和标准**一律没有实际浏览核实**。第 8 节的 URL 只是建议你去核对的来源，不是调研结论。

---

## 1. 当前阻力（已在仓库核实）

1. **入口分散，没有启动命令。** 有四个脚本入口：`ac`、`agent-costbook-export`、`agent-costbook-backup`、`agent-costbook-migrate`（`pyproject.toml:26-30`）。`ac` 只有 `estimate`、`collect`、`data` 三个子命令（`offline.py:331-345`），没有 `serve`、`init`、`doctor`、`--version`。启动服务要手写 uvicorn factory 长命令（`docs/reference.md:23-30`）。
2. **试玩离不开源码检出。** 合成快照放在 `tests/fixtures/`，wheel 不包含它。README 的快速上手还要求 `--extra dev`（`README.md:26-29`），可试玩并不需要 pytest 或 httpx。安装冒烟测试只检查入口文件存在（`test_install_smoke.py:103-110`），没有从 wheel 运行 demo。另外，这份快照用的是旧公式集 `ac-formulas-v1`，第一印象就是"不支持 M3/M5/M7"。
3. **数据库默认路径跟着当前目录走。**

```16:16:src/agent_costbook/settings.py
        db_path=Path(os.environ.get("ACB_DB", "agent_costbook.sqlite3")),
```

   而 `Store` 打开时会自动建目录和建库（`store.py:143-147`）。在不同目录启动就会悄悄生成不同的空库。将来用 `uv tool install` 安装后，这个问题会更明显。
4. **token 为空时服务照常启动。** `_authorized` 在 token 为空时直接返回 False（`api.py:45-49`）。结果是写入和能力读取全部 401，但启动时没有任何提示。
5. **能力目录只能用管理员 token 读。** `/v1/capabilities*` 全部走 `_require_admin`（`api.py:181-254`）。ar 想只读能力，也得拿一个可写的 token。离线估算里能力固定为 `unavailable`（`offline.py:261`）。
6. **没有面向消费者的 Skill。** 现有的 initialize 和 contribute 都是写入流程。估算和查询的说明夹在 contribute 那份 Skill 里。
7. **"真实数据"之后用户拿不到现成价值。** 采集器只固定解析 `openai/gpt-4o-mini`（`collectors.py:10-12`）。仓库不分发任何真实目录，用户必须自己调研、走批次流程。这是传播的最大门槛，靠改代码解决不了。
8. **命名冲突。** 设计文档明确写了"初始化指建立真实价格及能力数据，不指创建数据库"（`docs/design/catalog-initialization-update.md:3`）。如果再加一个创建空库的 `ac init`，会和 `ac data plan --mode init` 混淆。另外，Linux 上的 GNU acct 包也有一个叫 `ac` 的命令，这一点来自我的已有知识，本轮没有核实。
9. **操作系统证据只有 Linux。** CI 只跑 `ubuntu-latest`（`ci.yml:12`）。冒烟测试硬编码 `venv/bin/python`（`test_install_smoke.py:85`），备份依赖 0600/0700 这类 POSIX 权限语义。

## 2. 推荐用户旅程（以下命令都是提议，尚未实现）

**A. 30 秒试玩：不克隆、不建库、不联网取数据**

```sh
uvx --from "git+https://github.com/JarvanAI/agent-costbook@v1.1.0" ac demo
```

输出合成估算结果，并标明"合成数据，不是报价"，再给出下一步。`demo` 只读包内资源，绝不写用户的库。

**B. 真实数据：服务在前台运行，空库和样例分开**

```sh
uv tool install "git+https://github.com/JarvanAI/agent-costbook@v1.1.0"
ac db init                      # 只建空库；打印库路径、来源（参数/环境变量/默认）和 publisher_id
export ACB_ADMIN_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
ac serve                        # 固定 127.0.0.1:8080，前台运行；不启动 collector，不后台化
ac doctor --server http://127.0.0.1:8080
cp examples/data-scope.json my-scope.json   # 然后走现有的 plan→validate→diff→apply→verify
```

在线来源仍然只能走现有的批准批次流程。`ac collect` 依旧需要用户显式调用（或自己配 cron）。

**C. Agent 使用**

Agent 加载新的 `costbook-query` Skill。这个 Skill 只能调用 `ac estimate`、`POST /v1/estimates`、`GET /v1/catalog`、evidence 和 research 接口，以及（P1 才有的）本地只读能力查询。它不包含任何写操作，也要教 Agent 正确理解 `null`、`conflict`、`retrieved_at`、`missing_fields`。

**D. ar 调用**

ar 优先用离线方式：`agent-costbook-export --db … > snap.json`，再用 `ac estimate --snapshot`，决策时固定记录 `publisher_id`、`snapshot_id`、`content_sha256`。需要能力数据时，在线走 HTTP；如果 ar 和 ac 在同一台机器，也可以本地只读查询（P1）。

## 3. 最小代码与目录调整

- **新增 `src/agent_costbook/local.py`**。照搬 `data_batch.register` 的注册模式，提供 `serve`、`db init`、`doctor`、`demo`。`offline.py:main` 只多一行注册。
  - `serve`：构造 `Settings` 后调用 `uvicorn.run(create_app(settings))`。只允许 loopback 地址。启动信息里只显示 token"已设置/未设置"，不打印值。
  - `doctor`：用 `Store(path, readonly=True)`（`store.py:118-137` 已经支持只读，export 也在用）。检查库路径和来源、schema 版本（遇到 `migration_required` 只报告，不自动迁移）、`publisher_id`、`data_version`、记录数、token 是否设置、可选的 `/health`、在 POSIX 上提示库文件全局可读、当前目录下是否残留旧库。不读 `.env`。
- **`settings.py` 新增 `resolve_db_path(cli)`**，优先级是 `--db`、`ACB_DB`、平台用户数据目录。`load_settings()` 和 `create_app()` 的旧默认值在 1.x 内保持不变，旧的 uvicorn 命令行为也不变。
- **新增包内资源 `src/agent_costbook/demo/`**：快照和请求文件。`tests/fixtures` 里的原件保留，并加一个字节相等测试，防止两份内容漂移。
- **新增 `skills/costbook-query/SKILL.md`**，并把 contribute 里的纯估算说明改成链接到这里。
- **`pyproject.toml`** 增加入口 `agent-costbook = agent_costbook.offline:main`，避开 `ac` 可能的命名冲突，原有 `ac` 不删。版本号升到 1.1.0，API v1 和快照 schema 1 不变。
- **测试**：扩展 `test_install_smoke.py`，从 wheel 安装后实际跑 `demo`、`db init`、`doctor`、`serve`；同时断言 `FROZEN_PATHS` 和旧默认路径没变。

这一轮不需要动 `store.py`、`api.py` 的路由，也不需要动 `data_batch.py`。

## 4. README 首屏与文档树

README 首屏建议的顺序：

1. 一句话定位，保留现有标语。
2. 三点为什么：每条价格都有来源和时间；未知就是 `null`，不填 0；快照带版本和哈希，估算可复现。
3. 三行试玩命令，就是上面的 A。
4. 结果片段，并注明合成数据。
5. "接下来"三条链接：真实数据、Agent Skill、ar 和程序集成。
6. 支持矩阵：写"Linux 由 CI 验证"，macOS 和 Windows 写"未验证"。

已支持/计划中的长表格移到首屏之后。快速上手去掉 `--extra dev`；源码开发的命令移到 CONTRIBUTING。

文档树最小改动如下：

```text
README.md / README.zh-CN.md   首屏 + 三条旅程
docs/integration.md           新增：ar/Agent 应固定的字段、离线与 HTTP 的取舍、能力权限
docs/reference.md             顶部加目录；serve/doctor/db init 加一节，其余不重写
skills/costbook-query/        新增
```

## 5. 优先级与验收

**P0（本轮）**

- 用 `costbook-query` Skill 加文档检查：验收是 Skill 里没有 PUT、贡献或发布类写调用，`check-docs.py` 通过。
- `demo`：在没有检出目录的干净虚拟环境里，从 wheel 安装后能输出 `0.0177 USD`。
- `db init`：可以重复运行；遇到已有库只报告，不覆盖；不写入任何样例。
- `serve`：前台运行；非 loopback 地址被拒绝；SIGTERM 能退出；不启动采集；旧 uvicorn 命令照常可用。
- `doctor`：运行前后库文件字节不变；任何输出里都没有 token 值；退出码有区分。
- `agent-costbook` 别名入口、版本 1.1.0、Git tag，并写清"只验证了 Linux"。

**P1**

- macOS CI 矩阵。
- Skill 打进 wheel，提供 `ac skills path` 或 `ac skills show`，只打印，不自动写入任何 Agent 的配置目录。
- 本地只读能力查询 `ac capabilities --db`。
- 换成 v2 公式、包含 2-3 个候选（API 按量 vs 订阅）的合成演示。
- PyPI 发布（Trusted Publishing）。
- 给依赖加上限，或在文档说明 `uv tool install` 可能不遵循 `uv.lock`（未核实）。

**延期**

- MCP 服务和只读 HTTP token（看第 7 节决定）。
- 宣称 Windows 支持。
- Docker、文档站、后台守护进程、自动爬取、更多 collector。
- 公开真实价格快照（看第 7 节决定）。

## 6. 传播策略（获星只是观察指标，不是承诺；我不会代你发布推广）

- **讲清差异点**：可追溯、不编造（未知是 `null`，冲突显式标出）、可复现（快照哈希）、离线可用。不要说"实时全量价格"。
- **准备素材**：A 路径的 30 秒终端录屏；一篇"为什么 Agent 路由需要有来源的成本事实"的短文（你来写和发）；GitHub topics，例如 `llm-pricing`、`agent-skills`、`cost-estimation`。
- **可以考虑的渠道（都要你决定、你亲自操作）**：Agent Skills 目录、awesome 列表、Show HN 或相关社区。
- **观察指标**：首次运行成功率（加一个 issue 模板"首用失败"）、Skill 的使用反馈、star 和 clone 趋势。真正的长期驱动力是第 7 节的公开数据问题，不是文案。

## 7. 需要你决定的事项

1. **首发渠道**
   - 选项：仅 Git tag，再在 P1 上 PyPI / Git tag 和 PyPI 同时发。
   - **推荐**：先发 Git tag。
   - 理由：PyPI 的版本号发出去就不能重用，而且只验证了 Linux。PyPI 上 `agent-costbook` 这个名字是否可用，本轮没有核实。
2. **默认数据库位置**
   - 选项：平台用户目录（只对新命令生效）/ 维持当前目录。
   - **推荐**：新命令用平台用户目录，旧 factory 不变。
   - 理由：数据要独立于检出目录，同时保持旧合同。用 `platformdirs`（一个纯 Python 依赖）还是自己写十来行 XDG 和 macOS 路径逻辑，由你选。
3. **能力读权限**
   - 选项：本地 SQLite 只读 CLI / 拆分只读 HTTP token / 做 MCP。
   - **推荐**：本轮只做本地只读 CLI，而且放在 P1。
   - 理由：能读库文件的人本来就能读全部数据，文件权限就是边界。只读 token 等 ar 跨用户或跨容器运行时再做。MCP 会多一套合同和依赖。
4. **token 怎么保存**
   - 选项：只用环境变量 / 在用户配置目录写一个 0600 权限的 token 文件。
   - **推荐**：只用环境变量。
   - 理由：和现有合同一致，也避免 Windows 上权限语义未验证的问题。
5. **是否在后续发布经过权利审查的公开价格快照**（只含官方文档和 OpenRouter 价格行，不含 AA 数据）
   - **推荐**：本轮不发，作为 P1 候选，先审查来源条款。
   - 理由：这是真实采用的最大杠杆，但维护负担和权利风险都要你来承担。

## 8. 来源与证据限制

**已核实**：只有本仓库公开跟踪的文件，就是正文里引用的这些。`git ls-files` 被拒绝，文件清单是用 Glob 得到的。

**未浏览核实**：WebFetch 和 WebSearch 都被拒绝。下面的借鉴点来自我的已有知识，执行前请逐一核对：

- uv 的工具安装和 `uvx --from git+…@tag` 语法：https://docs.astral.sh/uv/guides/tools/ 、https://docs.astral.sh/uv/concepts/tools/
- Agent Skills 规范：https://agentskills.io/specification
- `npx skills` 安装器：https://github.com/vercel-labs/skills
- `llm` 的首用体验（`llm keys set` 把配置放在用户目录）：https://llm.datasette.io/en/stable/setup.html
- FastMCP 快速上手（只作为 MCP 延期时的参照）：https://gofastmcp.com/getting-started/quickstart
- 平台用户目录：https://platformdirs.readthedocs.io/
- PyPI Trusted Publishing：https://docs.pypi.org/trusted-publishers/

其他没有实测、只是推断的点：hatch 默认会把包目录里的非 `.py` 文件打进 wheel、`uv tool install` 不使用 `uv.lock`、`ac` 和 GNU acct 的命名冲突。这些都需要在 P0 用安装冒烟测试来确认。
