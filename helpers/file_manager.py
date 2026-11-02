import os
import logging

def ensure_default_log_files(config_path, log_path):
    """Ensures logs and config exist."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    if not os.path.exists(log_path):
        with open(log_path, 'w') as f:
            f.write("Log initialized.\n")
    # Ensure the per-purpose log files the WebUI reads exist too: the /logs
    # page lists only files present on disk, and the dashboard's unified-log
    # endpoint reads unified_scan.log. A fresh volume must not hide them.
    try:
        from helpers.logging_config import resolve_log_dir
        log_dir = resolve_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        for name in (
            "unified_scan.log",
            "info.log",
            "debug.log",
            "queue.log",
            "search.log",
            "activity.log",
            "navidrome_import.log",
        ):
            path = os.path.join(log_dir, name)
            if not os.path.exists(path):
                with open(path, 'w', encoding='utf-8') as f:
                    f.write("")
    except Exception:
        pass