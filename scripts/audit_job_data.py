"""Audit the local BOSS job-research database without contacting BOSS.

The audit deliberately preserves raw search/detail/assessment history.  It
creates only derived quality flags and a concise report so later prioritization
uses one current assessment per job instead of counting historical revisions.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_issue(
    conn: sqlite3.Connection,
    job_id: str,
    code: str,
    severity: str,
    note: str,
    observed_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO job_audit_issues
            (job_id, issue_code, severity, note, source, observed_at)
        VALUES (?, ?, ?, ?, 'derived', ?)
        """,
        (job_id, code, severity, note, observed_at),
    )


def refresh_derived_issues(conn: sqlite3.Connection) -> Counter[str]:
    """Rebuild only derived open issues; manual review history remains intact."""
    observed_at = now_iso()
    conn.execute(
        "DELETE FROM job_audit_issues WHERE source='derived' AND resolved_at IS NULL"
    )
    counts: Counter[str] = Counter()

    for row in conn.execute(
        """
        SELECT job_id, COUNT(*) AS versions
        FROM assessments
        GROUP BY job_id
        HAVING COUNT(*) > 1
        """
    ):
        add_issue(
            conn,
            row[0],
            "REASSESSMENT_HISTORY",
            "info",
            f"保留 {row[1]} 条评估历史；岗位池统计应使用 current_assessments 视图。",
            observed_at,
        )
        counts["REASSESSMENT_HISTORY"] += 1

    for row in conn.execute(
        """
        SELECT a.job_id, a.grade, a.eligibility
        FROM current_assessments a
        LEFT JOIN job_decisions d ON d.job_id=a.job_id
        WHERE ((a.grade='排除' AND a.eligibility NOT IN ('ineligible', 'exclude'))
            OR (a.grade<>'排除' AND a.eligibility IN ('ineligible', 'exclude')))
          AND COALESCE(d.hard_gate, '待核验') <> '排除'
        """
    ):
        add_issue(
            conn,
            row[0],
            "GRADE_ELIGIBILITY_CONFLICT",
            "warning",
            f"当前评估 grade={row[1]} 与 eligibility={row[2]} 不一致；需人工确定硬门槛。",
            observed_at,
        )
        counts["GRADE_ELIGIBILITY_CONFLICT"] += 1

    for row in conn.execute(
        """
        SELECT job_id, company, title
        FROM jobs
        WHERE company LIKE '%...%' OR title LIKE '%...%'
           OR company LIKE '%…%' OR title LIKE '%…%'
        """
    ):
        add_issue(
            conn,
            row[0],
            "TRUNCATED_DISPLAY_NAME",
            "warning",
            f"公司或岗位名称来自页面截断：{row[1]}｜{row[2]}；投递前以详情页/官网全称校验。",
            observed_at,
        )
        counts["TRUNCATED_DISPLAY_NAME"] += 1

    for row in conn.execute(
        """
        SELECT job_id, work_days_per_week
        FROM job_facts
        WHERE work_days_per_week < 1 OR work_days_per_week > 6
        """
    ):
        add_issue(
            conn,
            row[0],
            "WORKDAY_OUTLIER",
            "warning",
            f"解析到每周 {row[1]} 天；可能是 JD 其他文本被误匹配，投递前人工确认。",
            observed_at,
        )
        counts["WORKDAY_OUTLIER"] += 1

    for row in conn.execute(
        """
        SELECT c.job_id
        FROM job_catalog c
        WHERE c.detail_fetched_at IS NOT NULL
          AND (c.degree_requirement IS NULL OR trim(c.degree_requirement)='')
        """
    ):
        add_issue(
            conn,
            row[0],
            "MISSING_DEGREE_REQUIREMENT",
            "info",
            "完整 JD 未解析出学历要求；不应据此假设岗位接受硕士在读。",
            observed_at,
        )
        counts["MISSING_DEGREE_REQUIREMENT"] += 1

    return counts


def build_report(conn: sqlite3.Connection, issue_counts: Counter[str]) -> str:
    summary = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM jobs) AS jobs,
          (SELECT COUNT(*) FROM job_details) AS detail_versions,
          (SELECT COUNT(*) FROM current_assessments) AS current_assessments,
          (SELECT COUNT(*) FROM job_decisions) AS decisions,
          (SELECT COUNT(*) FROM job_audit_issues WHERE resolved_at IS NULL) AS open_issues
        """
    ).fetchone()
    grades = conn.execute(
        """
        SELECT grade, eligibility, COUNT(*)
        FROM current_assessments
        GROUP BY grade, eligibility
        ORDER BY grade, eligibility
        """
    ).fetchall()
    cities = conn.execute(
        """
        SELECT j.city,
               COUNT(*) AS assessed,
               SUM(CASE WHEN a.grade IN ('A', 'A-') AND a.eligibility='eligible'
                        THEN 1 ELSE 0 END) AS high_eligible
        FROM current_assessments a
        JOIN jobs j ON j.job_id=a.job_id
        GROUP BY j.city
        ORDER BY j.city
        """
    ).fetchall()

    lines = [
        "# 岗位库校对报告",
        "",
        f"> 生成时间：{now_iso()}｜数据源：本地 SQLite；本报告未访问 BOSS。",
        "",
        "## 当前可用数据",
        "",
        f"- 去重岗位：{summary[0]}；完整 JD 历史版本：{summary[1]}；当前评估：{summary[2]}；当前投递决策：{summary[3]}。",
        f"- 未关闭质量标记：{summary[4]}。历史评估不删除，所有统计以 `current_assessments` 为准。",
        "",
        "## 当前评估分布",
        "",
        "| 评级 | 资格 | 数量 |",
        "|---|---|---:|",
    ]
    lines.extend(f"| {grade} | {eligibility} | {count} |" for grade, eligibility, count in grades)
    lines.extend([
        "",
        "## 城市覆盖（按当前评估）",
        "",
        "| 城市 | 已评估 | A/A- 且资格通过 |",
        "|---|---:|---:|",
    ])
    lines.extend(f"| {city} | {assessed} | {high} |" for city, assessed, high in cities)
    lines.extend([
        "",
        "## 质量标记",
        "",
        "| 标记 | 数量 | 处理方式 |",
        "|---|---:|---|",
    ])
    actions = {
        "REASSESSMENT_HISTORY": "仅使用最新评估；保留历史便于追溯。",
        "GRADE_ELIGIBILITY_CONFLICT": "人工确认硬门槛后写入当前投递决策。",
        "TRUNCATED_DISPLAY_NAME": "投递前以详情页或官网补全全称。",
        "WORKDAY_OUTLIER": "人工复核出勤天数，避免解析误匹配。",
        "MISSING_DEGREE_REQUIREMENT": "不假定学历放宽；沟通前核验。",
    }
    for code, count in sorted(issue_counts.items()):
        lines.append(f"| {code} | {count} | {actions.get(code, '人工核验')} |")
    lines.extend([
        "",
        "## 结论",
        "",
        "当前瓶颈是决策校准而不是继续扩大列表：先完成硬门槛、真实岗位内容、团队可信度与材料匹配的复核，再按暴露出的缺口做行业/翻页搜索。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("job-data/boss_job_research.sqlite3"))
    parser.add_argument("--write-issues", action="store_true", help="刷新 derived 审计标记")
    parser.add_argument("--report", type=Path, help="输出 Markdown 校对报告")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        issue_counts: Counter[str] = Counter()
        if args.write_issues:
            issue_counts = refresh_derived_issues(conn)
            conn.commit()
        else:
            for code, count in conn.execute(
                "SELECT issue_code, COUNT(*) FROM job_audit_issues WHERE resolved_at IS NULL GROUP BY issue_code"
            ):
                issue_counts[code] = count
        report = build_report(conn, issue_counts)
    finally:
        conn.close()

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
