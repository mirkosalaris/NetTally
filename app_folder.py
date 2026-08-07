import json
import os
import re

DEFAULT_APP_MAP_PATH = os.path.expanduser("~/Library/Application Support/NetTally/app_map.json")
LOCAL_APP_MAP_PATH = os.path.join(os.path.dirname(__file__), "app_map.json")

class AppFolder:
    def __init__(self, config_path=None):
        self.exact_map = {}
        self.prefix_map = {}
        self.suffix_patterns = []
        self.load_config(config_path)

    def load_config(self, config_path=None):
        paths_to_try = []
        if config_path:
            paths_to_try.append(config_path)
        paths_to_try.extend([DEFAULT_APP_MAP_PATH, LOCAL_APP_MAP_PATH])

        for path in paths_to_try:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.exact_map = data.get("exact_map", {})
                        self.prefix_map = data.get("prefix_map", {})
                        self.suffix_patterns = data.get("suffix_patterns", [])
                        return
                except Exception as e:
                    print(f"Warning: Failed to load app_map from {path}: {e}")

    def fold(self, raw_name: str) -> str:
        if not raw_name:
            return "Unknown"
        
        name = raw_name.strip()
        
        # 1. Exact match check
        if name in self.exact_map:
            return self.exact_map[name]
        
        # 2. Strip standard suffixes
        cleaned = name
        for pattern in self.suffix_patterns:
            if cleaned.endswith(pattern):
                cleaned = cleaned[:-len(pattern)].strip()
                break
        
        if cleaned in self.exact_map:
            return self.exact_map[cleaned]

        # 3. Prefix match check
        for prefix, target in self.prefix_map.items():
            if name.startswith(prefix) or cleaned.startswith(prefix):
                return target
        
        # 4. Strip regex patterns like " (Renderer)", " Helper", etc. if present
        regex_cleaned = re.sub(r'\s+(Helper|\(Renderer\)|\(GPU\)|\(Plugin\)).*$', '', name, flags=re.IGNORECASE).strip()
        if regex_cleaned in self.exact_map:
            return self.exact_map[regex_cleaned]
        for prefix, target in self.prefix_map.items():
            if regex_cleaned.startswith(prefix):
                return target

        return regex_cleaned if regex_cleaned else name

_default_folder = None

def get_app_folder(config_path=None):
    global _default_folder
    if _default_folder is None or config_path is not None:
        _default_folder = AppFolder(config_path)
    return _default_folder

def fold_app_name(raw_name: str, config_path=None) -> str:
    folder = get_app_folder(config_path)
    return folder.fold(raw_name)
