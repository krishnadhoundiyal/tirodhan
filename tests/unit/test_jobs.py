from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tirodhan.core.config import Settings
from tirodhan.workers.pending_payment_expiry import run as pending_payment_run
from tirodhan.workers.planning_scheduler import run as planning_scheduler_run


def test_job_main_exits_cleanly_on_failure() -> None:
    from tirodhan.workers.pending_payment_expiry import main as pp_main
    from tirodhan.workers.planning_scheduler import main as ps_main

    with patch(
        "tirodhan.workers.pending_payment_expiry.run", side_effect=RuntimeError("Job Failed")
    ):
        with pytest.raises(SystemExit) as exc_info:
            pp_main()
        assert exc_info.value.code == 1

    with patch("tirodhan.workers.planning_scheduler.run", side_effect=RuntimeError("Job Failed")):
        with pytest.raises(SystemExit) as exc_info:
            ps_main()
        assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_pending_payment_expiry_propagates_failure(monkeypatch) -> None:
    settings = Settings(_env_file=None)
    monkeypatch.setattr("tirodhan.workers.pending_payment_expiry.get_settings", lambda: settings)

    mock_engine = AsyncMock()

    mock_session = MagicMock()
    mock_transaction = MagicMock()

    async def mock_aenter(self):
        return mock_session

    async def mock_aexit(self, exc_type, exc_val, exc_tb):
        pass

    async def mock_tx_aenter(self):
        return mock_transaction

    async def mock_tx_aexit(self, exc_type, exc_val, exc_tb):
        pass

    mock_session.__aenter__ = mock_aenter
    mock_session.__aexit__ = mock_aexit

    mock_transaction.__aenter__ = mock_tx_aenter
    mock_transaction.__aexit__ = mock_tx_aexit
    mock_session.begin.return_value = mock_transaction

    mock_sf = MagicMock(return_value=mock_session)

    with (
        patch(
            "tirodhan.workers.pending_payment_expiry.create_database_engine",
            return_value=mock_engine,
        ),
        patch(
            "tirodhan.workers.pending_payment_expiry.create_session_factory", return_value=mock_sf
        ),
        patch(
            "tirodhan.workers.pending_payment_expiry.expire_pending_collection_requests"
        ) as mock_expire,
    ):
        mock_expire.side_effect = RuntimeError("Database error")
        with pytest.raises(RuntimeError, match="Database error"):
            await pending_payment_run()


@pytest.mark.asyncio
async def test_planning_scheduler_propagates_partial_failures(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        planning_lead_time_minutes=30,
        planning_max_attempts=3,
        planning_compaction_distance_m=1000,
        planning_max_group_requests=10,
    )
    monkeypatch.setattr("tirodhan.workers.planning_scheduler.get_settings", lambda: settings)

    mock_work_unit_1 = AsyncMock()
    mock_work_unit_1.cell_id = "cell-1"
    mock_work_unit_2 = AsyncMock()
    mock_work_unit_2.cell_id = "cell-2"

    mock_engine = AsyncMock()

    mock_session = MagicMock()
    mock_transaction = MagicMock()

    async def mock_aenter(self):
        return mock_session

    async def mock_aexit(self, exc_type, exc_val, exc_tb):
        pass

    async def mock_tx_aenter(self):
        return mock_transaction

    async def mock_tx_aexit(self, exc_type, exc_val, exc_tb):
        pass

    mock_session.__aenter__ = mock_aenter
    mock_session.__aexit__ = mock_aexit

    mock_transaction.__aenter__ = mock_tx_aenter
    mock_transaction.__aexit__ = mock_tx_aexit
    mock_session.begin.return_value = mock_transaction

    mock_sf = MagicMock(return_value=mock_session)

    with (
        patch(
            "tirodhan.workers.planning_scheduler.create_database_engine", return_value=mock_engine
        ),
        patch("tirodhan.workers.planning_scheduler.create_session_factory", return_value=mock_sf),
        patch(
            "tirodhan.workers.planning_scheduler.discover_due_planning_work_units",
            return_value=[mock_work_unit_1, mock_work_unit_2],
        ),
        patch("tirodhan.workers.planning_scheduler.freeze_planning_batch") as mock_freeze,
    ):
        # Make the first work unit fail and the second succeed
        mock_freeze.side_effect = [RuntimeError("Freeze failed"), None]

        with pytest.raises(RuntimeError, match="planning scheduler finished with 1 failures"):
            await planning_scheduler_run()

        # It should have still attempted to process the second work unit
        assert mock_freeze.call_count == 2
