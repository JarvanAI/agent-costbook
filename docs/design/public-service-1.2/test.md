# 公共服务验证文档

基线1.1.0原生测试223项。新增行为先失败再实现；配置、文档与标准Docker声明做实际命令验证，不添加镜像实现细节的测试。

## 协议与隔离

- 临时独立公开库，public factory 匿名返回真实 exporter snapshot；复算 canonical records hash，schema/kind/版本正确。未知 snapshot 404，旧版本不回退。
- 无 token 查询 Agent/model-effort 与列表；null 档不当 low；缺行404；有无价格/usage均独立读能力。
- 同一估算批次冻结价格版本，保留逐候选 ok/missing、decimal/null/source/time/version；top/result public-reference，不变为实际个人费用。
- 缺 usage/extra_cost 的例子由真实引擎生成，不填写猜测；一条真实公开可计算候选、一条未知模型在同批中部分成功。
- public 不执行 private overrides/M7/observations/contributions/PUT/publish；拒绝未知私密字段；422不包含给定私密测试字符串。
- local private factory 的 admin/read/public端点及M7行为不变。原 fetch 安全策略不增加私网开关。

## 发布物与资源

- public DB 验证 manifest hash/publisher/schema；拒绝篡改、观察记录、未发布贡献和未审核来源。私有live DB不用于bootstrap。
- 同一固定DB在多个进程/容器冷启动、重启/重建后 publisher/价格版本/hash/能力版本稳定；消费期间DB文件哈希不变。
- body>64KiB含chunked分块时413；candidate>64为422；实例达到60/60s返回429/Retry-After，窗口后恢复。limit scope明确为instance，不用用户可伪造XFF来宣称可靠IP限额。
- ETag/304与实际版本对应，历史/当前缓存政策明确；service健康与无目录coverage独立。
- 容器非root、read-only、无模型APIkey/env/source凭据，不启动AI/collector。PORT切换、SIGTERM、API健康与纯HTTP消费可验证。

## 原生检查

`uv run --locked pytest -q`、`uv build`、`scripts/check-docs.py`、`scripts/check-demo.py`。新增 `tests/test_public_api.py`、`tests/test_public_data.py` 与实际容器 smoke；最后全套一次。保留Linux/Python3.12/3.13证据，不假装覆盖所有平台。

## 云与真实数据验收

- 真实公开来源具有原URL/采集时间/原作者摘要/字段缺项；AA许可未核实及私人资料排除。不把synthetic fixture当真实API价格。
- 用无AC源码/CLI/管理key的HTTP客户端查询公网URL/版本/能力并计算已知公开参考成本；错误版本/无usage/缺能力/服务不可达可区分。
- 验证免费plan/无收费开关、部署权限、公开production URL、重启/冷启动数据保留、更新/回滚/备份路径。账号未就绪只记录本地证据，公网验收未完成。
- 给AR交完整request/response/status fixture、OpenAPI与实际缺项。AR自己的真实Jev推荐不由本侧替代。

## 协议复核补充

- 完整响应 metadata变化但records hash未变时ETag变化；单行ETag不受其他行变化影响。弱If-None-Match/列表/*按RFC与实际字节强ETag匹配，304无正文且cache header保留。
- 内部snapshot ID404；公开估算无record_snapshot_id；缺当前snapshot503且能力仍可读取。currency未传422，private factory仍保持原行为。
- SQLite单文件DELETE journal，观测/collector jobs/未发布贡献拒绝；capability表/schema/publisher/hash明确验证，无启动修复。
- Docker80/8080由PORT显式校准；Vercel原生ASGI入口和依赖/资源打包验证，VCR有价不当作免费权益。
