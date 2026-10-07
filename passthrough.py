#!/usr/bin/env python3
"""Register pass-through apps (VPNs) and exclude them from reports.

A tunnel process carries a second copy of the bytes its clients transfer:
nettop attributes each payload both to the originating app's sockets and to the
VPN client that reads it off the tunnel interface and writes it out again, so
summing per-app bytes with the VPN included roughly doubles the real traffic.
Which processes behave this way is architecture-dependent and cannot be
detected reliably on demand (docs/decisions/0007-passthrough-apps-excluded-at-
report-time.md), so the user registers them by hand:

    nettally passthrough add PanGPS     # folds to "GlobalProtect VPN"

The registry lives in a small JSON file beside config.json in Application
Support rather than in usage.db: it is configuration, it stays hand-editable,
and report-time reads never need the database to be open.

Exclusion happens when you report, not when the collector records. The raw rows
stay in usage_5m untouched, so flagging a pass-through app is retroactive (the
next report corrects all recorded history) and reversible (removing the flag
restores it in full).
"""

import argparse
import difflib
import json
import logging
import os
from collections.abc import Iterable
from typing import Optional

from app_folder import fold_app_name
from db import get_connection, get_db_path

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = os.path.expanduser(
    "~/Library/Application Support/NetTally/passthrough.json"
)


def _resolve(state_path: Optional[str] = None) -> str:
    """Return the registry path to use, defaulting to the installed location."""
    return state_path or DEFAULT_REGISTRY_PATH


def read_registry(state_path: Optional[str] = None) -> frozenset[str]:
    """Load the registered pass-through names.

    A missing, unreadable or malformed file is reported as an empty registry
    rather than raising: exclusion is a reporting convenience and must never
    break a report run. Problems are logged so a truncated or hand-edited file
    still shows up in the log.
    """
    path = _resolve(state_path)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable pass-through registry %s: %s", path, e)
        return frozenset()

    if not isinstance(raw, dict):
        logger.warning("Ignoring pass-through registry %s: expected a JSON object", path)
        return frozenset()
    entries = raw.get("pass_through_apps")
    if not isinstance(entries, list):
        logger.warning(
            "Ignoring pass-through registry %s: 'pass_through_apps' must be a list", path
        )
        return frozenset()

    return frozenset(entry.strip() for entry in entries if isinstance(entry, str) and entry.strip())


def resolve_names(
    names: Optional[Iterable[str]], state_path: Optional[str] = None
) -> frozenset[str]:
    """None means "whatever the registry says right now"; an explicit set wins."""
    if names is None:
        return read_registry(state_path)
    return frozenset(names)


def lowered(names: Iterable[str]) -> set[str]:
    """Lowercased names for case-insensitive membership tests."""
    return {name.lower() for name in names}


def is_pass_through(app_name: str, lowered_names: set[str]) -> bool:
    """True when `app_name` (a report-visible, already-folded name) is registered."""
    return bool(app_name) and app_name.lower() in lowered_names


def exclude_rows(
    rows: list[dict],
    names: Optional[Iterable[str]] = None,
    state_path: Optional[str] = None,
) -> tuple[list[dict], list[str]]:
    """Drop pass-through rows; returns (kept rows, hidden names as the rows spell them).

    Matching is case-insensitive against the `app_name` column value, i.e.
    against the canonical name reports display (folding already happened at
    collection time). `names=None` resolves the live registry, so a caller
    that simply forgets about pass-through apps still excludes by default.
    The hidden list only names entries actually present in `rows`, so a
    footer never claims to have hidden something that wasn't there.
    """
    if not rows:
        return [], []
    resolved = resolve_names(names, state_path)
    if not resolved:
        return list(rows), []
    names_lower = lowered(resolved)
    hidden = {
        str(row.get("app_name", ""))
        for row in rows
        if str(row.get("app_name", "")).lower() in names_lower
    }
    kept = [row for row in rows if str(row.get("app_name", "")).lower() not in names_lower]
    return kept, sorted(hidden, key=str.lower)


def format_names(names: Iterable[str]) -> str:
    """Comma-separated names in a stable order, for notes and status lines."""
    return ", ".join(sorted(names, key=str.lower))


def _write_registry(names: Iterable[str], state_path: Optional[str] = None) -> None:
    """Persist the registry atomically so a report can never read a half-written file."""
    path = _resolve(state_path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {"pass_through_apps": sorted(set(names), key=str.lower)}
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def add_name(
    name: str,
    state_path: Optional[str] = None,
    config_path: Optional[str] = None,
) -> tuple[bool, str]:
    """Register `name`, folded the way reports name it. Returns (changed, canonical).

    An entry already registered under any casing leaves the file untouched and
    reports changed=False, so repeating the command is harmless.
    """
    canonical = fold_app_name(name.strip(), config_path=config_path)
    current = read_registry(state_path)
    if canonical.lower() in lowered(current):
        return False, canonical
    _write_registry([*current, canonical], state_path)
    return True, canonical


def remove_name(
    name: str,
    state_path: Optional[str] = None,
    config_path: Optional[str] = None,
) -> Optional[str]:
    """Unregister `name`. Returns the stored entry that was removed, or None.

    Both the raw and the folded spelling are tried (case-insensitively), so
    `remove PanGPS` works even though the registry stores "GlobalProtect VPN".
    """
    stripped = name.strip()
    candidates = {stripped.lower(), fold_app_name(stripped, config_path=config_path).lower()}
    current = read_registry(state_path)
    removed = [n for n in current if n.lower() in candidates]
    if not removed:
        return None
    _write_registry((n for n in current if n.lower() not in candidates), state_path)
    return sorted(removed, key=str.lower)[0]


def _known_app_names(db_path: str) -> list[str]:
    """Distinct app names actually recorded, for the add-time suggestion; never fatal."""
    if not os.path.exists(db_path):
        return []
    try:
        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT DISTINCT app_name FROM usage_5m")
            return sorted(str(row[0]) for row in cursor.fetchall())
        finally:
            conn.close()
    except Exception as e:
        # Suggestions must never fail the command; the worst case is no hint.
        logger.debug("Could not list recorded apps from %s: %s", db_path, e)
        return []


def main() -> None:
    """CLI entry point behind `nettally passthrough add|remove|list`."""
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="NetTally: register pass-through apps (e.g. VPNs) to keep them out of reports"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --registry lives on each subparser, not the top-level one: `nettally passthrough
    # add --registry PATH` must parse, and a top-level option would stop at the
    # subcommand name (same reason pause.py puts --state on each one).
    def add_registry_option(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--registry",
            default=None,
            help="Path to the pass-through registry file (default: installed location)",
        )

    add_parser = sub.add_parser(
        "add", help="Register an app as pass-through (excluded from reports)"
    )
    add_registry_option(add_parser)
    add_parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (used only to suggest known app names)",
    )
    add_parser.add_argument(
        "name",
        help="App name as reports show it, or a raw nettop process name (folded through app_map.json)",
    )

    remove_parser = sub.add_parser("remove", help="Unregister a pass-through app")
    add_registry_option(remove_parser)
    remove_parser.add_argument("name", help="Registered app name (raw or folded spelling)")

    list_parser = sub.add_parser("list", help="Show the registered pass-through apps")
    add_registry_option(list_parser)
    list_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only a comma-separated list (nothing when empty); used by `nettally status`",
    )

    args = parser.parse_args()

    if args.command == "add":
        changed, canonical = add_name(args.name, args.registry)
        if not changed:
            print(f'Already registered: "{canonical}" is a pass-through app.')
        else:
            print(f'Registered "{canonical}" as a pass-through app.')
            print("CLI reports exclude it from the next run (--include-passthrough opts back in).")
            known = _known_app_names(get_db_path(args.db))
            if canonical.lower() not in lowered(known):
                print(f'Heads up: no recorded traffic under "{canonical}" yet.')
                close = difflib.get_close_matches(canonical, known, n=3, cutoff=0.6)
                if close:
                    print(f"Closest recorded apps: {', '.join(close)}")
        print(f"Registry: {_resolve(args.registry)}")

    elif args.command == "remove":
        removed = remove_name(args.name, args.registry)
        if removed is None:
            registered = read_registry(args.registry)
            if registered:
                print(f"Not registered. Pass-through apps: {format_names(registered)}")
            else:
                print("Not registered (no pass-through apps are registered at all).")
        else:
            print(f'Removed "{removed}". Reports include it again from the next run.')
            print(f"Registry: {_resolve(args.registry)}")

    else:  # list
        names = read_registry(args.registry)
        if args.quiet:
            if names:
                print(format_names(names))
        elif names:
            print(f"Pass-through apps (excluded from reports): {format_names(names)}")
        else:
            print("No pass-through apps registered (nothing is excluded from reports).")


if __name__ == "__main__":
    main()
