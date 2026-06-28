"""Audit log service — records compliance and business events for the audit trail.

This is a logging stub for KER-105 import compatibility; KER-112 will replace it
with full DB persistence to an audit_events table with RLS and tenant isolation.
"""

import logging

logger = logging.getLogger(__name__)

__all__ = ["write_audit_event"]


def write_audit_event(
    conn,
    tenant_id: str,
    event_type: str,
    event_data: dict,
) -> None:
    """Emit an audit event for the given tenant and event type.

    Stub implementation: logs at INFO level. KER-112 replaces this with an
    INSERT into the audit_events table inside the caller's transaction so the
    audit record is committed or rolled back atomically with the business row.
    """
    logger.info("AUDIT [%s] tenant=%s data=%s", event_type, tenant_id, event_data)
