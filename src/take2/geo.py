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
from typing import Self

from pyproj import Geod, Transformer
from shapely import Point, Polygon

from take2.models import Route, Waypoint

M_PER_NM = 1852.0
DEFAULT_STEP_NM = 0.25

# Legs shorter than this are treated as zero length. The geodesic distance between two
# identical points can come out as floating-point noise (about 1e-12 m) instead of 0.
_MIN_LEG_M = 1e-3

_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True, slots=True)
class RouteSample:
    """A point of the densified route."""

    lon: float
    lat: float
    dist_nm: float  # cumulative distance from the route start
    alt_ft: float  # planned altitude on QNH: the start altitude of the leg (flat legs)


def geodesic_distance_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Return the WGS84 geodesic distance in metres between two points."""
    _, _, dist_m = _GEOD.inv(lon1, lat1, lon2, lat2)
    return dist_m


def densify_route(route: Route, step_nm: float = DEFAULT_STEP_NM) -> list[RouteSample]:
    """Sample the route along its geodesic legs, at most ``step_nm`` apart.

    Samples are evenly spaced within each leg and every waypoint is a sample. Altitude along
    a leg is the altitude of the leg's start waypoint (v0.1 has no climbs or descents), so
    the final point carries the altitude of the last leg. Zero-length legs add no samples.
    """
    if not step_nm > 0:
        raise ValueError(f"step_nm must be positive, got {step_nm}")

    samples: list[RouteSample] = []
    leg_start_nm = 0.0
    alt_ft = 0.0
    for start, end in pairwise(route.waypoints):
        alt_ft = _leg_altitude_ft(start)
        leg_m = geodesic_distance_m(start.lon, start.lat, end.lon, end.lat)
        if leg_m < _MIN_LEG_M:
            continue
        leg_nm = leg_m / M_PER_NM
        intervals = math.ceil(leg_nm / step_nm)
        if intervals:
            samples.append(RouteSample(start.lon, start.lat, leg_start_nm, alt_ft))
            # Geod.npts rejects a count of 0, so a leg shorter than one step skips it.
            interior = (
                _GEOD.npts(start.lon, start.lat, end.lon, end.lat, intervals - 1)
                if intervals > 1
                else []
            )
            samples.extend(
                RouteSample(lon, lat, leg_start_nm + i * leg_nm / intervals, alt_ft)
                for i, (lon, lat) in enumerate(interior, start=1)
            )
        leg_start_nm += leg_nm

    last = route.waypoints[-1]
    samples.append(RouteSample(last.lon, last.lat, leg_start_nm, alt_ft))
    return samples


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
