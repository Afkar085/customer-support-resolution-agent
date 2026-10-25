"""Mock carrier API. Read-only, and allowed to fail transiently so the retry
policy has something to exercise."""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import Shipment
from app.services.common import iso
from mock_services.payments.provider import (
    FaultMode,
    ProviderTimeout,
    ProviderUnavailable,
    _consume_fault,
)

SERVICE = "shipping"


def get_tracking(order_id: str) -> dict[str, Any] | None:
    fault = _consume_fault(SERVICE)
    if fault == FaultMode.UNAVAILABLE:
        raise ProviderUnavailable("carrier API unavailable")
    if fault == FaultMode.TIMEOUT:
        raise ProviderTimeout("carrier API timed out")

    with session_scope() as session:
        row = session.scalar(select(Shipment).where(Shipment.order_id == order_id))
        if row is None:
            return None
        return {
            "shipment_id": row.id,
            "carrier": row.carrier,
            "tracking_number": row.tracking_number,
            "status": row.status,
            "last_scan_location": row.last_scan_location,
            "last_scan_at": iso(row.last_scan_at),
            "estimated_delivery_at": iso(row.estimated_delivery_at),
        }
