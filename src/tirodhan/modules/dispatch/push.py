from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import UUID, uuid4

from firebase_admin import (  # type: ignore[import-untyped]
    credentials,
    delete_app,
    exceptions,
    initialize_app,
    messaging,
)

from tirodhan.core.config import Settings


class PushConfigurationError(RuntimeError):
    pass


class PushUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class PushRecipient:
    delivery_id: UUID
    registration_token: str = field(repr=False)


@dataclass(frozen=True)
class PushNotification:
    collection_group_id: UUID
    offer_round: int

    def data(self) -> dict[str, str]:
        return {
            "type": "ASSIGNMENT_OFFER",
            "collection_group_id": str(self.collection_group_id),
            "offer_round": str(self.offer_round),
        }


@dataclass(frozen=True)
class PushDeliveryResult:
    delivery_id: UUID
    status: Literal["SENT", "PERMANENTLY_FAILED", "PENDING"]
    provider_message_id: str | None = None


class PushNotificationPort(Protocol):
    async def send_batch(
        self, recipients: Sequence[PushRecipient], notification: PushNotification
    ) -> Sequence[PushDeliveryResult]: ...


class FcmPushNotificationPort:
    """Real Firebase Admin multicast off the event loop; chunks start together."""

    def __init__(self, settings: Settings) -> None:
        if not settings.fcm_project_id or settings.fcm_credentials_json is None:
            raise PushConfigurationError("FCM runtime is not configured")
        try:
            material = json.loads(settings.fcm_credentials_json.get_secret_value())
            self._app = initialize_app(
                credentials.Certificate(material),
                options={"projectId": settings.fcm_project_id},
                name=f"tirodhan-push-{uuid4()}",
            )
        except Exception:
            raise PushConfigurationError("invalid FCM runtime configuration") from None

    async def send_batch(
        self, recipients: Sequence[PushRecipient], notification: PushNotification
    ) -> Sequence[PushDeliveryResult]:
        chunks = [recipients[index : index + 500] for index in range(0, len(recipients), 500)]
        batches = await asyncio.gather(*(self._send_chunk(chunk, notification) for chunk in chunks))
        return tuple(result for batch in batches for result in batch)

    async def _send_chunk(
        self, recipients: Sequence[PushRecipient], notification: PushNotification
    ) -> tuple[PushDeliveryResult, ...]:
        try:
            response = await asyncio.to_thread(
                messaging.send_each_for_multicast,
                messaging.MulticastMessage(
                    tokens=[recipient.registration_token for recipient in recipients],
                    notification=messaging.Notification(
                        "New pickup opportunity", "Open Tirodhan to review and accept."
                    ),
                    data=notification.data(),
                ),
                app=self._app,
            )
        except Exception:
            # SDK response/exception text can contain registration tokens. Never propagate it.
            raise PushUnavailableError("FCM send did not establish a result") from None
        results: list[PushDeliveryResult] = []
        if len(response.responses) != len(recipients):
            raise PushUnavailableError("FCM returned an incomplete result")
        for recipient, device in zip(recipients, response.responses, strict=True):
            outcome: Literal["SENT", "PERMANENTLY_FAILED", "PENDING"] = "PENDING"
            if device.success:
                outcome = "SENT"
            elif isinstance(
                device.exception, (messaging.UnregisteredError, exceptions.InvalidArgumentError)
            ):
                # Notification payload is fixed/valid; per-device INVALID_ARGUMENT
                # therefore indicates an invalid token, not an arbitrary custom payload.
                outcome = "PERMANENTLY_FAILED"
            results.append(
                PushDeliveryResult(
                    recipient.delivery_id, outcome, device.message_id if device.success else None
                )
            )
        return tuple(results)

    async def close(self) -> None:
        # Firebase app cleanup runs its own event loop even for the unused async
        # messaging client. Keep both supported synchronous SDK calls off ours.
        await asyncio.to_thread(delete_app, self._app)
