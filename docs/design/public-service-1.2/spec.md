# AC 公共服务规格 1.2

日期：2026-10-08。Human 已确认公开服务边界，AR 接受 public-reference、缺 usage 不猜测、独立审核公开库三点挑战。基线 `30f7764` / 1.1.0。本文冻结最小消费契约；云账号与线上验收仍待实际回执。

## 目标与边界

AR 默认用 HTTP 消费公开价格、Agent/model-effort 事实及确定性估算，无 AC checkout、CLI 或消费 key 前置。保留两仓独立，AR 的用户资产、私人实付价、剩余额度、合法候选、偏好、Jev 和派遣不转入 AC。AC 不接收任务正文、账户清单或模型 key，不执行 AI。

既有本地服务、管理写 API、公式、SQLite schema 2、价格 snapshot schema 1 与 read/admin token 行为继续兼容。新增独立 public factory，独立公开 SQLite 发布物；不能对混合本机库直接打开匿名读取。

## 发行和持久性

首发采用本地管理员写入与审核 → 固定公开 SQLite/manifest 发布物 → Docker/云部署。线上只读，没有在线写库或 AI，也不存模型 API key；管理员授权由本地管理 API 与仓库/部署权限执行。既有 authoring DB 保存历史，更新从上一公开版继续，价格 publisher、版本和行版本不因冷启动改变。数据更新发布新镜像/部署；已发布版本不改写，可回滚。

公开发行物需明确审核，manifest 固定文件 SHA256 与 publisher。验证 schema、发布版本、无 observations、无未发布贡献/collector jobs，拒绝未审核/损坏/混合库。Data builder 不读取用户 live DB、私人观察或 Agent 配置。公开文字是数据，不执行其中指令。仅加入有明确可公开依据的价格与原作者编写的资料摘要；AA 原始数据与许可未核实的 benchmark 暂不随发行物公开。价格记录须注明渠道与适用计费条件；已知条件不能由现有身份/usage明确表达的价目先排除可计算覆盖，不能把标准价当成所有长上下文/地区/模式的实际价。

首选 Vercel Hobby 原生 FastAPI/Python Function，在账号符合个人非商业条件且额度内；复用同一 Python 引擎与审核发布物。Docker 发行与云适配分开：VCR 存储有单独标价，不能因容器支持所有计划就自动启用它；可选容器适配只有实际0新增费用/已含额度经核实才使用。发布物中的只读 SQLite 不依赖可写容器目录或外部云数据库。CF Containers 不满足免费条件，首版不启用。CF Worker/D1 是后续另行评估的运行时适配；不为它重写当前引擎。Render 只读镜像可作备用，需实查免费账户及冷启动，不能把 ephemeral filesystem 称为可写持久库。

Docker 以非 root、只读文件系统运行，通过 PORT（默认8080）绑定0.0.0.0；这独立于保留 loopback 约束的 `ac serve`。数据文件在构建前定版，不在容器启动时创建随机 publisher/record IDs。自托管允许挂载另一个已审核公开发布物。

## 公共 HTTP 契约

API version 1；snapshot schema 1；capabilities/estimate/service envelopes schema 1；服务版本 1.2.0。公共 profile 的以下资源匿名：

| 路径 | 语义 |
| --- | --- |
| `GET /health` | 进程存活；不等同目录完整 |
| `GET /ready` | schema/发布物与公开覆盖就绪；无目录503 |
| `GET /v1/info` | service/API/schema版本、publisher、价格当前版本/hash、能力hash/覆盖、访问模式与实际限额 |
| `GET /openapi.json` | 仅公开消费 API 的请求/响应合同 |
| `GET /v1/catalog?snapshot_id=snap-N` | 复用真正的 `build_document`，返回可校验的 snapshot envelope，不手工补造导出字段；不指定为当前版本，不存在404 |
| `GET /v1/capabilities` | 独立 Agent 与 model_efforts 集合；保留 row_version/source/as_of/null |
| `GET /v1/capabilities/agents?agent_id=ID` | 指定行；不存在404 |
| `GET /v1/capabilities/model-efforts?provider=P&model=M&effort=E` | 指定档；空/未传 effort 命中真实 null 档，不回退到 low。列全档使用集合资源，再按 provider/model 过滤 |
| `GET /v1/evidence/{id}`、`GET /v1/research/{id}` | 仅发行物中已审核且关联已发布公开行的资料；无 draft/private 暴露 |
| `POST /v1/estimates` | M0–M6 一批一种 method/currency/usage；保留原逐候选结果和版本，并匿名附加公开能力 |

公共估算请求保留既有 public candidate 身份字段：candidate_id/provider/channel/model/effort/plan/feature_scope/window_start/window_end/agent_id。字段有界，仅传公开/opaque身份；不传本机清单或 task text。禁止 private_rates、private_subscription、marginal_cash，以及账户余额/模型 key 等额外字段。extra_cost 只接受显式0或缺失，不接收用户私人附加费用；缺失仍交现有公式报告缺项，不默认制造0。usage 仅接受现有公开数字桶（uncached_input/cache_read/cache_write/billed_output/reasoning）与十进制字符串；不是任务文本。

`M7` 为公共 profile 禁用，403；私人覆盖/未知输入422；普通写操作403。在线公共访问不使用 shared read secret；本地 private factory 仍维持1.1.0鉴权。

响应 top level 与各 candidate 明确 `cost_basis: "public-reference"`，不代表个人实付现金、剩余额度或已测任务成功率。价格空/usage 缺失时给真实 missing/status，能力仍独立查询且可附带缺项结果。首发 `usage_assumptions.supported=false`，不新增通用用量表或猜测 Token 数。AR 本地私人事实可作为其自身选择约束，不能冒充 AC 算出的个人实付估算。

公开 currency 必填。公开 snapshot_id 只接受 `^snap-[1-9][0-9]*$`；内部 snap_<uuid> 不可作为公开读版本。公开结果删除 record_snapshot_id，保留公开 snap-N/publisher/data_version；本地API原字段不动。价格一次批量请求冻结同一 snapshot。显式不存在旧版本404，不使用最新替代；价格版本只在同一 publisher 内比较。现有 content_sha256 是 canonical records 数组哈希；能力另算 canonical 集合哈希，actual row_version/as_of 保留。无统一价格+能力总版本。价格文档的 freshness 保持 exporter 的 null；估算结果中 freshness 是现有 retrieved_at/stale:null，均未评估，消费者比较来源时间。无当前价格snapshot时目录与估算503 not_ready，不伪造 snapshot 空壳；显式不存在版本404。能力为零行不等于进程不健康，info/count与404表达缺项。

## 限额、缓存和错误

初始请求体最大64KiB，估算最多64 candidates；实例总请求60/60s，超限429并 Retry-After。限制是实例级，不声称跨实例全局/IP配额；health/ready可独立豁免以免健康探针消耗额度。只使用进程内有界计数，不引入Redis。发布数据只读，无跨实例写锁。

GET snapshot/能力响应以 canonical JSON 实际输出字节计算 SHA256 强 ETag（带引号）；content_sha256 独立为完整性字段，不是ETag。If-None-Match按RFC9110弱比较，支持匹配标签/列表/*，匹配时304空正文重复ETag/Cache-Control。当前目录/能力及单行 max-age=300，无immutable/s-maxage；指定 snap-N 历史版本 max-age=31536000, immutable，可同值s-maxage。单行hash只关联该行；不公开history路由。估算不保存请求或正文，建议 no-store；AR 仅按其完整输入/method/版本一致复用历史结果。默认客户端建议连接3s/总15s并有限退避；免费托管冷启动实际预算在部署验收后报告。

区分403/404/413/422/429/5xx及逐候选错误。422错误不反射 input/private values；HTTP/容器访问日志不记录请求正文、认证秘密或完整敏感query。无云权限时不虚构 URL。

## 交付

Dockerfile/平台适配、独立审核公开数据与覆盖/缺项、OpenAPI、实际生成 fixtures、contract/数据/容器测试、备份与回滚维护文档。云账号就绪后部署匿名HTTPS并回报真实 URL/publisher/版本/冷启动与远端验证；未就绪时这项明确未完成。AR 端真实 Jev 联调归 AR，本侧提供真实公开数据消费证据。

## 冻结的响应字段

`/v1/info` 使用如下键（下例占位值表示合同，不是部署回执）：

```json
{"kind":"agent-costbook.service","schema_version":1,"api_version":1,"service_version":"1.2.0","profile":"public-reference","publisher_id":"pub_<published>","catalog":{"snapshot_id":"snap-1","data_version":1,"content_sha256":"<canonical-records-sha256>"},"capabilities":{"content_sha256":"<canonical-capabilities-sha256>","agent_count":1,"model_effort_count":1},"access":{"anonymous_read":true,"remote_write":false,"admin_publication":"reviewed-artifact"},"limits":{"body_bytes":65536,"candidates":64,"requests":60,"window_seconds":60,"scope":"instance"},"usage_assumptions":{"supported":false}}
```

能力集合在既有 `agents`/`model_efforts` 外加 `kind:agent-costbook.capabilities`、schema_version=1、publisher_id、content_sha256。能力hash只针对 canonical JSON 对象 `{agents:[...],model_efforts:[...]}`，agent按agent_id、model_effort按provider/model/effort（null按空键）排序；行内原字段及 benchmark 顺序保留。单行GET仍为既有行对象，不伪造统一总版本。

估算在现有 top metadata/results 外加 `kind:agent-costbook.estimate`、schema_version=1 与 cost_basis=public-reference；每个result也带cost_basis。不能把 estimate envelope 当 snapshot hash对象。

HTTP失败为 `{"error":{"code":"<stable-code>","message":"<safe-message>"}}`；422可带只含type/loc的验证细节，不含input/context/body值。主要code：public_read_only/private_measurement_unavailable（403）、snapshot_not_found/capability_not_found/evidence_not_found/research_not_found（404）、request_too_large（413）、invalid_public_request（422）、rate_limited（429）、not_ready（503）。逐候选计算失败继续保留原status/missing_fields，与HTTP失败不同。

## 复核修正与平台前置

public factory 是独立请求DTO/响应信封；复用的共享估算函数只做选行、冻结价格、调用run_estimate、附加行。不复制公式，不把CandidateIn的私人字段带入public OpenAPI。只读验证还检查两张能力表、observations/collector_jobs空、贡献均published、schema2、无WAL/SHM依赖、manifest文件hash与publisher匹配；首次数据只收录官方资料和原创短摘要，不由source标签推导许可。已知厂商/AA页面正文再分发权无统一法律结论。

Docker进程默认8080；Vercel容器默认期望80，容器选项必须项目显式PORT一致。默认Vercel原生app.py导出public ASGI app，根目录不放自动触发收费VCR的Dockerfile.vercel；可选模板放deploy/vercel-container目录。Docker run --read-only/--tmpfs只作为本机/自托管验证，不能伪称Vercel运行标志已设置。

Vercel生产别名须匿名可达，不能All Deployments登录保护；plan/项目非商业适用性/配额/VCR实际0费用都需要账号实查。缺授权即交本地合同、Docker与真实公开数据，公网URL保持未部署，不采购付费。

审阅采用完整响应ETag、snapshot freshness null、内部id隔离、独立public DTO与平台端口/收费条件等具体修正。审阅建议的严格强标签比较不采用：If-None-Match的GET按官方RFC使用弱比较；输出本身仍是实际canonical字节的强ETag。
