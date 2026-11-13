#!/usr/bin/env python3
"""Report current graph evidence without inventing local acceptance claims."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys

from kb import KnowledgeBase


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    kb = KnowledgeBase(args.root)
    summary = kb.nodes.get("observation:global-summary", {}).get("properties", {})
    skills = [n for n in kb.nodes.values() if n["kind"] == "skill"]
    uses = [n for n in kb.nodes.values() if n["kind"] == "usage"]
    reviewed = [n for n in uses if n["properties"].get("manual_review_id") or
                n["properties"].get("outcome", {}).get("status") in {"verified_help", "mixed", "failure"}]
    top = sorted(skills, key=lambda n: -n["properties"].get("usage_metrics", {}).get("observed_episodes", 0))[:15]
    lines = ["# 能力与经验图谱评估报告", "",
             f"生成时间：{datetime.now(timezone.utc).isoformat()}；账本 revision：{kb.revision}。", "",
             "报告只汇总当前图谱中的记录。示例、候选、验证状态和来源范围需要分别读取；记录数量不是能力或效果保证。", "",
             "## 覆盖与统计", "",
             "| 节点类型 | 数量 |", "|---|---:|"]
    for kind, count in sorted(Counter(n["kind"] for n in kb.nodes.values()).items()):
        lines.append(f"| {kind} | {count} |")
    lines += ["", "| 已记录指标 | 值 |", "|---|---|"]
    for key in ("physical_skill_files", "logical_skills", "current_document_versions", "observed_episodes",
                "skills_with_observed_use", "skills_with_verified_help", "verified_help_real_task_cases",
                "verified_help_synthetic_cases", "outcome_reviewed_cases", "observed_workspaces",
                "inventory_snapshot", "history_cutoff"):
        lines.append(f"| {key} | {cell(summary.get(key, 'unknown'))} |")
    lines += ["", "未记录值保留 unknown。未观察到使用不等于从未使用；读文件、工具成功和项目效果不是同一个指标。", "",
              "## 观察频率", "", "| 技能入口 | 观察片段 | 读取 | 执行 | 已审结果 | 真实帮助案例 |", "|---|---:|---:|---:|---:|---:|"]
    for node in top:
        metrics = node["properties"].get("usage_metrics", {})
        values = [metrics.get(key, "unknown") for key in
                  ("observed_episodes", "read_episodes", "executed_episodes", "reviewed_outcome_episodes", "verified_help_real_task_episodes")]
        lines.append("| " + cell(node["title"]) + " | " + " | ".join(map(cell, values)) + " |")
    lines += ["", "同一片段可以同时读取和执行，不能将两列相加；频次不是效果排名。", "",
              "## 已审查案例与知识反馈", ""]
    if not reviewed:
        lines += ["当前图谱没有已审查使用案例；不据此推断技能无效。", ""]
    for node in reviewed:
        properties = node["properties"]
        outcome = properties.get("outcome", {})
        lines += [f"### {node['title']}", "", f"ID：`{node['id']}`；状态：{outcome.get('status', 'unknown')}；范围：{outcome.get('scope', 'unknown')}。",
                  str(outcome.get("claim", "未提供结论")), ""]
        for limit in outcome.get("limitations", []):
            lines.append("- " + str(limit))
        feedback = properties.get("knowledge_feedback") or properties.get("assessment", {}).get("knowledge_feedback")
        if isinstance(feedback, dict):
            for key, label in (("learned_fact", "获得的知识"), ("changed_action", "改变的操作"), ("progress_impact", "进度影响")):
                if feedback.get(key):
                    lines.append(f"- {label}：{feedback[key]}")
        lines.append("")
    lines += ["## 解释与维护边界", "",
              "图谱将节点、来源、使用观察与结果评价分开。方法卡可以直接复用；需要完整技能时，应再读取实际 SKILL.md。",
              "检索为词面和中文片段匹配；热度不能替代适用条件、验证范围、反例和成本。自动归集不能建立反事实因果。",
              "本报告没有执行部署、桌面操作或设备验收。通过 kb get/history 检查案例来源；基线不可覆盖，后续变更经 propose/review 保留历史。",
              "本地规模较大或高频并发写入时，应测量内存加载和整账本事务成本，再决定是否升级存储。", ""]
    target = args.root / "reports" / "评估报告.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(target)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
