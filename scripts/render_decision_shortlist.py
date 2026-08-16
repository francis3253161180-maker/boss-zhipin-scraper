"""Render the current local job decisions as a recruiter-reviewable Markdown list."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


TIER_ORDER = {"冲刺": 1, "主申": 2, "核验后扩展": 3, "排除": 4}


def clean(value: object) -> str:
    return str(value or "—").replace("|", "；").replace("\n", " ").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("job-data/boss_job_research.sqlite3"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--tiers",
        default="冲刺,主申,核验后扩展,排除",
        help="逗号分隔的决策分层；例如 '冲刺,主申,核验后扩展'。",
    )
    parser.add_argument("--title", default="第一批投递决策清单")
    args = parser.parse_args()
    selected_tiers = [tier.strip() for tier in args.tiers.split(",") if tier.strip()]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(
            conn.execute(
                """
                SELECT company, title, city, location_text, salary_text, job_link,
                       work_days_per_week, internship_months, degree_requirement,
                       hard_gate, data_quality_status, value_score, fit_score,
                       probability_score, priority_score, priority_tier,
                       application_track, recommendation, verification_items,
                       decision_reason
                FROM job_catalog
                WHERE priority_tier IS NOT NULL
                """
            )
        )
    finally:
        conn.close()

    rows = [row for row in rows if row["priority_tier"] in selected_tiers]
    rows.sort(key=lambda row: (TIER_ORDER.get(row["priority_tier"], 99), -(row["priority_score"] or 0), row["company"]))
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["priority_tier"], []).append(row)

    lines = [
        f"# {args.title}",
        "",
        "> 日期：2026-08-14｜基于 BOSS 完整 JD、当前评估与本地校对结果生成。此清单不代表已经投递；投递、沟通和附件上传均由本人确认后执行。",
        "",
        "## 使用方式",
        "",
        "- **冲刺**：价值高、竞争也高，应投但不以单一结果判断能力。",
        "- **主申**：岗位主线、现有证据和实习条件相对平衡，是优先沟通与投递的核心池。",
        "- **核验后扩展**：先完成主体、技术栈或职责边界核验，再决定是否投入一次投递机会。",
        "- 分数用于排序，不是对 offer 概率的承诺：价值分看技术与长期发展，匹配分看现有经历证据，机会分综合硬门槛、技术缺口与竞争强度。",
        "",
    ]
    for tier in selected_tiers:
        section = grouped.get(tier, [])
        if not section:
            continue
        lines.extend([f"## {tier}（{len(section)} 个）", ""])
        for index, row in enumerate(section, start=1):
            score = "/".join(str(row[name]) if row[name] is not None else "—" for name in ("value_score", "fit_score", "probability_score"))
            link = row["job_link"]
            title = f"[{clean(row['company'])}｜{clean(row['title'])}]({link})" if link else f"{clean(row['company'])}｜{clean(row['title'])}"
            lines.extend([
                f"### {index}. {title}",
                "",
                f"- **地点与条件：** {clean(row['location_text'])}｜{clean(row['salary_text'])}｜{clean(row['work_days_per_week'])} 天/周｜{clean(row['internship_months'])} 个月｜学历：{clean(row['degree_requirement'])}",
                f"- **材料：** {clean(row['application_track'])}；建议：{clean(row['recommendation'])}",
                f"- **决策分：** 价值 / 匹配 / 机会 = {score}；综合优先级：{clean(row['priority_score'])}；硬门槛：{clean(row['hard_gate'])}",
                f"- **为什么入选：** {clean(row['decision_reason'])}",
                f"- **投递前核验：** {clean(row['verification_items'])}",
                f"- **数据状态：** {clean(row['data_quality_status'])}",
                "",
            ])
    lines.extend([
        "## 下一步执行顺序",
        "",
        "1. 先核验所有“冲刺”和“主申”岗位是否仍开放，以及城市、届别、出勤和团队主体；大厂优先同步看官网。",
        "2. 将核验通过的岗位分两批投递：第一批以冲刺 + 主申中最匹配的 10–15 个为主；第二批根据回复率补投其余主申与扩展岗位。",
        "3. 只有在某一城市或方向的可投岗位不足时，才继续进行行业筛选或第 2–3 页搜索。",
        "",
    ])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.output} ({len(rows)} decisions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
