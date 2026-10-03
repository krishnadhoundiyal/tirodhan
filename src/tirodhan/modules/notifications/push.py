from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PushRecipient:
    push_registration_id: str
    registration_token: str
    platform: str


@dataclass(frozen=True)
class PushNotification:
    title: str
    body: str
    data: dict[str, str]


@dataclass(frozen=True)
class PushDeliveryResult:
    push_registration_id: str
    success: bool
    permanently_failed: bool
    provider_message_id: str | None


class PushNotificationPort(Protocol):
    async def send_batch(
        self,
        recipients: Sequence[PushRecipient],
        notification: PushNotification,
    ) -> Sequence[PushDeliveryResult]: ...
