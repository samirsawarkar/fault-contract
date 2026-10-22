"""Pydantic copies of the refund agent's contracts, to test both engines on the same calls."""
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifyIdentity(Strict):
    customer_id: str


class LookupOrder(Strict):
    order_id: str


class SearchOrders(Strict):
    query: str


class IssueRefund(Strict):
    order_id: str
    amount: float
    customer_id: str


class Verified(BaseModel):
    verified: bool


class Order(BaseModel):
    order_id: str
    customer_id: str
    charges: list[float]
    status: Literal["pending", "shipped", "delivered"]


SearchResults = list[Order]


class Refunded(BaseModel):
    refunded: float
    order_id: str
