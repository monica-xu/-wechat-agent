"""Database foundation: schema, migrations, connection management, backup."""

import os
import sqlite3
import time
import json
import logging
from contextlib import contextmanager
from datetime import datetime

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "wechat.db")


def init_db():
    """Create data dir, initialize schema, run migrations."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "previews"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "backups"), exist_ok=True)

    with _db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _create_tables(conn)
        _migrate(conn)
        _seed_config(conn)
    logger.info(f"Database initialized at {DB_PATH}")


def _create_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            content_markdown TEXT NOT NULL DEFAULT '',
            summary TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            topic TEXT DEFAULT '',
            angle TEXT DEFAULT '',
            status TEXT DEFAULT 'draft',
            human_mode TEXT DEFAULT 'auto',
            critic_overall_score REAL DEFAULT 0,
            critic_dimension_scores TEXT DEFAULT '{}',
            critic_adversarial_score REAL DEFAULT 0,
            integrity_style_drift REAL DEFAULT 0,
            integrity_contradiction INTEGER DEFAULT 0,
            integrity_template_risk TEXT DEFAULT 'low',
            embedding TEXT DEFAULT '[]',
            embedding_model TEXT DEFAULT '',
            content_hash TEXT DEFAULT '',
            wechat_draft_id TEXT DEFAULT '',
            wechat_publish_id TEXT DEFAULT '',
            published_at TEXT DEFAULT '',
            status_flag TEXT DEFAULT 'active',
            deleted_at TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_articles_session ON articles(session_id, status);
        CREATE INDEX IF NOT EXISTS idx_articles_topic ON articles(topic, published_at);
        CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);

        CREATE TABLE IF NOT EXISTS content_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            topic_category TEXT DEFAULT '',
            times_shown INTEGER DEFAULT 0,
            times_published INTEGER DEFAULT 0,
            avg_read_count REAL DEFAULT 0,
            avg_share_rate REAL DEFAULT 0,
            ucb_score REAL DEFAULT 0,
            last_published_at TEXT DEFAULT '',
            embedding TEXT DEFAULT '[]',
            embedding_model TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            key_points TEXT DEFAULT '[]',
            writing_style TEXT DEFAULT '',
            world_state_snapshot TEXT DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(session_id, topic)
        );

        CREATE INDEX IF NOT EXISTS idx_content_memory_topic ON content_memory(session_id, topic);
        CREATE INDEX IF NOT EXISTS idx_content_memory_ucb ON content_memory(ucb_score DESC);

        CREATE TABLE IF NOT EXISTS topic_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            reason TEXT DEFAULT '',
            source TEXT DEFAULT 'auto',
            category TEXT DEFAULT '',
            trend_score REAL DEFAULT 0,
            bandit_score REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            used_at TEXT DEFAULT '',
            UNIQUE(session_id, topic)
        );

        CREATE INDEX IF NOT EXISTS idx_topic_pool_active ON topic_pool(session_id, is_active);

        CREATE TABLE IF NOT EXISTS publish_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            macro_stages TEXT DEFAULT '[]',
            generate_trace TEXT DEFAULT '{}',
            review_trace TEXT DEFAULT '{}',
            total_duration_ms INTEGER DEFAULT 0,
            llm_call_count INTEGER DEFAULT 0,
            rewrite_count INTEGER DEFAULT 0,
            critic_overall_score REAL DEFAULT 0,
            final_stage TEXT DEFAULT '',
            failure_reason TEXT DEFAULT '',
            publish_mode TEXT DEFAULT '',
            wechat_draft_id TEXT DEFAULT '',
            wechat_publish_id TEXT DEFAULT '',
            publish_status TEXT DEFAULT '',
            publish_error TEXT DEFAULT '',
            human_mode TEXT DEFAULT 'auto',
            human_approved_at TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_publish_log_session ON publish_log(session_id, created_at);

        CREATE TABLE IF NOT EXISTS article_metrics (
            article_id TEXT PRIMARY KEY,
            read_count INTEGER DEFAULT 0,
            share_count INTEGER DEFAULT 0,
            like_count INTEGER DEFAULT 0,
            completion_rate REAL DEFAULT 0,
            title_click_rate REAL DEFAULT 0,
            feedback_applied INTEGER DEFAULT 0,
            fetched_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS runtime_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS job_locks (
            job_key TEXT PRIMARY KEY,
            started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            finished_at TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS pipeline_runs (
            pipeline_id TEXT PRIMARY KEY,
            article_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            status TEXT DEFAULT 'started',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );

        CREATE TABLE IF NOT EXISTS article_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT '',
            note TEXT DEFAULT '',
            article_type TEXT DEFAULT '',
            edit_type TEXT DEFAULT '',
            core_opinion TEXT DEFAULT '',
            core_conflict TEXT DEFAULT '',
            metaphors_used TEXT DEFAULT '',
            edited_markdown TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_article ON article_feedback(article_id, created_at);
    """)


def _seed_config(conn):
    """Ensure essential config rows exist."""
    defaults = {
        "human_mode": '"dry-run"',
        "publish_schedule_cron": '"0 9 * * 1-5"',
        "daily_publish_limit": "1",
        "weekly_publish_limit": "5",
        "llm_provider": '"deepseek"',
        "last_pipeline_run": '""',
        "scoring_weight_time": "0.30",
        "scoring_weight_content": "0.40",
        "scoring_weight_risk": "0.30",
        "publish_threshold": "0.70",
        "topic_focus": '""',
        "topic_source": '""',
        "reading_list": '""',
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO runtime_config (key, value) VALUES (?, ?)",
            (key, value),
        )


def _migrate(conn):
    """Apply schema migrations incrementally."""
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing = {row[0] for row in cursor.fetchall()}
    migrations = [
        ("articles", "ALTER TABLE articles ADD COLUMN narrative_shape TEXT DEFAULT ''"),
        ("articles", "ALTER TABLE articles ADD COLUMN opening_type TEXT DEFAULT ''"),
        ("publish_log", "ALTER TABLE publish_log ADD COLUMN narrative_shape TEXT DEFAULT ''"),
        ("article_feedback", "ALTER TABLE article_feedback ADD COLUMN edited_markdown TEXT DEFAULT ''"),
        ("article_feedback", "ALTER TABLE article_feedback ADD COLUMN article_type TEXT DEFAULT ''"),
        ("article_feedback", "ALTER TABLE article_feedback ADD COLUMN edit_type TEXT DEFAULT ''"),
        ("article_feedback", "ALTER TABLE article_feedback ADD COLUMN core_opinion TEXT DEFAULT ''"),
        ("article_feedback", "ALTER TABLE article_feedback ADD COLUMN core_conflict TEXT DEFAULT ''"),
        ("article_metrics", "ALTER TABLE article_metrics ADD COLUMN comment_count INTEGER DEFAULT 0"),
        ("article_feedback", "ALTER TABLE article_feedback ADD COLUMN metaphors_used TEXT DEFAULT ''"),
    ]
    for table, sql in migrations:
        if table in existing:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass


@contextmanager
def _db():
    """Context manager for SQLite connection with WAL mode."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def daily_backup():
    """Backup DB to JSON files, clean backups older than 7 days."""
    backup_dir = os.path.join(DATA_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")

    with _db() as conn:
        tables = ["articles", "content_memory", "topic_pool", "publish_log", "article_metrics"]
        backup = {}
        for table in tables:
            try:
                cursor = conn.execute(f"SELECT * FROM {table} WHERE 1=1")
                rows = [dict(row) for row in cursor.fetchall()]
                backup[table] = rows
            except sqlite3.OperationalError:
                backup[table] = []

    backup_path = os.path.join(backup_dir, f"{date_str}.json")
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2, default=str)

    # Clean backups older than 7 days
    cutoff = time.time() - 7 * 86400
    for fname in os.listdir(backup_dir):
        fpath = os.path.join(backup_dir, fname)
        if fname.endswith(".json") and os.path.getmtime(fpath) < cutoff:
            os.remove(fpath)

    logger.info(f"Backup saved to {backup_path}")
