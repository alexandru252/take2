"""Domain model for Take2 (SPEC.md section 6).

Units are explicit in field names wherever a field carries a physical quantity.
"""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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


class Airspace(BaseModel):
    """An airspace volume: a WGS84 polygon between a lower and an upper limit."""

    id: str
    name: str
    type: str  # CTR, TMA, CTA, ATZ, TMZ, RMZ, P, R, D, ...
    icao_class: str | None = Field(pattern=r"^[A-G]$")  # "A".."G" or None
    lower: VerticalLimit
    upper: VerticalLimit
    polygon_lonlat: list[tuple[float, float]]  # WGS84 (lon, lat), closed ring
    fictional: bool = False

    @field_validator("polygon_lonlat")
    @classmethod
    def _check_closed_ring(cls, ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(ring) < 4:
            raise ValueError(f"a polygon ring needs at least 4 positions, got {len(ring)}")
        if ring[0] != ring[-1]:
            raise ValueError("the polygon ring must be closed (first position == last)")
        return ring


class Waypoint(BaseModel):
    """A route point in WGS84 with an optional planned altitude."""

    model_config = ConfigDict(allow_inf_nan=False)

    name: str
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    alt_ft: float | None = None  # planned altitude AMSL on QNH


class Route(BaseModel):
    """An ordered list of at least two waypoints."""

    waypoints: list[Waypoint] = Field(min_length=2)


class Event(BaseModel):
    """Something that happens along the route with respect to one airspace."""

    airspace_id: str
    airspace_name: str
    kind: Literal["INFRINGEMENT", "WARNING", "REQUIREMENT", "INFO"]
    start_nm: float = Field(ge=0.0)  # distance along route
    end_nm: float
    min_lateral_nm: float = Field(ge=0.0)  # 0 when horizontally inside
    min_vertical_ft: float = Field(ge=0.0)  # 0 when inside the vertical band
    approximate: bool = False  # true if a GND-referenced limit was used without terrain data

    @model_validator(mode="after")
    def _check_end_not_before_start(self) -> Self:
        if self.end_nm < self.start_nm:
            raise ValueError(f"end_nm ({self.end_nm}) is before start_nm ({self.start_nm})")
        return self
