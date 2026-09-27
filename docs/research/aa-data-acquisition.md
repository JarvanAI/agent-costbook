# Artificial Analysis 公开数据怎么读

日期：2026-09-27。本文记录当天实际打开过的公开入口、解开页面数据的步骤，以及 673 / 67 / 20 这三组计数怎么来的。步骤只使用下面的公开 URL。复现时重新 GET 这些 URL，计算正文 sha256，再按标记解开数组。

这些文件目前只在本地工作区。本文没有 commit，也没有 push，也不构成把 Artificial Analysis 分数发布到网上。仓库代码的许可证不覆盖网站上的分数。Free API 页面写的是内部使用、不可再分发；再分发权写在商业套餐上。网站 Terms of Use 2.1 是个人、非商业的访问许可，2.2 限制分发、托管和商业利用网站内容。页脚 PDF `https://artificialanalysiscdn.com/legal/ProDataPlatformTerms.pdf` 当天没有打开，内部目录是否另有许可仍然未知。

存储字段装不下哪些事实，见 [aa-storage-findings.md](aa-storage-findings.md)。

## 三个入口

| 入口 | URL | 2026-09-27 的结果 |
| --- | --- | --- |
| 公开 OpenAPI | `https://artificialanalysis.ai/api/v2/openapi` | 无需密钥，HTTP 200。正文 sha256 `4304ad3759463fb3aaa0baee86920870b4c448f9f86e06daf605780fe9d0a96d`，取回时间 `2026-09-27T14:05:03.854805+00:00` |
| 数据 API | 见下一节 | 不带密钥时 HTTP 401，正文 `{"error":"API key is required"}`，sha256 `f2843e944d421613321b371cdeec502c75d90b23e142a45b9287df7729cfdc49` |
| 公开网页 | 榜页、GPT-6 Astra 模型页、Coding Agent 页 | 模型指数和组合行都在 HTML 的 Next.js flight 数据里 |

数据 API 说明页是 `https://artificialanalysis.ai/data-api`。API 参考页是 `https://artificialanalysis.ai/api-reference`。参考页里的语言模型示例路径是 `GET https://artificialanalysis.ai/api/v2/data/llms/models`。OpenAPI 里的语言模型路径是 `/api/v2/language/models` 一族。两条路径当天不带密钥都返回上面的 401 正文。参考页没有写出 coding-agent 端点。OpenAPI 全文里没有 `effort` 这个词，也没有 `/agents` 路径。

## 官方 API 与 OpenAPI

规格文件可以直接取：

```sh
curl -fsS -D - -o openapi.yaml \
  https://artificialanalysis.ai/api/v2/openapi
```

保存后核对 sha256。规格头部写明它由服务端契约生成。安全方案名是 `ApiKeyAuth`，位置是请求头 `x-api-key`。本文没有使用密钥，也不记录任何密钥。

不带密钥的探测：

```sh
curl -sS -D - -o body.json \
  https://artificialanalysis.ai/api/v2/language/models
curl -sS -D - -o body-free.json \
  https://artificialanalysis.ai/api/v2/language/models/free
curl -sS -D - -o body-data.json \
  https://artificialanalysis.ai/api/v2/data/llms/models
```

当天这三次，以及 `GET /api/v2/data/llms/models?include_reasoning=true`，都是 401，正文都是上一节那个 JSON。

OpenAPI 里和语言模型分数相关的路径是：

- `GET /api/v2/language/models`
- `GET /api/v2/language/models/free`
- `GET /api/v2/language/models/{slug}`
- `GET /api/v2/language/models/{slug}/performance`

同文件还有 provider、媒体和 CritPt 路径。673 行模型表不是从这些路径读到的，因为没有密钥。规格里的模型对象使用 `slug`、`name`、`evaluations`，以及 `intelligence_index_version`。`evaluations` 里能看到 `artificial_analysis_intelligence_index` 和 `artificial_analysis_coding_index`。后者是模型 Coding Index，和 Coding Agent Index 不是同一列。规格示例里的 `intelligence_index_version: 4.1` 是文档样例，不是 2026-09-27 榜页读数。

带密钥时，按规格把密钥放进 `x-api-key`。密钥来自读者自己的 Artificial Analysis 账号。本仓库不保存这个值。

## 公开网页里的模型表

榜页 `https://artificialanalysis.ai/leaderboards/models` 于 `2026-09-27T13:56:16.466257+00:00` 返回 HTTP 200，正文 sha256 `ba7d70a15ef84376e13474cadbf688dda060cb01c22b6e18d9c1ce745dcbfd03`。

页面是 Next.js flight：HTML 里有多段 `self.__next_f.push([1,"..."])`。模型表在被转义的 JSON 里，标记是 `"models":[{"slug":`。在原始 HTML 中它写成 `\"models\":[{\"slug\":`。当天这个标记出现两次，两次都是 673 个 slug，集合相同。先出现的对象只有 `slug`、`name`、`release`、`releaseDate`、`deprecated`、`creator`。后出现的对象才有 `intelligenceIndex`。指数计数以后者为准。

```python
import hashlib
import json
import urllib.request

URL = "https://artificialanalysis.ai/leaderboards/models"
MARKER = '\\"models\\":[{\\"slug\\":'


def unescape(source: str) -> str:
    out = []
    i = 0
    mapping = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "r": "\r", "t": "\t"}
    while i < len(source):
        if source[i] == "\\" and i + 1 < len(source):
            nxt = source[i + 1]
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
            if nxt == "u" and i + 5 < len(source):
                out.append(chr(int(source[i + 2 : i + 6], 16)))
                i += 6
                continue
        out.append(source[i])
        i += 1
    return "".join(out)


html = urllib.request.urlopen(URL, timeout=60).read()
print(hashlib.sha256(html).hexdigest())
text = html.decode("utf-8", "replace")
decoder = json.JSONDecoder(parse_float=str, parse_int=str)
found = []
start = 0
while True:
    at = text.find(MARKER, start)
    if at < 0:
        break
    bracket = text.find("[", at)
    decoded = unescape(text[bracket : bracket + 8_000_000])
    rows, _ = decoder.raw_decode(decoded)
    if rows and isinstance(rows[0], dict) and "intelligenceIndex" in rows[0]:
        found.append(rows)
    start = at + len(MARKER)

models = found[0]
assert all(isinstance(row, dict) and "slug" in row for row in models)
```

`parse_float=str` 把指数留成十进制字符串。当天这 673 行：

| 计数 | 值 |
| --- | ---: |
| 行数 | 673 |
| `intelligenceIndex` 有数 | 664 |
| `intelligenceIndex` 为空 | 9 |
| `intelligenceIndexIsEstimated` 为 true | 492 |
| `intelligenceIndexIsEstimated` 为 false | 181 |
| `deprecated` 为 true | 404 |

这张扁平表没有 `effort` 字段，也没有指数版本字段。`slug` 不加档位后缀时，档位写在 `name` 里。当天读到的对应关系：

| slug | name |
| --- | --- |
| `gpt-6-astra` | GPT-6 Astra (max) |
| `grok-4-7` | Grok 4.7 (xhigh) |
| `grok-4-6` | Grok 4.6 (high) |
| `grok-4-5` | Grok 4.5 (high)，`deprecated` 为 true |
| `gemini-3-8-flash` | Gemini 3.8 Flash (high) |
| `claude-fable-5-1` | Claude Fable 5.1 (Adaptive Reasoning, Max Effort, Default Fallback) |
| `claude-sonnet-5` | Claude Sonnet 5 (Adaptive Reasoning, Max Effort) |

榜页横幅写 Intelligence Index v4.3：AutomationBench-AA 加入，τ³-Banking 移除，Terminal-Bench 改为 4.0。这份 HTML 里 `v4.3.2` 出现 0 次。方法页 `https://artificialanalysis.ai/methodology/intelligence-benchmarking` 写的是 Intelligence Index v4.3.2，包含十项：AA-Briefcase v1.1、GDPval-AA v2.1、AutomationBench-AA、Terminal-Bench 4.0、SciCode、AA-LCR v1.1、AA-Omniscience、Humanity's Last Exam、GDP.pdf、CritPt。行内没有版本字段，所以两句都保留。横幅上的整数和 JSON 原小数也分开：Claude Opus 5.5 的横幅整数是 58，JSON 是 `57.6223698102963`。Grok 4.7 (xhigh) 的 JSON 是 `46.4465506302286`。

## 模型页上的 effort

`https://artificialanalysis.ai/models/gpt-6-astra` 于 `2026-09-27T13:56:18.119295+00:00` 返回 HTTP 200，正文 sha256 `eb83b5fe4ee6c6075b5cda7d1f08a30d7fa56ead380a12632c4ca099bfe83a7a`。

同一套模型目录嵌在这一页里。Intelligence Index 和 `intelligenceIndexIsEstimated` 与榜页按 slug 逐条一致，不一致条数是 0。十项成分分数取自这一页的载荷。sitemap 上的 `canonical_model_url` 只标模型身份；除这一页自己的 slug `gpt-6-astra` 外，当天没有再 GET 那些规范 URL。

结构化 effort 也在这一页，不在榜页扁平表里。转义标记 `\"effort\":{` 当天出现 165 次。对象形状是：

```json
{"slug": "max", "label": "max", "level": 60}
```

当天见到的 level：`minimal` 10，`low` 20，`medium` 30，`high` 40，`xhigh` 50，`max` 60。当前模型对象是 slug `gpt-6-astra`、name `GPT-6 Astra (max)`、effort `max` / 60。共享 `models` 数组里有一个槽位是未展开的 flight 引用，所以只把数组当字典解析时得到 672 个对象，缺的 slug 正是这个当前模型。读这一页时同时保留当前模型对象和共享数组。

没有 `effort` 键的行包括显示名里的 Non-reasoning。缺键是一个空 effort，映射规则在下一节。这一页的已解析对象里，负的 `omniscience` 有 434 条（分母是 672 个解开的对象）。负分留在证据里，目录小数格式见存储说明。

下面这些 URL 当天是 HTTP 404，673 行里也没有同名 slug：

- `https://artificialanalysis.ai/models/grok-4-7-low`
- `https://artificialanalysis.ai/models/grok-4-7-medium`
- `https://artificialanalysis.ai/models/grok-4-7-fast`
- `https://artificialanalysis.ai/models/grok-4-7-build-fast`
- `https://artificialanalysis.ai/models/gpt-6-astra-non-reasoning`
- `https://artificialanalysis.ai/models/gemini-3-8-flash-xhigh`
- `https://artificialanalysis.ai/models/gemini-3-8-flash-max`
- `https://artificialanalysis.ai/models/claude-fable-5-1-minimal`
- `https://artificialanalysis.ai/models/jev`
- `https://artificialanalysis.ai/models/jev-1-13-free`

404 正文是错误页，不补分数。`grok-4-5-low` 和 `grok-4-5-medium` 同样是 404。

## slug、effort、none、Fallback

按这个顺序读，停在第一条命中的规则。

1. 身份先用 AA `slug` 和显示名。无后缀 slug 按显示名里的档读，例如上一节的表。Coding Agent 载荷里的 `hostModelSlug` 是另一套名字，例如 GPT-6 Astra (max) 为 `openai_vega-alpha`，Grok 4.7 (xhigh) 为 `xai_grok-bop-o86-xhigh`，Muse Spark 1.3 (max) 为 `meta_aa_glacier135`。这三列都保留，host slug 单独不作为产品模型 id。
2. 模型页有 `effort.slug` 时，目录档用这个 slug。`level` 只是排序数字。
3. 显示名含 Fallback（Default Fallback、Opus 4.8 Fallback，或组合行标签里的 `with fallback`）时，分数留在变体证据里。当前能力行没有 variant 字段，所以不挂到同名的普通 max 或 high 上。
4. AA 侧 effort 为空时，目录 token `none` 只用于厂商模型页已经列出 `none` 的家族。当天有记录的是 GPT-6 Sol 与 GPT-6 Luna。Sol 页 `https://developers.openai.com/api/docs/models/gpt-6-sol.md`，`2026-09-27T13:13:21Z`，sha256 `5566589803b6f0ac79c00125a7c1af9291e46d5724c1c414f932d36ab92a8ad0`。Luna 页同一研究记录为 `2026-09-27T13:13:22Z`，sha256 `561a86af72eced9a76a4e3a96c45ae7fff7782732514a8429b55ebddb4404945`。显示名 Non-reasoning 收到 `none` 标为 `inferred`。
5. 其余空 effort 不生成目录档。`gpt-5-6-terra-non-reasoning` 与已弃用的 `gpt-5-6-luna-non-reasoning` 记为 `hold_mapping`。`gpt-5-6-sol-non-reasoning` 与 `claude-sonnet-5-non-reasoning` 的 `intelligenceIndexIsEstimated` 为 true，记为 `hold_estimated`，不推断成 `none`。Sonnet 这一行的显示名同时写着 Non-reasoning 和 High Effort。`Grok 4` 的名字里没有档，且 estimated 为 true。
6. 组合行没有单独的 effort 字段。括号里的 `max`、`xhigh`、`high`、`medium`、`low`、`none` 来自显示名。名字里有 `+` 的两行是双模型设置。只有 `displayLabel` 里出现 `reasoning_effort` 时，档位记为只在标签里。括号和标签都没有时，effort 记为缺失。Qwen3.8 Max 的 Max 属于模型名。

`deprecated: true` 并且显示名不含 Fallback 的行记为 `hold_deprecated`。estimated 为 true 的两行没有合并片段。负的 AA-Omniscience 留在不可写入的分数旁，不改成 0。

## 67 条重点候选

67 是当天对照本地已有模型家族筛出的重点行，不是 673 行全表。每条都有来源 URL 和哈希。`import_body` 为 null，`put_ready` 为 0。能通过基准对象形状的片段也还缺整行写入所需的当前版本号。

| 处置 | 条数 | 范围 |
| --- | ---: | --- |
| `schema_valid_candidate` | 36 | 基准对象形状可并入或另案首写。其中 GPT-6 Sol、GPT-6 Luna 的 Non-reasoning 收到 `none`，标记为推断 |
| `hold_variant_mapping` | 11 | Fable 5.1 五档、Opus 5.5 五档的 Default Fallback，以及 Fable 5 的 Opus 4.8 Fallback |
| `hold_deprecated` | 16 | Opus 5 五档、GPT-5.6 Luna 五档、GPT-5.6 Sol 五档、Grok 4.5 (high) |
| `hold_estimated` | 2 | GPT-5.6 Sol Non-reasoning，Claude Sonnet 5 Non-reasoning |
| `hold_mapping` | 2 | GPT-5.6 Terra Non-reasoning，GPT-5.6 Luna Non-reasoning |
| 合计 | 67 |  |

36 条里，家族和档是：GPT-6 Astra 的 low、medium、high、xhigh、max；GPT-6 Sol 与 GPT-6 Luna 各六档（五档加推断的 `none`）；Grok 4.7 的 high、xhigh；Grok 4.6 的 low、medium、high、xhigh；Claude Sonnet 5 的 low、medium、high、xhigh、max；Gemini 3.8 Flash 的 low、medium、high；GPT-5.6 Terra 的 low、medium、high、xhigh、max。Terra 与 Sonnet 5 的这十档当时不在已有能力行里。AA slug `gpt-5-6-terra` 也不是目录里带点号的模型 id，不能自动改名后新建。

人工批准的直接合并边界是 24 条已经存在、档位直接对应、非估计、非弃用、无 Fallback 的身份：GPT-6 Sol 的 low、medium、high、xhigh、max；GPT-6 Luna 的 low、medium、high、xhigh、max；GPT-6 Astra 的 low、medium、high、xhigh、max；Grok 4.6 的 low、medium、high、xhigh；Grok 4.7 的 high、xhigh；Gemini 3.8 Flash 的 low、medium、high。Sol 与 Luna 的 `none` 仍单列，等映射确认。本文不写数据库。

独立交叉核对里曾有一个中间计数 41：Astra 5、Sol 6、Luna 6、Grok 4.7 的 2、Grok 4.6 的 4、Fable 5.1 的 5、Opus 5.5 的 5、Sonnet 5 的 5、Gemini 3.8 Flash 的 3。那 41 把 Fallback 的 Fable 5.1 和 Opus 5.5 也算进“形状上像一条模型档”。修订后的 67 条处置把这 10 条放进 `hold_variant_mapping`，并另把 Terra 五档放进 `schema_valid_candidate`，所以是 36 而不是 41。写入边界以修订后的 24 条为准。

## Coding Agent Index 的 20 条

公开页：

- `https://artificialanalysis.ai/agents/coding-agents`
- `https://artificialanalysis.ai/agents/coding-agents/comparisons/antigravity-sdk-vs-grok-build`
- 方法页 `https://artificialanalysis.ai/methodology/coding-agents-benchmarking`

主页面于 `2026-09-27T14:02:33.723515+00:00` 取回，sha256 `1842b7ac3a7b51f3d0fb87c6c2c49493dd0cd3723d4085ac500241b1755e2580`。比较页正文 sha256 `16a38b0fa0946fd7c78328046b7397ea25101e8ad122841d876bf7d21a3d6da0`。两页在 `2026-09-27T13:56:11Z` 的读数与后来这份主页面哈希一致。

组合行用两个标记，解开方法和模型表相同：

```python
rows = []
for marker in ('\\"benchmarkRows\\":[', '\\"rows\\":[{'):
    at = html.find(marker)
    bracket = html.find("[", at)
    decoded = unescape(html[bracket : bracket + 500_000])
    array, _ = decoder.raw_decode(decoded)
    rows.extend(row for row in array if isinstance(row, dict) and "id" in row)
by_id = {row["id"]: row for row in rows}
```

两页按 `id` 合并后是 20 条，相同 id 的 `indexScore` 没有冲突。方法页写明 v1.5 是 DeepSWE v1.1、Terminal-Bench 4.0、SWE-Atlas-QnA 的等权平均。载荷里的 Terminal-Bench 字段名是 `Terminal-Bench v4` / `terminal-bench-v4`，方法页标题写 4.0，两处都保留。用三项 `pass_at_1` reward 乘权重再相加，和 `indexScore` 的最大绝对差是 `6.6e-16`。单位是 `mean_reward`，20 个 raw 都在 0 和 1 之间。文章里的整数点是 raw × 100 后四舍五入到整数；保留一位小数的分项百分数会和文章差大约 0.4 到 0.6。

20 条都带 harness 版本，而且版本经常按基准分开。标志计数：`with fallback` 1 条，双模型 2 条，effort 缺失 2 条，effort 只在标签里 1 条。按 harness 计：Codex 7，Claude Code 4，Devin Fusion CLI 2，Grok Build 2，Muse Code 2，Antigravity SDK 1，Kimi Code CLI 1，Opencode 1。站点地图里的英文比较页是这 8 个名字的 28 对。

这 20 条的身份是 agent/harness、按基准的 harness 版本、模型显示名、host slug、effort 或双模型设置、Coding Agent Index v1.5、三项 raw reward。`import_status` 全部是 `not_importable_as_model_effort`。Grok 4.7 的模型指数 `46.4465506302286` 和 Grok Build 组合 raw `0.5626764360706693`（整数点 56）是两列。模型 Coding Index 是第三列。

v1.0 的成分是 SWE-Bench-Pro-Hard-AA、Terminal-Bench 2.0、SWE-Atlas-QnA。2026-05-20 的 Cursor Composer 文章用的是这套旧成分，主页面和比较页站点地图都没有 Cursor 的 v1.5 行。Antigravity CLI / IDE 也没有单独行，SDK 那一行不能挪过去。

## 十个仓库

2026-09-27 用 `git ls-remote HEAD` 核对了前九个 commit，与下表一致。第十个是组织搜索没有列出、随后按公开 URL 克隆的仓库。表中许可来自仓库里的 LICENSE 文件，只覆盖代码。

| 仓库 | commit | 代码许可 | 作用 |
| --- | --- | --- | --- |
| https://github.com/EvanZhouDev/ai-model-index | `11c6b4a752d154f8eaeb63aeaa5f7e48804e0ca4` | 无 LICENSE 文件 | 模型指数的 API 包装。当天快照各约 30 行，没有 harness，也没有这 20 条组合 |
| https://github.com/oolong-tea-2026/artificial-analysis-leaderboards | `ed1690f664bd134e6f949b0764e781db0bea7f28` | MIT | 2026-04-10 至 2026-07-10 的模型榜日快照。最新 `llms.json` 570 个模型，文件中没有 Coding Agent |
| https://github.com/tkellogg/model-selection | `9107b0a638c6f6f50169c315b88095ba7dbec5ad` | MIT | 客户端，只请求 `GET /api/v2/language/models/free`。0 行分数表 |
| https://github.com/deyil/artificial-analysis-leaderboards-scraper | `99167bae89b203e427c48f04edcf7366eae04ef5` | GPL-3.0 | 面向 provider `medium_coding` 表的爬虫。仓库内 0 行 CSV，当天没有跑它 |
| https://github.com/romancircus/sota-tracker-claw | `aa4418b30618417044a52a4a1275b9b9bf9ef024` | MIT | `data/aa_llm_latest.json` 30 行，抓取时间早于提交时间，没有 Coding Agent |
| https://github.com/ArtificialAnalysis/Stirrup | `247f24d56b2108235880ed2a2baea5d35b5a67ee` | MIT | Python agent 框架。0 行组合分 |
| https://github.com/ArtificialAnalysis/StirrupJS | `516208963f0a6255bbae5eb8894651a53bd535da` | MIT | TypeScript agent 框架。0 行组合分 |
| https://github.com/ArtificialAnalysis/optima | `28c7b9e1c0f428607c6a5a79334da86815956008` | 无 LICENSE 文件 | 评测相关工具。README 里的 `curl ... \| sh` 没有执行 |
| https://github.com/ArtificialAnalysis/aiperf | `7db2ba37a62aa80c882bc90eaf61cc8073e2387b` | Apache-2.0 | 推理性能 fork。0 行 Coding Agent Index |
| https://github.com/ArtificialAnalysis/aa-agentperf-local | `b1a4fcfa14c753e36b90dcfec801939b7b1afc0f` | Apache-2.0 | 重放 agent 轨迹，测吞吐和延迟。提交时间 `2026-09-27T20:12:10+10:00`。安装命令没有执行 |

组织对象的 `public_repos` 是 5。当天保存的组织搜索返回 4 个仓库：Stirrup、StirrupJS、optima、`aa-agentperf-local`。这次搜索 JSON 没有列入 aiperf，因此它是不是那第五个公开仓库，JSON 本身没有写明。`GET /orgs/ArtificialAnalysis/repos` 先遇到 TLS 超时，重试是 HTTP 403 未认证速率限制。

Hugging Face 用户 `ArtificialAnalysis` 当天有 9 个数据集。datasets-server 的 size 接口给出 AA-LCR 100 行、ITBench-AA 40 行、AA-Omniscience-Public 600 行、AA-Briefcase-Lite 63 行。这些是题目或音频行，不是 20 条组合分。Kaggle 数据集页 `ocean240812/llm-arena-benchmark-snapshot-july-2026` 返回 HTTP 200，CSV 没有下载，行数未核验。

## 复核方法

1. 榜页和 GPT-6 Astra 模型页各自解开模型数组，按 slug 比较 `intelligenceIndex` 与 `intelligenceIndexIsEstimated`。不一致列表为空。成分分数只取 Astra 页，不取未打开的规范 URL。
2. 指数版本同时摘榜页横幅和方法页原句，并数横幅 HTML 里的 `v4.3.2`。行 JSON 不补版本字段。
3. 无密钥请求四个数据 URL，比对状态码和正文哈希。OpenAPI 单独取回，检索路径名和 `effort` 是否出现。
4. Coding Agent 两页按行 id 去重，比较 `indexScore`，再用三项 reward 的加权和检查浮点差。effort、fallback、双模型和 harness 版本都留在行上。
5. 九个仓库的 `git ls-remote HEAD` 与报告 commit 比较。第十个仓库按公开 URL 克隆后读 LICENSE 和 README。没有执行各仓库的安装脚本。
6. 交叉核对的 41 条保持为中间意见。随后的候选修订给出 36 / 11 / 16 / 2 / 2，直接合并边界是 24 条已有身份。

页面 flight 的标记以后可能改形。计数和哈希只描述 2026-09-27 这次读取。
