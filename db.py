import os
import sqlite3
import time
from typing import Dict, List, Tuple, Optional

DEFAULT_DB_DIR = os.path.expanduser("~/Library/Application Support/NetTally")
DEFAULT_DB_PATH = os.path.join(DEFAULT_DB_DIR, "usage.db")

def get_db_path(custom_path: Optional[str] = None) -> str:
    if custom_path:
        path = os.path.abspath(os.path.expanduser(custom_path))
    else:
        path = DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db(db_path: str) -> None:
    conn = get_connection(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_daily (
                day          TEXT NOT NULL,
                app_name     TEXT NOT NULL,
                bytes_in     INTEGER NOT NULL DEFAULT 0,
                bytes_out    INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, app_name)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS process_state (
                pid             INTEGER NOT NULL,
                process_name    TEXT NOT NULL,
                last_bytes_in   INTEGER NOT NULL,
                last_bytes_out  INTEGER NOT NULL,
                last_seen_epoch REAL NOT NULL,
                PRIMARY KEY (pid, process_name)
            );
        """)
    conn.close()

def load_process_states(db_path: str) -> Dict[Tuple[int, str], Tuple[int, int, float]]:
    conn = get_connection(db_path)
    states = {}
    try:
        cursor = conn.execute("SELECT pid, process_name, last_bytes_in, last_bytes_out, last_seen_epoch FROM process_state")
        for row in cursor.fetchall():
            states[(row["pid"], row["process_name"])] = (
                row["last_bytes_in"],
                row["last_bytes_out"],
                row["last_seen_epoch"]
            )
    finally:
        conn.close()
    return states

def update_process_states(db_path: str, states: Dict[Tuple[int, str], Tuple[int, int, float]]) -> None:
    conn = get_connection(db_path)
    with conn:
        conn.executemany("""
            INSERT INTO process_state (pid, process_name, last_bytes_in, last_bytes_out, last_seen_epoch)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pid, process_name) DO UPDATE SET
                last_bytes_in = excluded.last_bytes_in,
                last_bytes_out = excluded.last_bytes_out,
                last_seen_epoch = excluded.last_seen_epoch
        """, [
            (pid, proc_name, bytes_in, bytes_out, last_seen)
            for (pid, proc_name), (bytes_in, bytes_out, last_seen) in states.items()
        ])
    conn.close()

def prune_stale_process_states(db_path: str, max_age_seconds: float = 86400.0) -> None:
    conn = get_connection(db_path)
    cutoff = time.time() - max_age_seconds
    with conn:
        conn.execute("DELETE FROM process_state WHERE last_seen_epoch < ?", (cutoff,))
    conn.close()

def record_usage_deltas(db_path: str, day_str: str, deltas: Dict[str, Tuple[int, int]], is_poll: bool = True) -> None:
    if not deltas:
        return
    conn = get_connection(db_path)
    with conn:
        conn.executemany("""
            INSERT INTO usage_daily (day, app_name, bytes_in, bytes_out, sample_count)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(day, app_name) DO UPDATE SET
                bytes_in = bytes_in + excluded.bytes_in,
                bytes_out = bytes_out + excluded.bytes_out,
                sample_count = sample_count + excluded.sample_count
        """, [
            (day_str, app_name, din, dout, 1 if is_poll else 0)
            for app_name, (din, dout) in deltas.items()
        ])
    conn.close()

def query_usage_totals(db_path: str, days: Optional[int] = None, app_filter: Optional[str] = None) -> List[Dict]:
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT app_name,
                   SUM(bytes_in) as total_bytes_in,
                   SUM(bytes_out) as total_bytes_out,
                   SUM(bytes_in + bytes_out) as total_bytes,
                   SUM(sample_count) as total_samples,
                   MIN(day) as earliest_day,
                   MAX(day) as latest_day
            FROM usage_daily
        """
        where_clauses = []
        params = []

        if days is not None:
            where_clauses.append("day >= date('now', 'localtime', '-' || ? || ' days')")
            params.append(days)

        if app_filter:
            where_clauses.append("app_name LIKE ?")
            params.append(f"%{app_filter}%")

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        sql += " GROUP BY app_name ORDER BY total_bytes DESC"

        cursor = conn.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
        return results
    finally:
        conn.close()

def query_usage_by_day(db_path: str, days: Optional[int] = None, app_filter: Optional[str] = None) -> List[Dict]:
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT day, app_name, bytes_in, bytes_out, (bytes_in + bytes_out) as total_bytes, sample_count
            FROM usage_daily
        """
        where_clauses = []
        params = []

        if days is not None:
            where_clauses.append("day >= date('now', 'localtime', '-' || ? || ' days')")
            params.append(days)

        if app_filter:
            where_clauses.append("app_name LIKE ?")
            params.append(f"%{app_filter}%")

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        sql += " ORDER BY day DESC, total_bytes DESC"

        cursor = conn.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
        return results
    finally:
        conn.close()
