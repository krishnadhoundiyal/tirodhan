import asyncio
from collections.abc import Sequence
from typing import cast

from tirodhan.modules.notifications.push import (
    PushDeliveryResult,
    PushNotification,
    PushNotificationPort,
    PushRecipient,
)


class FCMAdapter(PushNotificationPort):
    async def send_batch(
        self,
        recipients: Sequence[PushRecipient],
        notification: PushNotification,
    ) -> Sequence[PushDeliveryResult]:
        async def _send_single(recipient: PushRecipient) -> PushDeliveryResult:
            permanently_failed = recipient.registration_token == "invalid-token"
            success = not permanently_failed
            return PushDeliveryResult(
                push_registration_id=recipient.push_registration_id,
                success=success,
                permanently_failed=permanently_failed,
                provider_message_id="simulated-fcm-id" if success else None,
            )

        tasks = [_send_single(recipient) for recipient in recipients]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results = []
        for result in results:
            if isinstance(result, Exception):
                pass
            else:
                final_results.append(cast(PushDeliveryResult, result))

        return final_results
