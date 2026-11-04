"""Periodic database housekeeping: orphaned output rows and connection-history retention.

Deletes run in small batches so beast-proxy and the CoT pipeline never wait long on the
write lock. Reclaiming the freed file space (VACUUM) is a one-off manual step.
"""

import logging
import os

from models import MaintenanceModel

log = logging.getLogger(__name__)

CONNECTION_RETENTION_DAYS = int(os.environ.get("CONNECTION_RETENTION_DAYS", "30"))


def run_db_maintenance():
    try:
        orphans = MaintenanceModel.purge_orphan_output_rows()
        pruned = MaintenanceModel.prune_connections(CONNECTION_RETENTION_DAYS)
    except Exception:
        log.exception("DB maintenance failed")
        return
    if pruned or any(orphans.values()):
        log.warning("DB maintenance: removed orphan output rows %s, %d connection rows older than %d days",
                    orphans, pruned, CONNECTION_RETENTION_DAYS)
