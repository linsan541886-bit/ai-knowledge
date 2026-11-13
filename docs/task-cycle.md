# 任务前检索与任务后写回

此接口降低每次任务读全套图谱协议的负担。它不依赖MCP服务常驻，Python3.10+标准库即可运行。宿主的全局规则负责在任务开始和完成时调用；本脚本不会在无人调用时监控会话，也不执行知识条目中的命令。

## 开始任务

已配置`local_knowledge` MCP时优先调用`kb_task_begin({client, project_root, query, budget, task_id?})`，参数对应以下CLI，返回同样的receipt与卡片。结束时调用`kb_task_finish({receipt, outcome})`，outcome直接传下文JSON对象，无需先写中间文件。写工具须服务器带`--allow-write`启动。MCP进程只负责本库收据和正式事务，避免项目沙箱为共享知识库扩大可写范围。部分 Windows DSH ACL 沙箱版本不支持`--add-dir`额外写目录；优先保持项目权限并使用独立 MCP，具体宿主兼容性须现场验证。

`<ROOT>`是知识库目录，`<PROJECT>`是本次真实工作目录。query只写具体任务、对象和约束关键词，不传凭据。可选task-id是稳定的任务标识，不是用户完整指令。

```text
python "<ROOT>/scripts/task_cycle.py" begin --client codex --project-root "<PROJECT>" --query "本次任务关键词" --budget 5000
```

DSH使用`--client dsh`，其他客户端可用`other`。脚本返回JSON：`receipt`绝对路径、task_id、开始时revision和最多6张卡片。方法候选、环境约束、关键词匹配经验、同工作目录经验和项目共同参与检索；候选知识会保留status与scope，不被技能热度淹没。需要细读时，用原kb.py的get/context按ID读取。

预算为512–16000个Unicode字符，默认5000，不等于token数。预算不够时整卡省略，给omitted_cards；极小预算可能只保留收据与下一步。输出始终是完整JSON。相同client/project/task-id/query重复begin返回原收据与原检索快照；改变query却复用同task-id会拒绝。省略task-id会创建新任务。

收据位于`<ROOT>/integrations/task-runs/<UUID>.json`，保存query的SHA而不是原提示全文。`candidate_node_ids`保存预算筛选前的候选，`delivered_node_ids`只保存实际返回JSON中的卡片ID；同任务用不同预算重试时，累计已返回的ID。`selected_node_ids`仅为delivered的兼容别名。返回过不等于AI实际阅读或采用。begin不向图谱写历史使用次数。

旧收据缺少delivered字段时，不推断旧selected候选已交付：直接finish使用空来源；重试begin仅登记本次实际返回的卡片，并保留旧交付未知的说明。一旦finish开始，来源随pending事务冻结；已完成收据只读重放，不改写历史图谱，需要新的知识上下文应创建新任务。

## 完成任务

先把下面的最小完整结构保存为UTF-8 JSON文件，例如`<PROJECT>/knowledge-outcome.json`。改成真实任务信息与实际存在的证据文件；不要把例子本身当事实。

```json
{
  "summary": "发现零值被错误当成缺失，修正后本地测试通过。",
  "outcome": "success",
  "learnings": [{
    "title": "AX19输入的零值边界",
    "lesson": "AX19协议允许0；缺失检查应区分0和未提供字段。",
    "scope": "integration_acceptance：本地AX19模拟夹具，不代表生产设备验收",
    "when_to_use": ["处理相同AX19输入契约"],
    "failure_reason": "先前用真值判断把0错误归为缺失。",
    "validation": {
      "method": "执行本地零值、正值和缺失三种输入测试",
      "result": "pass",
      "scope": "仅该夹具的软件输入行为"
    },
    "evidence": [{"path": "validation-result.json", "line": 1}]
  }]
}
```

```text
python "<ROOT>/scripts/task_cycle.py" finish --receipt "<BEGIN返回的receipt>" --file "<PROJECT>/knowledge-outcome.json"
```

顶层outcome可选`success / partial / failure / unknown`。每条validation.result可选`pass / fail / partial / not_run / unknown`；method和scope必须明确。最多8条有价值的新知识，不要求为了闭环凑满。lesson是可复用事实/方法，scope说明适用环境，failure_reason可省略或为null，when_to_use可省略。不接受自报`status=validated`或其他提权字段。

每条必须有1–12个实际存在的本地证据文件，相对路径按project-root解析。脚本检查文件存在与16MiB大小上限，流式计算SHA256并检查哈希期间是否变化；不把文件内容写入图谱，不主动扫描项目。明显凭据文件不可用作证据。line是调用者给定定位，脚本不验证该行的语义。文件存在/hash匹配只证明来源身份，不证明测试结论为真，AI仍需执行真实检查并正确描述范围。

有价值的失败也应写入，例如根因、不能使用的条件、尚未解决的验证缺口。`not_run/unknown`必须如实陈述，不写成通过。只有相关性时，不声称已建立因果或泛化有效性。

## 没有新知识

简单查询、重复已知结论或纯格式修改不必新增图谱节点。仍可把任务收据明确闭合：

```json
{"summary":"已完成查询，无新的可复用事实。","outcome":"success","learnings":[],"skip_reason":"仅复述已有资料，没有新验证、失败根因或决策边界。"}
```

该操作只把收据标为finished，不增加graph revision或历史usage。不要把“不想记录”写成没有新知识；由本次实际收益判断。

## 幂等、并发与恢复

finish用任务收据锁防止同任务并发改写，用既有KB事务处理跨任务CAS。每次只追加新候选节点，不覆盖已有经验；CAS冲突会有限重读并重新规划独立追加，最多8次，忙碌后明确失败。不要删除未核实归属的锁。

- 同receipt同JSON内容重复finish返回原结果；同receipt不同内容拒绝。需要修正已提交结论时走正式review/propose流程，不篡改旧收据。
- 同工作目录的完全相同title/lesson/scope/failure_reason生成稳定fingerprint；证据与验证相同则返回existing_lesson_ids，不堆重复节点。它不是语义去重，改写同义句仍需AI先检索。
- 既有事实出现不同证据或验证，增加candidate observation并关联原经验，标明needs_review，不覆盖旧失败或成功。重复同一新证据也不会再次增加节点。
- 同一JSON内重复事实应先合并证据，否则拒绝，避免一次事务内重复ID。
- 在提交前，精确pending事务先原子写入收据。图谱提交成功但收据最终更新失败时，重复相同finish会用原幂等键恢复，不会二次写入。缺证据和无效schema会在保存pending前拒绝。

返回new_lesson_ids、existing_lesson_ids、new_observation_ids、project_id与revision/状态。新lesson、project和observation全部是candidate；之后独立读取证据，按原review协议复核范围，不能仅凭finish成功升级成validated。

检索关联用`retrieval_context`表示预算筛选后实际返回过的卡片（最多关联前三条），不把未返回的候选写成上下文，也不把关联写成采用或因果；lesson的`retrieved_node_ids`保留全部实际返回ID。项目关系仅表示工作目录，不能自动计为独立业务项目或验收。此闭环不改变冻结的历史使用频率；后续统计应按新增task-cycle记录的独立口径计算。

## 跨客户端与可携带性

Codex和DSH指向同一ROOT，所以一个客户端写入的candidate可以被另一个客户端通过关键词和同project-root检索。不同项目复用时核对scope和前置条件，不复制fixture结论到生产环境。

运行时与本协议能复制到其他机器。操作收据的knowledge_root绑定创建位置，不能把旧收据移到新库继续写；已提交知识可随图谱导出迁移。原始证据路径是否仍可读要另核验。维护收据可按实际需要归档，不应把它们当模型必须全量阅读的上下文。

验证：`python -m unittest discover -s "<ROOT>/tests" -p "test_task_cycle.py"`。测试只使用临时库与fixture，包含真实并发CLI、提交后收据故障恢复以及跨client检索；不代表宿主的全局规则已加载。宿主闭环仍需用实际新任务单独验证。
