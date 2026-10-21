"""Pharmacy adapters. CareLoop only ever talks to the `Pharmacy` interface, so a real refill API
(a retail pharmacy, a mail-order service) can replace the simulator without touching the tools.

The bundled `SimulatedPharmacy` is deterministic so the demo and the tests are reproducible.
It never charges anything and says so in every order it returns (`simulated: true`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


@dataclass(frozen=True)
class Price:
    unit_cents: int
    delivery_cents: int


class Pharmacy(Protocol):
    name: str

    def price(self, item_id: str) -> Price: ...

    def place_order(self, item_id: str, quantity: int, total_cents: int, now: datetime) -> dict: ...


class SimulatedPharmacy:
    name = "CareLoop simulated pharmacy"
    CATALOG = {  # item_id -> (unit price in cents, delivery fee in cents)
        "lisinopril-10mg": (12, 499),
        "metformin-500mg": (9, 499),
        "atorvastatin-20mg": (15, 499),
        "vitamin-d3-1000iu": (6, 299),
    }
    DEFAULT = (20, 499)

    def __init__(self) -> None:
        self._n = 0

    def price(self, item_id: str) -> Price:
        unit, fee = self.CATALOG.get(item_id, self.DEFAULT)
        return Price(unit, fee)

    def place_order(self, item_id: str, quantity: int, total_cents: int, now: datetime) -> dict:
        self._n += 1
        eta = (now + timedelta(days=1)).replace(hour=14, minute=0, second=0)
        return {"confirmation": f"SIM-{now:%m%d}-{self._n:03d}", "eta": eta.isoformat(), "simulated": True,
                "pharmacy": self.name}
