import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

Language = Literal["en-IN", "hi-IN"]
Slug = Literal["help", "about", "privacy", "terms"]


@dataclass(frozen=True)
class CustomerCollectionNotification:
    event_id: UUID
    request_id: UUID
    title: str
    body: str

    def data(self) -> dict[str, str]:
        # FCM data values are strings. Native receivers must normalize schema_version.
        return {
            "schema_version": "1",
            "event_id": str(self.event_id),
            "target": "COLLECTION",
            "request_id": str(self.request_id),
        }


def plain(value: str) -> str:
    value = value.strip()
    if any(ord(c) < 32 and c not in "\n\t" for c in value) or "<" in value or ">" in value:
        raise ValueError("plain text required")
    return value


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Command(Input):
    client_request_id: UUID


class VersionCommand(Command):
    expected_version: Annotated[int, Field(strict=True, ge=1)]


class ProfileWrite(VersionCommand):
    display_name: str | None = Field(max_length=100)
    email: str | None = Field(max_length=254)

    @field_validator("display_name", "email")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = plain(value)
        if not value:
            raise ValueError("empty value must be null")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", value):
            raise ValueError("invalid email")
        return value


class ProfileDto(BaseModel):
    user_id: UUID
    display_name: str | None
    email: str | None
    phone_display: str
    version: int


class PreferencesWrite(VersionCommand):
    language: Language
    collection_notifications: Annotated[bool, Field(strict=True)]


class PreferencesDto(BaseModel):
    language: Language = "en-IN"
    collection_notifications: bool = True
    version: int = 1


class FavouritesWrite(VersionCommand):
    category_codes: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        max_length=500
    )

    @field_validator("category_codes")
    @classmethod
    def unique_codes(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate category")
        return value


class FavouritesDto(BaseModel):
    category_codes: list[str] = []
    version: int = 1


class FeedbackWrite(Command):
    message: str = Field(max_length=2000)

    @field_validator("message")
    @classmethod
    def bounded_message(cls, value: str) -> str:
        value = plain(value)
        if len(value) < 5:
            raise ValueError("message requires 5..2000 characters")
        return value


class FeedbackDto(BaseModel):
    feedback_id: UUID


class PaymentMethodDto(BaseModel):
    code: str
    label: str
    description: str


class PaymentMethodsDto(BaseModel):
    methods: list[PaymentMethodDto] = []


class ContentDraft(Command):
    expected_version: Annotated[int, Field(strict=True, ge=0)]
    title: str = Field(min_length=1, max_length=200)
    paragraphs: list[Annotated[str, Field(min_length=1, max_length=4000)]] = Field(
        min_length=1, max_length=50
    )

    @field_validator("title")
    @classmethod
    def text_title(cls, value: str) -> str:
        value = plain(value)
        if not value:
            raise ValueError("empty title")
        return value

    @field_validator("paragraphs")
    @classmethod
    def text_paragraphs(cls, value: list[str]) -> list[str]:
        result = [plain(item) for item in value]
        if not all(result):
            raise ValueError("empty paragraph")
        return result


class ContentPublish(Command):
    expected_version: Annotated[int, Field(strict=True, ge=1)]
    approval_reference: str = Field(min_length=1, max_length=200)

    @field_validator("approval_reference")
    @classmethod
    def reference(cls, value: str) -> str:
        value = plain(value)
        if not value:
            raise ValueError("approval reference required")
        return value


class ContentDto(BaseModel):
    slug: Slug
    title: str
    paragraphs: list[str]
    updated_at: datetime
    revision: int


class ContentRevisionDto(BaseModel):
    revision_id: UUID
    revision: int
    status: str


class NativeRegistration(Input):
    client_device_id: UUID
    platform: Literal["ANDROID", "IOS"]
    token_provider: Literal["FCM", "APNS"]
    token: SecretStr

    @model_validator(mode="after")
    def matching_provider(self) -> "NativeRegistration":
        if (self.platform, self.token_provider) not in (("ANDROID", "FCM"), ("IOS", "APNS")):
            raise ValueError("platform/provider mismatch")
        token = self.token.get_secret_value()
        if not 1 <= len(token) <= 4096 or any(c.isspace() or ord(c) < 32 for c in token):
            raise ValueError("invalid token")
        return self


class SubscriptionKeys(Input):
    p256dh: str = Field(min_length=86, max_length=90)
    auth: str = Field(min_length=22, max_length=24)


class BrowserSubscription(Input):
    endpoint: SecretStr
    expirationTime: Annotated[int | None, Field(strict=True, ge=0)] = None
    keys: SubscriptionKeys


class RegistrationDto(BaseModel):
    client_device_id: UUID
    registered_at: datetime


class NotificationDto(BaseModel):
    event_id: str
    title: str
    body: str
    created_at: datetime
    target: Literal["COLLECTION"] = "COLLECTION"
    request_id: UUID


class NotificationPage(BaseModel):
    items: list[NotificationDto]
    next_cursor: str | None
