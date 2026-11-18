"""Domain model for Take2 (SPEC.md section 6).

Units are explicit in field names wherever a field carries a physical quantity.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class LimitReference(StrEnum):
    """Reference datum of a vertical airspace limit."""

    GND = "GND"  # above ground / surface
    MSL = "MSL"  # above mean sea level
    STD = "STD"  # flight level (1013.25 hPa)


class VerticalLimit(BaseModel):
    """A vertical airspace limit exactly as published."""

    value: float  # number as published (feet, metres or FL number)
    unit: Literal["FT", "M", "FL"]
    reference: LimitReference
