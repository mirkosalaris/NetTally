"""Configuration loading with comment-stripping and type validation.

NetTally's config.json allows JS-style `//` and `/* */` comments; this module
owns that parsing (single source of truth, reused by install.sh via main()).
Loads user config over a built-in defaults dict, validates value types, and
keeps unknown keys for forward compatibility.
"""

import argparse
import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.expanduser("~/Library/Application Support/NetTally/config.json")

DEFAULTS: dict[str, Any] = {
    "polling_interval_seconds": 30,
    "process_state_prune_interval_seconds": 21600,
    "process_state_max_age_seconds": 86400,
    "default_report_days": 30,
    "html_top_apps_limit": 8,
    "gap_threshold_multiplier": 3,
}


def strip_comments(text: str) -> str:
    """Remove `//` and `/* */` comments while preserving quoted strings."""
    pattern = re.compile(r'("(?:[^"\\]|\\.)*")|(/\*[\s\S]*?\*/)|(//.*)')

    def replacer(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        return ""

    return pattern.sub(replacer, text)


def load_config(config_path: Optional[str] = None) -> dict[str, Any]:
    """Load config, merging file values over DEFAULTS with type validation.

    Falls back to defaults on a missing/corrupt/malformed file, logging a
    warning rather than failing hard so a bad config never stops the daemon.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH

    config = dict(DEFAULTS)

    if not os.path.exists(config_path):
        return config

    try:
        with open(config_path, encoding="utf-8") as f:
            raw_content = f.read()

        cleaned_content = strip_comments(raw_content)
        user_config = json.loads(cleaned_content)

        if isinstance(user_config, dict):
            for key, val in user_config.items():
                if key in DEFAULTS:
                    default_val = DEFAULTS[key]
                    # Validate type (int or float)
                    if isinstance(default_val, (int, float)):
                        if isinstance(val, (int, float)) and not isinstance(val, bool):
                            # Cast to int if the default is int
                            if isinstance(default_val, int):
                                config[key] = int(val)
                            else:
                                config[key] = float(val)
                        else:
                            # Keep default if invalid type
                            pass
                    else:
                        config[key] = val
                else:
                    # Keep unknown keys for forward-compatibility
                    config[key] = val
    except Exception as e:
        # On error (missing, corrupt, malformed), fall back to defaults or whatever
        # was parsed, but say so — a silent default is how config typos go unnoticed.
        logger.warning("Failed to load config from %s: %s", config_path, e)

    return config


def main() -> None:
    """Print a single config value (used by install.sh to read the polling interval)."""
    parser = argparse.ArgumentParser(description="Print a single value from the NetTally config")
    parser.add_argument(
        "--get", type=str, required=True, help="Config key to print, e.g. polling_interval_seconds"
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help=f"Path to config.json (default: {DEFAULT_CONFIG_PATH})",
    )
    args = parser.parse_args()
    try:
        print(load_config(args.config)[args.get])
    except KeyError:
        parser.error(f"Unknown config key: {args.get}")


if __name__ == "__main__":
    main()
