"""상점·꾸미기 API 계약. 장착은 4슬롯 전체 교체(null=선택 슬롯 해제)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


PublicID = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    width: float
    height: float


class Canvas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: Literal[393]
    height: Literal[852]


class ThemeSceneLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: PublicID
    frame: Frame
    z_index: int
    day_url: AnyHttpUrl
    night_url: AnyHttpUrl | None = None


class ThemeScene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canvas: Canvas
    # 방 안 캐릭터는 테마가 정한다 — character_frame(어디에) + character_url(무엇을).
    character_frame: Frame
    character_url: AnyHttpUrl
    layers: list[ThemeSceneLayer] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_layer_ids(self) -> "ThemeScene":
        ids = [layer.id for layer in self.layers]
        if len(ids) != len(set(ids)):
            raise ValueError("테마 안의 레이어 id는 고유해야 합니다.")
        return self


class ProductAssets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thumbnail_url: AnyHttpUrl
    detail_url: AnyHttpUrl
    scene: ThemeScene | None = None
    # 착용 아이템은 upright 포즈에만 입힌다. 방 안 포즈는 테마마다 달라서
    # 상품당 하나뿐인 레이어 URL로는 표현할 수 없다.
    upright_layer_url: AnyHttpUrl | None = None


class ShopProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: PublicID
    name: str = Field(min_length=1)
    slot: Literal["theme", "head", "neck", "body"]
    price_hay: int | None = Field(ge=0)
    owned: bool
    equipped: bool
    asset_version: int = Field(ge=1)
    assets: ProductAssets

    @model_validator(mode="after")
    def validate_assets_for_slot(self) -> "ShopProduct":
        assets = self.assets
        if self.slot == "theme":
            if assets.scene is None:
                raise ValueError("테마 상품에는 scene이 필요합니다.")
            if assets.upright_layer_url is not None:
                raise ValueError("테마 상품에는 착용 레이어 URL을 보낼 수 없습니다.")
        else:
            if assets.scene is not None:
                raise ValueError("착용 상품에는 scene을 보낼 수 없습니다.")
            if assets.upright_layer_url is None:
                raise ValueError("착용 상품에는 upright 레이어 URL이 필요합니다.")
        return self


class ProductsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    themes: list[ShopProduct]
    items: list[ShopProduct]


class InventoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[ShopProduct]


class EquipmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme_id: PublicID
    head_id: PublicID | None
    neck_id: PublicID | None
    body_id: PublicID | None


class PurchaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: PublicID
    order_id: str
    price_hay: int = Field(ge=0)
    balance_after: int = Field(ge=0)


class PurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: PublicID


class EquipmentPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 4슬롯 모두 필수(전체 교체). 테마는 해제할 수 없다.
    theme_id: PublicID
    head_id: PublicID | None
    neck_id: PublicID | None
    body_id: PublicID | None
