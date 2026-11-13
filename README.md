# AI Knowledge

可检索、可逐层展开、带证据和变更历史的本地 AI 能力与经验图谱。把 skill、方法卡、项目经验和失败边界放在同一套开放文件协议中，让不同 AI 按需读取并受控写回，而不是把整份历史塞进上下文。

本仓库发布运行代码、制作方法、评价指标、宿主适配器和演示数据生成器。个人技能清单、实际项目经验、原始对话、账户配置与运行账本留在使用者自己的机器上。

## 快速开始

建议使用 Python 3.14；核心 CLI、网页与 MCP 使用 Python 3.10+ 标准库，技能盘点与部分配置适配需要 3.11+，DSH zstd 历史采集需要 3.14。无需安装 Python 第三方依赖。

```sh
git clone https://github.com/linsan541886-bit/ai-knowledge.git
cd ai-knowledge
python scripts/bootstrap.py
python scripts/kb.py health
python scripts/kb.py recommend "验证项目结果并记录可复用经验"
python scripts/serve.py --port 18766
```

浏览器打开 `http://127.0.0.1:18766/`，可以搜索、筛选、展开关系、查看证据、评价及历史，并复制有字符预算的 AI 上下文。演示条目仅展示协议，不代表真实项目使用记录。初始化不会覆盖已有知识库；`data/` 默认不纳入 Git。

## 给 AI 的入口

先读 [AI_ENTRY.md](AI_ENTRY.md)。通常只需要：

```sh
python scripts/kb.py recommend "具体任务、输入和环境"
python scripts/kb.py search "关键词" --limit 8
python scripts/kb.py get "<NODE_ID>"
python scripts/kb.py expand "<NODE_ID>" --limit 8
python scripts/kb.py context "<NODE_ID>" --budget 4000
```

默认返回短卡片，按 ID 补充一层关系。检索是词面与中文片段匹配；相关性分数不是效果评分。方法卡可以直接作为操作方法使用，不必为每条经验创建 skill。

## 开始检索，完成后写回

MCP 服务与 CLI 操作同一份数据：

```sh
python scripts/mcp_server.py --root "<ABSOLUTE_REPO_PATH>" --allow-write
```

在客户端登记该 stdio 服务，再安装任务规则，AI 才会在普通任务中调用 `kb_task_begin`，完成后通过 `kb_task_finish` 写入有证据的新知识，或明确记录没有新增知识。仅启动服务不等于客户端会自动调用。安装器默认预演，实际修改使用 `--apply`，详见 [Codex / DSH 接入](docs/host-integration.md) 和 [任务闭环协议](docs/task-cycle.md)。

新知识默认 `candidate`。证据、适用范围、验证结果及失败原因分别记录；写入成功并不证明结论正确。可以使用 [薄 skill 入口](skills/global-knowledge-index/SKILL.md)，也可以完全不安装 skill，只用 CLI、MCP 或开放 JSONL。

## 能力与边界

| 能力 | 实现与边界 |
|---|---|
| 知识图谱 | 节点与关系 JSONL，局部展开；不依赖图数据库 |
| 评价 | 适用性、频次、实际帮助、关键贡献、失败、验证强度、成本、架构、迁移性和知识收益等多维指标；未知值保留未知 |
| 证据与历史 | 稳定 ID、来源引用、内容哈希、revision、追加事件；不存原始私密对话 |
| 受控写入 | CAS 并发检查、幂等键、写锁、原子替换；冲突时拒绝覆盖 |
| 生命周期 | candidate / active / validated / contested / deprecated / archived；修正保留旧结论 |
| 跨项目复用 | 按场景寻找方法与相似经验，使用前核对版本、环境和验证范围 |
| 历史采集 | 显式运行盘点与日志提取工具；频次不冒充成功率 |
| 网页 | 回环地址、只读 API、无源文件下载或网页写入端点 |
| MCP | stdio，固定 2025-11-25 协议；8 个只读工具，允许写入时另有 4 个工具 |

适用于本地中小规模知识库。检索以内存加载为主，写入复制整个账本；没有向量检索、自动真实性裁决、无人触发的自主成长或远程多用户服务。客户端遵守任务规则的实际情况，应通过真实会话、账本和再次检索核对。

## 制作、维护与测试

- [完整制作与维护流程](docs/production-workflow.md)
- [本机技能和历史数据导入](docs/importing.md)
- [评价指标与统计口径](docs/evaluation-method.md)
- [生命周期、纠错与恢复](docs/lifecycle.md)
- [CLI / HTTP / MCP、数据格式及写回契约](docs/runtime.md)
- [发布范围与数据隔离](docs/distribution.md)

```sh
python -m unittest discover -s tests -p "test_*.py"
```

```text
scripts/    核心图谱、检索、任务闭环、导入、安装与服务
ui/         无外部依赖的只读网页
tests/      临时目录中的运行时、协议与回归验证
examples/   明确标注的演示输入
skills/     可选的薄入口
docs/       方法、评价、协议和维护说明
data/       使用者本地生成的数据，不跟踪到 Git
```
