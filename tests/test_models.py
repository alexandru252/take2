"""Tests for the validation rules of the domain model (SPEC.md section 6)."""

import math

import pytest
from pydantic import ValidationError

from take2.models import (
    MAX_ROUTE_WAYPOINTS,
    Airspace,
    Event,
    LimitReference,
    Route,
    VerticalLimit,
    Waypoint,
)

SQUARE_LONLAT = [(25.9, 44.4), (26.1, 44.4), (26.1, 44.6), (25.9, 44.6), (25.9, 44.4)]


def make_airspace(**overrides: object) -> Airspace:
    fields: dict[str, object] = {
        "id": "FIC-TMA",
        "name": "Fictional TMA",
        "type": "TMA",
        "icao_class": "D",
        "lower": VerticalLimit(value=65, unit="FL", reference=LimitReference.STD),
        "upper": VerticalLimit(value=195, unit="FL", reference=LimitReference.STD),
        "polygon_lonlat": SQUARE_LONLAT,
    }
    fields.update(overrides)
    return Airspace(**fields)


def make_event(**overrides: object) -> Event:
    fields: dict[str, object] = {
        "airspace_id": "FIC-TMA",
        "airspace_name": "Fictional TMA",
        "kind": "WARNING",
        "start_nm": 3.0,
        "end_nm": 7.5,
        "min_lateral_nm": 0.0,
        "min_vertical_ft": 136.4,
    }
    fields.update(overrides)
    return Event(**fields)


# --- Waypoint ----------------------------------------------------------------------


@pytest.mark.parametrize(("lat", "lon"), [(-90.0, -180.0), (90.0, 180.0), (0.0, 0.0)])
def test_waypoint_accepts_coordinates_on_the_valid_range_limits(lat: float, lon: float) -> None:
    waypoint = Waypoint(name="P", lat=lat, lon=lon)

    assert (waypoint.lat, waypoint.lon) == (lat, lon)


def test_waypoint_altitude_is_optional() -> None:
    assert Waypoint(name="P", lat=44.5, lon=26.0).alt_ft is None


@pytest.mark.parametrize("lat", [90.0001, -90.0001, math.nan, math.inf])
def test_waypoint_rejects_invalid_latitude(lat: float) -> None:
    with pytest.raises(ValidationError, match="lat"):
        Waypoint(name="P", lat=lat, lon=26.0)


@pytest.mark.parametrize("lon", [180.0001, -180.0001, math.nan, -math.inf])
def test_waypoint_rejects_invalid_longitude(lon: float) -> None:
    with pytest.raises(ValidationError, match="lon"):
        Waypoint(name="P", lat=44.5, lon=lon)


@pytest.mark.parametrize("alt_ft", [math.nan, math.inf])
def test_waypoint_rejects_non_finite_altitude(alt_ft: float) -> None:
    with pytest.raises(ValidationError, match="alt_ft"):
        Waypoint(name="P", lat=44.5, lon=26.0, alt_ft=alt_ft)


# --- Route -------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1])
def test_route_needs_at_least_two_waypoints(count: int) -> None:
    waypoints = [Waypoint(name=f"P{i}", lat=44.5, lon=26.0 + i) for i in range(count)]

    with pytest.raises(ValidationError, match="at least 2"):
        Route(waypoints=waypoints)


def test_route_rejects_more_than_the_maximum_number_of_waypoints() -> None:
    waypoint = Waypoint(name="P", lat=44.5, lon=26.0)

    with pytest.raises(ValidationError, match="at most"):
        Route(waypoints=[waypoint] * (MAX_ROUTE_WAYPOINTS + 1))


def test_route_with_two_waypoints_is_valid() -> None:
    route = Route(
        waypoints=[Waypoint(name="A", lat=44.5, lon=26.0), Waypoint(name="B", lat=44.6, lon=26.1)]
    )

    assert [w.name for w in route.waypoints] == ["A", "B"]


# --- Airspace ----------------------------------------------------------------------


def test_airspace_defaults_to_not_fictional() -> None:
    assert make_airspace().fictional is False


def test_airspace_rejects_an_open_polygon_ring() -> None:
    with pytest.raises(ValidationError, match="closed"):
        make_airspace(polygon_lonlat=SQUARE_LONLAT[:-1])


def test_airspace_rejects_a_ring_with_fewer_than_four_positions() -> None:
    with pytest.raises(ValidationError, match="at least 4"):
        make_airspace(polygon_lonlat=[(25.9, 44.4), (26.1, 44.4), (25.9, 44.4)])


@pytest.mark.parametrize("icao_class", ["A", "D", "G", None])
def test_airspace_accepts_icao_classes_a_to_g_or_none(icao_class: str | None) -> None:
    assert make_airspace(icao_class=icao_class).icao_class == icao_class


@pytest.mark.parametrize("icao_class", ["H", "d", "", "AB"])
def test_airspace_rejects_unknown_icao_class(icao_class: str) -> None:
    with pytest.raises(ValidationError, match="icao_class"):
        make_airspace(icao_class=icao_class)


@pytest.mark.parametrize(
    "bad_position",
    [
        (244.4, 44.4),
        (-180.5, 44.4),
        (25.9, 95.0),
        (25.9, -90.5),
        (math.nan, 44.4),
        (25.9, math.inf),
    ],
)
def test_airspace_rejects_invalid_polygon_coordinates(bad_position: tuple[float, float]) -> None:
    ring = [SQUARE_LONLAT[0], bad_position, *SQUARE_LONLAT[2:]]

    with pytest.raises(ValidationError, match="polygon_lonlat"):
        make_airspace(polygon_lonlat=ring)


# --- Event -------------------------------------------------------------------------


def test_event_defaults_to_not_approximate() -> None:
    assert make_event().approximate is False


@pytest.mark.parametrize("field", ["start_nm", "min_lateral_nm", "min_vertical_ft"])
def test_event_rejects_negative_distances(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        make_event(**{field: -0.1})


def test_event_rejects_end_before_start() -> None:
    with pytest.raises(ValidationError, match="end_nm"):
        make_event(start_nm=5.0, end_nm=4.9)


def test_event_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError, match="kind"):
        make_event(kind="DANGER")


@pytest.mark.parametrize(
    ("field", "value"),
    [("end_nm", math.nan), ("min_lateral_nm", math.inf), ("min_vertical_ft", math.nan)],
)
def test_event_rejects_non_finite_distances(field: str, value: float) -> None:
    with pytest.raises(ValidationError, match=field):
        make_event(**{field: value})


# --- VerticalLimit -----------------------------------------------------------------


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_vertical_limit_rejects_non_finite_value(value: float) -> None:
    # NaN would make every altitude comparison False, so an infringement would read as clear.
    with pytest.raises(ValidationError, match="value"):
        VerticalLimit(value=value, unit="FL", reference=LimitReference.STD)
