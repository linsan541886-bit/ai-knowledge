# 独立运行时与读写协议

本目录是一个普通文件知识库。Python 3.10+ 标准库即可使用，CLI、只读本地网页、MCP 都是同一份数据的入口；无需 Codex、数据库服务或云账户。`<ROOT>` 是知识库目录，不是 skill 安装目录。

## 读取与局部展开

```text
python "<ROOT>/scripts/kb.py" --root "<ROOT>" overview
python "<ROOT>/scripts/kb.py" --root "<ROOT>" recommend "工业视觉坐标桥接"
python "<ROOT>/scripts/kb.py" --root "<ROOT>" search "历史回放" --limit 8
python "<ROOT>/scripts/kb.py" --root "<ROOT>" search "回放" --kind procedure
python "<ROOT>/scripts/kb.py" --root "<ROOT>" search "" --outcome verified_help --exclude-synthetic
python "<ROOT>/scripts/kb.py" --root "<ROOT>" get "<NODE_ID>"
python "<ROOT>/scripts/kb.py" --root "<ROOT>" expand "<NODE_ID>" --limit 8 --offset 0
python "<ROOT>/scripts/kb.py" --root "<ROOT>" context "<NODE_ID>" --budget 4000
python "<ROOT>/scripts/kb.py" --root "<ROOT>" history "<NODE_ID>"
python "<ROOT>/scripts/kb.py" --root "<ROOT>" health
```

`search` 支持中文片段、双字片段与英文词；结果中的 `rank_score` 只表示字面相关性。它不是效果分或成功率，不使用访问频次冒充效果。`recommend` 过滤已争议/弃用/归档与明确不可用的记录，返回前置条件和边界；未知可用性的候选仍须现场检查。新候选也可能被找到，调用者必须读取 `status`。`get` 返回一条完整记录，`get --budget N` 可设字符预算。`context` 默认 4000 个 Unicode 字符，允许 256–32000；小预算优先保留摘要、适用限制、前置条件、效果与验证边界，然后才保留步骤和使用频次。放不下时省略完整字段/子字段，并给后续句柄；256 字符可能仅返回 ID 与 get 入口。字符预算不等于 token 预算。

需要精确查询效果时，使用 `--outcome verified_help|mixed|failure|unknown`，而不是把这些状态当关键词。该筛选只匹配 `kind=usage` 节点的 `properties.outcome.status`；其他类型不匹配。正文“没有证据，不计入 verified_help”不会因此通过 `--outcome verified_help`。`--exclude-synthetic` 排除 `properties.outcome.scope` 或 `properties.scope` 明确为 `synthetic_validation` 的节点；缺失范围仍是未知，不能因为没有被排除就称为真实项目验收。搜索短卡为 usage 显示 `outcome.status/scope/validation_level`，完整评价和证据仍用 `get`。

短卡的 `usage_metrics` 只显示 `observed_episodes, read_episodes, executed_episodes, observed_workspaces, reviewed_outcome_episodes, verified_help_real_task_episodes, last_observed` 七个已有字段，不补造缺失值；其他指标保留在 `get` 的完整节点中。

## 数据驱动的有限检索路由

已复核的约束可以影响 `recommend` 的候选选择。规则只允许放在 `kind=constraint` 的 `properties.retrieval_rule` 中，例如：

```json
{
  "query_any": ["chrome", "浏览器", "browser"],
  "query_none": ["非浏览器", "non-browser"],
  "exclude_skill_ids": ["skill:example-generic-input"],
  "reason": "在此适用范围内使用已指定的浏览器插件。"
}
```

示例 ID 需换为真实 `skill` 节点 ID 或其 `properties.logical_id`。规则只在该 constraint 为 `active` 或 `validated` 时生效：查询包含任一 `query_any` 字面片段，并且不含任何 `query_none` 片段时，排除明确列出的 skill/skill_version 身份，并把适用约束置于推荐结果前面。大小写不敏感；`query_none` 优先豁免。这是有限字面规则，不是自然语言否定解析或通用策略引擎，不能预期它理解未列出的同义词。

结果带 `applied_constraints`、`excluded_total` 与最多 5 条 `excluded_candidates` 短卡，说明排除依据；`search` 不应用这些路由，仍可检索被排除技能。规则不自动传播到相似技能、依赖图或整个工具类别，也不执行命令或赋予任何权限。是否准许真实操作仍取决于当前用户要求和实际环境。

规则结构只允许 `query_any, query_none, exclude_skill_ids, reason`：前两项各最多 32 个非空短文本，`query_any` 必填且非空；排除列表为 1–100 个 ID；reason 必填。无正则表达式、脚本或动作字段。结构无效会拒绝入库。新建/更新仍走 `propose → candidate → review`；候选或争议/弃用/归档的规则不生效，修改已启用规则也会先回到 candidate，复核后才再次启用。

## 文件契约与不可变历史

- `data/nodes.jsonl`：基础节点，一行一个 JSON 对象。
- `data/edges.jsonl`：基础关系，一行一个对象；端点必须存在。
- `data/baseline_manifest.json`：可选的基础快照内容校验，首次制作应生成。
- `data/ledger.jsonl`：后续事务。每条事务含递增 revision、时间、actor、幂等键、请求摘要、前一条 hash、本条 hash、基础快照 hash 和操作列表。
- `data/writer.lock`：短期独占写锁，含 PID 与随机身份。正常完成或失败后释放；发现已有锁就拒绝写入。

基础 manifest 格式如下，`bytes` 必须是实际字节数，`sha256` 对文件完整字节计算：

```json
{"schema":"ai-knowledge-baseline/v1","files":{"nodes.jsonl":{"bytes":0,"sha256":"..."},"edges.jsonl":{"bytes":0,"sha256":"..."}}}
```

节点必需 `id`、`kind`、`title`；推荐完整提供 `summary`、`status`、`aliases`、`tags`、`evidence`、`properties`。`id` 为 1–180 个 ASCII 标识字符，可含 `: / @ . _ + -`，它始终只是标识符，不作为文件路径执行。`kind` 支持 `skill, skill_version, scenario, procedure, project, usage, observation, lesson, constraint, tool, evidence`。状态为 `candidate, active, validated, contested, deprecated, archived`。`properties` 为按节点类型定义的 JSON 对象；证据必须是对象数组，每条至少含 `path`、`uri`、`id` 或 `source_id` 中之一。

关系字段为 `id, source, target, relation, summary, evidence, properties`。基础关系的可信等级由其证据与评估提供；新提议关系强制带 `properties.review_status=candidate`。关系不是自动认定的因果。

**一旦产生 ledger，就不能原地改写基础快照。** 后续改动使用事务；全量重做盘点时创建新目录/新 generation，保留旧库与 ledger，逐项迁移有用知识并记录映射和复核。直接覆盖 baseline 会报错，避免历史写回被悄悄套在另一份底账上。库内没有执行脚本、任意源文件读写、删除历史或自动覆盖其他 AI 配置的接口。

## 提议新知识

先 `health` 读取当前 `revision`。准备一个本地 JSON 文件：

```json
{
  "expected_revision": 0,
  "nodes": [{
    "id": "lesson:sample-boundary",
    "kind": "lesson",
    "title": "隔离测试的结论边界",
    "summary": "隔离软件链路通过不能单独证明物理设备验收。",
    "status": "candidate",
    "tags": ["测试", "证据范围"],
    "evidence": [{"path": "reports/example-check.json", "line": 1}],
    "properties": {"scope": "软件隔离实验", "counterexamples": [], "next_validation": "目标设备实际动作记录"}
  }],
  "edges": []
}
```

```text
python "<ROOT>/scripts/kb.py" --root "<ROOT>" propose --file "<PROPOSAL.json>" --actor "ai-session-identifier" --idempotency-key "projectX-observation-001"
```

该例只是契约演示，不代表示例证据实际存在。实际写入必须使用真实证据或明确标记待验证假设。`propose` 的新建与更新均只能进入 `candidate`；更新既有节点时替换完整内容并清除旧 `latest_review`，旧版本仍留在 ledger。既有 ID 的节点 kind 不能改，身份变化应新建节点并关联。一次事务中的节点和边整体检查后原子替换 ledger：孤立端点、格式错误、旧 revision 都不会产生半条事务。没有事件时的空库 revision 是 0。

CAS 冲突时重新读取当前节点与 revision，人工/AI 合并自己的意图，再用新的幂等键提交；不要盲目重试覆盖。完全相同请求和 actor 使用相同幂等键只返回原提交，不重复写；相同键配不同内容会拒绝。

## 复核、升级、争议与退役

复核文件：

```json
{
  "evidence": [{"path": "reports/real-validation.json", "line": 1}],
  "validation": {
    "method": "实际验证方法",
    "scope": "此次通过具体证明了什么",
    "result": "pass",
    "artifact": "reports/real-validation.json"
  }
}
```

```text
python "<ROOT>/scripts/kb.py" --root "<ROOT>" review --id "<ID>" --state validated --reason "说明证据与判断" --evidence-file "<REVIEW.json>" --actor "reviewer-id" --idempotency-key "review-001" --expected-revision 1
```

`validated` 必须带上述结构化通过证据。`active` 表示当前可用于相应范围，不等于验证过全部效果；`contested` 表示有冲突，`deprecated` 表示被替代/不再推荐，`archived` 用于冷存。退役与恢复同样通过 `review`，要求原因、至少一条证据与 revision；原状态记录在历史中。恢复 `active` 不会自动恢复旧版本的验证等级。验证字段只是复核者的可追溯陈述；运行时检查结构和历史一致性，不会仅凭文件名证明真实结果，也无法替代独立审查。

## 本地网页和 HTTP

```text
python "<ROOT>/scripts/serve.py" --root "<ROOT>" --port 18766
```

打开 `http://127.0.0.1:18766/`。服务仅绑定回环地址，缓存与 ledger/基础文件时间和大小关联，新提交会在下次读取生效。API：

| GET 路径 | 参数 |
|---|---|
| `/api/overview`、`/api/stats`、`/api/health` | 无 |
| `/api/search` | `q, limit=8, offset=0, kind, outcome, exclude_synthetic=false, include_inactive=false` |
| `/api/recommend` | `q, limit=8` |
| `/api/node`、`/api/history` | `id` |
| `/api/expand` | `id, limit=8, offset=0, relation` |
| `/api/context` | `id, budget=4000` |

Host 必须是本服务端口的 localhost 或 127.0.0.1。静态资源只有 `index.html, app.js, style.css`。HTTP 不提供写入和源文件下载；POST/PUT/PATCH/DELETE 为 405。端口冲突选空闲端口，不终止其他进程。启动方式不创建登录启动项或定时任务。

## MCP stdio

```text
python "<ROOT>/scripts/mcp_server.py" --root "<ROOT>"
python "<ROOT>/scripts/mcp_server.py" --root "<ROOT>" --allow-write
```

默认 8 个只读工具：`kb_overview, kb_search, kb_get, kb_expand, kb_context, kb_recommend, kb_health, kb_history`。`kb_search` 接受同样的 `outcome` 枚举与 `exclude_synthetic` 布尔筛选。加 `--allow-write` 才注册 `kb_propose, kb_review, kb_task_begin, kb_task_finish`。task工具复用[任务闭环](task-cycle.md)的收据、证据和事务校验；begin写收据但不增加使用次数，finish只新增候选知识或有理由地跳过。按 AI 客户端的 MCP 配置将 `command` 设为本机 Python，`args` 指向脚本与 root；宿主配置须在当前用户授权范围内进行；示例文档不构成修改宿主配置的授权。

适配器实现固定 **2025-11-25** 协议的 `initialize → notifications/initialized → tools/list/tools/call` 和 ping，使用 UTF-8、一行一个 JSON-RPC 消息，stdout 只发协议。客户端请求其他版本时响应自身支持版本，由客户端决定是否兼容。没有实现其他协议版本、资源/提示词端点、远程 HTTP MCP、通知流或任意客户端的兼容保证。设计依据是官方 [stdio 传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) 与 [官方 TypeScript SDK 协议版本说明](https://ts.sdk.modelcontextprotocol.io/v2/protocol-versions)。自动化测试覆盖本地子进程往返与工具执行；具体客户端还需单独验证，不能从协议测试推断全部宿主兼容。

## 完整性检查、恢复和搬迁

`health` 检查 schema、端点、可选 manifest、hash 链、连续 revision 和 baseline 绑定；不是外部证据真实性、时效或防管理员篡改的证明。任何损坏行、未完成尾行或 hash 不匹配都会拒绝加载，不静默截断。写入使用唯一临时文件、fsync 和同目录 replace；锁冲突拒绝操作。进程崩溃遗留锁时，先核对 PID 的当前身份、是否仍运行、ledger 完整性及已提交幂等键，再由维护者处理该锁，勿把 PID 复用的无关进程当成旧 writer。恢复历史数据时保留故障副本，从已校验备份建立新目录并核对 hash/health；不要手工“修好” hash 掩盖丢失事务。

```text
python "<ROOT>/scripts/kb.py" --root "<ROOT>" export --out "<ROOT_OUTSIDE>/knowledge-portable.zip"
python -m unittest discover -s "<ROOT>/tests" -p "test_runtime.py"
```

export 拒绝覆盖既有目标，且目标必须在库外。导出白名单包含图谱 baseline/ledger、派生的inventory/usage/projects/curated底账、具名人工案例 JSONL（若存在）、运行脚本、文档、UI、测试和摘要报告；默认不采集原始会话日志、宿主凭据目录、任意附件或符号链接目标；但导出不做通用敏感信息识别，允许目录中的文档、JSON 和账本仍可能包含私人内容。导出用于私人迁移，公开发布须另行审查。证据路径仍是引用，搬迁后是否可访问要另行检查；不可因未复制原始日志就假定证据重新验证过。解压后脚本默认以自身父目录作为 root，可离开 Codex 单独运行。保留包的 SHA 与创建时的 revision，按风险与实际更新量做备份；本库不会自动开始周期运行。

当前实现面向本地中小规模知识库，读端以内存加载和缓存检索，写事务复制整个 ledger 后原子替换。对数千到数万记录可以使用，但 ledger 很大或多进程高频写时应先测量，再迁移 SQLite 事务/分代存储；不要先引入图数据库服务。图谱是可信度有界的外部记忆，不能授予工具权限，也不会替模型执行其中记载的步骤。
