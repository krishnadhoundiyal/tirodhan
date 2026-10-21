"""Persisted advisory scores retain feasibility and never release failed ownership."""

import pytest
from sqlalchemy import select
from test_rider_failure_recovery import failed
from test_routing_fleet import LineMatrix, engines

from tirodhan.modules.dispatch.models import RiderAssignmentItem
from tirodhan.modules.planning.routing.validation import validate_proposal
from tirodhan.modules.routing_operations import replacement_options
from tirodhan.modules.routing_operations.capacity_models import RiderReplacement
from tirodhan.modules.routing_operations.failure_runtime import recommend_one
from tirodhan.modules.routing_operations.replacement_reads import read_replacement_options
from tirodhan.modules.routing_operations.service import PROBLEM_CODEC, PROPOSAL_CODEC

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("baseline_available", [True, False])
async def test_scoring_metadata_and_unpriced_options_preserve_authority(
    database_session_factory, monkeypatch, baseline_available
):
    factory = database_session_factory
    data, incident, root = await failed(factory)
    if not baseline_available:
        # A baseline comparison may be unavailable independently of rescue feasibility.
        monkeypatch.setattr(replacement_options, "incumbent_cost", lambda *args: None)
    async with factory() as session:
        row = await session.scalar(
            select(RiderReplacement).where(RiderReplacement.failure_incident_id == incident)
        )
    assert await recommend_one(
        factory,
        row.replacement_id,
        providers={"test-car": LineMatrix()},
        optimizers=engines(),
        now=data[6][0],
    )
    async with factory() as session:
        view = await read_replacement_options(session, row.replacement_id, data[6][0])
        assert view["evaluation"]["search"]["candidate_order"] == (
            "TEMPORAL_ENVELOPE_AND_GEOGRAPHIC_INSERTION_V1"
        )
        assert view["evaluation"]["search"]["exhaustive"] is False
        assert view["authoritative"] is False
        assert view["candidates"]
        for candidate in view["candidates"]:
            assert candidate["selectable"]
            if baseline_available:
                assert candidate["ranking_basis"] == "INCREMENTAL_GENERALIZED_COST"
                assert "generalized_seconds" in candidate["incremental_impact"]
            else:
                assert candidate["ranking_basis"] == "UNPRICED_BASELINE_UNAVAILABLE"
                assert candidate["incremental_impact"] == {
                    "baseline": "UNAVAILABLE_OR_NOT_CURRENTLY_FEASIBLE"
                }
        from tirodhan.modules.routing_operations.replacement_models import ReplacementCandidate

        persisted = list(await session.scalars(select(ReplacementCandidate)))
        for candidate in persisted:
            validate_proposal(
                PROBLEM_CODEC.validate_python(candidate.problem),
                PROPOSAL_CODEC.validate_python(candidate.proposal),
            )
        owners = list(
            await session.scalars(
                select(RiderAssignmentItem).where(
                    RiderAssignmentItem.assignment_id == root.assignment_id
                )
            )
        )
        assert owners and all(item.released_at is None for item in owners)
