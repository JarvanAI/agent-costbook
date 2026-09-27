# AC 数据初始化与更新

日期：2026-09-27。本文的初始化指建立真实价格及能力数据，不指创建数据库或启动服务。

阶段 1 已实现：`ac data plan|validate|diff|apply|verify` 覆盖当前价格与能力 API，编排说明在 `skills/costbook-initialize/`。人确认了本设计，并明确不再单写 spec、test doc、exec plan。阶段 2 的联网来源适配和阶段 3 的组合评测、私有配额仍未实现。`plan` 只解析调用者给出的本地 OpenRouter JSON，且现有解析器只认识固定的 `openai/gpt-4o-mini`；它不访问网络，也不把待调研条目标成已采集。

价格行的内部 `snap_<uuid>` 不在 `GET /v1/catalog` 里。`diff` 从 `POST /v1/estimates` 的 `record_snapshot_id` 读取它，并作为 `base_snapshot_id`。没有贡献状态接口。响应丢失后，只有 journal 里已经记下的 contribution id 能通过 `GET /v1/evidence` 证明已发布；尚未发布时，对同一个 id 再次 `publish` 会返回原 snapshot，不会新写一版。没有记下 id 时不会编造或重放。能力 PUT 只有在远端正文与获批目标一致、且 `row_version` 正好是预期的下一版时才确认为完成。

## 本次初始化的边界

已完成公开价格与能力目录首批填充，随后合并了 24 条 AA 模型档评分；研究方法见 [AA 获取指南](../research/aa-data-acquisition.md)。这不是全模型、全 Agent 覆盖完成的声明。私有订阅及配额仍等待用户资料；组合评测、变体和负分等完整记录依赖另行确认的 [评测存储扩展](benchmark-records-extension.md)。缺失项应进入待办清单，不以默认值补齐。

本次验证了可复用的流程：确认范围 → 调研采集 → 整理来源与候选 → 映射和校验 → 预览差异 → 用户确认批次 → 备份 → API 写入 → 读回与重复运行验证 → 保存报告。

## 选择

| 方案 | 好处 | 限制 |
| --- | --- | --- |
| 仅 Skill | 最快，能指导任意 Agent 使用现有 API | 版本合并、重复运行和验收仍易因 Agent 实现而不同 |
| 仅 CLI | 固定来源采集和写入可重复 | 论坛、非官方来源及未知字段仍需 Agent 研究 |
| Skill + 薄 CLI（推荐） | Agent 负责研究判断，程序保证机械步骤一致 | 需要定义一个稳定候选批次合同 |

复用现有 `skills/costbook-contribute/` 的贡献说明，新增初始化/更新编排 Skill；CLI 共用现有 API 合同。Skill 不依赖 AO、Orca 或特定 Agent 品牌。现有固定 OpenRouter collector 可作为来源适配器复用，首版不建立第二个调度系统。

## 输入和产物

输入为范围清单：目标服务、来源、模型/Agent 集合、数据类别、公开/私有范围、更新策略。首次初始化默认公开数据；读取本机 Agent 配置或私有账户资料必须有相应范围授权，凭据只引用环境变量或本地受限配置，不进入批次与公开文档。

批次包含：合同版本、批次 ID、来源及抓取时间、研究 Markdown、证据 hash/引用、原始标签、规范身份映射及依据、候选值、对应远端记录版本、待解决问题。每类记录保留其 API 合同，不把价格、能力、评测混成一种记录。

输出包括机器可读差异、可供用户确认的 Markdown 计划、应用回执与验证报告。报告区分新增/更新/无变化/冲突/暂缓/失败，以及声明范围内的覆盖率；不能以命令成功代替数据完整。

## 命令

```text
ac data plan --mode init --scope scope.json --out batch-dir
ac data plan --mode update --scope scope.json --out batch-dir
ac data validate --batch batch-dir
ac data diff --batch batch-dir --server SERVER
ac data apply --batch batch-dir --approved-diff-sha256 HASH --server SERVER
ac data verify --receipt receipt.json --server SERVER
```

`plan` 生成批次骨架与采集清单，并调用已支持的固定来源适配器；需要 Agent 检索的条目明确标为待调研，由 Skill 指导调用者补充，CLI 不自行假装完成研究。`init` 与 `update` 使用同一条流程，只是前者建立覆盖基线，后者比较已存版本与新证据；init 也绝不清空现有数据。

`diff` 从服务读取当前状态，输出按类别区分的变更计划及 hash。`apply` 必须匹配用户确认的计划；该参数是防止批次被悄悄改动的绑定，不是人类身份认证。Skill 负责取得批次许可，自动化调用者可使用此前明确授予的范围策略。

确认对象是完整 apply plan：包含目标服务身份、范围、批次内容摘要、操作类别、记录身份、基线版本以及完整目标值；展示用 Markdown 不作为 hash 输入。计划采用固定字段排序的 UTF-8 JSON、确定性的记录顺序，金额与评分保留十进制字符串；具体规范由实现及 `tests/test_data_batch.py` 的测试向量约束。

应用前重新核对远端版本。价格沿用 contribution、publish 和 base_snapshot_id；能力沿用 GET 合并与 expected_version。版本变动时暂停相应记录并重做差异，不能静默改写获批计划。相同内容跳过；未在更新范围中的字段保留；来源消失不能自动删除。写前必须有可恢复备份，本地复用既有备份命令，远端备份由服务操作者完成并返回可核验回执；首版不凭空新增远端备份 API。

现有多个 API 不提供跨类别原子事务。因此回执逐步记录已完成写入及 publication ID/row_version；中断后读回判断再续跑。发布成功但响应丢失时先查询确认，不盲目再次 publish。恢复备份是操作者决定的故障恢复，不自动覆盖其他并发写入。

每个操作先落盘 prepared 状态，再发送请求，随后记 confirmed 或 uncertain。价格 contribution 的幂等键由目标服务、操作类别、记录集合、基线与规范请求内容推导，同一请求恢复时复用同一键；已发布价格按业务内容比较，不能因生成了新批次时间而重复发布。publication 核实保存 contribution ID，但不假定存在贡献状态查询端点；只有现有读取接口能唯一证明这次发布时才能确认，否则保持 uncertain 并报告，不能声称成功或自动重发。能力 PUT 响应丢失时，只有远端目标正文精确一致且版本为预期的下一版才确认完成；其他版本前进或正文不符都转冲突。新批次先读回 diff，内容不变不产生任何写操作。

## 资料保存

- `docs/`：公开来源获取方法、字段解释、维护流程、脱敏示例，链接于 AGENTS.md。
- 服务数据库：规范化、可查询的记录与版本。
- 未跟踪的批次目录：采集证据、确认后的差异、回执及私有资料，限制文件权限；原始第三方数据不因代码开源而自动公开。

研究文档解释来源、映射、冲突和缺失的原因；数据库提供精确查询。两者通过来源引用、批次 ID 和内容 hash 关联，不要求公开文档成为数据库逐行镜像。

## 实现顺序与验收

1. **现有数据类别的初始化/更新**：已实现。Skill + CLI 候选合同、validate/diff/apply/verify。验收覆盖空库初始化、已有数据保留、无变化的第二次 apply、旧字段保留、冲突不覆盖、响应丢失后的恢复、错误 token 不进入输出。验证不调用付费接口。
2. **来源适配**：只复用了本地 OpenRouter JSON 解析，而且只覆盖固定的 `openai/gpt-4o-mini`。没有联网抓取，页面结构变化或解析失败保持待研究。AA 仍按公开获取指南由调用 Agent 调研后补充（也可人工补充），不把待研究条目标成已采集。
3. **后续数据类别**：评测存储扩展批准并上线后接入组合评测；私有订阅/配额持久化合同未就绪前只列缺口，不把资料硬塞进公开价格表。当前命令会拒绝这些类别。

已确认并完成阶段 1：Skill + 薄 CLI，先覆盖当前价格与能力 API。评测扩展仍独立推进。初始化或更新由使用者主动触发，没有自动定时写入。

## 本次交付验证（2026-09-28）

Grok 实现，独立复核后修正恢复日志与计划绑定、备份数据库身份、回执覆盖与版本校验、部分写入处理。根协调者运行 `uv run pytest -q`：154 passed。测试使用临时数据库及 HTTP 服务，真实 AC 数据未改动。原有信号退出测试的固定等待改为有界就绪检查，消除启动时序导致的误失败。新增文档无需另拆三份。
