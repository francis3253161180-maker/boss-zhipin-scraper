"""Import BOSS search/detail JSON into the local research SQLite database.

This is intentionally an ingestion-only tool: it does not contact BOSS and
does not send messages or applications.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS search_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    searched_at TEXT NOT NULL,
    keyword TEXT,
    city TEXT,
    filters_json TEXT,
    pages INTEGER,
    result_count INTEGER,
    status TEXT NOT NULL DEFAULT 'completed',
    raw_file TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_link TEXT,
    company TEXT,
    title TEXT,
    city TEXT,
    district TEXT,
    salary_text TEXT,
    company_scale TEXT,
    company_stage TEXT,
    industry TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    current_status TEXT NOT NULL DEFAULT 'discovered',
    latest_snapshot_json TEXT
);
CREATE TABLE IF NOT EXISTS job_occurrences (
    run_id INTEGER NOT NULL,
    job_id TEXT NOT NULL,
    rank_in_result INTEGER,
    seen_at TEXT NOT NULL,
    card_snapshot_json TEXT NOT NULL,
    PRIMARY KEY (run_id, job_id),
    FOREIGN KEY (run_id) REFERENCES search_runs(run_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE TABLE IF NOT EXISTS job_details (
    detail_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    jd_text TEXT,
    tags_json TEXT,
    recruiter_active TEXT,
    company_link TEXT,
    raw_snapshot_json TEXT NOT NULL,
    content_hash TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE TABLE IF NOT EXISTS assessments (
    assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    assessed_at TEXT NOT NULL,
    eligibility TEXT,
    direction_score INTEGER,
    evidence_score INTEGER,
    growth_score INTEGER,
    trust_score INTEGER,
    conditions_score INTEGER,
    resource_score INTEGER,
    penalty_score INTEGER,
    total_score INTEGER,
    grade TEXT,
    resume_version TEXT,
    risks TEXT,
    next_action TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(current_status);
CREATE INDEX IF NOT EXISTS idx_details_job ON job_details(job_id);
CREATE TABLE IF NOT EXISTS job_facts (
    job_id TEXT PRIMARY KEY,
    salary_min REAL,
    salary_max REAL,
    salary_unit TEXT,
    work_days_per_week INTEGER,
    internship_months INTEGER,
    degree_requirement TEXT,
    experience_requirement TEXT,
    arrival_time_text TEXT,
    long_term_flag INTEGER,
    benefits_json TEXT,
    skill_tags_json TEXT,
    job_labels_text TEXT,
    keyword_hits_json TEXT,
    resource_tags_json TEXT,
    conversion_flag INTEGER,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_facts_salary ON job_facts(salary_min, salary_max);
CREATE INDEX IF NOT EXISTS idx_facts_conditions ON job_facts(work_days_per_week, internship_months, long_term_flag);
CREATE INDEX IF NOT EXISTS idx_facts_degree ON job_facts(degree_requirement);
CREATE TABLE IF NOT EXISTS job_tags (
    job_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    tag_type TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'derived',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, tag, tag_type),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_job_tags_tag ON job_tags(tag);
CREATE INDEX IF NOT EXISTS idx_job_tags_type_tag ON job_tags(tag_type, tag);
CREATE TABLE IF NOT EXISTS job_audit_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    issue_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    note TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'derived',
    observed_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_audit_issues_job ON job_audit_issues(job_id, resolved_at);
CREATE INDEX IF NOT EXISTS idx_audit_issues_code ON job_audit_issues(issue_code, severity);
CREATE TABLE IF NOT EXISTS job_decisions (
    job_id TEXT PRIMARY KEY,
    reviewed_at TEXT NOT NULL,
    hard_gate TEXT NOT NULL DEFAULT '待核验',
    data_quality_status TEXT NOT NULL DEFAULT '待校对',
    value_score INTEGER,
    fit_score INTEGER,
    probability_score INTEGER,
    priority_score INTEGER,
    priority_tier TEXT,
    application_track TEXT,
    recommendation TEXT,
    verification_items TEXT,
    decision_reason TEXT,
    source_assessment_id INTEGER,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id),
    FOREIGN KEY (source_assessment_id) REFERENCES assessments(assessment_id)
);
CREATE INDEX IF NOT EXISTS idx_decisions_tier ON job_decisions(priority_tier, priority_score);
CREATE INDEX IF NOT EXISTS idx_decisions_track ON job_decisions(application_track);
CREATE TABLE IF NOT EXISTS job_source_checks (
    source_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    match_level TEXT NOT NULL,
    result TEXT NOT NULL,
    evidence TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
);
CREATE INDEX IF NOT EXISTS idx_source_checks_job ON job_source_checks(job_id, checked_at);
CREATE INDEX IF NOT EXISTS idx_source_checks_match ON job_source_checks(match_level);
DROP VIEW IF EXISTS job_catalog;
DROP VIEW IF EXISTS current_assessments;
CREATE VIEW current_assessments AS
SELECT assessment_id, job_id, assessed_at, eligibility, direction_score,
       evidence_score, growth_score, trust_score, conditions_score,
       resource_score, penalty_score, total_score, grade, resume_version,
       risks, next_action
FROM (
    SELECT a.*,
           ROW_NUMBER() OVER (
               PARTITION BY a.job_id
               ORDER BY a.assessed_at DESC, a.assessment_id DESC
           ) AS row_number
    FROM assessments a
)
WHERE row_number = 1;
CREATE VIEW job_catalog AS
WITH latest_details AS (
    SELECT d.*
    FROM job_details d
    JOIN (SELECT job_id, MAX(detail_id) AS detail_id FROM job_details GROUP BY job_id) x
      ON x.detail_id = d.detail_id
), search_summary AS (
    SELECT jo.job_id,
           COUNT(*) AS search_hit_count,
           MIN(sr.searched_at) AS first_searched_at,
           MAX(sr.searched_at) AS last_searched_at,
           GROUP_CONCAT(DISTINCT COALESCE(sr.city, '') || ' / ' || COALESCE(sr.keyword, '')) AS search_conditions
    FROM job_occurrences jo
    JOIN search_runs sr ON sr.run_id = jo.run_id
    GROUP BY jo.job_id
), audit_summary AS (
    SELECT job_id,
           COUNT(*) AS open_issue_count,
           MAX(CASE WHEN severity = 'warning' THEN 1 ELSE 0 END) AS has_warning
    FROM job_audit_issues
    WHERE resolved_at IS NULL
    GROUP BY job_id
)
SELECT
    j.job_id, j.company, j.title, j.city, j.district,
    j.city || CASE WHEN j.district IS NOT NULL THEN '·' || j.district ELSE '' END AS location_text,
    j.salary_text,
    j.company_scale, j.company_stage, j.industry, j.job_link,
    j.first_seen_at, j.last_seen_at, j.current_status,
    d.fetched_at AS detail_fetched_at, d.jd_text, d.tags_json,
    d.recruiter_active, d.company_link,
    f.salary_min, f.salary_max, f.salary_unit,
    f.work_days_per_week, f.internship_months,
    f.degree_requirement, f.experience_requirement,
    f.arrival_time_text, f.long_term_flag,
    f.benefits_json, f.skill_tags_json, f.job_labels_text,
    f.keyword_hits_json, f.resource_tags_json, f.conversion_flag,
    a.assessed_at, a.eligibility, a.total_score, a.grade,
    a.resume_version, a.risks, a.next_action,
    r.hard_gate, r.data_quality_status,
    r.value_score, r.fit_score, r.probability_score, r.priority_score,
    r.priority_tier, r.application_track, r.recommendation,
    r.verification_items, r.decision_reason,
    q.open_issue_count, q.has_warning,
    x.source_check_count, x.best_match_level, x.latest_source_checked_at,
    s.search_hit_count, s.first_searched_at, s.last_searched_at,
    s.search_conditions
FROM jobs j
LEFT JOIN latest_details d ON d.job_id = j.job_id
LEFT JOIN job_facts f ON f.job_id = j.job_id
LEFT JOIN current_assessments a ON a.job_id = j.job_id
LEFT JOIN job_decisions r ON r.job_id = j.job_id
LEFT JOIN audit_summary q ON q.job_id = j.job_id
LEFT JOIN (
    SELECT job_id,
           COUNT(*) AS source_check_count,
           MAX(CASE match_level
               WHEN '精确同岗' THEN 3
               WHEN '官方渠道/方向一致' THEN 2
               WHEN '官方渠道存在' THEN 1
               ELSE 0 END) AS best_match_level,
           MAX(checked_at) AS latest_source_checked_at
    FROM job_source_checks
    GROUP BY job_id
) x ON x.job_id = j.job_id
LEFT JOIN search_summary s ON s.job_id = j.job_id;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def split_values(value: Any) -> list[str]:
    """Normalize BOSS's pipe-delimited or list-valued fields."""
    if value is None:
        return []
    if isinstance(value, list):
        values = value
    else:
        values = re.split(r"\s*[|｜]\s*", str(value))
    return [str(item).strip() for item in values if str(item).strip()]


def parse_salary(value: Any) -> tuple[float | None, float | None, str | None]:
    raw = str(value or "")
    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:[-~至]\s*(\d+(?:\.\d+)?))?\s*(元/天|元/月|K/月|K·月|千/月)",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return None, None, "面议" if "面议" in raw else None
    minimum = float(match.group(1))
    maximum = float(match.group(2) or match.group(1))
    unit = match.group(3)
    if unit and (unit.upper().startswith("K") or "千" in unit):
        minimum *= 1000
        maximum *= 1000
        unit = "元/月"
    return minimum, maximum, unit


def first_int(pattern: str, raw: str) -> int | None:
    match = re.search(pattern, raw, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


FACT_KEYWORDS = (
    "Agent", "RAG", "LangGraph", "LangChain", "MCP", "Skill", "FAISS",
    "Milvus", "Qdrant", "pgvector", "向量检索", "混合检索", "重排",
    "后训练", "量化", "PEFT", "LoRA", "评测", "FastAPI", "Python",
    "PyTorch", "Transformers", "Go", "Java", "React", "TypeScript",
)
RESOURCE_KEYWORDS = ("GPU", "CUDA", "显卡", "算力", "模型训练", "评测平台", "云服务器", "推理服务")


def extract_facts(item: dict[str, Any], jd_text: str | None = None) -> dict[str, Any]:
    """Extract searchable facts without inventing values absent from BOSS data."""
    jd = jd_text or ""
    labels = split_values(item.get("job_labels") or item.get("job_labels_text"))
    detail_labels = split_values(item.get("skill_tags"))
    combined = " | ".join(
        part for part in (
            item.get("title"), item.get("tags"), item.get("tags_list"),
            item.get("skills"), item.get("welfare"), *labels, *detail_labels, jd,
        ) if part
    )
    salary_min, salary_max, salary_unit = parse_salary(item.get("salary"))
    degree_terms = [term for term in ("博士", "硕士", "本科", "大专") if term in combined]
    experience_terms = [
        term for term in ("在校生", "应届生", "经验不限", "1年以内", "1-3年", "3-5年")
        if term in combined
    ]
    arrival_lines = [
        line.strip() for line in jd.splitlines()
        if any(key in line for key in ("到岗", "入职", "可长期", "尽快"))
    ]
    benefits = split_values(item.get("welfare"))
    skill_tags = split_values(item.get("skills"))
    keyword_hits = [keyword for keyword in FACT_KEYWORDS if keyword.lower() in combined.lower()]
    resource_hits = [keyword for keyword in RESOURCE_KEYWORDS if keyword.lower() in combined.lower()]
    work_days = first_int(r"(\d+)\s*天\s*/\s*周", combined)
    # Require the full ``个月`` form so a JD date such as ``10月1日`` is not
    # mistaken for a ten-month internship.
    internship_months = first_int(r"(\d+)\s*个月", combined)
    if not benefits and detail_labels:
        benefits = [
            value for value in detail_labels
            if not re.fullmatch(r"\d+\s*天\s*/\s*周", value)
            and not re.fullmatch(r"\d+\s*个月", value)
            and value not in ("本科", "硕士", "博士", "大专")
        ]
    return {
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_unit": salary_unit,
        "work_days_per_week": work_days,
        "internship_months": internship_months,
        "degree_requirement": " / ".join(degree_terms) or None,
        "experience_requirement": " / ".join(experience_terms) or None,
        "arrival_time_text": "；".join(dict.fromkeys(arrival_lines)) or None,
        "long_term_flag": 1 if "长期" in combined else 0,
        "benefits_json": json_text(benefits) if benefits else None,
        "skill_tags_json": json_text(skill_tags) if skill_tags else None,
        "job_labels_text": " | ".join(labels) or None,
        "keyword_hits_json": json_text(keyword_hits) if keyword_hits else None,
        "resource_tags_json": json_text(resource_hits) if resource_hits else None,
        "conversion_flag": 1 if "转正" in combined else 0,
    }


def upsert_facts(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    job_id: str,
    jd_text: str | None = None,
    updated_at: str | None = None,
) -> None:
    facts = extract_facts(item, jd_text)
    updated_at = updated_at or now_iso()
    conn.execute(
        """
        INSERT INTO job_facts (
            job_id, salary_min, salary_max, salary_unit, work_days_per_week,
            internship_months, degree_requirement, experience_requirement,
            arrival_time_text, long_term_flag, benefits_json, skill_tags_json,
            job_labels_text, keyword_hits_json, resource_tags_json,
            conversion_flag, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            salary_min=COALESCE(excluded.salary_min, job_facts.salary_min),
            salary_max=COALESCE(excluded.salary_max, job_facts.salary_max),
            salary_unit=COALESCE(excluded.salary_unit, job_facts.salary_unit),
            work_days_per_week=COALESCE(excluded.work_days_per_week, job_facts.work_days_per_week),
            internship_months=COALESCE(excluded.internship_months, job_facts.internship_months),
            degree_requirement=COALESCE(excluded.degree_requirement, job_facts.degree_requirement),
            experience_requirement=COALESCE(excluded.experience_requirement, job_facts.experience_requirement),
            arrival_time_text=COALESCE(excluded.arrival_time_text, job_facts.arrival_time_text),
            long_term_flag=CASE WHEN excluded.long_term_flag=1 THEN 1 ELSE job_facts.long_term_flag END,
            benefits_json=COALESCE(excluded.benefits_json, job_facts.benefits_json),
            skill_tags_json=COALESCE(excluded.skill_tags_json, job_facts.skill_tags_json),
            job_labels_text=COALESCE(excluded.job_labels_text, job_facts.job_labels_text),
            keyword_hits_json=COALESCE(excluded.keyword_hits_json, job_facts.keyword_hits_json),
            resource_tags_json=COALESCE(excluded.resource_tags_json, job_facts.resource_tags_json),
            conversion_flag=CASE WHEN excluded.conversion_flag=1 THEN 1 ELSE job_facts.conversion_flag END,
            updated_at=excluded.updated_at
        """,
        (
            job_id,
            facts["salary_min"], facts["salary_max"], facts["salary_unit"],
            facts["work_days_per_week"], facts["internship_months"],
            facts["degree_requirement"], facts["experience_requirement"],
            facts["arrival_time_text"], facts["long_term_flag"],
            facts["benefits_json"], facts["skill_tags_json"],
            facts["job_labels_text"], facts["keyword_hits_json"],
            facts["resource_tags_json"], facts["conversion_flag"], updated_at,
        ),
    )
    tag_groups = {
        "skill": split_values(item.get("skills")),
        "benefit": split_values(item.get("welfare")),
        "label": split_values(item.get("job_labels") or item.get("job_labels_text")),
        "keyword": json.loads(facts["keyword_hits_json"]) if facts["keyword_hits_json"] else [],
        "resource": json.loads(facts["resource_tags_json"]) if facts["resource_tags_json"] else [],
    }
    for tag_type, tags in tag_groups.items():
        for tag in tags:
            conn.execute(
                """
                INSERT OR REPLACE INTO job_tags (job_id, tag, tag_type, source, updated_at)
                VALUES (?, ?, ?, 'derived', ?)
                """,
                (job_id, tag, tag_type, updated_at),
            )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations, then rebuild the convenience view."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(job_facts)")}
    migrations = {
        "resource_tags_json": "ALTER TABLE job_facts ADD COLUMN resource_tags_json TEXT",
        "conversion_flag": "ALTER TABLE job_facts ADD COLUMN conversion_flag INTEGER",
    }
    for column, statement in migrations.items():
        if column not in columns:
            conn.execute(statement)
    # Re-run the idempotent schema now that all view columns exist.
    conn.executescript(SCHEMA)


def company_of(item: dict[str, Any]) -> str | None:
    return text(item.get("company") or item.get("boss_name"))


def upsert_job(conn: sqlite3.Connection, item: dict[str, Any], seen_at: str) -> str:
    job_id = text(item.get("job_id"))
    if not job_id:
        raise ValueError("岗位缺少 job_id，无法安全去重")
    location = text(item.get("location")) or ""
    parts = location.split("·")
    city = parts[0] if parts else None
    district = "·".join(parts[1:]) if len(parts) > 1 else None
    company = company_of(item)
    existing_row = conn.execute(
        "SELECT latest_snapshot_json FROM jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    try:
        existing_snapshot = json.loads(existing_row[0]) if existing_row and existing_row[0] else {}
    except json.JSONDecodeError:
        existing_snapshot = {}
    snapshot = json_text({**existing_snapshot, **item})
    conn.execute(
        """
        INSERT INTO jobs (
            job_id, job_link, company, title, city, district, salary_text,
            company_scale, company_stage, industry, first_seen_at, last_seen_at,
            current_status, latest_snapshot_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)
        ON CONFLICT(job_id) DO UPDATE SET
            job_link=excluded.job_link,
            company=COALESCE(excluded.company, jobs.company),
            title=COALESCE(excluded.title, jobs.title),
            city=COALESCE(excluded.city, jobs.city),
            district=COALESCE(excluded.district, jobs.district),
            salary_text=COALESCE(excluded.salary_text, jobs.salary_text),
            company_scale=COALESCE(excluded.company_scale, jobs.company_scale),
            company_stage=COALESCE(excluded.company_stage, jobs.company_stage),
            industry=COALESCE(excluded.industry, jobs.industry),
            last_seen_at=excluded.last_seen_at,
            latest_snapshot_json=excluded.latest_snapshot_json
        """,
        (
            job_id,
            text(item.get("job_link") or item.get("link")),
            company,
            text(item.get("title")),
            city,
            district,
            text(item.get("salary")),
            text(item.get("company_scale")),
            text(item.get("company_stage")),
            text(item.get("company_industry")),
            seen_at,
            seen_at,
            snapshot,
        ),
    )
    return job_id


def import_search(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    data = load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise ValueError(f"不是搜索列表 JSON：{path}")
    seen_at = text(data.get("scraped_at")) or now_iso()
    cursor = conn.execute(
        """
        INSERT INTO search_runs (searched_at, keyword, city, filters_json, pages,
                                 result_count, raw_file)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            seen_at,
            text(data.get("keyword")),
            text(data.get("city")),
            json_text(data.get("filters") or {}),
            int(data.get("pages") or 1),
            int(data.get("total") or len(data["jobs"])),
            # List JSON is a transient ingestion artifact; search conditions
            # are already stored in structured columns and raw_file remains
            # NULL unless a future evidence-retention policy is introduced.
            None,
        ),
    )
    run_id = int(cursor.lastrowid)
    count = 0
    for rank, item in enumerate(data["jobs"], start=1):
        if not isinstance(item, dict):
            continue
        job_id = upsert_job(conn, item, seen_at)
        upsert_facts(conn, item, job_id, updated_at=seen_at)
        conn.execute(
            """
            INSERT OR IGNORE INTO job_occurrences
                (run_id, job_id, rank_in_result, seen_at, card_snapshot_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, job_id, rank, seen_at, json_text(item)),
        )
        count += 1
    return run_id, count


def import_details(conn: sqlite3.Connection, path: Path) -> int:
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"不是详情 JSON 数组：{path}")
    fetched_at = now_iso()
    count = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        job_id = upsert_job(conn, item, fetched_at)
        jd = text(item.get("jd")) or ""
        upsert_facts(conn, item, job_id, jd_text=jd, updated_at=fetched_at)
        conn.execute(
            """
            INSERT INTO job_details
                (job_id, fetched_at, jd_text, tags_json, recruiter_active,
                 company_link, raw_snapshot_json, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                fetched_at,
                jd,
                json_text(item.get("skill_tags") or []),
                text(item.get("boss_active_status")),
                text(item.get("company_link")),
                json_text(item),
                hashlib.sha256(jd.encode("utf-8")).hexdigest(),
            ),
        )
        conn.execute(
            "UPDATE jobs SET current_status='detailed', last_seen_at=? WHERE job_id=?",
            (fetched_at, job_id),
        )
        count += 1
    return count


def rebuild_facts(conn: sqlite3.Connection) -> int:
    """Backfill structured facts from all existing jobs and latest details."""
    detail_rows = conn.execute(
        """
        SELECT d.job_id, d.jd_text, d.raw_snapshot_json
        FROM job_details d
        JOIN (
            SELECT job_id, MAX(detail_id) AS detail_id
            FROM job_details GROUP BY job_id
        ) latest ON latest.detail_id = d.detail_id
        """
    ).fetchall()
    details = {}
    for job_id, jd_text, raw_snapshot_json in detail_rows:
        try:
            snapshot = json.loads(raw_snapshot_json)
        except (TypeError, json.JSONDecodeError):
            snapshot = {}
        details[job_id] = (snapshot if isinstance(snapshot, dict) else {}, jd_text or "")

    occurrence_rows = conn.execute(
        """
        SELECT o.job_id, o.card_snapshot_json
        FROM job_occurrences o
        JOIN (
            SELECT job_id, MAX(seen_at) AS seen_at
            FROM job_occurrences GROUP BY job_id
        ) latest ON latest.job_id=o.job_id AND latest.seen_at=o.seen_at
        """
    ).fetchall()
    occurrence_snapshots = {}
    for job_id, raw_snapshot in occurrence_rows:
        try:
            snapshot = json.loads(raw_snapshot)
        except json.JSONDecodeError:
            snapshot = {}
        if isinstance(snapshot, dict):
            occurrence_snapshots[job_id] = snapshot

    rows = conn.execute("SELECT job_id, latest_snapshot_json FROM jobs").fetchall()
    count = 0
    for job_id, raw_snapshot in rows:
        try:
            item = json.loads(raw_snapshot or "{}")
        except json.JSONDecodeError:
            item = {}
        if not isinstance(item, dict):
            item = {}
        detail_item, jd_text = details.get(job_id, ({}, ""))
        merged = {**occurrence_snapshots.get(job_id, {}), **item, **detail_item, "job_id": job_id}
        upsert_facts(conn, merged, job_id, jd_text=jd_text)
        count += 1
    return count


def import_assessments(conn: sqlite3.Connection, path: Path) -> int:
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"不是评分 JSON 数组：{path}")
    assessed_at = now_iso()
    count = 0
    for item in data:
        if not isinstance(item, dict) or not item.get("job_id"):
            continue
        conn.execute(
            """
            INSERT INTO assessments (
                job_id, assessed_at, eligibility, direction_score, evidence_score,
                growth_score, trust_score, conditions_score, resource_score,
                penalty_score, total_score, grade, resume_version, risks, next_action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                text(item.get("job_id")),
                assessed_at,
                text(item.get("eligibility")),
                item.get("direction_score"),
                item.get("evidence_score"),
                item.get("growth_score"),
                item.get("trust_score"),
                item.get("conditions_score"),
                item.get("resource_score"),
                item.get("penalty_score"),
                item.get("total_score"),
                text(item.get("grade")),
                text(item.get("resume_version")),
                text(item.get("risks")),
                text(item.get("next_action")),
            ),
        )
        conn.execute(
            "UPDATE jobs SET current_status='scored' WHERE job_id=?",
            (text(item.get("job_id")),),
        )
        count += 1
    return count


def import_decisions(conn: sqlite3.Connection, path: Path) -> int:
    """Upsert one current, human-reviewable decision per job without deleting history."""
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"不是投递决策 JSON 数组：{path}")
    reviewed_at = now_iso()
    count = 0
    for item in data:
        if not isinstance(item, dict) or not item.get("job_id"):
            continue
        job_id = text(item.get("job_id"))
        exists = conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not exists:
            raise ValueError(f"决策引用了未知岗位：{job_id}")
        conn.execute(
            """
            INSERT INTO job_decisions (
                job_id, reviewed_at, hard_gate, data_quality_status,
                value_score, fit_score, probability_score, priority_score,
                priority_tier, application_track, recommendation,
                verification_items, decision_reason, source_assessment_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                reviewed_at=excluded.reviewed_at,
                hard_gate=excluded.hard_gate,
                data_quality_status=excluded.data_quality_status,
                value_score=excluded.value_score,
                fit_score=excluded.fit_score,
                probability_score=excluded.probability_score,
                priority_score=excluded.priority_score,
                priority_tier=excluded.priority_tier,
                application_track=excluded.application_track,
                recommendation=excluded.recommendation,
                verification_items=excluded.verification_items,
                decision_reason=excluded.decision_reason,
                source_assessment_id=excluded.source_assessment_id
            """,
            (
                job_id,
                text(item.get("reviewed_at")) or reviewed_at,
                text(item.get("hard_gate")) or "待核验",
                text(item.get("data_quality_status")) or "待校对",
                item.get("value_score"),
                item.get("fit_score"),
                item.get("probability_score"),
                item.get("priority_score"),
                text(item.get("priority_tier")),
                text(item.get("application_track")),
                text(item.get("recommendation")),
                text(item.get("verification_items")),
                text(item.get("decision_reason")),
                item.get("source_assessment_id"),
            ),
        )
        count += 1
    return count


def import_source_checks(conn: sqlite3.Connection, path: Path) -> int:
    """Append externally checked source evidence; source checks are historical."""
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"不是来源核验 JSON 数组：{path}")
    checked_at = now_iso()
    required = ("source_name", "source_type", "source_url", "match_level", "result", "evidence")
    count = 0
    for item in data:
        if not isinstance(item, dict) or not item.get("job_id"):
            continue
        job_id = text(item.get("job_id"))
        exists = conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not exists:
            raise ValueError(f"来源核验引用了未知岗位：{job_id}")
        missing = [key for key in required if not text(item.get(key))]
        if missing:
            raise ValueError(f"来源核验缺少字段 {', '.join(missing)}：{job_id}")
        conn.execute(
            """
            INSERT INTO job_source_checks (
                job_id, checked_at, source_name, source_type, source_url,
                match_level, result, evidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                text(item.get("checked_at")) or checked_at,
                text(item.get("source_name")),
                text(item.get("source_type")),
                text(item.get("source_url")),
                text(item.get("match_level")),
                text(item.get("result")),
                text(item.get("evidence")),
            ),
        )
        count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, help="search JSON file")
    parser.add_argument("--details", type=Path, help="detail JSON array file")
    parser.add_argument("--assessments", type=Path, help="assessment JSON array file")
    parser.add_argument("--decisions", type=Path, help="current decision JSON array file")
    parser.add_argument("--source-checks", type=Path, help="source-check JSON array file")
    parser.add_argument("--init-only", action="store_true", help="only create/update the schema and views")
    parser.add_argument("--rebuild-facts", action="store_true", help="backfill structured fields from existing JSON")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("job-data/boss_job_research.sqlite3"),
    )
    args = parser.parse_args()
    if not args.jobs and not args.details and not args.assessments and not args.decisions and not args.source_checks and not args.init_only and not args.rebuild_facts:
        parser.error("至少提供 --jobs、--details、--assessments、--decisions、--source-checks、--rebuild-facts 或 --init-only")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    try:
        conn.executescript(SCHEMA)
        ensure_schema(conn)
        if args.init_only:
            conn.commit()
            print(json.dumps({"db": str(args.db), "schema": "ready"}, ensure_ascii=False))
            return 0
        rebuilt_count = rebuild_facts(conn) if args.rebuild_facts else 0
        run_id = None
        search_count = 0
        detail_count = 0
        assessment_count = 0
        decision_count = 0
        source_check_count = 0
        if args.jobs:
            run_id, search_count = import_search(conn, args.jobs)
        if args.details:
            detail_count = import_details(conn, args.details)
        if args.assessments:
            assessment_count = import_assessments(conn, args.assessments)
        if args.decisions:
            decision_count = import_decisions(conn, args.decisions)
        if args.source_checks:
            source_check_count = import_source_checks(conn, args.source_checks)
        conn.commit()
        print(
            json.dumps(
                {
                    "db": str(args.db),
                    "run_id": run_id,
                    "search_rows": search_count,
                    "detail_rows": detail_count,
                    "assessment_rows": assessment_count,
                    "decision_rows": decision_count,
                    "source_check_rows": source_check_count,
                    "rebuilt_fact_rows": rebuilt_count,
                },
                ensure_ascii=False,
            )
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
