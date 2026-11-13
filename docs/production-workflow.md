# 怎样制作、复现与维护这份图谱

这是证据图谱方法在“AI能力与经验”领域的应用：沿真实来源建立关系、区分事实与评价、按需检索、保留版本。它没有把源码审计的文件节点生硬套成能力节点；能力、版本、使用片段、项目、方法卡分别建模。

## 首次制作

1. **固定问题与范围。** 全局技能盘点查已知AI宿主根目录、插件/市场缓存、共享链接和从历史发现的项目技能目录；不把“全局”解释成拥有全磁盘全部私有文件的事实。记录不存在/禁用/未知位置。历史使用截止在本轮盘点请求之前，避免评估自己制造虚假热度。
2. **盘点实体与文档。** `inventory.py`只读SKILL.md、相关可用性配置，输出inventory.jsonl和覆盖报告。身份为namespace+name+正文hash，合并物理路径别名；保留跨宿主相同正文关系。正文hash不覆盖脚本，也不是历史调用版本。读取凭据文件没有必要。
3. **提取实际使用。** `mine_usage.py`流式解析允许的公开事件字段，匹配技能读取与脚本执行，关联结果；按call/会话轮次去重。系统技能清单、工具输出中的旧日志、隐藏推理、压缩内部上下文不当使用。解析失败与不支持源留覆盖缺口。当前日志适配包括 Codex、Claude 和 DSH，其他格式先增加具名适配与小样本测试。
4. **审查知识作用。** 挑有可追溯证据的案例，将技能内容→学习事实→操作变化→验证结果连起来。效果不能从exit0自动生成；未知案例保留unknown。少量目的性抽样不代表全部历史成功率。人工案例在usage-evidence，应用到usage条目，文档路由在inventory，方法卡在curated。
5. **构建图谱。** `assemble.py`将输入转换成统一nodes/edges与manifest，计算频率、覆盖与案例数。已有ledger的库禁止原地重建；初次构建后跑health，沿真实问题验证关联和预算上下文。
6. **交付与前向测试。** 测读取、筛选、分页、上下文预算、候选写入、验证、冲突、退役恢复和导出。独立AI从入口处理陌生请求，记录实际输出，不预告想要的答案。浏览器、客户端、硬件或外部服务未验收时明确区分。

## 首次运行命令

```text
python "<ROOT>/scripts/inventory.py" --out "<NEW_GENERATION>"
python "<ROOT>/scripts/mine_usage.py" --root "<NEW_GENERATION>" --discover-hosts --cutoff "<UTC_EXCLUSIVE_TIMESTAMP>"
python "<ROOT>/scripts/assemble.py" --source "<NEW_GENERATION>"
python "<ROOT>/scripts/kb.py" --root "<NEW_GENERATION>" health
python -m unittest discover -s "<ROOT>/tests" -p "test_*.py"
```

采集工具支持已知宿主目录布局，也接受显式来源配置，详见 [导入说明](importing.md)。inventory需要Python3.11+的tomllib；完整DSH zstd解码使用Python3.14标准库。检索、图谱写回、UI/MCP运行时只需Python3.10+。迁移到别的机器时先检查采集根列表及源格式，不直接套用本机路径或历史截止。日常检索不需要再次扫描GB级历史。

## 关系含义

| 关系 | 含义 | 不代表 |
|---|---|---|
| offers_method | 某场景的文档候选 | 已实测适合一切该类任务 |
| has_version | 当前正文版本与逻辑技能 | 历史使用了今天的正文 |
| same_document_content | 文本hash相同 | 脚本/授权/依赖完全相同 |
| observed_in | 技能和一次观察片段 | 产生业务收益 |
| occurred_in | 观察发生的工作目录 | 独立业务项目已验收 |
| belongs_to_repository | 当前Git元数据对应仓库 | 所有历史任务语义相同 |
| helped_in_reviewed_case | 某个有范围的帮助案例 | 普遍成功、唯一因果或全项目完成 |
| derived_from | 方法或经验的来源 | 来源永远正确 |
| constrained_by | 本地条件影响选用 | 图谱可以授予执行权限 |

结构图默认双向逐层展开，方向仍显式显示。任何新因果边须有机制与证据；词面命中、时间邻近和依赖关系不能自动升为因果。

## 日常更新与大规模刷新

日常新经验、使用记录、纠错走CLI/MCP的propose/review。事务保留revision、actor、幂等键与hash链。知识变化后，查询关联节点，记录哪些派生知识需复核。不要因写入成功就提高实际效果指标；新usage也需要按同一统计口径汇总，首次baseline中的汇总快照不会自己改变。

大规模技能更新/补充历史时，在**新目录/generation**复制运行时和需要的输入，再重新采集、构建、验证。旧库导出后保留，旧ledger不得覆盖。逐项迁移经过复核的新增知识，维护old_id→new_id与源revision；相同知识不同环境保留范围。完成health、代表性检索、关键知识无丢失核对后，再更新用户目录.ai-knowledge.json的root。不要直接用新baseline覆盖有ledger的库。

用 `kb.py export --out <库外新ZIP路径>` 导出可迁移图谱与运行材料。导出包含canonical nodes/edges/ledger及具名的派生采集底账和人工评估，不包含GB级原始日志。派生底账并非运行时必需；若要重跑原始采集，带上相应输入或在新环境重新发现。导出不等于已迁移外部证据，路径可达性与hash仍须复核。

## 哪些操作当前是自动的

脚本自动：已知根盘点、支持格式提取、去重、结构检验、按口径计数、检索、预算裁剪、事务一致性。

AI/人审：技能是否适用、真实效果归因、为什么关键、必要性、知识是否改变行动、相似项目是否可迁移、争议裁决。

未实现：全磁盘未知位置保证、所有AI客户端日志适配、自动语义真实性验证、无人触发持续成长、嵌入向量召回、自动更改项目或外部系统。本库通过开放文件、CLI和MCP让任何获授权AI接力维护，不以这些未实现能力作宣传。
