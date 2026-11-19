"""Tests for take2.analysis, using the reference cases from SPEC.md section 7.4."""

import math
import os
import threading
import time
from pathlib import Path

import pytest
from pyproj import Geod

from take2 import analysis
from take2.analysis import (
    DEFAULT_SEVERITY_FILE,
    MAX_EVENTS,
    SeverityRules,
    analyze_route,
    load_severity_rules,
)
from take2.geo import densify_route, geodesic_distance_m
from take2.models import Airspace, LimitReference, Route, VerticalLimit, Waypoint

M_PER_NM = 1852.0
TOLERANCE_FT = 0.5  # altimetry tolerance, SPEC.md 7.1
TOLERANCE_NM = 0.01
CENTER = (26.0, 44.5)  # lon, lat
HALF_SIZE_M = 10_000  # 20 km x 20 km square

_GEOD = Geod(ellps="WGS84")


def offset(lon: float, lat: float, azimuth_deg: float, dist_m: float) -> tuple[float, float]:
    lon2, lat2, _ = _GEOD.fwd(lon, lat, azimuth_deg, dist_m)
    return lon2, lat2


def square_ring(lon: float, lat: float, half_size_m: float) -> list[tuple[float, float]]:
    _, lat_n = offset(lon, lat, 0, half_size_m)
    _, lat_s = offset(lon, lat, 180, half_size_m)
    lon_e, _ = offset(lon, lat, 90, half_size_m)
    lon_w, _ = offset(lon, lat, 270, half_size_m)
    return [(lon_w, lat_s), (lon_e, lat_s), (lon_e, lat_n), (lon_w, lat_n), (lon_w, lat_s)]


def fl(value: float) -> VerticalLimit:
    return VerticalLimit(value=value, unit="FL", reference=LimitReference.STD)


def ft_msl(value: float) -> VerticalLimit:
    return VerticalLimit(value=value, unit="FT", reference=LimitReference.MSL)


def ft_gnd(value: float) -> VerticalLimit:
    return VerticalLimit(value=value, unit="FT", reference=LimitReference.GND)


def airspace(**overrides: object) -> Airspace:
    fields: dict[str, object] = {
        "id": "FIC-TMA",
        "name": "Fictional TMA",
        "type": "TMA",
        "icao_class": "D",
        "lower": fl(65),
        "upper": fl(195),
        "polygon_lonlat": square_ring(*CENTER, HALF_SIZE_M),
        "fictional": True,
    }
    fields.update(overrides)
    return Airspace(**fields)


def route(*points: tuple[float, float], alt_ft: float) -> Route:
    return Route(
        waypoints=[
            Waypoint(name=f"P{i}", lon=lon, lat=lat, alt_ft=alt_ft)
            for i, (lon, lat) in enumerate(points)
        ]
    )


def route_through_centre(alt_ft: float) -> Route:
    """West to east through the centre, from 30 km west to 30 km east of it."""
    return route(offset(*CENTER, 270, 30_000), offset(*CENTER, 90, 30_000), alt_ft=alt_ft)


def route_north_of_square(offset_nm: float, alt_ft: float) -> Route:
    """Parallel to the north side, ``offset_nm`` outside it, 10 km past each corner."""
    ring = square_ring(*CENTER, HALF_SIZE_M)
    north_west, north_east = ring[3], ring[2]
    start = offset(*offset(*north_west, 0, offset_nm * M_PER_NM), 270, 10_000)
    end = offset(*offset(*north_east, 0, offset_nm * M_PER_NM), 90, 10_000)
    return route(start, end, alt_ft=alt_ft)


CTR = {
    "id": "FIC-CTR",
    "name": "Fictional CTR",
    "type": "CTR",
    "icao_class": "D",
    "lower": ft_gnd(0),  # SFC
    "upper": ft_msl(3500),
}


# --- SPEC.md 7.4 reference cases: TMA base FL65 over a route at 6000 ft -------------


def test_tma_at_standard_qnh_gives_no_event() -> None:
    # Base 6500.0 ft on QNH: the route is 500 ft below, more than the 200 ft margin.
    assert analyze_route(route_through_centre(6000), [airspace()], qnh_hpa=1013.25) == []


def test_tma_at_qnh_1000_gives_one_warning_136_ft_below() -> None:
    events = analyze_route(route_through_centre(6000), [airspace()], qnh_hpa=1000.0)

    assert [e.kind for e in events] == ["WARNING"]
    assert events[0].min_vertical_ft == pytest.approx(136.4, abs=TOLERANCE_FT)
    assert events[0].min_lateral_nm == 0.0


def test_tma_at_qnh_993_gives_an_infringement() -> None:
    events = analyze_route(route_through_centre(6000), [airspace()], qnh_hpa=993.0)

    infringements = [e for e in events if e.kind == "INFRINGEMENT"]
    assert len(infringements) == 1
    assert infringements[0].min_lateral_nm == 0.0
    assert infringements[0].min_vertical_ft == 0.0
    # Inside the square: from 20 km to 40 km along the route.
    assert infringements[0].start_nm == pytest.approx(20_000 / M_PER_NM, abs=0.13)
    assert infringements[0].end_nm == pytest.approx(40_000 / M_PER_NM, abs=0.13)


def test_tma_infringement_is_flanked_by_warnings_within_2_nm() -> None:
    events = analyze_route(route_through_centre(6000), [airspace()], qnh_hpa=993.0)

    assert [e.kind for e in events] == ["WARNING", "INFRINGEMENT", "WARNING"]
    warning_before = events[0]
    assert warning_before.end_nm < events[1].start_nm
    assert events[1].start_nm - warning_before.start_nm == pytest.approx(2.0, abs=0.26)


# --- SPEC.md 7.4 reference cases: lateral margin to a CTR --------------------------


def test_route_1_5_nm_outside_a_ctr_gives_a_warning() -> None:
    events = analyze_route(route_north_of_square(1.5, 2500), [airspace(**CTR)], qnh_hpa=1013.25)

    assert [e.kind for e in events] == ["WARNING"]
    assert events[0].min_lateral_nm == pytest.approx(1.5, abs=TOLERANCE_NM)
    assert events[0].min_vertical_ft == 0.0
    assert events[0].approximate is False  # SFC is exact


def test_route_2_5_nm_outside_a_ctr_gives_no_event() -> None:
    assert analyze_route(route_north_of_square(2.5, 2500), [airspace(**CTR)], 1013.25) == []


# --- coverage between samples ------------------------------------------------------


def test_clipping_a_corner_between_samples_is_an_infringement() -> None:
    # Found in the M1-4 QA review: a leg that cuts a corner with an inside chord of about
    # 0.2 NM can have no sample inside. The whole route between samples must be checked.
    ring = square_ring(*CENTER, HALF_SIZE_M)
    north_east = ring[2]
    # A straight line at 45 degrees passing 0.07 NM inside the corner (chord ~0.2 NM).
    inside_point = offset(*north_east, 225, 0.07 * M_PER_NM)
    start = offset(*inside_point, 315, 15_000)
    end = offset(*inside_point, 135, 15_000)

    events = analyze_route(route(start, end, alt_ft=2500), [airspace(**CTR)], qnh_hpa=1013.25)

    assert "INFRINGEMENT" in [e.kind for e in events]


# --- severity mapping (config/severity.yaml) ---------------------------------------


@pytest.mark.parametrize(
    ("overrides", "kinds_inside", "kinds_close"),
    [
        ({"type": "TMA", "icao_class": "C"}, ["WARNING", "INFRINGEMENT", "WARNING"], ["WARNING"]),
        ({"type": "P", "icao_class": None}, ["WARNING", "INFRINGEMENT", "WARNING"], ["WARNING"]),
        ({"type": "R", "icao_class": None}, ["WARNING", "INFRINGEMENT", "WARNING"], ["WARNING"]),
        ({"type": "TMZ", "icao_class": None}, ["REQUIREMENT"], []),
        ({"type": "RMZ", "icao_class": "E"}, ["REQUIREMENT"], []),
        ({"type": "ATZ", "icao_class": "G"}, ["INFO"], []),
        ({"type": "D", "icao_class": None}, ["INFO"], []),
        ({"type": "CTA", "icao_class": "E"}, [], []),
    ],
    ids=["class-C-TMA", "prohibited", "restricted", "TMZ", "RMZ", "ATZ", "danger", "class-E"],
)
def test_severity_mapping(overrides: dict, kinds_inside: list[str], kinds_close: list[str]) -> None:
    space = airspace(lower=ft_gnd(0), upper=ft_msl(5000), **overrides)

    inside = analyze_route(route_through_centre(3000), [space], qnh_hpa=1013.25)
    close = analyze_route(route_north_of_square(1.5, 3000), [space], qnh_hpa=1013.25)

    assert [e.kind for e in inside] == kinds_inside
    assert [e.kind for e in close] == kinds_close


def test_repository_severity_file_matches_the_spec() -> None:
    rules = load_severity_rules(DEFAULT_SEVERITY_FILE)

    assert rules == SeverityRules(
        clearance_classes=frozenset({"A", "B", "C", "D"}),
        clearance_types=frozenset({"P", "R"}),
        requirement_types=frozenset({"TMZ", "RMZ"}),
        info_types=frozenset({"D", "ATZ"}),
    )


@pytest.mark.parametrize(
    "content",
    ["just text", "clearance_required: [A, B]", "info:\n  types: 5", "- a\n- b"],
    ids=["plain-text", "section-is-a-list", "types-not-a-list", "top-level-list"],
)
def test_malformed_severity_file_is_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "severity.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(path)


# --- vertical cases, margins and flags ---------------------------------------------


def test_route_just_above_the_upper_limit_is_a_warning() -> None:
    # Class D CTR up to 3500 ft MSL, route at 3600 ft: 100 ft above, within the 200 ft margin.
    events = analyze_route(route_through_centre(3600), [airspace(**CTR)], qnh_hpa=1013.25)

    assert [e.kind for e in events] == ["WARNING"]
    assert events[0].min_vertical_ft == pytest.approx(100.0)


def test_margins_are_configurable() -> None:
    events = analyze_route(
        route_north_of_square(2.5, 2500), [airspace(**CTR)], qnh_hpa=1013.25, lateral_margin_nm=3.0
    )

    assert [e.kind for e in events] == ["WARNING"]
    assert events[0].min_lateral_nm == pytest.approx(2.5, abs=TOLERANCE_NM)


def test_vertical_margin_is_configurable() -> None:
    # 500 ft below the base at standard QNH: a warning only with a margin of at least 500 ft.
    events = analyze_route(
        route_through_centre(6000), [airspace()], qnh_hpa=1013.25, vertical_margin_ft=600.0
    )

    assert [e.kind for e in events] == ["WARNING"]
    assert events[0].min_vertical_ft == pytest.approx(500.0, abs=TOLERANCE_FT)


def test_ground_referenced_limit_makes_the_event_approximate() -> None:
    space = airspace(type="CTR", lower=ft_gnd(0), upper=ft_gnd(3000))

    events = analyze_route(route_through_centre(2500), [space], qnh_hpa=1013.25)

    assert events and all(e.approximate for e in events)


def test_entering_twice_gives_two_events() -> None:
    # Two squares 20 km apart along the same line: two infringements, not one merged event.
    east_centre = offset(*CENTER, 90, 40_000)
    first = airspace(id="A1", name="West", type="CTR", lower=ft_gnd(0), upper=ft_msl(3500))
    second = airspace(
        id="A2",
        name="East",
        type="CTR",
        lower=ft_gnd(0),
        upper=ft_msl(3500),
        polygon_lonlat=square_ring(*east_centre, HALF_SIZE_M),
    )
    long_route = route(offset(*CENTER, 270, 30_000), offset(*east_centre, 90, 30_000), alt_ft=2500)

    events = analyze_route(long_route, [first, second], qnh_hpa=1013.25)

    infringements = [(e.airspace_id, e.kind) for e in events if e.kind == "INFRINGEMENT"]
    assert infringements == [("A1", "INFRINGEMENT"), ("A2", "INFRINGEMENT")]
    assert [e.start_nm for e in events] == sorted(e.start_nm for e in events)


def test_far_away_airspace_gives_no_event() -> None:
    far = airspace(id="FAR", polygon_lonlat=square_ring(30.0, 47.0, 5_000))

    assert analyze_route(route_through_centre(6000), [far], qnh_hpa=993.0) == []


def test_altitude_change_at_a_waypoint_uses_each_legs_altitude() -> None:
    # Leg A-B at 2500 ft ends at B, 1 NM west of the CTR; leg B-C climbs to 5000 ft, above the
    # CTR (3500 ft) plus margin. The warning must cover leg A-B right up to B (1.0 NM), and
    # nothing after B may be judged at 2500 ft.
    b = offset(*CENTER, 270, HALF_SIZE_M + M_PER_NM)
    a = offset(*b, 270, 20_000)
    c = offset(*CENTER, 90, 30_000)
    climbing = Route(
        waypoints=[
            Waypoint(name="A", lon=a[0], lat=a[1], alt_ft=2500),
            Waypoint(name="B", lon=b[0], lat=b[1], alt_ft=5000),
            Waypoint(name="C", lon=c[0], lat=c[1], alt_ft=5000),
        ]
    )

    events = analyze_route(climbing, [airspace(**CTR)], qnh_hpa=1013.25)

    assert [e.kind for e in events] == ["WARNING"]
    assert events[0].min_lateral_nm == pytest.approx(1.0, abs=TOLERANCE_NM)
    assert events[0].end_nm <= 20_000 / M_PER_NM


def test_clearance_wins_over_a_requirement_for_the_same_airspace() -> None:
    # A class C RMZ needs a clearance: the strictest matching rule decides.
    space = airspace(type="RMZ", icao_class="C", lower=ft_gnd(0), upper=ft_msl(5000))

    events = analyze_route(route_through_centre(3000), [space], qnh_hpa=1013.25)

    assert [e.kind for e in events] == ["WARNING", "INFRINGEMENT", "WARNING"]


# --- QA review of milestone 6 (mutation-tested) ------------------------------------

HALF_STEP_NM = 0.13  # samples are at most 0.25 NM apart; a boundary is at most half of that away


def kinds(events):
    return [e.kind for e in events]


# --- vertical boundaries (default 200 ft) ------------------------------------------


def test_exactly_200_ft_below_the_base_is_a_warning_201_ft_is_clear() -> None:
    # FL65 at QNH 1013.25 is exactly 6500.0 ft.
    at_margin = analyze_route(route_through_centre(6300), [airspace()], qnh_hpa=1013.25)
    beyond = analyze_route(route_through_centre(6299), [airspace()], qnh_hpa=1013.25)

    assert kinds(at_margin) == ["WARNING"]
    assert at_margin[0].min_vertical_ft == 200.0
    assert beyond == []


def test_exactly_200_ft_above_the_top_is_a_warning_201_ft_is_clear() -> None:
    at_margin = analyze_route(route_through_centre(3700), [airspace(**CTR)], qnh_hpa=1013.25)
    beyond = analyze_route(route_through_centre(3701), [airspace(**CTR)], qnh_hpa=1013.25)

    assert kinds(at_margin) == ["WARNING"]
    assert at_margin[0].min_vertical_ft == 200.0
    assert beyond == []


def test_configured_vertical_margin_applies_above_the_top_too() -> None:
    events = analyze_route(
        route_through_centre(3800), [airspace(**CTR)], qnh_hpa=1013.25, vertical_margin_ft=300.0
    )

    assert kinds(events) == ["WARNING"]
    assert events[0].min_vertical_ft == 300.0


@pytest.mark.parametrize(("space", "alt_ft"), [(airspace(), 6500), (airspace(**CTR), 3500)])
def test_flying_exactly_at_a_limit_is_inside(space, alt_ft) -> None:
    events = analyze_route(route_through_centre(alt_ft), [space], qnh_hpa=1013.25)

    assert kinds(events) == ["WARNING", "INFRINGEMENT", "WARNING"]


# --- lateral boundaries (default 2 NM) ---------------------------------------------


def test_default_lateral_margin_is_2_nm() -> None:
    # route_north_of_square's offset is ~0.008 NM short of the true lateral distance.
    inside_margin = analyze_route(route_north_of_square(1.98, 2500), [airspace(**CTR)], 1013.25)
    outside_margin = analyze_route(route_north_of_square(2.02, 2500), [airspace(**CTR)], 1013.25)

    assert kinds(inside_margin) == ["WARNING"]
    assert outside_margin == []


def test_lateral_distance_equal_to_the_margin_is_close() -> None:
    probe = analyze_route(
        route_north_of_square(1.99, 2500), [airspace(**CTR)], 1013.25, lateral_margin_nm=3.0
    )
    measured_nm = probe[0].min_lateral_nm

    events = analyze_route(
        route_north_of_square(1.99, 2500),
        [airspace(**CTR)],
        1013.25,
        lateral_margin_nm=measured_nm,
    )

    assert kinds(events) == ["WARNING"]
    assert events[0].min_lateral_nm == measured_nm


def test_passing_just_outside_is_a_warning_not_an_infringement() -> None:
    events = analyze_route(route_north_of_square(0.05, 2500), [airspace(**CTR)], 1013.25)

    assert kinds(events) == ["WARNING"]
    assert events[0].min_lateral_nm == pytest.approx(0.05, abs=TOLERANCE_NM)


# --- event fields ------------------------------------------------------------------


def three_waypoints(alt1_ft: float, alt2_ft: float) -> Route:
    a, b, c = offset(*CENTER, 270, 30_000), CENTER, offset(*CENTER, 90, 30_000)
    return Route(
        waypoints=[
            Waypoint(name="A", lon=a[0], lat=a[1], alt_ft=alt1_ft),
            Waypoint(name="B", lon=b[0], lat=b[1], alt_ft=alt2_ft),
            Waypoint(name="C", lon=c[0], lat=c[1], alt_ft=alt2_ft),
        ]
    )


def test_min_vertical_is_the_smallest_over_a_merged_event() -> None:
    # QNH 1000: base 6136.4 ft. Leg A-B 186.4 ft below, leg B-C 136.4 ft below: one warning.
    events = analyze_route(three_waypoints(5950, 6000), [airspace()], qnh_hpa=1000.0)

    assert kinds(events) == ["WARNING"]
    assert events[0].min_vertical_ft == pytest.approx(136.4, abs=TOLERANCE_FT)


def test_min_vertical_is_measured_within_its_own_event() -> None:
    # Leg A-B 186.4 ft below the base, leg B-C inside the TMA: the first warning keeps 186.4.
    events = analyze_route(three_waypoints(5950, 6200), [airspace()], qnh_hpa=1000.0)

    assert kinds(events)[:2] == ["WARNING", "INFRINGEMENT"]
    assert events[0].min_vertical_ft == pytest.approx(186.4, abs=TOLERANCE_FT)


def test_min_lateral_can_be_at_the_first_sample_of_an_event() -> None:
    # Route leaving from 1 NM west of the CTR and flying away from it.
    b = offset(*CENTER, 270, HALF_SIZE_M + M_PER_NM)
    events = analyze_route(
        route(b, offset(*b, 270, 20_000), alt_ft=2500), [airspace(**CTR)], 1013.25
    )

    assert kinds(events) == ["WARNING"]
    assert events[0].start_nm == 0.0
    assert events[0].min_lateral_nm == pytest.approx(1.0, abs=TOLERANCE_NM)


def test_infringement_boundaries_are_within_half_a_step() -> None:
    events = analyze_route(route_through_centre(6000), [airspace()], qnh_hpa=993.0)
    infringement = next(e for e in events if e.kind == "INFRINGEMENT")

    assert infringement.start_nm == pytest.approx(20_000 / M_PER_NM, abs=HALF_STEP_NM)
    assert infringement.end_nm == pytest.approx(40_000 / M_PER_NM, abs=HALF_STEP_NM)


@pytest.mark.parametrize("extra_m", [0, 60, 120, 180, 240, 300, 360, 420])
def test_boundaries_within_half_a_step_for_several_sample_phases(extra_m: float) -> None:
    start = offset(*CENTER, 270, 30_000 + extra_m)
    events = analyze_route(
        route(start, offset(*CENTER, 90, 30_000), alt_ft=2500), [airspace(**CTR)], 1013.25
    )
    infringement = next(e for e in events if e.kind == "INFRINGEMENT")

    assert infringement.start_nm == pytest.approx((20_000 + extra_m) / M_PER_NM, abs=HALF_STEP_NM)
    assert infringement.end_nm == pytest.approx((40_000 + extra_m) / M_PER_NM, abs=HALF_STEP_NM)


def test_event_names_its_airspace() -> None:
    events = analyze_route(route_north_of_square(1.5, 2500), [airspace(**CTR)], 1013.25)

    assert [(e.airspace_id, e.airspace_name) for e in events] == [("FIC-CTR", "Fictional CTR")]


# --- ground-referenced limits ------------------------------------------------------


def gnd_1000_cta():
    return airspace(id="GL", type="CTA", icao_class="D", lower=ft_gnd(1000), upper=ft_msl(4500))


def test_ground_referenced_base_above_the_surface() -> None:
    warning = analyze_route(route_through_centre(900), [gnd_1000_cta()], qnh_hpa=1013.25)
    clear = analyze_route(route_through_centre(500), [gnd_1000_cta()], qnh_hpa=1013.25)

    assert kinds(warning) == ["WARNING"]
    assert warning[0].min_vertical_ft == 100.0
    assert warning[0].approximate is True
    assert clear == []


def test_ground_elevation_raises_a_ground_referenced_base() -> None:
    space = gnd_1000_cta()

    clear = analyze_route(route_through_centre(900), [space], 1013.25, ground_elev_ft=500)
    warning = analyze_route(route_through_centre(1400), [space], 1013.25, ground_elev_ft=500)

    assert clear == []
    assert kinds(warning) == ["WARNING"]
    assert warning[0].min_vertical_ft == 100.0


def test_ground_elevation_raises_a_ground_referenced_top() -> None:
    space = airspace(type="CTR", lower=ft_gnd(0), upper=ft_gnd(3000))

    events = analyze_route(route_through_centre(3300), [space], 1013.25, ground_elev_ft=500)

    assert "INFRINGEMENT" in kinds(events)


# --- parameters --------------------------------------------------------------------


def test_custom_rules_are_used() -> None:
    rules = SeverityRules(
        clearance_classes=frozenset({"E"}),
        clearance_types=frozenset(),
        requirement_types=frozenset(),
        info_types=frozenset(),
    )
    space = airspace(type="CTA", icao_class="E", lower=ft_gnd(0), upper=ft_msl(5000))

    events = analyze_route(route_through_centre(3000), [space], 1013.25, rules=rules)

    assert kinds(events) == ["WARNING", "INFRINGEMENT", "WARNING"]


def test_step_sets_the_sample_resolution() -> None:
    the_route = route_through_centre(6000)
    coarse = {s.dist_nm for s in densify_route(the_route, 5.0)}

    events = analyze_route(the_route, [airspace()], qnh_hpa=993.0, step_nm=5.0)

    assert "INFRINGEMENT" in kinds(events)
    assert all(e.start_nm in coarse and e.end_nm in coarse for e in events)


def test_requirement_wins_over_info_when_both_match() -> None:
    rules = SeverityRules(
        clearance_classes=frozenset(),
        clearance_types=frozenset(),
        requirement_types=frozenset({"TMZ"}),
        info_types=frozenset({"TMZ"}),
    )
    space = airspace(type="TMZ", icao_class=None, lower=ft_gnd(0), upper=ft_msl(5000))

    events = analyze_route(route_through_centre(3000), [space], 1013.25, rules=rules)

    assert kinds(events) == ["REQUIREMENT"]


# --- ordering ----------------------------------------------------------------------


def test_events_are_ordered_along_the_route_whatever_the_airspace_order() -> None:
    ctr = airspace(**CTR)
    tmz = airspace(
        id="FIC-TMZ",
        name="Fictional TMZ",
        type="TMZ",
        icao_class=None,
        lower=ft_gnd(0),
        upper=ft_msl(5000),
        polygon_lonlat=square_ring(*CENTER, 20_000),
    )

    events = analyze_route(route_through_centre(2500), [ctr, tmz], 1013.25)

    assert [(e.airspace_id, e.kind) for e in events] == [
        ("FIC-TMZ", "REQUIREMENT"),
        ("FIC-CTR", "WARNING"),
        ("FIC-CTR", "INFRINGEMENT"),
        ("FIC-CTR", "WARNING"),
    ]


def test_events_starting_together_are_ordered_by_airspace_id() -> None:
    first = airspace(id="B", type="CTR", lower=ft_gnd(0), upper=ft_msl(3500))
    second = airspace(id="A", type="CTR", lower=ft_gnd(0), upper=ft_msl(3500))

    events = analyze_route(route_through_centre(2500), [first, second], 1013.25)

    assert [e.airspace_id for e in events] == ["A", "B"] * 3


# --- route shapes ------------------------------------------------------------------


def test_route_entirely_inside_a_large_airspace_is_one_infringement() -> None:
    big = airspace(
        id="BIG",
        type="CTR",
        lower=ft_gnd(0),
        upper=ft_msl(3500),
        polygon_lonlat=square_ring(*CENTER, 50_000),
    )
    the_route = route(offset(*CENTER, 270, 5_000), offset(*CENTER, 90, 5_000), alt_ft=2500)

    events = analyze_route(the_route, [big], 1013.25)

    assert kinds(events) == ["INFRINGEMENT"]
    assert events[0].start_nm == 0.0
    assert events[0].end_nm == pytest.approx(10_000 / M_PER_NM, abs=1e-6)


def test_route_ending_inside_a_ctr_ends_with_an_infringement() -> None:
    the_route = route(offset(*CENTER, 270, 30_000), CENTER, alt_ft=2500)

    events = analyze_route(the_route, [airspace(**CTR)], 1013.25)

    assert events[-1].kind == "INFRINGEMENT"
    assert events[-1].end_nm == pytest.approx(30_000 / M_PER_NM, abs=1e-6)


def test_route_of_one_point_inside_a_ctr() -> None:
    events = analyze_route(route(CENTER, CENTER, alt_ft=2500), [airspace(**CTR)], 1013.25)

    assert [(e.kind, e.start_nm, e.end_nm) for e in events] == [("INFRINGEMENT", 0.0, 0.0)]


def test_leg_shorter_than_a_step_clipping_a_corner_in_its_first_half() -> None:
    north_east = square_ring(*CENTER, HALF_SIZE_M)[2]
    inside_point = offset(*north_east, 225, 0.03 * M_PER_NM)  # chord ~0.06 NM
    start = offset(*inside_point, 315, 0.05 * M_PER_NM)
    end = offset(*inside_point, 135, 0.15 * M_PER_NM)

    events = analyze_route(route(start, end, alt_ft=2500), [airspace(**CTR)], 1013.25)

    assert "INFRINGEMENT" in kinds(events)


def test_zero_length_leg_where_the_altitude_changes() -> None:
    b = offset(*CENTER, 270, HALF_SIZE_M + M_PER_NM)
    a = offset(*b, 270, 20_000)
    c = offset(*CENTER, 90, 30_000)
    climbing = Route(
        waypoints=[
            Waypoint(name="A", lon=a[0], lat=a[1], alt_ft=2500),
            Waypoint(name="B", lon=b[0], lat=b[1], alt_ft=2500),
            Waypoint(name="B-climb", lon=b[0], lat=b[1], alt_ft=5000),
            Waypoint(name="C", lon=c[0], lat=c[1], alt_ft=5000),
        ]
    )

    events = analyze_route(climbing, [airspace(**CTR)], qnh_hpa=1013.25)

    assert kinds(events) == ["WARNING"]
    assert events[0].min_lateral_nm == pytest.approx(1.0, abs=TOLERANCE_NM)
    assert events[0].end_nm == pytest.approx(geodesic_distance_m(*a, *b) / M_PER_NM, abs=0.26)


# --- severity file -----------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "clearence_required:\n  classes: [D]\n",  # misspelt section
        "requirement:\n  classes: [C]\n",  # key not allowed in this section
        "info:\n  types: [D, 5]\n",  # not a name
        "info:\n  types: [D, ATZ\n",  # YAML syntax error
        "info: !!python/object/apply:builtins.dict [[]]\n",  # Python tag
    ],
    ids=["unknown-section", "unknown-key", "non-string", "yaml-syntax", "python-tag"],
)
def test_more_malformed_severity_files_are_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "severity.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(path)


def test_severity_file_that_is_not_utf8_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "severity.yaml"
    path.write_bytes(b"info:\n  types: [\xff]\n")

    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(path)


@pytest.mark.parametrize(("size", "accepted"), [(100_000, True), (100_001, False)])
def test_severity_file_size_limit_is_100_000_bytes(
    tmp_path: Path, size: int, accepted: bool
) -> None:
    body = (
        "clearance_required:\n  classes: [D]\n  types: [P]\n"
        "requirement:\n  types: [TMZ]\ninfo:\n  types: [D]\n#"
    )
    path = tmp_path / "severity.yaml"
    path.write_text(body + "x" * (size - len(body)), encoding="utf-8")
    assert path.stat().st_size == size

    if accepted:
        assert load_severity_rules(path).info_types == frozenset({"D"})
    else:
        with pytest.raises(ValueError, match="too large"):
            load_severity_rules(path)


@pytest.mark.parametrize("extend_m", range(0, 480, 40))
def test_boundaries_within_half_a_step_when_the_route_end_moves(extend_m: int) -> None:
    the_route = route(
        offset(*CENTER, 270, 30_000), offset(*CENTER, 90, 30_000 + extend_m), alt_ft=2500
    )
    events = analyze_route(the_route, [airspace(**CTR)], 1013.25)
    infringement = next(e for e in events if e.kind == "INFRINGEMENT")

    assert infringement.start_nm == pytest.approx(20_000 / M_PER_NM, abs=HALF_STEP_NM)
    assert infringement.end_nm == pytest.approx(40_000 / M_PER_NM, abs=HALF_STEP_NM)


# --- severity file must be complete ------------------------------------------------

COMPLETE = (
    "clearance_required:\n"
    "  classes: [A, B, C, D]\n"
    "  types: [P, R]\n"
    "requirement:\n"
    "  types: [TMZ, RMZ]\n"
    "info:\n"
    "  types: [D, ATZ]\n"
)


@pytest.mark.parametrize(
    "content",
    [
        COMPLETE.replace("info:\n  types: [D, ATZ]\n", ""),  # a section missing
        COMPLETE.replace("info:\n  types: [D, ATZ]\n", "info:\n"),  # a null section
        COMPLETE.replace("[A, B, C, D]", "[a, b, c, d]"),  # lower-case classes
        COMPLETE.replace("[A, B, C, D]", "[A, B, C, H]"),  # not an ICAO class
        COMPLETE.replace("[TMZ, RMZ]", "[tmz, rmz]"),  # lower-case types
    ],
    ids=[
        "missing-section",
        "null-section",
        "lowercase-classes",
        "unknown-class",
        "lowercase-types",
    ],
)
def test_incomplete_severity_file_is_rejected(tmp_path: Path, content: str) -> None:
    # Any of these used to be accepted and silently switched infringement detection off.
    path = tmp_path / "severity.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(path)


def test_complete_severity_file_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "severity.yaml"
    path.write_text(COMPLETE, encoding="utf-8")

    assert load_severity_rules(path) == load_severity_rules(DEFAULT_SEVERITY_FILE)


def test_missing_severity_file_is_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(tmp_path / "nope.yaml")


# --- security review of milestone 6 -------------------------------------------------


def star_ring(lon: float, lat: float, points: int, radius_m: float) -> list[tuple[float, float]]:
    """A self-intersecting star: each vertex jumps far around the circle."""
    ring = [
        offset(lon, lat, (i * 360 * (points // 2 - 1) / points) % 360, radius_m)
        for i in range(points)
    ]
    return [*ring, ring[0]]


def test_self_intersecting_airspace_is_analysed_quickly_and_correctly() -> None:
    # make_valid on a 201-vertex star used to take ~21 s on every analysis.
    star = airspace(
        type="CTR",
        lower=ft_gnd(0),
        upper=ft_msl(3500),
        polygon_lonlat=star_ring(*CENTER, 201, 9_000),
    )

    started = time.perf_counter()
    events = analyze_route(route_through_centre(2500), [star], qnh_hpa=1013.25)

    assert time.perf_counter() - started < 5.0
    assert "WARNING" in kinds(events)


def test_event_count_is_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    assert MAX_EVENTS == 10_000
    monkeypatch.setattr(analysis, "MAX_EVENTS", 2)

    with pytest.raises(ValueError, match="events"):
        analyze_route(route_through_centre(6000), [airspace()], qnh_hpa=993.0)  # 3 events


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("negative lateral margin", {"lateral_margin_nm": -0.01}),
        ("infinite lateral margin", {"lateral_margin_nm": math.inf}),
        ("negative vertical margin", {"vertical_margin_ft": -1.0}),
        ("NaN vertical margin", {"vertical_margin_ft": math.nan}),
        ("NaN ground elevation", {"ground_elev_ft": math.nan}),
        ("infinite ground elevation", {"ground_elev_ft": math.inf}),
    ],
)
def test_invalid_parameters_are_rejected(name: str, kwargs: dict) -> None:
    # Each used to return no events at all for a route straight through a class D CTR.
    with pytest.raises(ValueError):
        analyze_route(route_through_centre(2500), [airspace(**CTR)], qnh_hpa=1013.25, **kwargs)


@pytest.mark.parametrize("qnh_hpa", [math.nan, 1e-300, 29.92, 101_325.0, 849.9, 1100.1])
def test_implausible_qnh_is_rejected(qnh_hpa: float) -> None:
    # 29.92 is inHg and 101325 is Pa: typed in the wrong unit.
    with pytest.raises(ValueError, match="QNH"):
        analyze_route(route_through_centre(2500), [airspace(**CTR)], qnh_hpa=qnh_hpa)


@pytest.mark.parametrize("qnh_hpa", [850.0, 1100.0])
def test_qnh_at_the_plausible_limits_is_accepted(qnh_hpa: float) -> None:
    assert "INFRINGEMENT" in kinds(
        analyze_route(route_through_centre(2500), [airspace(**CTR)], qnh_hpa)
    )


def test_airspace_near_the_antipode_is_not_reported() -> None:
    # Far from the projection centre the local plane is badly distorted; an airspace on the
    # other side of the Earth used to give a false infringement on a route in Romania.
    antipode = (CENTER[0] - 180.0, -CENTER[1])
    far = airspace(
        id="FAR",
        type="CTR",
        lower=ft_gnd(0),
        upper=ft_msl(3500),
        polygon_lonlat=square_ring(*antipode, 20_000),
    )

    assert analyze_route(route_through_centre(2500), [far], qnh_hpa=1013.25) == []


def write_severity(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "severity.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_severity_file_with_yaml_aliases_is_rejected(tmp_path: Path) -> None:
    # A 534-byte merge-key bomb used 1.8 GB in yaml.safe_load; no alias is ever needed here.
    content = COMPLETE.replace("classes: [A, B, C, D]", "classes: &c [A, B, C, D]").replace(
        "types: [TMZ, RMZ]", "types: *c"
    )

    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(write_severity(tmp_path, content))


def test_deeply_nested_severity_file_is_a_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(write_severity(tmp_path, "[" * 30_000 + "]" * 30_000))


def test_severity_file_with_duplicate_sections_is_rejected(tmp_path: Path) -> None:
    # The second section silently replaced the first and switched class A-D detection off.
    content = COMPLETE + "clearance_required:\n  classes: []\n  types: []\n"

    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(write_severity(tmp_path, content))


def test_clearance_section_needs_both_classes_and_types(tmp_path: Path) -> None:
    content = COMPLETE.replace("  classes: [A, B, C, D]\n", "")

    with pytest.raises(ValueError, match="severity"):
        load_severity_rules(write_severity(tmp_path, content))


def test_severity_file_that_is_a_fifo_does_not_block(tmp_path: Path) -> None:
    path = tmp_path / "severity.yaml"
    os.mkfifo(path)
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            load_severity_rules(path)
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "load_severity_rules blocked"
    assert isinstance(outcome[0], ValueError)


# --- security review of milestone 7: bounded memory and early stop ------------------


def test_memory_stays_small_for_a_long_route_near_many_airspaces() -> None:
    # 20,000 samples and 200 small CTRs along the route: keeping per-airspace arrays for
    # every sample used to cost ~28 bytes x samples x airspaces (1.5 GB measured end to end).
    import tracemalloc

    points = [(26.0, 40.0)]
    for _ in range(19_999):
        points.append(offset(*points[-1], 0, 0.03 * M_PER_NM))
    long_route = route(*points, alt_ft=2500)
    ctrs = [
        airspace(
            id=f"C{i}",
            type="CTR",
            lower=ft_gnd(0),
            upper=ft_msl(3500),
            polygon_lonlat=square_ring(*offset(*points[i * 100], 90, 1_000), 300),
        )
        for i in range(200)
    ]

    tracemalloc.start()
    try:
        events = analyze_route(long_route, ctrs, qnh_hpa=1013.25)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len({e.airspace_id for e in events}) == 200
    assert peak < 40_000_000


def test_event_limit_stops_before_evaluating_every_airspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(analysis, "MAX_EVENTS", 5)
    consumed = 0

    def ctrs():  # each one gives warning, infringement, warning
        nonlocal consumed
        for i in range(50):
            consumed += 1
            yield airspace(id=f"C{i}", type="CTR", lower=ft_gnd(0), upper=ft_msl(3500))

    with pytest.raises(ValueError, match="events"):
        analyze_route(route_through_centre(2500), ctrs(), qnh_hpa=1013.25)

    assert consumed <= 3


def test_total_samples_kept_for_airspaces_are_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analysis, "MAX_TRACK_SAMPLES", 10)

    with pytest.raises(ValueError, match="samples"):
        analyze_route(route_through_centre(2500), [airspace(**CTR)], qnh_hpa=1013.25)
