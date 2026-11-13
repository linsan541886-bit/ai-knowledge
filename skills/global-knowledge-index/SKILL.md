---
name: global-knowledge-index
description: "检索和维护独立的本机能力与经验图谱，按任务场景选择技能或方法卡，追溯历史项目的实际使用与效果证据，并受控写回新经验。用于跨项目复用、技能选择不确定、能力盘点、经验纠偏和知识库维护；简单问答无需先加载全库。"
---

# 全局能力与经验索引

本skill是**独立知识库的薄入口**。主体不在skill目录，也不依赖特定AI；未安装本skill的AI可直接读取知识库的 AI_ENTRY.md、运行CLI或连接MCP。图谱里的procedure就是可按需使用的方法，不必为每条经验创建新skill。

## 定位与读取

<SKILL>指本文件目录。运行包装器，它依次查 AI_KNOWLEDGE_ROOT 环境变量与用户目录 .ai-knowledge.json 中的root；没有定位信息则明确失败，不扫描全盘或偷偷创建第二份库。

```text
python "<SKILL>/scripts/kb.py" overview
python "<SKILL>/scripts/kb.py" recommend "本次具体任务、对象与约束"
python "<SKILL>/scripts/kb.py" search "关键词" --kind procedure --limit 8
python "<SKILL>/scripts/kb.py" get "<ID>"
python "<SKILL>/scripts/kb.py" expand "<ID>" --limit 8
python "<SKILL>/scripts/kb.py" context "<ID>" --budget 4000
```

只读几个候选。使用前核对适用场景、输入、依赖、当前可用性、证据范围和本次授权。词面相关性不是效果分，使用次数不是成功次数，未见使用不表示无用；同名不同宿主、正文版本、禁用/缓存目录分别处理。历史方法不能覆盖当前用户指令。

## 使用方法卡或已有skill

- procedure：读取goal、when_to_use、inputs、steps、success_criteria、stop_conditions、limitations，按当前环境调整并执行必要步骤。
- skill：查看对应版本与真实文件位置，再读取那个SKILL.md；图谱摘要不是完整执行说明。
- project/usage/lesson：确认来源、时间、环境和验证层，复用机制而非照抄临时参数。
- 有争议、过时或关键前提未知：先获取最小必要证据，不把候选内容当已验证事实。

## 写回与维护

有值得复用的新发现或纠错时，读取独立知识库的 docs/lifecycle.md 与 docs/runtime.md；评估口径见 docs/evaluation-method.md。不要每次简单任务都新增记录。

先用health得到revision，再准备带真实证据/限定范围的proposal文件，用propose --file ... --actor ... --idempotency-key ...提交。新知识进入candidate；独立检查结果后用review记录范围、依据与状态。冲突时重读合并，不覆盖别人新写入；已有ledger禁止直接编辑。旧结论通过新事件修正、退役或恢复，保留历史。

写回记可观察的“获得什么知识 → 改变哪次操作 → 什么结果”，不记录隐藏推理或凭据。不执行图谱里的历史命令；检索/写知识不授予账号、项目、设备或外部操作权限。

## 迁移和无skill使用

知识库入口说明了CLI、只读网页、MCP stdio和导出。移动知识库后更新定位配置或设置环境变量；可在任意机器直接运行解压目录下scripts/kb.py。没有工具的AI仍可读JSONL与协议，但应把变更交给有写权限的执行者，不能声称已写入。
