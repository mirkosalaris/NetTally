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
            CREATE TABLE IF NOT EXISTS usage_5m (
                timestamp_5m TEXT NOT NULL,
                day          TEXT NOT NULL,
                app_name     TEXT NOT NULL,
                bytes_in     INTEGER NOT NULL DEFAULT 0,
                bytes_out    INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL DEFAULT 0,
                gap_classification TEXT,
                PRIMARY KEY (timestamp_5m, app_name)
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS power_events (
                ts_epoch    REAL NOT NULL,
                event_type  TEXT NOT NULL,
                reason_raw  TEXT,
                PRIMARY KEY (ts_epoch, event_type)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gaps (
                start_epoch     REAL NOT NULL,
                end_epoch       REAL NOT NULL,
                classification  TEXT NOT NULL,
                PRIMARY KEY (start_epoch, end_epoch)
            );
        """)
        # Schema migration to add gap_classification column if not present
        try:
            cursor = conn.execute("PRAGMA table_info(usage_5m);")
            columns = [row["name"] for row in cursor.fetchall()]
            if "gap_classification" not in columns:
                conn.execute("ALTER TABLE usage_5m ADD COLUMN gap_classification TEXT;")
        except Exception:
            pass
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

from config import load_config

def prune_stale_process_states(db_path: str, max_age_seconds: Optional[float] = None) -> None:
    if max_age_seconds is None:
        cfg = load_config()
        max_age_seconds = float(cfg["process_state_max_age_seconds"])
    conn = get_connection(db_path)
    cutoff = time.time() - max_age_seconds
    with conn:
        conn.execute("DELETE FROM process_state WHERE last_seen_epoch < ?", (cutoff,))
    conn.close()

def record_usage_deltas(db_path: str, timestamp_5m: str, day_str: str, deltas: Dict[str, Tuple[int, int]], is_poll: bool = True, gap_classification: Optional[str] = None) -> None:
    if not deltas:
        return
    conn = get_connection(db_path)
    with conn:
        conn.executemany("""
            INSERT INTO usage_5m (timestamp_5m, day, app_name, bytes_in, bytes_out, sample_count, gap_classification)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(timestamp_5m, app_name) DO UPDATE SET
                bytes_in = bytes_in + excluded.bytes_in,
                bytes_out = bytes_out + excluded.bytes_out,
                sample_count = sample_count + excluded.sample_count,
                gap_classification = excluded.gap_classification
        """, [
            (timestamp_5m, day_str, app_name, din, dout, 1 if is_poll else 0, gap_classification)
            for app_name, (din, dout) in deltas.items()
        ])
    conn.close()

def record_power_events(db_path: str, events: List[Tuple[float, str, str]]) -> None:
    if not events:
        return
    conn = get_connection(db_path)
    with conn:
        conn.executemany("""
            INSERT OR IGNORE INTO power_events (ts_epoch, event_type, reason_raw)
            VALUES (?, ?, ?)
        """, events)
    conn.close()

def record_gap(db_path: str, start_epoch: float, end_epoch: float, classification: str) -> None:
    conn = get_connection(db_path)
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO gaps (start_epoch, end_epoch, classification)
            VALUES (?, ?, ?)
        """, (start_epoch, end_epoch, classification))
    conn.close()

def query_usage_totals(db_path: str, days: Optional[int] = None, app_filter: Optional[str] = None, exclude_classifications: Optional[List[str]] = None) -> List[Dict]:
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
            FROM usage_5m
        """
        where_clauses = []
        params = []

        if days is not None:
            where_clauses.append("day >= date('now', 'localtime', '-' || ? || ' days')")
            params.append(days)

        if app_filter:
            where_clauses.append("app_name LIKE ?")
            params.append(f"%{app_filter}%")

        if exclude_classifications:
            placeholders = ",".join("?" * len(exclude_classifications))
            where_clauses.append(f"(gap_classification IS NULL OR gap_classification NOT IN ({placeholders}))")
            params.extend(exclude_classifications)

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        sql += " GROUP BY app_name ORDER BY total_bytes DESC"

        cursor = conn.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
        return results
    finally:
        conn.close()

def query_usage_by_day(db_path: str, days: Optional[int] = None, app_filter: Optional[str] = None, exclude_classifications: Optional[List[str]] = None) -> List[Dict]:
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT day, app_name,
                   SUM(bytes_in) as bytes_in,
                   SUM(bytes_out) as bytes_out,
                   SUM(bytes_in + bytes_out) as total_bytes,
                   SUM(sample_count) as sample_count,
                   MAX(gap_classification) as gap_classification
            FROM usage_5m
        """
        where_clauses = []
        params = []

        if days is not None:
            where_clauses.append("day >= date('now', 'localtime', '-' || ? || ' days')")
            params.append(days)

        if app_filter:
            where_clauses.append("app_name LIKE ?")
            params.append(f"%{app_filter}%")

        if exclude_classifications:
            placeholders = ",".join("?" * len(exclude_classifications))
            where_clauses.append(f"(gap_classification IS NULL OR gap_classification NOT IN ({placeholders}))")
            params.extend(exclude_classifications)

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        sql += " GROUP BY day, app_name ORDER BY day DESC, total_bytes DESC"

        cursor = conn.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
        return results
    finally:
        conn.close()

def query_usage_by_hour(db_path: str, days: Optional[int] = None, app_filter: Optional[str] = None, exclude_classifications: Optional[List[str]] = None) -> List[Dict]:
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT (substr(timestamp_5m, 1, 13) || ':00') as timestamp_hour,
                   app_name,
                   SUM(bytes_in) as bytes_in,
                   SUM(bytes_out) as bytes_out,
                   SUM(bytes_in + bytes_out) as total_bytes,
                   SUM(sample_count) as sample_count,
                   MAX(gap_classification) as gap_classification
            FROM usage_5m
        """
        where_clauses = []
        params = []

        if days is not None:
            where_clauses.append("day >= date('now', 'localtime', '-' || ? || ' days')")
            params.append(days)

        if app_filter:
            where_clauses.append("app_name LIKE ?")
            params.append(f"%{app_filter}%")

        if exclude_classifications:
            placeholders = ",".join("?" * len(exclude_classifications))
            where_clauses.append(f"(gap_classification IS NULL OR gap_classification NOT IN ({placeholders}))")
            params.extend(exclude_classifications)

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        sql += " GROUP BY timestamp_hour, app_name ORDER BY timestamp_hour DESC, total_bytes DESC"

        cursor = conn.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
        return results
    finally:
        conn.close()

def query_usage_by_5m(db_path: str, days: Optional[int] = None, app_filter: Optional[str] = None, exclude_classifications: Optional[List[str]] = None) -> List[Dict]:
    conn = get_connection(db_path)
    try:
        sql = """
            SELECT timestamp_5m, day, app_name,
                   SUM(bytes_in) as bytes_in,
                   SUM(bytes_out) as bytes_out,
                   SUM(bytes_in + bytes_out) as total_bytes,
                   SUM(sample_count) as sample_count,
                   MAX(gap_classification) as gap_classification
            FROM usage_5m
        """
        where_clauses = []
        params = []

        if days is not None:
            where_clauses.append("day >= date('now', 'localtime', '-' || ? || ' days')")
            params.append(days)

        if app_filter:
            where_clauses.append("app_name LIKE ?")
            params.append(f"%{app_filter}%")

        if exclude_classifications:
            placeholders = ",".join("?" * len(exclude_classifications))
            where_clauses.append(f"(gap_classification IS NULL OR gap_classification NOT IN ({placeholders}))")
            params.extend(exclude_classifications)

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        sql += " GROUP BY timestamp_5m, app_name ORDER BY timestamp_5m DESC, total_bytes DESC"

        cursor = conn.execute(sql, params)
        results = [dict(row) for row in cursor.fetchall()]
        return results
    finally:
        conn.close()
