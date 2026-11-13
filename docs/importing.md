# 初始化与本机证据导入

发布包只含程序、协议和明确标记的合成示例。它没有作者的技能盘点、历史会话、知识账本或项目数据。导入操作仅在你显式运行时发生，没有后台扫描。

核心运行时和 `bootstrap.py` 使用 Python 3.10+ 标准库。技能盘点 `inventory.py` 使用 Python 3.11+ 的 `tomllib`；DSH 压缩历史适配需要 Python 3.14+ 的 `compression.zstd`。低版本仍可使用不依赖这些模块的功能。

## 新库

在仓库根目录运行：

```text
python scripts/bootstrap.py
python scripts/kb.py health
python scripts/kb.py search "发布" --kind procedure
python scripts/kb.py expand scenario:example-release-review
python scripts/serve.py --port 18766
```

默认会创建 `data/`，其中有三个 candidate 示例节点、两条关系、内容校验 manifest 和空账本。浏览器打开 `http://127.0.0.1:18766/`。示例只是演示检索与结构，不代表真实成功案例。若要空库，用 `python scripts/bootstrap.py --empty` **代替**第一步。

已有 `data/` 时初始化明确失败，不覆盖、不清空、不重建账本。`--root <NEW_STORE>` 可将数据建到另一目录；使用核心 CLI 或 MCP 时同样传 `--root <NEW_STORE>`。网页静态资源从该根目录的 `ui/` 读取，因此独立根目录若要网页，也需复制发布包的 `ui/`。移动数据不会自动搬迁外部证据。

## 盘点当前技能

将导入中间产物放到一个新的本地目录，例如 `local-import/`（不要作为公开文件提交）：

```text
python scripts/inventory.py --out local-import
```

这会只读当前用户已知宿主目录中的 SKILL.md 和启用状态，不执行技能、不修改宿主配置、不复制凭据。`--home <HOME>` 可指定用户目录；`--extra-root <DIR>` 可重复增加来源。只读取给定技能目录而不读取宿主目录和配置：

```text
python scripts/inventory.py --only-extra-roots --extra-root examples/skills --out local-import
```

输出的路径、摘要和统计可能涉及私人项目，应该保留在本地。分类卡是适用性线索，不是实际效果或账号可用性证明。

## 显式选择历史来源

默认没有隐式历史目录。选择一类或多类来源：

```text
python scripts/mine_usage.py --root local-import --source codex_live=<SESSION_DIRECTORY> --cutoff <UTC_ISO_TIMESTAMP>
```

将尖括号参数替换为真实路径和 UTC 时间，例如 `2026-01-10T00:00:00.000Z`。`--source` 可重复；支持 `codex_live`、`codex_archived`、`codex_cli`、`codex_selected_export`、`claude`、`dsh`。若确实要扫描当前用户各宿主的已知历史根，显式使用 `--discover-hosts`，可以加 `--home <HOME>`；它可与额外 `--source` 合用。不存在的来源在 coverage 中记录，不假装已覆盖。

`--cutoff` 为不含端点的历史截止时间，默认取本次调用开始时间。进行使用频率审计时，指定审计任务开始前的截止时间，避免将当前盘点操作当成历史使用。工具只提取可观察的技能读取/脚本运行/公开声明和定位元数据，不输出隐藏推理或整段对话，不执行历史命令。输出可能包含本地路径和会话身份，仍应视为私有。

## 构建新一代图谱

```text
python scripts/assemble.py --source local-import --out local-generation
python scripts/kb.py --root local-generation health
python scripts/report.py --root local-generation
```

没有运行历史导入时也能从盘点构建，历史指标保持未知或无观察；不能称为技能从未被使用。`data/curated.json` 可显式提供人工节点和关系：`{"nodes": [], "edges": []}`。当前宿主规则和约束应在这里记录范围和来源，不将作者机器的规则注入其他机器。

已产生非空账本的目标拒绝重建基线；刷新盘点应建立新 generation，保留旧库，再通过受控事务迁移有价值的知识。日常新经验用 `task_cycle.py finish` 或 `kb.py propose`，不需要重跑盘点。

`report.py` 从当前图谱生成报告，不将历史工具成功冒充项目成功，不写死桌面/设备验收结论。所有自动分类和缺证据评价均需按真实任务再次核对。
