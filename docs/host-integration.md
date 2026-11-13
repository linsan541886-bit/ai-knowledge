# 接入 Codex、DSH 和其他 AI

知识库是普通目录，宿主只配置一个本地 MCP 服务和一段任务规则。无需修改模型、提供商、权限或 Agent Teams。安装程序默认预演；只有显式 `--apply` 才改变配置。先完成 README 中的初始化，使用稳定的绝对路径；移动库后应核对并更新宿主路径，不能假定旧配置自动跟随。

## Codex

适配器需要 Python 3.11+ 的 `tomllib`。从仓库根目录执行：

```text
python scripts/install_codex_mcp.py
python scripts/install_codex_mcp.py --apply
python scripts/install_host_policy.py
python scripts/install_host_policy.py --apply
```

`--root` 默认是脚本所在仓库，`--home` 默认是当前用户目录，`--python` 默认是运行安装器的解释器。Codex 安装器只处理已存在的 `~/.codex/config.toml` 和 `~/.codex-cli/config.toml`，至少需要一个；仅追加 `mcp_servers.local_knowledge`。相同配置不会再次追加，已有不同配置会拒绝覆盖。原文件字节作为前缀保留，并用 TOML 语义检查确认其他字段不变。不会创建第二套 Codex 账号或改模型路由。

全局规则安装器处理现存 `~/.codex`、`~/.codex-cli`、`~/.dsh` 目录中的 AGENTS.md，仅替换 `AI-KNOWLEDGE-TASK-CYCLE` 管理块，保留其他规则。规则要求任务前 `kb_task_begin`、结束前 `kb_task_finish` 和提交后读回。没有新知识时允许明确跳过，不把跳过算成新增知识验收。

安装输出包含回滚 manifest 路径。备份可能包含私人配置，留在本机，不提交到 Git。回滚示例：

```text
python scripts/install_codex_mcp.py --rollback "<CODEX_INSTALL_MANIFEST>"
python scripts/install_host_policy.py --rollback "<POLICY_INSTALL_MANIFEST>"
```

回滚会校验备份和当前文件；安装后发生其他编辑时拒绝整文件覆盖。先检查冲突，再手工合并，不能强制覆盖用户后续修改。若安装用了自定义 `--home`，规则回滚也传相同 `--home`。

## DSH Windows 桌面端

这是 Windows DeepSeek Harness 的版本敏感适配器，依赖已安装应用的配置 composer、schema collector 和 Electron `ELECTRON_RUN_AS_NODE`。不是通用 YAML 重写器。原适配方案针对 DSH 0.2.2 系列；其他版本必须先预演，缺少对应导出或 schema 不兼容时会明确失败。仓库夹具测试不等于在新版本客户端实际验收。

```text
python scripts/install_dsh_mcp.py install --install-dir "<DIRECTORY_CONTAINING_DeepSeek_Harness.exe>"
python scripts/install_dsh_mcp.py install --install-dir "<DIRECTORY_CONTAINING_DeepSeek_Harness.exe>" --apply
python scripts/install_host_policy.py --apply
```

`--home` 在此脚本中指 DSH 配置目录（默认 `~/.dsh`）；`--install-dir` 默认是 Windows 当前用户 LocalAppData 下的 `Programs/DeepSeek Harness`。`--root` 与 `--python` 的含义同上。默认选择现存 desktop、headless 目录，可用重复的 `--profile desktop --profile headless` 显式指定。自定义 profile 是否加载 AGENTS.md 需单独检查。

预演仅读取 composer 结果，不启动模型/MCP 插件，不写配置。应用时先备份 profile 的 `cordis.patch.yml`，追加一条 `@deepseek-ai/dsh-mcp-client` insert；用配置实体哈希、顺序、schema 诊断和其他文件哈希比较确认范围。无法验证时回滚本次仍未被其他进程修改的文件。配置验证不会打印凭据或完整配置，但备份文件必须保持私密。

```text
python scripts/install_dsh_mcp.py rollback --report "<DSH_INSTALL_MANIFEST>"
```

不要用 `--add-dir` 或放宽项目沙箱来获得共享知识库写入权；独立 MCP 负责共享库事务。适配器不改模型选择、凭据、Agent Teams 或其他 MCP。非 Windows 或不兼容 DSH 版本使用客户端正式支持的 MCP 配置界面，按下一节手动接入。

## 其他客户端和可选 skill

使用 [MCP 配置示例](mcp-client.example.json)，把占位符替换为本机 Python 和知识根绝对路径。服务启动命令：

```text
python "<ROOT>/scripts/mcp_server.py" --root "<ROOT>" --allow-write
```

不带 `--allow-write` 时只开放读取。服务 stdout 仅用于 JSON-RPC，不能向其写普通日志。客户端的配置字段可能不同，应按对应客户端文档转换。参见 [运行协议](runtime.md) 和 [任务闭环](task-cycle.md)。全局规则属于宿主指令，是否加载并遵守需要实际任务验证；安装成功不能替代这一层验证。

`skills/global-knowledge-index` 是可选薄入口，可复制到宿主支持的 skills 目录。设置 `AI_KNOWLEDGE_ROOT` 为知识根，或在用户目录创建 `.ai-knowledge.json`：

```json
{"root":"<ABSOLUTE_KNOWLEDGE_ROOT>"}
```

包装器优先环境变量，然后读取定位文件；找不到库会失败，不扫描全盘或偷偷建立另一份库。没有 skill 的 AI 可以直接读取 AI_ENTRY.md、调用 CLI/MCP，图谱 procedure 本身就是按需可用的方法。

## 验证自动闭环

安装后让客户端重新加载配置或新建普通任务。任务提示只描述实际需求，不提醒知识库。用真实可观察的会话工具记录检查：

1. `kb_task_begin` 成功返回早于项目扫描、读取、编辑或测试，project_root 与任务匹配。
2. 任务完成或失败后出现 `kb_task_finish` 的实际成功结果，不只是一句“已记录”。
3. 若有新知识，账本增加对应事务，返回 ID 读回成功，并用任务领域的自然关键词检索命中；核对证据文件哈希和验证边界。
4. 没有有价值发现时，明确 `skipped` 是正确关闭收据，但它不证明新增知识写回链路。选择真实有待解决的问题补验，不能为了验收编造知识。

检查工具结果、账本和源文件，不把模型总结当执行证据。不要发布原始私人会话或隐藏推理。客户端版本、配置模式和任务差异都可能影响触发；这是一套可验证的行为约定，不是所有 AI 自动遵守的保证。
