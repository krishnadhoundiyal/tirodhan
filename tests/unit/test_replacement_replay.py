"""Schema evolution retains fingerprints of commands recorded before candidate support."""

import hashlib
from uuid import UUID

from tirodhan.modules.routing_operations.operational_commands import operational_fingerprint
from tirodhan.modules.routing_operations.replacement_commands import (
    DecideReplacement,
    RequestReplacement,
)


def test_legacy_replacement_request_fingerprint_survives_optional_reason():
    identity = UUID(int=1)
    old = (
        '{"client_command_id":"00000000-0000-0000-0000-000000000001",'
        '"operation":"REPLACEMENT_REQUEST","request_id":"00000000-0000-0000-0000-000000000001"}'
    )
    command = RequestReplacement(client_command_id=identity, request_id=identity)
    assert operational_fingerprint(command) == hashlib.sha256(old.encode()).digest()
    assert operational_fingerprint(
        command.model_copy(update={"reason": "BREAKDOWN"})
    ) != operational_fingerprint(command)


def test_legacy_decision_fingerprint_and_explicit_candidate_identity():
    identity = UUID(int=1)
    old = (
        '{"client_command_id":"00000000-0000-0000-0000-000000000001",'
        '"operation":"REPLACEMENT_DECIDE","replacement_id":"00000000-0000-0000-0000-000000000001",'
        '"expected_version":2,"decision":"APPROVE"}'
    )
    command = DecideReplacement(
        client_command_id=identity, replacement_id=identity, expected_version=2, decision="APPROVE"
    )
    assert operational_fingerprint(command) == hashlib.sha256(old.encode()).digest()
    assert operational_fingerprint(
        command.model_copy(update={"selected_candidate_id": identity})
    ) != operational_fingerprint(command)
