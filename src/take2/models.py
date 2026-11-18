"""Domain model for Take2 (SPEC.md section 6).

Units are explicit in field names wherever a field carries a physical quantity.
"""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Upper bound on route size, so a small crafted file cannot exhaust memory downstream.
# 50,000 points is about 14 hours of a 1 Hz GPS track.
MAX_ROUTE_WAYPOINTS = 50_000
# Upper bound on airspace polygon size; real openAIP rings have at most a few thousand points,
# and analysis cost grows with route samples times vertices.
MAX_POLYGON_POSITIONS = 20_000

_FT_PER_UNIT = {"FT": 1.0, "M": 3.28084, "FL": 100.0}

Latitude = Annotated[float, Field(ge=-90.0, le=90.0, allow_inf_nan=False)]
Longitude = Annotated[float, Field(ge=-180.0, le=180.0, allow_inf_nan=False)]


class LimitReference(StrEnum):
    """Reference datum of a vertical airspace limit."""

    GND = "GND"  # above ground / surface
    MSL = "MSL"  # above mean sea level
    STD = "STD"  # flight level (1013.25 hPa)


class VerticalLimit(BaseModel):
    """A vertical airspace limit exactly as published."""

    model_config = ConfigDict(allow_inf_nan=False)

    value: float  # number as published (feet, metres or FL number)
    unit: Literal["FT", "M", "FL"]
    reference: LimitReference

    @model_validator(mode="after")
    def _check_flight_level_reference(self) -> Self:
        if self.unit == "FL" and self.reference is not LimitReference.STD:
            raise ValueError("a flight level limit must use the STD reference")
        return self

    def value_ft(self) -> float:
        """The published value in feet, in this limit's own reference."""
        return self.value * _FT_PER_UNIT[self.unit]


class Airspace(BaseModel):
    """An airspace volume: a WGS84 polygon between a lower and an upper limit."""

    id: str
    name: str = Field(max_length=256)
    type: str  # CTR, TMA, CTA, ATZ, TMZ, RMZ, P, R, D, ...
    icao_class: str | None = Field(pattern=r"^[A-G]$")  # "A".."G" or None
    lower: VerticalLimit
    upper: VerticalLimit
    polygon_lonlat: list[tuple[Longitude, Latitude]] = Field(
        max_length=MAX_POLYGON_POSITIONS
    )  # WGS84 (lon, lat), closed ring
    fictional: bool = False

    @field_validator("polygon_lonlat")
    @classmethod
    def _check_closed_ring(cls, ring: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(ring) < 4:
            raise ValueError(f"a polygon ring needs at least 4 positions, got {len(ring)}")
        if ring[0] != ring[-1]:
            raise ValueError("the polygon ring must be closed (first position == last)")
        return ring

    @model_validator(mode="after")
    def _check_limits_order(self) -> Self:
        # Only comparable when both limits use the same reference; otherwise the order depends
        # on QNH or terrain on the day.
        same = self.lower.reference is self.upper.reference
        if same and self.lower.value_ft() > self.upper.value_ft():
            raise ValueError("the lower limit is above the upper limit")
        return self


class Waypoint(BaseModel):
    """A route point in WGS84 with an optional planned altitude."""

    model_config = ConfigDict(allow_inf_nan=False)

    name: str = Field(max_length=256)
    lat: Latitude
    lon: Longitude
    alt_ft: float | None = None  # planned altitude AMSL on QNH


class Route(BaseModel):
    """An ordered list of at least two (and at most MAX_ROUTE_WAYPOINTS) waypoints."""

    waypoints: list[Waypoint] = Field(min_length=2, max_length=MAX_ROUTE_WAYPOINTS)


class Event(BaseModel):
    """Something that happens along the route with respect to one airspace."""

    model_config = ConfigDict(allow_inf_nan=False)

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
