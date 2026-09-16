#!/usr/bin/env python3
import argparse
import datetime
import re
import subprocess
import sys
import time
import signal
import zlib
from typing import Dict, Tuple, List, Optional

from app_folder import fold_app_name
from db import (
    get_db_path,
    init_db,
    load_process_states,
    update_process_states,
    prune_stale_process_states,
    record_usage_deltas,
    record_power_events,
    record_gap
)

from config import load_config

RUNNING = True

def signal_handler(signum, frame):
    global RUNNING
    print(f"\nReceived signal {signum}, shutting down collector gracefully...")
    RUNNING = False

def _stable_proc_id(proc_id_str: str) -> int:
    return zlib.crc32(proc_id_str.encode("utf-8"))

def parse_nettop_proc_id(proc_id_str: str) -> Tuple[int, str]:
    """
    Splits a nettop process identifier such as "Google Chrome H.1535" or
    "configd.557" into a (pid, raw_name) tuple. When no numeric PID suffix is
    present, a deterministic hash of the full identifier is used so the key stays
    stable across collector restarts.
    """
    if "." in proc_id_str:
        raw_name, pid_str = proc_id_str.rsplit(".", 1)
        if pid_str.isdigit():
            return int(pid_str), raw_name
    return _stable_proc_id(proc_id_str), proc_id_str

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
        pid, raw_name = parse_nettop_proc_id(proc_id_str)
        samples.append((pid, raw_name, bytes_in, bytes_out))

    return samples

def poll_once(db_path: str, process_states: Dict[Tuple[int, str], Tuple[int, int, float]], config_path: Optional[str] = None, gap_classification: Optional[str] = None) -> Tuple[int, int]:
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
    record_usage_deltas(db_path, timestamp_5m, day_str, db_deltas, is_poll=True, gap_classification=gap_classification)

    return (total_delta_in, total_delta_out)

# NOTE: (?!\s*Requests\b) excludes "Wake Requests" log lines
POWER_EVENT_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4})\s+(Sleep|Wake|DarkWake)\b(?!\s*Requests\b)(.*)")

def fetch_pmset_events(t0: float, t1: float, margin: float = 60.0, tail_lines: Optional[int] = None) -> List[Tuple[float, str, str]]:
    """
    Runs `pmset -g log`, parses Sleep/Wake/DarkWake lines, and returns the ones whose
    timestamp falls within [t0 - margin, t1 + margin] as (epoch, event_type, reason) tuples.

    `pmset -g log` has no native flag to limit its output, and the log can grow very
    large over weeks of uptime. When `tail_lines` is set, the command is piped through
    `tail -n <tail_lines>` first to keep the call cheap; this is intended for callers
    that run frequently (e.g. once per poll) and only care about recent history. Callers
    that run rarely (e.g. only when an actual gap is detected) should leave it unset to
    scan the full log.
    """
    events = []
    try:
        if tail_lines:
            res = subprocess.run(
                f"pmset -g log | tail -n {tail_lines}",
                shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10
            )
        else:
            res = subprocess.run(["pmset", "-g", "log"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True, timeout=10)
        for line in res.stdout.splitlines():
            m = POWER_EVENT_PATTERN.match(line.strip())
            if m:
                ts_str, event_type, reason = m.groups()
                try:
                    # Parse timestamp with timezone
                    dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S %z")
                    epoch = dt.timestamp()
                    if (t0 - margin) <= epoch <= (t1 + margin):
                        events.append((epoch, event_type, reason.strip()))
                except Exception:
                    continue
    except Exception as e:
        print(f"Error running pmset or parsing: {e}", file=sys.stderr)
    return events

def detect_and_classify_gap(db_path: str, t0: float, t1: float) -> str:
    events = fetch_pmset_events(t0, t1)

    if events:
        record_power_events(db_path, events)

    if not events:
        classification = 'unknown_gap'
    else:
        events = sorted(events, key=lambda x: x[0])
        last_sleep_idx = -1
        for i, (_, et, _) in enumerate(events):
            if et == 'Sleep':
                last_sleep_idx = i

        if last_sleep_idx != -1:
            subsequent = events[last_sleep_idx+1:]
            has_wake = any(et == 'Wake' for (_, et, _) in subsequent)
            has_darkwake = any(et == 'DarkWake' for (_, et, _) in subsequent)
            if has_wake:
                classification = 'sleep_then_full_wake'
            elif has_darkwake:
                classification = 'dark_wake_only'
            else:
                classification = 'dark_wake_only'
        else:
            has_wake = any(et == 'Wake' for (_, et, _) in events)
            has_darkwake = any(et == 'DarkWake' for (_, et, _) in events)
            if has_wake:
                classification = 'sleep_then_full_wake'
            elif has_darkwake:
                classification = 'dark_wake_only'
            else:
                classification = 'unknown_gap'

    record_gap(db_path, t0, t1, classification)
    return classification

def check_dark_wake_still_active(db_path: str, since_epoch: float, now_epoch: float) -> str:
    """
    Called when the current poll did NOT exceed the gap threshold, but the *previous*
    poll was classified 'dark_wake_only'.
    """
    events = fetch_pmset_events(since_epoch, now_epoch, tail_lines=200)

    if events:
        record_power_events(db_path, events)
        has_wake = any(et == 'Wake' for (_, et, _) in events)
        classification = 'sleep_then_full_wake' if has_wake else 'dark_wake_only'
    else:
        # No new events since the last check: nothing contradicts the ongoing dark-wake
        # state, so keep propagating it.
        classification = 'dark_wake_only'

    return classification

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
    last_poll_epoch = None
    last_classification = None

    if args.once:
        delta_in, delta_out = poll_once(db_path, process_states, config_path=args.config)
        print(f"Single poll complete. Delta in: {delta_in} bytes, Delta out: {delta_out} bytes.")
        return

    while RUNNING:
        start_time = time.time()
        gap_class = None

        if last_poll_epoch is not None:
            time_diff = start_time - last_poll_epoch
            gap_threshold = args.interval * cfg.get("gap_threshold_multiplier", 3)
            if time_diff > gap_threshold:
                gap_class = detect_and_classify_gap(db_path, last_poll_epoch, start_time)
            elif last_classification == 'dark_wake_only':
                # This poll arrived on time, but the previous one was still dark-waking.
                gap_class = check_dark_wake_still_active(db_path, last_poll_epoch, start_time)

        last_poll_epoch = start_time
        last_classification = gap_class

        try:
            delta_in, delta_out = poll_once(db_path, process_states, config_path=args.config, gap_classification=gap_class)
            if delta_in > 0 or delta_out > 0:
                print(f"[{datetime.datetime.now().isoformat()}] Sample recorded: +{delta_in} B in, +{delta_out} B out")
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
