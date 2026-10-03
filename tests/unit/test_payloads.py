import json
from uuid import uuid4

from tirodhan.modules.reliability.models import OutboxEvent
from tirodhan.modules.reliability.publisher import dispatch_message


def test_dispatch_outbox_payload_contains_no_pii():
    group_id = uuid4()
    event = OutboxEvent(
        outbox_event_id=uuid4(),
        event_key="123",
        aggregate_type="collection_group",
        aggregate_id=group_id,
        event_type="CollectionGroupDispatchRequested",
        payload={
            "collection_group_id": str(group_id),
            "planning_batch_id": str(uuid4()),
            "cell_id": "8928308280fffff",
            "slot_start": "2023-10-01T08:00:00Z",
            "slot_end": "2023-10-01T12:00:00Z",
            "dispatch_stage": "FLEET_FIRST",
        },
    )
    message = dispatch_message(event)
    data = json.loads(message.body)
    assert "cell_id" in data
    assert "address" not in data
    assert "lat" not in data
    assert "phone" not in data
