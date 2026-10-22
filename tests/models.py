"""Pydantic models the tests point contracts at."""
from datetime import date
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict


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


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    items: list[Item]
    color: Color
