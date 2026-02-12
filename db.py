"""
Database connection management for PostgreSQL.

All other modules import from here. Uses a lazy-initialized per-process
ThreadedConnectionPool. Tables are created idempotently via init_tables().
"""

import os
import threading

import psycopg2
import psycopg2.extras
import psycopg2.pool

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None
_pool_lock = threading.Lock()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS copy_trading_configs (
    id TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    profile_name TEXT NOT NULL,
    max_amount DOUBLE PRECISION NOT NULL DEFAULT 5.0,
    extra_pct DOUBLE PRECISION NOT NULL DEFAULT 0.10,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_check_timestamp DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS monitor_configs (
    id TEXT PRIMARY KEY,
    token_id TEXT NOT NULL,
    name TEXT NOT NULL,
    side TEXT NOT NULL,
    shares DOUBLE PRECISION NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL DEFAULT '',
    take_profit_pct DOUBLE PRECISION,
    take_profit_price DOUBLE PRECISION,
    stop_loss_pct DOUBLE PRECISION,
    stop_loss_price DOUBLE PRECISION,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_state (
    daemon_name TEXT PRIMARY KEY,
    pid INTEGER,
    started_at DOUBLE PRECISION,
    last_heartbeat DOUBLE PRECISION
);

CREATE TABLE IF NOT EXISTS daemon_logs (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    timestamp DOUBLE PRECISION NOT NULL,
    time TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_daemon_logs_channel_ts ON daemon_logs (channel, timestamp);

CREATE TABLE IF NOT EXISTS detected_trades (
    id BIGSERIAL PRIMARY KEY,
    run_timestamp DOUBLE PRECISION NOT NULL,
    handle TEXT NOT NULL,
    profile_name TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL DEFAULT '',
    price DOUBLE PRECISION NOT NULL DEFAULT 0,
    usdc_size DOUBLE PRECISION NOT NULL DEFAULT 0,
    size DOUBLE PRECISION NOT NULL DEFAULT 0,
    fill_count INTEGER NOT NULL DEFAULT 1,
    timestamp DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_detected_trades_run ON detected_trades (run_timestamp);

CREATE TABLE IF NOT EXISTS executed_trades (
    id BIGSERIAL PRIMARY KEY,
    run_timestamp DOUBLE PRECISION NOT NULL,
    handle TEXT NOT NULL,
    profile_name TEXT NOT NULL DEFAULT '',
    side TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    token_id TEXT NOT NULL DEFAULT '',
    price DOUBLE PRECISION NOT NULL DEFAULT 0,
    usdc_size DOUBLE PRECISION NOT NULL DEFAULT 0,
    size DOUBLE PRECISION NOT NULL DEFAULT 0,
    order_id TEXT NOT NULL DEFAULT '',
    timestamp DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_executed_trades_run ON executed_trades (run_timestamp);

CREATE TABLE IF NOT EXISTS pnl_history (
    id BIGSERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
    portfolio_value DOUBLE PRECISION NOT NULL DEFAULT 0,
    cash DOUBLE PRECISION NOT NULL DEFAULT 0,
    total DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scan_history (
    scan_id TEXT PRIMARY KEY,
    timestamp DOUBLE PRECISION NOT NULL,
    scan_type TEXT NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}',
    retention_hours DOUBLE PRECISION NOT NULL DEFAULT 48,
    expires_at DOUBLE PRECISION NOT NULL,
    opportunities_count INTEGER NOT NULL DEFAULT 0,
    stats JSONB NOT NULL DEFAULT '{}',
    opportunities JSONB NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_scan_history_expires ON scan_history (expires_at);

CREATE TABLE IF NOT EXISTS api_cache (
    key TEXT PRIMARY KEY,
    data JSONB NOT NULL DEFAULT '{}',
    cache_type TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_cache_expires ON api_cache (expires_at);

-- Seed daemon_state rows so UPSERTs work cleanly
INSERT INTO daemon_state (daemon_name, pid) VALUES ('copy_trader', NULL)
    ON CONFLICT (daemon_name) DO NOTHING;
INSERT INTO daemon_state (daemon_name, pid) VALUES ('profit_monitor', NULL)
    ON CONFLICT (daemon_name) DO NOTHING;
"""


def _get_pool():
    """Lazy-init the connection pool (per-process)."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL environment variable is not set. "
                "Set it to a PostgreSQL connection string."
            )
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=DATABASE_URL,
        )
        return _pool


def init_tables():
    """Create all tables and indexes idempotently."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    finally:
        pool.putconn(conn)


def execute(query, params=None, fetch=False, fetchone=False):
    """
    Run a SQL query.

    - fetch=True  -> returns list[dict]
    - fetchone=True -> returns dict or None
    - otherwise    -> returns None (for INSERT/UPDATE/DELETE)
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            if fetch:
                rows = cur.fetchall()
                conn.commit()
                return [dict(r) for r in rows]
            if fetchone:
                row = cur.fetchone()
                conn.commit()
                return dict(row) if row else None
            conn.commit()
            return None
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
