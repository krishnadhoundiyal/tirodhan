from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from azure.servicebus import NEXT_AVAILABLE_SESSION

from tirodhan.core.config import Settings
from tirodhan.workers.planning_worker import run as planning_worker_run


@pytest.mark.asyncio
async def test_planning_worker_handle_delivery_logging() -> None:
    from tirodhan.modules.reliability.service_bus import AzurePlanningDelivery
    from tirodhan.workers.planning_worker import handle_planning_delivery

    delivery_mock = AsyncMock(spec=AzurePlanningDelivery)
    delivery_mock.body = b"not-json"

    with patch("tirodhan.workers.planning_worker.logger.error") as mock_logger_error:
        await handle_planning_delivery(delivery_mock, AsyncMock())
        mock_logger_error.assert_called_with(
            "planning_message_invalid", extra={"error_type": "JSONDecodeError"}
        )
        delivery_mock.dead_letter.assert_awaited_once()

    import uuid

    delivery_mock = AsyncMock(spec=AzurePlanningDelivery)
    batch_id = str(uuid.uuid4())
    delivery_mock.body = f'{{"planning_batch_id": "{batch_id}"}}'.encode()

    delivery_mock.message_id = "test-msg"
    delivery_mock.message_type = "test-type"

    with (
        patch(
            "tirodhan.workers.planning_worker.execute_planning_attempt",
            side_effect=RuntimeError("Some transient DB issue"),
        ),
        patch("tirodhan.workers.planning_worker.logger.error") as mock_logger_error,
    ):
        await handle_planning_delivery(delivery_mock, AsyncMock())
        mock_logger_error.assert_called_with(
            "planning_delivery_failed", extra={"error_type": "RuntimeError"}
        )
        delivery_mock.abandon.assert_awaited_once()


@pytest.mark.asyncio
async def test_planning_worker_session_acquisition(monkeypatch) -> None:
    settings = Settings(
        _env_file=None,
        service_bus_namespace="test.servicebus.windows.net",
        planning_queue_name="planning-q",
        planning_lock_renewal_seconds=120,
        service_bus_operation_timeout_seconds=30.0,
    )
    monkeypatch.setattr("tirodhan.workers.planning_worker.get_settings", lambda: settings)

    mock_receiver = MagicMock()
    mock_receiver.session = MagicMock()

    # Track the number of iterations
    iteration_count = 0

    async def mock_async_for():
        nonlocal iteration_count
        iteration_count += 1
        if iteration_count >= 2:
            # Raise exception to break out of the infinite while True loop after 2 sessions
            raise KeyboardInterrupt()
        yield MagicMock()

    mock_receiver.__aiter__ = lambda self: mock_async_for()

    async def mock_aenter(self):
        return mock_receiver

    async def mock_aexit(self, exc_type, exc_val, exc_tb):
        pass

    mock_receiver.__aenter__ = mock_aenter
    mock_receiver.__aexit__ = mock_aexit

    mock_bus = MagicMock()
    mock_bus.get_queue_receiver.return_value = mock_receiver

    async def mock_bus_aenter(self):
        return mock_bus

    async def mock_bus_aexit(self, exc_type, exc_val, exc_tb):
        pass

    mock_bus.__aenter__ = mock_bus_aenter
    mock_bus.__aexit__ = mock_bus_aexit

    mock_renewer = MagicMock()
    mock_renewer.register = MagicMock()

    async def mock_renewer_aenter(self):
        return mock_renewer

    async def mock_renewer_aexit(self, exc_type, exc_val, exc_tb):
        pass

    mock_renewer.__aenter__ = mock_renewer_aenter
    mock_renewer.__aexit__ = mock_renewer_aexit

    mock_engine = AsyncMock()
    with (
        patch("tirodhan.workers.planning_worker.ServiceBusClient", return_value=mock_bus),
        patch("tirodhan.workers.planning_worker.AutoLockRenewer", return_value=mock_renewer),
        patch("tirodhan.workers.planning_worker.DefaultAzureCredential"),
        patch("tirodhan.workers.planning_worker.create_database_engine", return_value=mock_engine),
        patch("tirodhan.workers.planning_worker.create_session_factory"),
        patch("tirodhan.workers.planning_worker.handle_planning_delivery", new_callable=AsyncMock),
    ):
        with pytest.raises(KeyboardInterrupt):
            await planning_worker_run()

    from azure.servicebus import ServiceBusReceiveMode

    mock_bus.get_queue_receiver.assert_called_with(
        queue_name="planning-q",
        receive_mode=ServiceBusReceiveMode.PEEK_LOCK,
        prefetch_count=0,
        session_id=NEXT_AVAILABLE_SESSION,
        max_wait_time=5,
    )

    mock_renewer.register.assert_called_with(
        mock_receiver,
        mock_receiver.session,
        max_lock_renewal_duration=120,
    )
