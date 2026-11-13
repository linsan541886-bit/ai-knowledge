#!/usr/bin/env python3
"""Read-only local skill inventory. Python 3.11+ (tomllib); writes only its outputs.

Document classification is declared/inferred capability, never a usage or outcome.
Repeated physical aliases are folded into a content-version record with locations.
No shell command, skill script, or credential configuration is executed or copied.
"""
from __future__ import annotations
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tomllib


def emit(path, data, lines=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if lines:
        body = ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in data)
    else:
        body = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(body, encoding='utf-8')
    os.replace(temp, path)


def frontmatter(text):
    """Minimal scalar-only YAML reader; never instantiates YAML objects."""
    if not text.startswith('---'):
        return {}, 'missing_frontmatter'
    match = re.match(r'^---\s*\n(.*?)\n---(?:\s*\n|$)', text, re.S)
    if not match:
        return {}, 'unterminated_frontmatter'
    lines = match.group(1).splitlines()
    out = {}
    for i, line in enumerate(lines):
        m = re.match(r'^(name|description|version):\s*(.*)$', line)
        if not m:
            continue
        key, value = m.groups()
        if value.strip() in ('>', '|', '>-', '|-', ''):
            parts = []
            for nxt in lines[i+1:]:
                if nxt and not nxt[0].isspace():
                    break
                parts.append(nxt.strip())
            value = ('\n' if '|' in value else ' ').join(parts)
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            if value[0] == '"':
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    value = value[1:-1]
            else:
                value = value[1:-1].replace("''", "'")
        out[key] = value
    return out, None


# Chinese routing cards. They summarize a specific decision, not a quality rating.
# tuple(category, scenario, decisive condition, prerequisite/limit)
CARDS = {
 'skill-creator': ('能力管理', '把反复使用的方法写成或修订可发现的技能', '方法已可复现，需要交给另一 AI 独立执行与验收时', '先明确触发条件、产物和验证；不以文件齐全替代行为前向测试'),
 'skill-installer': ('能力管理', '从指定目录或仓库安装技能', '已有合适技能但当前环境不能发现它时', '安装来源、目标目录与依赖须核实'),
 'project-evidence-graph': ('知识与项目复盘', '用户指令、真实补丁、被动影响、风险与知识反馈的证据图谱', '跨会话理解为什么改代码、改动是否实现原意，且需按需检索时', '公开会话/工具记录、源码或产物；静态依赖不等于因果'),
 'global-knowledge-index': ('能力管理','按场景检索技能/方法卡并受控维护独立知识库','跨项目复用需要同时知道方法、实际使用证据、适用边界和新知识写回入口时','独立知识库与定位配置；图谱知识不增加当前操作权限'),
 'graph-engineering': ('任务编排', '多分支研究或开发的依赖、共享状态、汇合和返工', '独立分支可并行且有共同验收，串行执行等待明显时', '本技能处理执行工作流；不是源码图或知识图构建器'),
 'general-code-analysis': ('代码架构', '陌生代码仓的入口、主时钟、状态、参数和耦合边界', '修改前尚不清楚谁驱动系统、改动会触及哪些共享状态时', '可读取相关源码；先系统链路再逐行阅读'),
 'hardware-software-project-analysis': ('工业与硬件', '物理输入、驱动、决策、协议与执行器的整体分析', '硬件替换或联动故障会穿透软件层，需要确认HAL与时序边界时', '区分在线设备真相与本地副本；缺硬件资料时标推断'),
 'industrial-vision-code-analysis': ('工业与硬件', '线激光/RGB视觉与串口TCP机器人主链路审查', '单目标假设、对象结束回调或主控类同时控制算法和动作时', '先锚定入口、采集回调和对象状态，不只比较算法'),
 'keba-headless-operations': ('工业与硬件', 'KEBA连接、无头导出、CIF读取、锁存与TrackingLog归因', '必须把PC command_id与机器人执行记录或具体报警对应时', '合法KeStudio DLL、控制器连接或工程；现场地址需重读；写入与运动需任务授权'),
 'python-project-reorg': ('代码架构', 'Python大目录、上帝类和分发链重组', '需要拆模块但必须保留公共导入与既有行为时', '识别canonical实现与兼容壳；相关行为验证'),
 'anysearch': ('研究与检索', '实时网页、垂直领域、多查询并行与正文提取', '外部事实或文档缺失阻塞决定，需要可引用原始信息时', '可用搜索服务/CLI与网络；子域参数须按接口查明'),
 'academic-review-writing': ('研究与检索', '文献综述问题、筛选、证据矩阵和非流水账综合', '综述缺乏研究边界、可追溯引用或跨文献论证时', '实际原文与检索记录；原始实验报告不是主适用范围'),
 'open_source_tender_intelligence': ('商业与招采', '官方标讯、开源采集和低门槛机会筛选', '要区分官方公告、商业转载和真实可履约机会时', '官方采购文件与当前资质/截止日期必须再核验'),
 'powershell-expert': ('Windows工程', 'PowerShell模块、参数、管道、错误处理与GUI', '脚本成为可复用工具，需明确参数契约和兼容版本时', '目标PowerShell版本与模块可用性需确认'),
 'powershell-windows': ('Windows工程', 'PowerShell语法、插值、空值与命令错误排查', 'Windows shell语法差异或转义造成命令失败时', '旧版ASCII-only建议不等于中文UTF8不可用；按实际编码/版本验证'),
 'windows-low-level-input': ('桌面与浏览器', '无法由高层接口操作的原生Windows窗口', '原生控件只接受真实输入，且已确认目标窗口时', '遵守当前宿主和用户规则；不得通过低层输入绕过策略停止'),
 'computer-use': ('桌面与浏览器', '读取并操作原生桌面应用界面', '应用没有足够API/CLI，需要观察控件真实状态时', '确认宿主工具支持的表面，外部动作遵循当次授权'),
 'playwright': ('桌面与浏览器', '浏览器流程自动化与截图调试', '用户流程只能在真实浏览器行为中复现时', '核对当前浏览器工具和用户规则，不推断跨宿主权限'),
 'playwright-interactive': ('桌面与浏览器', '保持浏览器/Electron会话进行功能与视觉迭代验证', '重复重启丢失状态或需要检查实现后的用户交互时', '所需运行时存在，且当前宿主允许该交互方式'),
 'pcb-circuit-foundations': ('PCB电路', '节点端口、切换初态、RC/RLC和运放偏置分析', '错误等效、初态或虚地假设导致电压电流解释矛盾时', '真实拓扑、源负载、初态与器件工作区'),
 'pcb-circuit-systems': ('PCB电路', '频响、极零点、建立时间、采样与磁耦合', '滤波、振铃或混叠问题需要可否证系统模型时', '实际端口、工况、误差目标；不可忽略被抵消的内部状态'),
 'pcb-analog-performance': ('PCB电路', '模拟前端偏置、增益、稳定性、噪声和建立误差', '器件/偏置选型需要定位主导限制而非盲目加补偿时', '实际负载、供电、频带、测量设置和器件模型'),
 'pcb-design-workflow': ('PCB电路', '板卡需求到原理图、布局、制造和分段调试', '跨阶段接口或供电约束不一致使板卡难制造/验证时', '沿用EDA和具体器件工艺；DRC不等于实物功能通过'),
 'pcb-layout-review': ('PCB电路', '回流、去耦、电源环路、差分、热与天线检查', '送板前或异常板需要把失效机制定位到网络与几何时', '叠层/制造/型号资料；只有截图时不能证实阻抗与温升'),
 'figma': ('设计到实现', '读取Figma设计上下文、截图、变量与资产', '实现需要准确设计事实和资源，不能仅凭外观猜测时', 'Figma连接器和文件访问权限'),
 'figma-implement-design': ('设计到实现', '将Figma节点转成符合项目约定的界面代码', '需要组件复用与设计视觉保真同时成立时', '明确节点、项目栈与可访问的设计文件'),
 'gh-address-comments': ('代码交付', '针对当前PR审查意见修改并验证', '多条反馈需要逐项闭环而不能只回复解释时', 'GitHub身份、PR和本地检出对应'),
 'gh-fix-ci': ('代码交付', '读取GitHub Actions失败日志并修复', 'CI失败原因尚未定位或与本地结果不一致时', '具体失败run、日志和可复现实验'),
 'jupyter-notebook': ('数据与实验', '创建和整理可运行Notebook实验', '实验需要逐步复现、展示输入输出与参数时', '真实数据和运行内核；图像展示不等于计算已执行'),
 'imagegen': ('图像与媒体', '按描述生成图像或编辑已有图像', '需要新的视觉内容而非现有素材的拼接/代码图时', '图像生成工具与目标图像；依当前工具规范调用'),
 'photo-memory-stamp': ('图像与媒体', '把照片做成保留主体的收藏档案式构图', '需要一致的照片纪念卡/档案视觉而非任意风格时', '输入照片与图像编辑能力'),
 'pdf': ('文档与表格', '读取、生成与渲染检查PDF', '分页、字体或视觉布局对交付正确性关键时', '渲染/提取工具；文本提取不能替代版面检查'),
 'documents': ('文档与表格', '生成、编辑和审阅Word/DOCX文档', '需保留结构、格式、修订或真实文件产物时', '文档运行时或连接器；实际打开/渲染检查'),
 'presentations': ('文档与表格', '制作和修改可交付演示文稿', '信息层级、模板与可编辑幻灯片共同重要时', '演示运行时/模板及资产；逐页布局验证'),
 'spreadsheets': ('文档与表格', '编辑公式、格式与分析工作簿', '结果需留在可复算且结构完整的表格中时', '工作簿数据与表格运行时；公式结果要验证'),
 'excel-live-control': ('文档与表格', '在已打开Excel工作簿内精确操作', '必须使用用户当前工作簿状态而非另造副本时', 'Excel加载项、活动会话和目标区域'),
 'template-creator': ('能力管理', '把可重复的产物模板写成个人技能', '重复文档/图表要求已经稳定，需要保留视觉与结构契约时', '可访问的原始模板与可重复的制作过程'),
 'visualize': ('交互可视化', '在对话内制作交互解释和比较工具', '交互参数或状态变化比静态文字更能解释问题时', '支持内嵌可视化；可导出科学图应使用合适绘图库'),
 'sites': ('网站与发布', '构建网站、仪表盘或交互门户', '需可运行网站产物和明确的发布目标时', 'Sites工具/项目代码及发布授权边界'),
 'sites-remix': ('网站与发布', '从现有Site复用结构生成新站点', '已有站点设计/功能值得保留且需新版本时', '明确原站点、目标差异和访问权限'),
 'stop-that-shit': ('任务纪律', '约束只读范围和避免无关重构扩张', '代理开始把小修扩大成不必要工程时', '按当前用户任务边界判断，不用它拒绝必要工作'),
 'stss': ('任务纪律', '减少防御性免责声明和冗余保留措辞', '技术建议被套话/多重含糊表达淹没时', '保留真实不确定性和重要风险，不机械删掉证据边界'),
 'write-like-me': ('写作与表达', '按用户既有表达风格撰写内容', '需要保持作者声音而非通用AI文风时', '足够风格样本与目标读者'),
 'xianyu-purchases': ('个人记录', '查询闲鱼近期购买记录', '需要用户实际订单事实而非一般商品建议时', '已连接的购买记录工具；敏感字段最小披露'),
 'plugin-management': ('能力管理', '发现插件、检查权限依赖与管理连接', '任务缺少某个外部系统能力且需确认已有插件时', '安装/授权状态需实查，缓存文档不等于连接成功'),
 'aspnet-core': ('软件实现','开发ASP.NET Core服务与Web应用','后端路由、认证、中间件或部署需要匹配.NET项目约定时','实际.NET版本、已有架构与官方文档'),
 'develop-web-game': ('软件实现','以小迭代和行为截图验证HTML/JS游戏','游戏交互与时间推进无法由静态代码审查确认时','浏览器测试运行时；操作服从当前宿主和用户规则'),
 'screenshot': ('桌面与浏览器','捕获桌面、窗口或指定区域图像','工具专用截图不足且需要观察原生应用实际状态时','用户允许的本机截图范围；图像并不自动证明交互成功'),
 'winui-app': ('Windows工程','使用WinUI 3和Windows App SDK实现桌面应用','原生Windows窗口、导航与打包需要匹配框架实践时','实际SDK、目标Windows版本和项目结构'),
 'typer': ('软件实现','开发Typer命令行参数和子命令','Python CLI的参数类型、帮助和命令分发需保持一致时','本条位于依赖包内；不会自动作为顶层可调用技能暴露'),
 'review-agent': ('代码交付','只读、缺陷优先的代码改动审查','需要独立发现有证据和可行动的行为缺陷时','明确diff范围与源码；不能把风格偏好当缺陷'),
 'bmad-brainstorming': ('创意与表达','按多种创意方法主持头脑风暴','任务还在发散阶段，现有解法过早收敛时','先保留问题与筛选标准；创意不能当已验证事实'),
 'creative-ideation': ('创意与表达','利用命名创意思考方法生成候选方案','普通列举陷入同质化，需要可比较的不同机制时','随后按需求、成本和证据筛选，不自动实施所有方案'),
 'humanizer': ('写作与表达','去除AI套话并保留作者含义','语言风格问题影响阅读但内容判断应保持不变时','真实作者声音或指定语气；避免为自然感篡改事实'),
 'shadcn': ('软件实现','安装组合shadcn/ui组件并维护主题','需要统一组件交互、Tailwind主题和项目设计语言时','实际组件版本、注册表和已有项目约定'),
 'supabase-postgres-best-practices': ('软件实现','优化Postgres查询、索引、模式和配置','数据库延迟或访问模式需要真实执行计划支持改动时','实际schema、查询、数据规模与执行计划'),
 'plugin-creator': ('能力管理','脚手架化Codex插件和清单','技能、工具与资源需要按插件协议分发时','目标插件规范、注册位置和最小必要权限'),
 'session-report': ('能力管理','分析Claude会话token、缓存、子代理和技能成本','需要从真实转录定位昂贵提示词和能力使用模式时','Claude公开会话记录；用量不等于效果'),
 'playground': ('交互可视化','生成单文件交互HTML探索器','用户需通过参数操作比较候选结果并复制提示词时','明确输入参数、预览与导出边界'),
 'math-olympiad': ('研究与推理','分析竞赛数学问题与证明','需要严格推导而非仅数值答案时','原题条件、证明完整性和独立检查'),
 'frontend-design': ('设计到实现','构建具有明确设计语言的前端界面','产品界面需要视觉层次和可用交互同时成立时','项目约束、真实内容和运行后的界面验证'),
 'claude-md-improver': ('能力管理','审查并改善仓库CLAUDE.md指导','项目指令过时或不能帮助代理作出正确决定时','实际项目行为与用户范围；避免把偶发问题升成全局规则'),
 'claude-automation-recommender': ('能力管理','根据代码仓选择Claude hooks、agent、skill或MCP','重复工作可自动化但尚未确定最小机制时','推荐不等于安装或授权；按收益和维护成本选择'),
}

UNDERSTAND = {
 'understand': ('代码仓架构交互知识图谱', '大仓库结构和关系难以一次理解，需要按组件浏览时'),
 'understand-chat': ('查询现有代码知识图谱', '已有索引可以回答架构问题，避免重新扫全仓时'),
 'understand-dashboard': ('启动代码图谱交互界面', '需要可视展开和浏览已有图谱时'),
 'understand-diff': ('分析补丁的结构影响', 'PR/差异涉及多模块，需要定位受影响关系时'),
 'understand-domain': ('提取业务领域与流程图谱', '代码术语与真实业务流程之间缺乏清晰对应时'),
 'understand-explain': ('深入解释特定模块或函数', '已有问题定位，需要局部调用关系和行为解释时'),
 'understand-knowledge': ('索引知识库/Wiki而非源码', '已有知识文件需要分类、关联和缺口审查时'),
 'understand-onboard': ('根据代码图谱生成入门指南', '新人需要真实入口、主链路和渐进学习路径时'),
}
for _name, (_scenario, _decisive) in UNDERSTAND.items():
    CARDS[_name] = ('知识与项目复盘', _scenario, _decisive, '本地Understand-Anything插件及相应索引；核对索引与源码版本')

ACTION_LABELS = {
 'build-dashboard':'制作可探索仪表盘', 'build-report':'编写有来源的分析报告', 'analyze-data-quality':'审查数据可信度',
 'create-data-context':'整理可复用的数据语义上下文', 'design-kpis':'定义指标口径、分母与护栏',
 'gather-business-context':'获取业务目标、决策者与分析约束', 'index':'选择合适的数据分析工作流',
 'jupyter-notebooks':'构建可复算分析Notebook', 'kpi-reporting':'汇报指标变化与行动项',
 'market-sizing':'估算市场规模并记录假设', 'metric-diagnostics':'解释指标变化及驱动因素',
 'product-business-analysis':'用数据支持产品和业务决定', 'publish-artifact-to-sites':'将分析产物发布到Site',
 'validate-data':'验证数据分析方法、计算与结论', 'visualize-data':'选择并验证数据图表',
 'convert-to-doc':'将现有分析转成文档', 'convert-to-slides':'将现有分析转成幻灯片',
 'report-to-pdf':'生成可共享PDF报告', 'schedule-refresh-jobs':'配置分析刷新任务',
 'share-artifact-summary':'发送分析产物摘要',
 'google-docs':'编辑原生Google文档', 'google-drive':'检索和整理Drive文件',
 'google-drive-comments':'管理Drive评论与回复', 'google-sheets':'精确操作Google表格区域',
 'google-slides':'沿用模板编辑Google幻灯片',
 'hubspot':'操作HubSpot CRM记录', 'hubspot-crm-data-hygiene':'去重并检查CRM数据卫生',
 'hubspot-customer-prep':'为客户会议准备关联记录', 'hubspot-onboarding':'建立CRM初始结构',
 'hubspot-pipeline-health':'检查商机管道健康',
 'agents':'构建Agents API/SDK应用', 'agents-sdk':'构建多工具Agents SDK工作流',
 'devday-guide':'安排OpenAI DevDay参会行程', 'openai-api-migration':'迁移OpenAI API调用',
 'openai-api-troubleshooting':'定位OpenAI API失败', 'openai-computer-use':'开发API电脑操作应用',
 'openai-platform-api-key':'配置OpenAI API密钥目标', 'setup':'完成相关插件初始配置',
 'build-chatgpt-app':'开发ChatGPT App', 'chatgpt-app-submission':'准备ChatGPT App提交',
 'maintain-space':'把新证据合并到已有Pages知识空间', 'manage-schedules':'管理Pages刷新计划',
 'organize-space':'整理Pages空间结构', 'write-page':'撰写或更新Page',
 'create-pet':'创建动画宠物素材', 'pets':'管理现有Work宠物', 'update-pet':'修改并验证动画宠物',
 'bilibili-upload':'上传B站视频', 'douyin-upload':'通过sau上传抖音视频',
 'douyin-upload-mcp-skill':'通过创作者平台发布抖音内容', 'douyin-video':'下载抖音视频并提取文案',
 'kuaishou-upload':'上传快手视频', 'post-to-xhs':'发布小红书图文或长文',
 'wechatsync':'同步文章到多个发布平台', 'xhs-auth':'管理小红书登录状态',
 'xhs-content-ops':'组合小红书搜索与内容运营', 'xhs-explore':'搜索小红书笔记与用户',
 'xhs-interact':'执行小红书点赞评论互动', 'xhs-publish':'发布小红书内容',
 'xiaohongshu-skills':'路由小红书工具流程', 'xiaohongshu-upload':'自动化小红书上传',
 'aspnet-core':'开发ASP.NET Core应用', 'chatgpt-apps':'构建ChatGPT Apps', 'cloudflare-deploy':'部署Cloudflare项目',
 'develop-web-game':'迭代Web游戏并测试', 'hatch-pet':'创建早期版本Work宠物', 'linear':'管理Linear事项',
 'netlify-deploy':'部署Netlify站点', 'notion-knowledge-capture':'把记录收录进Notion知识库',
 'notion-meeting-intelligence':'用Notion资料准备会议', 'notion-research-documentation':'从Notion资料汇总研究',
 'notion-spec-to-implementation':'将Notion规格转成实施任务', 'render-deploy':'部署Render服务',
 'screenshot':'截取本机屏幕画面', 'security-best-practices':'审查安全编码习惯',
 'security-ownership-map':'构建安全相关代码所有权映射', 'security-threat-model':'建立应用威胁模型',
 'sentry':'读取Sentry错误与事件', 'sora':'生成与管理视频', 'speech':'生成语音', 'transcribe':'转录音频',
 'vercel-deploy':'部署Vercel站点', 'yeet':'打包Git提交推送和PR操作', 'openai-docs':'检索OpenAI官方开发文档',
 'frontend-app-builder':'实现前端产品界面', 'frontend-testing-debugging':'定位和测试前端交互问题',
 'react-best-practices':'审查React状态与渲染实践', 'shadcn-best-practices':'使用shadcn组件体系',
 'stripe-best-practices':'集成Stripe支付', 'supabase-best-practices':'集成Supabase数据服务',
 'accessibility-and-inclusive-visualization':'构建可访问数据可视化', 'canvas2d-data-visualization':'实现Canvas二维图表',
 'd3-data-visualization':'实现D3图形与交互', 'dashboards-and-real-time-visualization':'构建实时监控仪表盘',
 'data-visualization':'选择数据表达与图表结构', 'gantt-chart-visualization':'实现甘特计划视图',
 'geospatial-and-cartographic-visualization':'构建地图与地理数据图', 'grammar-of-graphics-and-declarative-visualization':'使用声明式图形语法',
 'node-link-and-diagram-layout':'计算节点边图的布局', 'react-and-nextjs-data-visualization':'在React/Next.js中集成数据图表',
 'reports-pdfs-and-slide-automation':'自动化可视化报告导出', 'scrollytelling-and-parallax-data-visualization':'制作滚动叙事可视化',
 'statistical-and-uncertainty-visualization':'表达统计误差与不确定性', 'testing-data-visualizations':'验证图表数据与交互',
 'threejs-data-visualization':'制作Three.js三维数据图', 'typescript-data-visualization-engineering':'组织类型化图表代码',
 'uml-and-software-architecture-visualization':'绘制UML与软件架构图', 'visualization-strategy-and-critique':'评审可视化选型与读者任务',
 'dashboard-expert':'在Mixpanel构建分析仪表盘', 'mixpanel-auth':'验证Mixpanel认证', 'mixpanelyst':'进行Mixpanel产品分析',
 'posthog':'执行PostHog产品分析',
 'gsap':'设计GSAP动画时间线', 'hyperframes':'编排Hyperframes视频', 'hyperframes-cli':'运行Hyperframes命令行',
 'hyperframes-registry':'复用Hyperframes素材与模板', 'website-to-hyperframes':'把网站视觉转换为视频',
}


def card_for(name, plugin, desc):
    # Skill authors occasionally prefix frontmatter names with plugin names.
    short = name.rsplit(':', 1)[-1].lower().replace(' ', '-')
    if not plugin:
        if short.startswith('remotion-'): plugin='remotion'
        elif short.startswith('figma-'): plugin='figma'
        elif short in {'agents','agents-sdk','build-chatgpt-app','openai-api-troubleshooting','openai-platform-api-key','openai-api-migration','openai-computer-use'}:plugin='openai-developers'
        elif short in {'build-dashboard','build-report','jupyter-notebooks','visualize-data','analyze-data-quality','design-kpis','validate-data'}:plugin='data-analytics'
        elif short in {'frontend-app-builder','frontend-testing-debugging','react-best-practices','stripe-best-practices'}:plugin='build-web-apps'
        elif 'visualization' in short:plugin='build-web-data-visualization'
    if short in CARDS:
        return CARDS[short], 'curated_routing_from_document_and_catalog'
    if plugin in ('plugin-dev','mcp-server-dev','hookify','example-plugin'):
        purpose={'agent-development':'开发子代理定义','command-development':'开发斜杠命令','hook-development':'开发事件钩子',
          'mcp-integration':'集成MCP服务','plugin-settings':'保存插件配置','plugin-structure':'组织插件目录',
          'skill-development':'编写可发现技能','build-mcp-app':'开发MCP交互组件','build-mcp-server':'开发MCP服务器',
          'build-mcpb':'打包MCP本地分发包','writing-rules':'编写Hookify规则','example-command':'阅读命令格式示例',
          'example-skill':'阅读技能格式示例'}.get(short,desc[:150])
        return ('能力管理',purpose,'目标能力需要'+purpose+'这一扩展接口时','核对目标宿主协议；示例与市场缓存不自动成为已安装能力'), 'document_and_name_inference'
    if plugin in ('telegram','imessage','discord'):
        purpose={'access':'审核配对和允许名单','configure':'设置频道连接与策略'}.get(short,short)
        return ('云端知识与协作',plugin+'：'+purpose,'用户要求管理'+plugin+'频道的具体连接或访问问题时','账户、频道和动作时授权；不从图谱历史获得新外发权限'), 'document_and_name_inference'
    label = ACTION_LABELS.get(short)
    if plugin == 'canva':
        sub = short.removeprefix('canva-')
        label = {'brand-check':'核对品牌规范','branded-presentation':'创建品牌幻灯片','bulk-create':'批量创建设计',
                 'design-feedback':'审阅设计','edit-design':'编辑已有设计','implement-feedback':'落实设计反馈',
                 'resize-for-social-media':'适配社交媒体尺寸','translate-design':'翻译已有设计'}.get(sub, sub)
        return ('设计到实现', 'Canva：'+label, '已有Canva设计和品牌资产，需要保持原生编辑能力完成'+label+'时', 'Canva连接器、目标设计权限与准确对象ID'), 'document_and_name_inference'
    if plugin == 'figma':
        sub = short.removeprefix('figma-')
        label = {'code-connect':'绑定代码组件','create-new-file':'创建新设计文件','design-to-code':'把设计转成代码',
                 'generate-design':'生成界面设计','generate-diagram':'生成关系图','generate-library':'生成设计组件库',
                 'implement-motion':'实现设计动画','swiftui':'把设计映射到SwiftUI','use':'操作设计文件',
                 'use-figjam':'操作FigJam白板','use-motion':'编辑动画','use-slides':'编辑设计演示稿'}.get(sub, sub)
        return ('设计到实现','Figma：'+label,'需要Figma原生结构、节点和资源完成'+label+'时','Figma连接器与明确文件/节点；缓存技能是否暴露需确认'), 'document_and_name_inference'
    if plugin == 'remotion':
        sub = short.removeprefix('remotion-')
        label = {'best-practices':'视频组件组织','captions':'字幕编排','create':'创建视频','docs':'查询API文档','interactivity':'交互视频',
                 'maps':'地图动画','markup':'标记驱动动画','multimedia':'媒体合成','render':'渲染导出','saas':'视频SaaS',
                 'studio':'预览编辑','upgrade':'版本迁移'}.get(sub, sub)
        return ('图像与媒体','Remotion：'+label,'代码化视频任务的'+label+'成为实施阻碍时','Node/Remotion项目与对应版本；预览和导出结果分别验证'), 'document_and_name_inference'
    if short.startswith('artifact-template-'):
        suffix=short.removeprefix('artifact-template-')
        translations={'analytics-dashboard':'分析仪表盘','business-review':'业务复盘','design-report':'设计报告','experiment-analysis':'实验分析',
          'financial-budget':'财务预算','investment-committee-memo':'投资委员会备忘录','legal-memorandum':'法律备忘录','market-trends-report':'市场趋势',
          'minimal-letterhead':'简洁信头','operating-calendar':'运营日历','operating-review':'运营复盘','project-kickoff':'项目启动',
          'project-tracker':'项目跟踪','sales-pipeline':'销售管道','simple-dark-mode':'暗色页面','simple-light-mode':'亮色页面',
          'strategy-memorandum':'战略备忘录','system-design':'系统设计','team-alignment':'团队协同','three-statement-forecast':'三表预测'}
        label=translations.get(suffix,suffix)
        return ('产物模板', label+'模板', '任务需要快速建立'+label+'的结构与视觉一致性时', '模板提供格式起点，不能替代真实数据、领域判断或验证'), 'document_and_name_inference'
    if plugin == 'data-analytics':
        return ('数据与实验', label or desc[:160], '分析流程当前缺少'+(label or short)+'这一可复查产物时', '真实数据、业务口径与来源；发布/刷新按当前授权'), 'document_and_name_inference'
    if plugin == 'google-drive' or short.startswith('notion-') or plugin == 'pages':
        return ('云端知识与协作', label or desc[:160], '已有云端文档/知识需要'+(label or short)+'并保留关联时', '对应连接器、文档访问权限、明确目标；不自动外发内容'), 'document_and_name_inference'
    if plugin == 'hubspot':
        return ('商业与招采', label or desc[:160], '客户/商机决策依赖'+(label or short)+'的实际记录时', 'HubSpot连接与对象权限；字段映射和变更范围先确认'), 'document_and_name_inference'
    if plugin == 'openai-developers' or short in ('chatgpt-apps','openai-docs'):
        return ('AI应用开发', label or desc[:160], 'AI应用开发在'+(label or short)+'阶段缺少准确接口方法时', '实际SDK/API版本与官方文档；凭据留在授权目的地'), 'document_and_name_inference'
    if short.startswith(('xhs-','xiaohongshu','douyin','bilibili','kuaishou')) or short in ('post-to-xhs','wechatsync'):
        return ('内容发布与运营', label or desc[:160], '需要在真实内容平台完成'+(label or short)+'而非仅制作草稿时', '平台会话/CLI或MCP；发送、互动、上传等按当前任务授权'), 'document_and_name_inference'
    if short.endswith('-deploy') or short=='yeet':
        return ('网站与发布', label or desc[:160], '已有可验证代码产物需要'+(label or short)+'时', '真实项目和平台账户；保留发布与本地测试的证据边界'), 'document_and_name_inference'
    if short.startswith('security-') or short in ('sentry','linear'):
        return ('代码交付', label or desc[:160], '明确的审计或交付问题需要'+(label or short)+'时', '相关代码/事件来源与当前任务范围；不将一般开发自动扩为安全审计'), 'document_and_name_inference'
    if plugin in ('build-web-apps',):
        return ('软件实现',label or desc[:160],'前端交付受'+(label or short)+'的实现细节阻塞时','对应项目栈、版本和真实交互验收'), 'document_and_name_inference'
    if plugin in ('build-web-data-visualization',):
        return ('交互可视化',label or desc[:160],'读者任务确需'+(label or short)+'而普通表格不足时','真实数据、图表语义和可访问性；验证数据映射与交互'), 'document_and_name_inference'
    if plugin in ('hyperframes', 'work-pets') or short in ('sora','speech','transcribe','hatch-pet'):
        return ('图像与媒体',label or desc[:160],'媒体产物需要'+(label or short)+'且格式/时序必须正确时','对应运行时或生成工具、输入素材与交付格式'), 'document_and_name_inference'
    if plugin in ('mixpanel-headless','posthog'):
        return ('数据与实验',label or desc[:160],'产品分析需读取真实事件与漏斗完成'+(label or short)+'时','平台连接、事件定义、时间窗口和分母口径'), 'document_and_name_inference'
    return ('待精细分类', label or desc[:200] or short, '文档声明的能力恰好构成当前任务缺口时；决定性价值尚未独立审查', '先读取原始技能和当前工具可用性'), 'description_only_unreviewed'


def roots_for(home):
    return [(home/'.codex/skills','codex_local'), (home/'.codex/plugins/cache','codex_plugin'),
            (home/'.claude/skills','claude_local'), (home/'.claude/plugins/cache','claude_plugin'),
            (home/'.agents/skills','shared_agent'), (home/'.cursor/skills','cursor_local'),
            (home/'.gemini/skills','gemini_local'), (home/'.config/opencode/skills','opencode_local'),
            (home/'.config/agents/skills','shared_agent'), (home/'AppData/Roaming/Code/User/prompts','vscode_prompts'),
            (home/'.dsh/skills','dsh_local'), (home/'.kiro/skills','kiro_local'), (home/'.kiro/powers','kiro_powers'),
            (home/'.codex-cli/skills','codex_cli_local'), (home/'.codex-cli/plugins/cache','codex_cli_plugin'),
            (home/'.claude/plugins/marketplaces','claude_marketplace'),
            (home/'.understand-anything-plugin/skills','standalone_understand'),
            (home/'.config/cagent/skills','cagent_local'),(home/'.cagent/store','cagent_store')]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--home',default=str(Path.home()))
    ap.add_argument('--out',default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument('--extra-root',action='append',default=[],help='Additional discovered skill root; no whole-drive scan')
    ap.add_argument('--only-extra-roots',action='store_true',help='Inspect only --extra-root paths; do not inspect host directories or configuration')
    ns=ap.parse_args()
    home,out=Path(ns.home),Path(ns.out)
    config=home/'.codex/config.toml'
    cfg=tomllib.loads(config.read_text(encoding='utf-8-sig')) if not ns.only_extra_roots and config.exists() else {}
    # Only booleans and skill paths are inspected from config. Never persist secrets.
    disabled={str(Path(i['path'])).casefold() for i in cfg.get('skills',{}).get('config',[]) if i.get('enabled') is False and 'path' in i}
    plugins={k:v.get('enabled') for k,v in cfg.get('plugins',{}).items() if isinstance(v,dict)}
    cli_config=home/'.codex-cli/config.toml'
    cli_cfg=tomllib.loads(cli_config.read_text(encoding='utf-8-sig')) if not ns.only_extra_roots and cli_config.exists() else {}
    cli_disabled={str(Path(i['path'])).casefold() for i in cli_cfg.get('skills',{}).get('config',[]) if i.get('enabled') is False and 'path' in i}
    cli_plugins={k:v.get('enabled') for k,v in cli_cfg.get('plugins',{}).items() if isinstance(v,dict)}
    cfg_sha=hashlib.sha256(config.read_bytes()).hexdigest() if not ns.only_extra_roots and config.exists() else None
    records=[]; counts={}; omissions=[]
    discovered=([] if ns.only_extra_roots else roots_for(home))+[(Path(p),'extra_local') for p in ns.extra_root]
    for root,origin in discovered:
        entry={'path':str(root),'exists':root.exists(),'origin':origin,'physical_skill_files':0}
        counts[str(root)]=entry
        if not root.exists():
            continue
        for p in sorted(root.rglob('SKILL.md')):
            entry['physical_skill_files']+=1
            try:
                raw=p.read_bytes(); text=raw.decode('utf-8-sig')
            except (OSError,UnicodeError) as e:
                omissions.append({'path':str(p),'error':type(e).__name__});continue
            meta,parse_error=frontmatter(text)
            name=meta.get('name') or p.parent.name
            parts=p.relative_to(root).parts
            resolved=str(p.resolve())
            plugin=None;version=meta.get('version')
            backup=bool(re.search(r'(?:backup|\.bak|archive|\.old)(?:[-_.\\/]|$)',str(p),re.I))
            if origin=='claude_marketplace':
                market,branch,plugin=parts[:3];namespace=f'claude-marketplace/{market}/{plugin}'
                availability='marketplace_cache';basis='marketplace_source_only; no_installed_plugins_manifest_found'
            elif origin.endswith('_plugin'):
                if len(parts)<5:
                    namespace=origin;availability='unknown';basis='unexpected_plugin_layout'
                else:
                    market,plugin,cache_version=parts[:3];namespace=f'{market}/{plugin}'
                    version=version or cache_version
                    switches=cli_plugins if origin=='codex_cli_plugin' else plugins
                    if origin=='codex_cli_plugin':namespace='codex-cli/'+namespace
                    switch=switches.get(f'{plugin}@{market}')
                    availability='configured_enabled' if switch is True else ('disabled' if switch is False else 'cached_unknown')
                    basis=f'config.plugins[{plugin}@{market}].enabled={switch}; cache_presence_is_not_tool_readiness'
            elif any(s.lower() in ('.venv','venv','node_modules','site-packages') for s in parts):
                namespace='dependency/'+name;availability='vendored_dependency';basis='SKILL.md_inside_dependency_runtime; not_a_top_level_discoverable_skill'
            elif 'understand-anything' in resolved.lower():
                namespace='understand-anything';availability='local_discoverable';basis='existing_junction_in_agent_skill_root'
            else:
                namespace='system' if origin=='codex_local' and parts[0]=='.system' else ('local' if origin=='codex_local' else origin)
                if origin=='dsh_local':namespace='dsh'
                if origin.startswith('project_'):namespace='project/'+origin.removeprefix('project_')
                if origin=='extra_local':namespace='extra/'+hashlib.sha256(str(root).casefold().encode('utf-8')).hexdigest()[:10]
                disabled_paths=cli_disabled if origin=='codex_cli_local' else disabled
                availability='disabled' if str(p).casefold() in disabled_paths else 'local_discoverable'
                basis='config.skills.enabled=false' if availability=='disabled' else 'SKILL.md_in_known_skill_root; actual_dependencies_not_executed'
                if origin.startswith('project_'):
                    availability='project_scoped';basis='known_project_skill_root; not_global_host_discovery'
                if '_disabled' in str(p).lower() or origin.endswith('_disabled'):
                    availability='disabled';basis='explicit_disabled_directory; not_default_selection'
            if backup:
                availability='backup';basis='backup_path_marker; do_not_select_as_default'
            desc=meta.get('description','')
            card,review=card_for(name,plugin,desc)
            category,scenario,decisive,prereq=card
            sha=hashlib.sha256(raw).hexdigest()
            # Strip a backup suffix from path identity by using frontmatter name.
            logical='skill:'+namespace+'/'+name
            identity=logical+'@'+sha[:12]
            warnings=[]
            if parse_error: warnings.append(parse_error)
            if not desc: warnings.append('missing_description')
            if availability in ('cached_unknown','configured_enabled'):warnings.append('cache_or_config_does_not_prove_current_tool_exposure_or_account_readiness')
            if availability=='disabled':warnings.append('disabled_in_current_config; do_not_silently_enable')
            if backup:warnings.append('backup_copy_in_discovery_tree_may_create_ambiguous_name')
            short=name.rsplit(':',1)[-1]
            if short in ('windows-low-level-input','computer-use','playwright','playwright-interactive'):
                warnings.append('Check current host capabilities and user rules; never bypass a tool policy stop')
            if short=='powershell-windows':warnings.append('document_ASCII_only_rule_is_overbroad_for_UTF8_Chinese_tasks; evaluate_actual_PowerShell_encoding')
            if short=='anysearch':warnings.append('document_fallback_approval_rule_must_not_override_current_user_authorization')
            if short=='keba-headless-operations':warnings.append('historical_controller_addresses_and_routes_are_not_current_device_truth')
            headings=[line.lstrip('#').strip() for line in text.splitlines() if line.startswith('#')][:12]
            refs=[]
            prose=re.sub(r'^```.*?^```[^\n]*$', '', text, flags=re.M|re.S)
            for ref in re.findall(r'\]\(([^)]+)\)',prose):
                if '/' in ref and not re.match(r'(https?://|#|mailto:)',ref) and not re.search(r'[A-Za-z]:|^/|[{}<> ]',ref):
                    refs.append(ref.split('#')[0])
            broken=[r for r in dict.fromkeys(refs) if r and not (p.parent/r).exists()]
            if broken: warnings.append('missing_relative_references:'+','.join(broken[:6]))
            record={'id':identity,'logical_id':logical,'name':name,'description':desc,'path':str(p),
              'resolved_path':resolved,'locations':[str(p)],'sha256':sha,'origin':origin,'availability':availability,
              'availability_basis':basis,'namespace':namespace,'version':version,'duplicate_of':None,
              'tags':[category,short]+([plugin] if plugin else []),'scenarios':[scenario],
              'decisive_scenarios':[decisive],'prerequisites':[prereq],
              'content_summary':scenario+'。关键作用：'+decisive+'。',
              'review_basis':review,'evaluation_status':'capability_document_only; observed_effectiveness_not_inferred',
              'document_metrics':{'lines':len(text.splitlines()),'bytes':len(raw),'has_scripts':(p.parent/'scripts').is_dir(),
                'has_references':(p.parent/'references').is_dir(),'headings':headings,'relative_reference_count':len(set(refs)),
                'missing_relative_references':broken},
              'warnings':warnings,'evidence':[{'path':str(p),'line':1,'sha256':sha,'kind':'skill_document'}]}
            records.append(record)
    # Same source code exposed through .codex and .agents is one version, with aliases.
    by_id={}
    for row in records:
        if row['id'] in by_id:
            same=by_id[row['id']];same['locations'].extend(row['locations'])
            same.setdefault('alias_origins',[]).append(row['origin'])
        else: by_id[row['id']]=row
    unique=sorted(by_id.values(),key=lambda x:(x['namespace'],x['name'],x['sha256']))
    by_sha={}; by_name=collections.defaultdict(list); by_logical=collections.defaultdict(list)
    for row in unique:
        if row['sha256'] in by_sha:
            row['duplicate_of']=by_sha[row['sha256']]
            row['warnings'].append('identical_content_in_another_namespace; do_not_count_as_independent_method')
        else:by_sha[row['sha256']]=row['id']
        by_name[row['name']].append(row['id']);by_logical[row['logical_id']].append(row['id'])
    for row in unique:
        row['same_name_ids']=[x for x in by_name[row['name']] if x!=row['id']]
        row['other_version_ids']=[x for x in by_logical[row['logical_id']] if x!=row['id']]
        if row['same_name_ids']:row['warnings'].append('same_frontmatter_name_has_other_versions_or_namespaces; resolve_by_path_and_version')
    source_mismatches=[]
    for row in unique:
        for location in row['locations']:
            try:
                current=hashlib.sha256(Path(location).read_bytes()).hexdigest()
            except OSError:
                current=None
            if current!=row['sha256']:source_mismatches.append(location)
    frozen=dt.datetime.now(dt.timezone.utc).isoformat()
    coverage={'schema':'ai-knowledge/inventory-v1','snapshot_at_utc':frozen,'home':str(home),'roots':list(counts.values()),
      'physical_skill_files':sum(r['physical_skill_files'] for r in counts.values()),'version_records':len(unique),
      'logical_skills':len(by_logical),'unique_document_hashes':len(by_sha),'alias_locations_folded':len(records)-len(unique),
      'availability_counts':dict(collections.Counter(r['availability'] for r in unique)),
      'review_basis_counts':dict(collections.Counter(r['review_basis'] for r in unique)),
      'category_counts':dict(collections.Counter(r['tags'][0] for r in unique)),
      'same_name_collisions':{k:v for k,v in by_name.items() if len(v)>1},
      'verification':{'unique_ids':len({r['id'] for r in unique})==len(unique),
        'source_locations_checked':sum(len(r['locations']) for r in unique),
        'source_recheck_mismatches':source_mismatches,
        'duplicate_reference_integrity':all(r['duplicate_of'] is None or r['duplicate_of'] in by_id for r in unique)},
      'config_evidence':{'path':str(config),'sha256':cfg_sha,'additional_config_path':str(cli_config),
        'additional_config_sha256':hashlib.sha256(cli_config.read_bytes()).hexdigest() if not ns.only_extra_roots and cli_config.exists() else None,
        'inspected_fields':[] if ns.only_extra_roots else ['skills.config.path','skills.config.enabled','plugins.*.enabled'],
        'secrets_persisted':False},'read_errors':omissions,
      'not_covered':['任意盘符下未注册/非标准目录的技能副本','仅云端提供且未落盘的技能','未安装插件的远端技能',
        '未遍历任意仓库内的项目专用技能；只包含已发现的宿主和明确项目skills根',
        '非SKILL.md形式的提示词、MCP工具、POWER.md、agent配置和隐含知识不在本次技能文件数量保证范围'],
      'interpretation':['local_discoverable只证明在已知目录中存在且未显式禁用；不证明脚本、账号或硬件可用',
        'configured_enabled只证明配置开启且缓存存在；本轮工具实际暴露与连接状态需调用时检查',
        'cached_unknown不是已启用，也不能仅按版本号判定旧版本；不同marketplace保留命名空间',
        'marketplace_cache是Claude市场源码，未发现installed_plugins清单；vendored_dependency是依赖包附带文档',
        'project_scoped位于已知项目技能目录，不能按全局默认能力调用；历史已消失路径保留覆盖缺口',
        '场景与关键作用来自技能文档及人工/规则分类，是适用性假设，不代表历史效果或调用频率',
        'sha256固定本次SKILL.md正文，不是整个脚本/引用资源包的版本，也不能据此还原历史调用时的正文',
        '统计按内容版本与逻辑技能分别报告；本次盘点读文件不计历史使用'],
      'source_mutation':False,'external_actions':False}
    emit(out/'data/inventory.jsonl',unique,True)
    emit(out/'reports/inventory-coverage.json',coverage)
    notes=['# 本机技能盘点边界','',f'快照 UTC：{frozen}',
      f'在标准/已知技能根中发现 {coverage["physical_skill_files"]} 份 SKILL.md；合并路径别名后 {len(unique)} 个内容版本、{len(by_logical)} 个命名空间内逻辑技能、{len(by_sha)} 份独立内容hash。',
      '', '## 可用性并非效果', '',
      *[f'- `{k}`：{v} 个版本。' for k,v in coverage['availability_counts'].items()],
      '', *['- '+s for s in coverage['interpretation']], '', '## 覆盖范围','',
      *[f'- `{x["path"]}`：'+(f'发现{x["physical_skill_files"]}份' if x['exists'] else '不存在') for x in counts.values()],
      '', '未覆盖：', *['- '+s for s in coverage['not_covered']], '',
      '## 值得保留的选择边界','',
      '- `project-evidence-graph`负责开发历史与因果证据；`understand`负责代码结构；`graph-engineering`负责执行依赖。三者名称相近但不可互相替代。',
      '- `.agents/skills`的Understand链接与Codex链接指向同一来源，不应重复计算能力和使用次数。',
      '- `anysearch.backup-*`位于发现目录，保留作历史来源，不作为默认版本。',
      '- 本地禁用技能仍存在磁盘；选择器需要区分禁用、缓存、已配置开启与工具就绪。',
      '- 旧技能中的固定路径、ASCII-only或额外批准规则不能覆盖当前任务、用户规则或已验证环境。',
      '- 浏览器自动化服从当前宿主与用户规则；低层输入不用于绕过策略停止。',
      '', '## 本轮验证','',
      f'- ID唯一、重复引用完整性：通过；复核{coverage["verification"]["source_locations_checked"]}个来源路径SHA256，变化/读取失败{len(source_mismatches)}个。',
      '- 本次hash仅针对入口正文；需要证明实际执行方法的版本时，应另冻结脚本与相关引用资源。',
      '', '本次未修改技能正文、插件状态、全局配置或账号。未运行技能的业务脚本。']
    (out/'reports/inventory-notes.md').write_text('\n'.join(notes)+'\n',encoding='utf-8')
    print(json.dumps({k:coverage[k] for k in ('physical_skill_files','version_records','logical_skills','unique_document_hashes','availability_counts','review_basis_counts','category_counts')},ensure_ascii=False))


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    main()
