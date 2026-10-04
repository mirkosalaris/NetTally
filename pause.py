#!/usr/bin/env python3
"""Pause and resume NetTally collection.

Pause state lives in a small JSON file beside config.json in Application
Support rather than as a row in usage.db, for two reasons: `nettally pause`
keeps working when the database is locked or corrupt, and uninstall.sh's
explicit file list already removes this file, so a usage.db preserved by a
plain `nettally uninstall` can never carry a stale "paused" flag into the next
install.

The collector reads this file once per polling cycle. A pause with a deadline
needs no timer process of its own: the collector's own loop notices when
`until_epoch` has passed and resumes by itself, so the timeout also survives a
reboot or a restart of the daemon.
"""

import argparse
import json
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PAUSE_STATE_PATH = os.path.expanduser(
    "~/Library/Application Support/NetTally/pause_state.json"
)

DURATION_PATTERN = re.compile(r"(\d+)([smhd]?)")
DURATION_UNIT_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """Parse a duration into whole seconds.

    Accepts a bare integer (seconds) and any sequence of number+unit pairs, so
    '90', '90s', '45m', '2h', '1h30m' and '1d' are all valid. Anything else
    raises ValueError rather than being silently reinterpreted -- '5x' must not
    quietly become 5 seconds.
    """
    normalized = text.strip().lower().replace(" ", "")
    if not normalized:
        raise ValueError("duration is empty")

    matches = DURATION_PATTERN.findall(normalized)
    # Require the pairs to cover the whole string so trailing junk is rejected.
    if not matches or "".join(f"{value}{unit}" for value, unit in matches) != normalized:
        raise ValueError(
            f"could not parse duration {text!r} "
            "(expected seconds, or a number with s/m/h/d units, e.g. 90s, 45m, 2h, 1h30m, 1d)"
        )

    total = sum(int(value) * DURATION_UNIT_SECONDS[unit] for value, unit in matches)
    if total <= 0:
        raise ValueError("duration must be greater than zero")
    return total


def format_duration(seconds: float) -> str:
    """Render a number of seconds as a compact human string ('1h 12m', '45s')."""
    remaining = int(max(0.0, round(seconds)))
    if remaining < 60:
        return f"{remaining}s"

    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{count}{label}")
    if remaining:
        parts.append(f"{remaining}s")
    return " ".join(parts)


def _resolve(state_path: Optional[str] = None) -> str:
    """Return the pause state path to use, defaulting to the installed location."""
    return state_path or DEFAULT_PAUSE_STATE_PATH


def read_pause_state(state_path: Optional[str] = None) -> Optional[dict]:
    """Load the current pause state, or None when tracking is not paused.

    A missing, unreadable or malformed file is reported as "not paused" rather
    than raising: a pause is a convenience and must never be able to wedge the
    collector. Problems are logged so a truncated or hand-edited file still
    shows up in collector.log.
    """
    path = _resolve(state_path)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable pause state file %s: %s", path, e)
        return None

    if not isinstance(raw, dict):
        logger.warning("Ignoring pause state file %s: expected a JSON object", path)
        return None

    # Inlined isinstance checks rather than a _is_number() predicate: mypy cannot
    # narrow through a plain bool helper without typing.TypeGuard, which needs 3.10.
    paused_at = raw.get("paused_at")
    until = raw.get("until_epoch")
    if isinstance(paused_at, bool) or not isinstance(paused_at, (int, float)):
        logger.warning("Ignoring pause state file %s: paused_at is not a number", path)
        return None
    if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))):
        logger.warning(
            "Ignoring pause state file %s: until_epoch is neither a number nor null", path
        )
        return None

    return {"paused_at": float(paused_at), "until_epoch": None if until is None else float(until)}


def write_pause_state(until_epoch: Optional[float], state_path: Optional[str] = None) -> dict:
    """Record a pause starting now, expiring at `until_epoch` (None = no expiry).

    Written to a temp file and renamed so the collector's read can never observe
    a half-written file, even if a pause lands mid-poll.
    """
    path = _resolve(state_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    state = {"paused_at": time.time(), "until_epoch": until_epoch}
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
        handle.write("\n")
    os.replace(tmp_path, path)
    return state


def clear_pause_state(state_path: Optional[str] = None) -> bool:
    """Remove the pause state file. Returns True if a file was there to remove."""
    try:
        os.remove(_resolve(state_path))
        return True
    except FileNotFoundError:
        return False


def pause_is_active(state: Optional[dict], now: Optional[float] = None) -> bool:
    """True when `state` describes a pause that is still in force.

    A state without `until_epoch` never expires. Otherwise the deadline is
    compared against wall-clock time, so a timeout elapses even while the
    machine -- and the collector with it -- was powered off.
    """
    if state is None:
        return False
    until = state.get("until_epoch")
    if until is None:
        return True
    if now is None:
        now = time.time()
    return bool(now < until)


def seconds_remaining(state: Optional[dict], now: Optional[float] = None) -> Optional[float]:
    """Seconds left on an active pause, or None if it is indefinite or inactive."""
    if state is None or not pause_is_active(state, now):
        return None
    until = state.get("until_epoch")
    if until is None:
        return None
    if now is None:
        now = time.time()
    return max(0.0, float(until) - now)


def describe_pause(state: Optional[dict], now: Optional[float] = None) -> str:
    """Short human description of how long an active pause has left."""
    remaining = seconds_remaining(state, now)
    if remaining is None:
        return "indefinite"
    return f"resumes in {format_duration(remaining)}"


def main() -> None:
    """CLI entry point behind `nettally pause`, `nettally resume` and the status line."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(description="NetTally: pause or resume usage collection")
    sub = parser.add_subparsers(dest="command", required=True)

    # --state lives on each subparser, not the top-level one: `nettally pause
    # --state PATH` must parse, and a top-level option would stop at the
    # subcommand name.
    def add_state_option(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--state",
            default=None,
            help="Path to the pause state file (default: installed location)",
        )

    pause_parser = sub.add_parser(
        "pause", help="Pause collection (indefinitely unless --for given)"
    )
    add_state_option(pause_parser)
    pause_parser.add_argument(
        "--for",
        dest="duration",
        default=None,
        metavar="DURATION",
        help="How long to pause, e.g. 90s, 45m, 2h, 1h30m, 1d, or a bare number of seconds",
    )

    resume_parser = sub.add_parser("resume", help="Resume collection immediately")
    add_state_option(resume_parser)

    state_parser = sub.add_parser("state", help="Print whether collection is paused")
    add_state_option(state_parser)
    state_parser.add_argument("--format", choices=("text", "json"), default="text")

    args = parser.parse_args()

    if args.command == "pause":
        seconds = None
        if args.duration is not None:
            try:
                seconds = parse_duration(args.duration)
            except ValueError as e:
                parser.error(str(e))

        previous = read_pause_state(args.state)
        if pause_is_active(previous):
            print(f"Already paused ({describe_pause(previous)}); replacing it with the new pause.")

        write_pause_state(None if seconds is None else time.time() + seconds, args.state)
        if seconds is None:
            print("Tracking paused indefinitely (until `nettally resume`).")
        else:
            deadline = time.localtime(time.time() + seconds)
            print(
                f"Tracking paused for {format_duration(seconds)} "
                f"(until {time.strftime('%Y-%m-%d %H:%M:%S', deadline)})."
            )
        print(f"Pause state file: {_resolve(args.state)}")
        print("The collector picks this up on its next polling cycle.")

    elif args.command == "resume":
        previous = read_pause_state(args.state)
        if not clear_pause_state(args.state):
            print("Tracking was not paused.")
        elif not pause_is_active(previous):
            print("Resumed tracking (the previous pause had already expired).")
        else:
            remaining = seconds_remaining(previous)
            detail = (
                "indefinitely" if remaining is None else f"with {format_duration(remaining)} left"
            )
            print(f"Resumed tracking (was paused {detail}).")

    else:
        state = read_pause_state(args.state)
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "paused": pause_is_active(state),
                        "paused_at": state.get("paused_at") if state else None,
                        "until_epoch": state.get("until_epoch") if state else None,
                        "seconds_remaining": seconds_remaining(state),
                    },
                    indent=2,
                )
            )
        elif pause_is_active(state):
            print(f"Tracking is PAUSED ({describe_pause(state)}).")
        else:
            print("Tracking is active (not paused).")


if __name__ == "__main__":
    main()
