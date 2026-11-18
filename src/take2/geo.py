"""Geometry on the WGS84 ellipsoid and in a local plane (SPEC.md section 7.3).

- Distances along the route are WGS84 geodesics (``pyproj.Geod``).
- Lateral distances to airspaces are measured in a local azimuthal equidistant (AEQD)
  projection centred on the route midpoint. AEQD keeps distances and directions from its
  centre exact; elsewhere the error stays small for routes up to about 500 km.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import NamedTuple, Self

import numpy as np
from pyproj import Geod, Transformer
from shapely import Point, Polygon

from take2.models import Route, Waypoint

M_PER_NM = 1852.0
DEFAULT_STEP_NM = 0.25

# Limits that keep work bounded for untrusted routes (a 239-byte CSV of near-antipodal legs
# used to produce 863,121 samples). A 1,000 NM route at the default step is 4,000 samples
# plus one per leg; the sample limit only stops unreasonably small steps.
MAX_ROUTE_NM = 1000.0
MAX_ROUTE_SAMPLES = 100_000

# Legs shorter than this are treated as zero length. The geodesic distance between two
# identical points can come out as floating-point noise (about 1e-12 m) instead of 0.
_MIN_LEG_M = 1e-3

_GEOD = Geod(ellps="WGS84")


class _Leg(NamedTuple):
    start: Waypoint
    end: Waypoint
    alt_ft: float
    length_nm: float
    intervals: int


@dataclass(frozen=True, slots=True)
class RouteSample:
    """A point of the densified route."""

    lon: float
    lat: float
    dist_nm: float  # cumulative distance from the route start
    alt_ft: float  # planned altitude on QNH: the start altitude of the leg (flat legs)
    leg_index: int  # index of the leg (waypoint i to i+1) the sample belongs to


def geodesic_distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Return the WGS84 geodesic distance in metres between two points."""
    _, _, dist_m = _GEOD.inv(lon1, lat1, lon2, lat2)
    return dist_m


def densify_route(route: Route, step_nm: float = DEFAULT_STEP_NM) -> list[RouteSample]:
    """Sample the route along its geodesic legs, at most ``step_nm`` apart.

    Samples are evenly spaced within each leg and every waypoint is a sample. Altitude along
    a leg is the altitude of the leg's start waypoint (v0.1 has no climbs or descents), so
    the final point carries the altitude of the last leg. Zero-length legs add no samples.

    Raises ``ValueError`` for a route longer than ``MAX_ROUTE_NM`` or one that would need
    more than ``MAX_ROUTE_SAMPLES`` samples, before any sample is created.
    """
    if not (math.isfinite(step_nm) and step_nm > 0):
        raise ValueError(f"step_nm must be a positive finite number, got {step_nm}")

    legs = [_measure_leg(start, end, step_nm) for start, end in pairwise(route.waypoints)]
    route_nm = sum(leg.length_nm for leg in legs)
    if route_nm > MAX_ROUTE_NM:
        raise ValueError(
            f"the route is {route_nm:.0f} NM long; Take2 supports routes up to "
            f"{MAX_ROUTE_NM:.0f} NM"
        )
    sample_count = sum(leg.intervals for leg in legs) + 1
    if sample_count > MAX_ROUTE_SAMPLES:
        raise ValueError(
            f"the route would need {sample_count:,} samples (limit {MAX_ROUTE_SAMPLES:,}); "
            "use a larger step"
        )

    samples: list[RouteSample] = []
    leg_start_nm = 0.0
    for leg_index, leg in enumerate(legs):
        if leg.intervals:
            start, end = leg.start, leg.end
            samples.append(RouteSample(start.lon, start.lat, leg_start_nm, leg.alt_ft, leg_index))
            # Geod.npts rejects a count of 0, so a leg shorter than one step skips it.
            interior = (
                _GEOD.npts(start.lon, start.lat, end.lon, end.lat, leg.intervals - 1)
                if leg.intervals > 1
                else []
            )
            samples.extend(
                RouteSample(
                    lon,
                    lat,
                    leg_start_nm + i * leg.length_nm / leg.intervals,
                    leg.alt_ft,
                    leg_index,
                )
                for i, (lon, lat) in enumerate(interior, start=1)
            )
        leg_start_nm += leg.length_nm

    last = route.waypoints[-1]
    samples.append(RouteSample(last.lon, last.lat, leg_start_nm, legs[-1].alt_ft, len(legs) - 1))
    return samples


def _measure_leg(start: Waypoint, end: Waypoint, step_nm: float) -> _Leg:
    """Length and number of sample intervals of one leg (0 for a zero-length leg)."""
    alt_ft = _leg_altitude_ft(start)
    leg_m = geodesic_distance_m(start.lon, start.lat, end.lon, end.lat)
    if leg_m < _MIN_LEG_M:
        return _Leg(start, end, alt_ft, 0.0, 0)
    length_nm = leg_m / M_PER_NM
    ratio = length_nm / step_nm
    # Beyond the sample limit the route is rejected anyway; saturating also avoids
    # math.ceil(inf), which raises OverflowError for a vanishingly small step.
    intervals = math.ceil(ratio) if ratio <= MAX_ROUTE_SAMPLES else MAX_ROUTE_SAMPLES + 1
    return _Leg(start, end, alt_ft, length_nm, intervals)


def route_midpoint(route: Route) -> tuple[float, float]:
    """Return ``(lon, lat)`` of the point halfway along the route, measured along its legs.

    Every route point is then at most half the route length from this point.
    """
    legs = []
    for start, end in pairwise(route.waypoints):
        azimuth_deg, _, leg_m = _GEOD.inv(start.lon, start.lat, end.lon, end.lat)
        legs.append((start, azimuth_deg, leg_m))

    remaining_m = sum(leg_m for _, _, leg_m in legs) / 2
    for start, azimuth_deg, leg_m in legs:
        if remaining_m <= leg_m:
            lon, lat, _ = _GEOD.fwd(start.lon, start.lat, azimuth_deg, remaining_m)
            return lon, lat
        remaining_m -= leg_m
    last = route.waypoints[-1]  # reached only through floating-point rounding
    return last.lon, last.lat


class LocalProjection:
    """Local azimuthal equidistant plane in metres (x east, y north) centred on a point."""

    def __init__(self, center_lon: float, center_lat: float) -> None:
        self.center_lon = center_lon
        self.center_lat = center_lat
        self._to_xy = Transformer.from_crs(
            "EPSG:4326",
            f"+proj=aeqd +lat_0={center_lat:.12f} +lon_0={center_lon:.12f} +datum=WGS84 +units=m",
            always_xy=True,
        )

    @classmethod
    def for_route(cls, route: Route) -> Self:
        """Projection centred on the route midpoint (see ``route_midpoint``)."""
        lon, lat = route_midpoint(route)
        return cls(center_lon=lon, center_lat=lat)

    def to_xy_m(self, lon: float, lat: float) -> tuple[float, float]:
        """Project a WGS84 point to local ``(x_m, y_m)``."""
        x_m, y_m = self._to_xy.transform(lon, lat)
        return x_m, y_m

    def to_xy_m_many(self, lons: Sequence[float], lats: Sequence[float]) -> np.ndarray:
        """Project many WGS84 points at once; returns an array of shape ``(n, 2)`` in metres."""
        xs_m, ys_m = self._to_xy.transform(list(lons), list(lats))
        return np.column_stack([xs_m, ys_m])

    def point(self, lon: float, lat: float) -> Point:
        """Project a WGS84 point to a shapely ``Point`` in local metres."""
        return Point(self.to_xy_m(lon, lat))

    def polygon(self, ring_lonlat: Sequence[tuple[float, float]]) -> Polygon:
        """Project a WGS84 ``(lon, lat)`` ring to a shapely ``Polygon`` in local metres."""
        lons = [lon for lon, _ in ring_lonlat]
        lats = [lat for _, lat in ring_lonlat]
        xs_m, ys_m = self._to_xy.transform(lons, lats)
        return Polygon(zip(xs_m, ys_m, strict=True))


def lateral_distance_nm(polygon_xy: Polygon, point_xy: Point) -> float:
    """Return the horizontal distance (NM) from a point to an airspace polygon.

    0 when the point is inside the polygon or on its boundary, otherwise the distance to the
    nearest point of the boundary. Both geometries are in the same local metric projection.
    """
    return polygon_xy.distance(point_xy) / M_PER_NM


def _leg_altitude_ft(waypoint: Waypoint) -> float:
    if waypoint.alt_ft is None:
        raise ValueError(
            f"waypoint {waypoint.name!r} has no altitude: give a cruise altitude for the route"
        )
    return waypoint.alt_ft
