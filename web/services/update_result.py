"""Hand-off of web-UI update results from the docker:cli updater to update_history.

The updater recreates the dashboard container, which kills the thread that launched it,
so the updater writes its outcome to INSTALL_DIR/var/update_result.json on the host
(mounted read-only at /app/var). Whichever dashboard process sees the file records it;
the run id in the file makes recording idempotent.
"""

import json
import os

from models import UpdateModel

UPDATE_RESULT_PATH = os.environ.get("UPDATE_RESULT_FILE", "/app/var/update_result.json")


def record_update_result(path=UPDATE_RESULT_PATH):
    """Log the updater's last result to update_history once. Returns True if a row was added."""
    try:
        with open(path, "r") as f:
            result = json.load(f)
    except (OSError, ValueError):
        return False
    run_id = str(result.get("id") or "")
    if not run_id:
        return False
    output = f"Web UI update (run {run_id})"
    if UpdateModel.has_output(output):
        return False
    UpdateModel.log_update(
        result.get("from") or "unknown",
        result.get("to") or "unknown",
        bool(result.get("success")),
        output,
    )
    return True
