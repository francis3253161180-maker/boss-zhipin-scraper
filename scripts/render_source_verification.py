"""Render a bounded, evidence-level-aware source-verification report."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


MATCH_ORDER = {"精确同岗": 1, "官方渠道/方向一致": 2, "官方渠道存在": 3, "仅BOSS已核验": 4}


def clean(value: object) -> str:
    return str(value or "—").replace("|", "；").replace("\n", " ").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("job-data/boss_job_research.sqlite3"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        decisions = list(
            conn.execute(
                """
                SELECT job_id, company, title, city, priority_tier, job_link
                FROM job_catalog
                WHERE priority_tier IS NOT NULL AND priority_tier <> '排除'
                """
            )
        )
        checks = {}
        for row in conn.execute(
            """
            SELECT s.*
            FROM job_source_checks s
            JOIN (
                SELECT job_id, MAX(source_check_id) AS source_check_id
                FROM job_source_checks
                GROUP BY job_id
            ) latest ON latest.source_check_id=s.source_check_id
            """
        ):
            checks[row["job_id"]] = row
    finally:
        conn.close()

    lines = [
        "# 高优先岗位：官网与主体核验",
        "",
        "> 核验日期：2026-08-14｜范围：当前冲刺、主申与核验后扩展岗位。BOSS 完整 JD 是岗位职责与当前薪资/时长的首要来源；官网检索用于确认公司官方招聘通道、方向与额外风险，不将‘方向相近’误写成‘同一职位已在官网确认’。",
        "",
        "## 证据等级",
        "",
        "| 等级 | 含义 | 可采取的动作 |",
        "|---|---|---|",
        "| 精确同岗 | 官网可确认同一职位或唯一岗位编号 | 可优先走官网/官方内推入口 |",
        "| 官方渠道/方向一致 | 官网确认招聘入口及相近技术方向，但未确认同一 job ID | 可投 BOSS，同时在官网搜索同类岗位 |",
        "| 官方渠道存在 | 官网入口存在，但公开信息不足以确认方向 | 先在官网系统检索/向 BOSS 招聘方确认 |",
        "| 仅BOSS已核验 | 当前仅有 BOSS 完整 JD 或未找到公开官方证据 | 不代表岗位有问题，但应先核实主体、用工与岗位状态 |",
        "",
    ]

    buckets: dict[str, list[sqlite3.Row]] = {}
    no_check: list[sqlite3.Row] = []
    for decision in decisions:
        check = checks.get(decision["job_id"])
        if check:
            buckets.setdefault(check["match_level"], []).append(decision)
        else:
            no_check.append(decision)

    for level in sorted(buckets, key=lambda item: MATCH_ORDER.get(item, 99)):
        rows = buckets[level]
        lines.extend([f"## {level}（{len(rows)} 个）", ""])
        for row in rows:
            check = checks[row["job_id"]]
            title = f"[{clean(row['company'])}｜{clean(row['title'])}]({row['job_link']})" if row["job_link"] else f"{clean(row['company'])}｜{clean(row['title'])}"
            lines.extend([
                f"### {title}",
                "",
                f"- **当前分组：** {clean(row['priority_tier'])}｜城市：{clean(row['city'])}",
                f"- **来源：** [{clean(check['source_name'])}]({check['source_url']})（{clean(check['source_type'])}）",
                f"- **结论：** {clean(check['result'])}",
                f"- **证据：** {clean(check['evidence'])}",
                "",
            ])

    if no_check:
        lines.extend(["## 尚未获得公开官方证据（%d 个）" % len(no_check), ""])
        lines.append("这些岗位保留 BOSS 完整 JD，但不把它们视为官网已确认岗位。建议在投递前通过公司官网、企业公示主体或 BOSS 招聘方完成一次短核验。")
        lines.append("")
        for row in no_check:
            title = f"[{clean(row['company'])}｜{clean(row['title'])}]({row['job_link']})" if row["job_link"] else f"{clean(row['company'])}｜{clean(row['title'])}"
            lines.append(f"- {title}｜{clean(row['priority_tier'])}｜{clean(row['city'])}")
        lines.append("")

    lines.extend([
        "## 核验后的行动原则",
        "",
        "1. 对官方渠道/方向一致的岗位：BOSS 与官网可以并行查看，但只选一个正式投递入口；若两个入口显示同一岗位，不重复投递。",
        "2. 对仅 BOSS 已核验或主体待核验的岗位：先确认公司主体、业务团队、实习证明、是否收费与岗位是否仍开放，再决定是否投递。",
        "3. 公司具备 GPU、数据或训练平台不等于个人可自由用于论文；所有资源均以岗位授权、数据合规和成果归属为边界。",
        "",
    ])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.output} ({len(decisions)} decisions, {len(checks)} source checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
