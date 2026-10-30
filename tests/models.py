"""Pydantic models the tests point contracts at."""
from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class Refund(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    amount: float
    customer_id: str


class Count(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int


class Shipping(BaseModel):
    when: date
    mode: Literal["air", "ground"] = "ground"


class Color(str, Enum):
    red = "red"
    blue = "blue"


class Item(BaseModel):
    sku: str
    qty: int


class StrictCount(BaseModel):
    model_config = ConfigDict(strict=True)
    n: int


class Token(BaseModel):
    token: str

    @field_validator("token")
    @classmethod
    def no_tokens(cls, v: str) -> str:
        raise ValueError("invalid token " + v)


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    items: list[Item]
    color: Color
