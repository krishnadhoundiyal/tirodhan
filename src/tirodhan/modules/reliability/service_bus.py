from __future__ import annotations

from azure.servicebus import ServiceBusMessage, ServiceBusReceivedMessage
from azure.servicebus.aio import ServiceBusClient, ServiceBusReceiver

from tirodhan.modules.reliability.publisher import RoutedMessage


class AzureServiceBusPublisher:
    def __init__(self, client: ServiceBusClient, *, timeout_seconds: float) -> None:
        self._client = client
        self._timeout = timeout_seconds

    async def send(self, entity: str, message: RoutedMessage) -> None:
        async with self._client.get_queue_sender(queue_name=entity) as sender:
            await sender.send_messages(
                ServiceBusMessage(
                    message.body,
                    message_id=message.message_id,
                    subject=message.message_type,
                    content_type="application/json",
                ),
                timeout=self._timeout,
            )


class AzureServiceabilityDelivery:
    def __init__(self, receiver: ServiceBusReceiver, message: ServiceBusReceivedMessage) -> None:
        self._receiver = receiver
        self._message = message
        self.message_id = str(message.message_id or "")
        self.message_type = str(message.subject or "")
        # Bound untrusted input before JSON parsing; oversized messages are rejected.
        chunks = bytearray()
        try:
            for chunk in message.body:
                if not isinstance(chunk, bytes):
                    chunks = bytearray(b"invalid")
                    break
                chunks.extend(chunk[: 513 - len(chunks)])
                if len(chunks) > 512:
                    break
        except (TypeError, ValueError):
            chunks = bytearray(b"invalid")
        self.body = bytes(chunks)

    async def complete(self) -> None:
        await self._receiver.complete_message(self._message)

    async def abandon(self) -> None:
        await self._receiver.abandon_message(self._message)

    async def dead_letter(self) -> None:
        await self._receiver.dead_letter_message(
            self._message,
            reason="INVALID_SERVICEABILITY_MESSAGE",
            error_description="Message could not be processed",
        )
