#!/usr/bin/env python3
import argparse
import datetime
import os
import subprocess
import sys
import time
import signal
from typing import Dict, Tuple, List, Optional

from app_folder import fold_app_name
from db import (
    get_db_path,
    init_db,
    load_process_states,
    update_process_states,
    prune_stale_process_states,
    record_usage_deltas
)

from config import load_config

RUNNING = True

def signal_handler(signum, frame):
    global RUNNING
    print(f"\nReceived signal {signum}, shutting down collector gracefully...")
    RUNNING = False

def fetch_nettop_sample() -> List[Tuple[int, str, int, int]]:
    """
    Executes nettop -P -L 1 -x -J bytes_in,bytes_out and parses CSV lines.
    Returns a list of tuples: (pid, raw_process_name, bytes_in, bytes_out)
    """
    cmd = ["nettop", "-P", "-L", "1", "-x", "-J", "bytes_in,bytes_out"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=10)
    except Exception as e:
        print(f"Error running nettop: {e}", file=sys.stderr)
        return []

    lines = res.stdout.strip().splitlines()
    if not lines:
        return []

    samples = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Skip header line
        if "bytes_in" in line and "bytes_out" in line:
            continue

        # Split from right to robustly handle process names containing commas
        parts = line.rstrip(",").rsplit(",", 2)
        if len(parts) < 3:
            continue

        proc_id_str = parts[0].strip()
        try:
            bytes_in = int(parts[1].strip())
            bytes_out = int(parts[2].strip())
        except ValueError:
            continue

        # Parse PID and process name from e.g. "Google Chrome H.1535" or "configd.557"
        if "." in proc_id_str:
            raw_name, pid_str = proc_id_str.rsplit(".", 1)
            if pid_str.isdigit():
                pid = int(pid_str)
                samples.append((pid, raw_name, bytes_in, bytes_out))
            else:
                samples.append((hash(proc_id_str) & 0x7fffffff, proc_id_str, bytes_in, bytes_out))
        else:
            samples.append((hash(proc_id_str) & 0x7fffffff, proc_id_str, bytes_in, bytes_out))

    return samples

def poll_once(db_path: str, process_states: Dict[Tuple[int, str], Tuple[int, int, float]], config_path: Optional[str] = None) -> Tuple[int, int]:
    """
    Performs a single polling cycle:
    1. Fetches nettop sample
    2. Calculates byte deltas per process
    3. Folds process names to canonical app names
    4. Updates process_states dict and DB
    5. Records 5-minute interval usage deltas in DB
    Returns (total_delta_in, total_delta_out)
    """
    now_dt = datetime.datetime.now()
    minute_bucket = (now_dt.minute // 5) * 5
    timestamp_5m = now_dt.strftime(f"%Y-%m-%d %H:{minute_bucket:02d}")
    day_str = now_dt.strftime("%Y-%m-%d")
    now_epoch = time.time()

    samples = fetch_nettop_sample()

    if not samples:
        return (0, 0)

    app_deltas: Dict[str, List[int]] = {}
    updated_states: Dict[Tuple[int, str], Tuple[int, int, float]] = {}

    total_delta_in = 0
    total_delta_out = 0

    for pid, raw_name, cur_in, cur_out in samples:
        key = (pid, raw_name)

        if key in process_states:
            last_in, last_out, _ = process_states[key]
            if cur_in >= last_in and cur_out >= last_out:
                delta_in = cur_in - last_in
                delta_out = cur_out - last_out
            else:
                # Process restarted or PID reused with lower counters -> reset baseline
                delta_in = 0
                delta_out = 0
        else:
            # First time seeing this process -> baseline sample, delta = 0
            delta_in = 0
            delta_out = 0

        # Update process state memory & staging
        updated_states[key] = (cur_in, cur_out, now_epoch)
        process_states[key] = (cur_in, cur_out, now_epoch)

        if delta_in > 0 or delta_out > 0:
            app_name = fold_app_name(raw_name, config_path=config_path)
            if app_name not in app_deltas:
                app_deltas[app_name] = [0, 0]
            app_deltas[app_name][0] += delta_in
            app_deltas[app_name][1] += delta_out
            total_delta_in += delta_in
            total_delta_out += delta_out

    # Persist updated process states and 5-minute interval deltas to SQLite
    update_process_states(db_path, updated_states)
    
    db_deltas = {app: (d[0], d[1]) for app, d in app_deltas.items()}
    record_usage_deltas(db_path, timestamp_5m, day_str, db_deltas, is_poll=True)

    return (total_delta_in, total_delta_out)

def main():
    cfg = load_config()
    parser = argparse.ArgumentParser(description="NetTally: Mac Per-App Network Usage Collector")
    parser.add_argument("--db", type=str, default=None, help="Path to SQLite database file")
    parser.add_argument("--interval", type=int, default=cfg["polling_interval_seconds"], help=f"Polling interval in seconds (default: {cfg['polling_interval_seconds']})")
    parser.add_argument("--once", action="store_true", help="Run a single poll sample and exit")
    parser.add_argument("--config", type=str, default=None, help="Path to custom app_map.json")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    db_path = get_db_path(args.db)
    init_db(db_path)

    print(f"[{datetime.datetime.now().isoformat()}] NetTally Collector started.")
    print(f"Database: {db_path}")
    print(f"Interval: {args.interval}s")

    process_states = load_process_states(db_path)
    last_prune = time.time()

    if args.once:
        delta_in, delta_out = poll_once(db_path, process_states, config_path=args.config)
        print(f"Single poll complete. Delta in: {delta_in} bytes, Delta out: {delta_out} bytes.")
        return

    while RUNNING:
        start_time = time.time()
        try:
            delta_in, delta_out = poll_once(db_path, process_states, config_path=args.config)
            if delta_in > 0 or delta_out > 0:
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] Sample recorded: +{delta_in} B in, +{delta_out} B out")
        except Exception as e:
            print(f"Error during polling cycle: {e}", file=sys.stderr)

        # Prune stale process state once every prune_interval
        if time.time() - last_prune > cfg["process_state_prune_interval_seconds"]:
            try:
                prune_stale_process_states(db_path)
                last_prune = time.time()
            except Exception as e:
                print(f"Error pruning process states: {e}", file=sys.stderr)

        # Sleep for remainder of interval
        elapsed = time.time() - start_time
        sleep_time = max(1.0, args.interval - elapsed)
        
        step = 0.5
        slept = 0.0
        while slept < sleep_time and RUNNING:
            time.sleep(min(step, sleep_time - slept))
            slept += step

    print(f"[{datetime.datetime.now().isoformat()}] Collector exiting cleanly.")

if __name__ == "__main__":
    main()
