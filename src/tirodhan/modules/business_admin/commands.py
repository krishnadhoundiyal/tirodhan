from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Name = Annotated[str, Field(min_length=1, max_length=200)]
Code = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_\-]+$")]


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_command_id: UUID
    resource_id: UUID
    expected_version: int = Field(strict=True, ge=0)
    reason: Code


class GroupEdit(Edit):
    operation: Literal["CATALOGUE_GROUP"] = "CATALOGUE_GROUP"
    group_code: Code
    display_name: Name
    display_order: int = Field(strict=True)
    active: bool


class CategoryEdit(Edit):
    operation: Literal["CATALOGUE_CATEGORY"] = "CATALOGUE_CATEGORY"
    category_code: Code
    group_id: UUID
    display_name: Name
    description: str = Field(max_length=2000)
    display_order: int = Field(strict=True)
    active: bool
    image_id: UUID | None = None
    thumbnail_id: UUID | None = None
    handling_hints: str | None = Field(default=None, max_length=2000)
    quantity_input: Literal["NONE", "OPTIONAL"]
    weight_input: Literal["NONE", "OPTIONAL"]
    quick_label: str | None = Field(default=None, max_length=200)
    quick_order: int | None = None


class ArtworkEdit(Edit):
    operation: Literal["CATALOGUE_ARTWORK"] = "CATALOGUE_ARTWORK"
    kind: Literal["hero", "home", "rickshaw", "receiving_point"]
    media_id: UUID


class MediaRegister(Edit):
    operation: Literal["CATALOGUE_MEDIA"] = "CATALOGUE_MEDIA"
    width: int = Field(strict=True, gt=0)
    height: int = Field(strict=True, gt=0)
    alt_text: Name
    blurhash: str | None = Field(default=None, max_length=100)


class PriceEdit(Edit):
    operation: Literal["PICKUP_PRICE"] = "PICKUP_PRICE"
    # Razorpay Orders' INR technical minimum, not a configured production price.
    amount_minor: int = Field(strict=True, ge=100, le=2**63 - 1)
    currency: Literal["INR"] = "INR"
    active: bool


class RiderEdit(Edit):
    operation: Literal["RIDER_PROFILE"] = "RIDER_PROFILE"
    status: Literal["ACTIVE", "SUSPENDED"]
    vehicle_type_code: Code | None = None
    capacity_class_code: Code | None = None


class RiderRoleEdit(Edit):
    operation: Literal["RIDER_ROLE"] = "RIDER_ROLE"
    rider_id: UUID
    active: bool


class ReceivingEdit(Edit):
    operation: Literal["RECEIVING_POINT"] = "RECEIVING_POINT"
    official_name: Name
    official_identifier: Name | None = None
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    allowed_radius_m: int = Field(strict=True, gt=0)
    status: Literal["ACTIVE", "INACTIVE"]
    unloading_capacity: int | None = Field(default=None, strict=True, gt=0)


class VehicleEdit(Edit):
    operation: Literal["VEHICLE_CONFIG"] = "VEHICLE_CONFIG"
    mass_capacity_g: int = Field(strict=True, gt=0, le=2**31 - 1)
    volume_capacity_ml: int = Field(strict=True, gt=0, le=2**31 - 1)
    profile_version: Code
    end_receiving_point_id: UUID


class RegionEdit(Edit):
    operation: Literal["REGION_CONFIG"] = "REGION_CONFIG"
    name: Annotated[str, Field(min_length=1, max_length=100)]
    cells: tuple[str, ...] = Field(min_length=1, max_length=10000)
    receiving_point_ids: tuple[UUID, ...] = Field(min_length=1, max_length=64)
    default_warehouse_id: UUID


class FleetEdit(Edit):
    operation: Literal["FLEET_CONFIG"] = "FLEET_CONFIG"
    name: Name
    status: Literal["ACTIVE", "INACTIVE"]


BusinessCommand = Annotated[
    GroupEdit
    | CategoryEdit
    | ArtworkEdit
    | MediaRegister
    | PriceEdit
    | RiderEdit
    | RiderRoleEdit
    | ReceivingEdit
    | VehicleEdit
    | RegionEdit
    | FleetEdit,
    Field(discriminator="operation"),
]
