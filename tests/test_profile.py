"""Tests for take2.profile (SPEC.md section 7.5)."""

import pytest

from take2.altimetry import fl_to_altitude_on_qnh_ft
from take2.geo import geodesic_distance_m
from take2.models import Route, Waypoint
from take2.profile import build_profile
from test_analysis import (
    CENTER,
    CTR,
    HALF_SIZE_M,
    M_PER_NM,
    TOLERANCE_FT,
    airspace,
    ft_gnd,
    ft_msl,
    offset,
    route_north_of_square,
    route_through_centre,
    square_ring,
)

HALF_STEP_NM = 0.13


def test_aircraft_line_is_flat_per_leg_with_a_step_at_each_waypoint() -> None:
    a, b, c = offset(*CENTER, 270, 20_000), CENTER, offset(*CENTER, 90, 20_000)
    climbing = Route(
        waypoints=[
            Waypoint(name="A", lon=a[0], lat=a[1], alt_ft=2000),
            Waypoint(name="B", lon=b[0], lat=b[1], alt_ft=3000),
            Waypoint(name="C", lon=c[0], lat=c[1], alt_ft=3000),
        ]
    )
    at_b_nm = geodesic_distance_m(*a, *b) / M_PER_NM

    line = build_profile(climbing, [], qnh_hpa=1013.25).aircraft

    assert line[0] == (0.0, 2000.0)
    assert line[-1][1] == 3000.0
    assert (pytest.approx(at_b_nm), 2000.0) in line  # end of leg A-B, still at 2000 ft
    assert (pytest.approx(at_b_nm), 3000.0) in line  # start of leg B-C at 3000 ft
    assert [d for d, _ in line] == sorted(d for d, _ in line)


def test_tma_segment_uses_the_limits_of_the_day() -> None:
    # SPEC.md 7.4 reference TMA (FL65-FL195) at QNH 1000: base 6136.4 ft.
    profile = build_profile(route_through_centre(6000), [airspace()], qnh_hpa=1000.0)

    [segment] = profile.segments
    assert segment.airspace_id == "FIC-TMA"
    assert segment.lower_ft == pytest.approx(6136.4, abs=TOLERANCE_FT)
    assert segment.upper_ft == pytest.approx(fl_to_altitude_on_qnh_ft(195, 1000.0))
    assert segment.kind == "clearance_required"
    assert segment.approximate is False


def test_segment_covers_the_route_within_2_nm_of_the_airspace() -> None:
    # Inside from 20 to 40 km along the route; within 2 NM from 2 NM before to 2 NM after.
    [segment] = build_profile(route_through_centre(6000), [airspace()], qnh_hpa=1013.25).segments

    assert segment.start_nm == pytest.approx(20_000 / M_PER_NM - 2.0, abs=HALF_STEP_NM)
    assert segment.end_nm == pytest.approx(40_000 / M_PER_NM + 2.0, abs=HALF_STEP_NM)


def test_margin_band_lies_200_ft_under_the_lower_limit() -> None:
    [segment] = build_profile(route_through_centre(6000), [airspace()], qnh_hpa=1013.25).segments

    assert segment.lower_ft == pytest.approx(6500.0)
    assert segment.margin_bottom_ft == pytest.approx(6300.0)


def test_margins_are_configurable() -> None:
    profile = build_profile(
        route_through_centre(6000),
        [airspace()],
        qnh_hpa=1013.25,
        lateral_margin_nm=3.0,
        vertical_margin_ft=500.0,
    )

    [segment] = profile.segments
    assert segment.margin_bottom_ft == pytest.approx(6000.0)
    assert segment.start_nm == pytest.approx(20_000 / M_PER_NM - 3.0, abs=HALF_STEP_NM)


def test_airspace_within_2_nm_is_shown_even_with_no_event() -> None:
    # 500 ft below the TMA base at standard QNH: no event, but the band belongs on the chart.
    profile = build_profile(route_through_centre(6000), [airspace()], qnh_hpa=1013.25)

    assert [s.airspace_id for s in profile.segments] == ["FIC-TMA"]


@pytest.mark.parametrize(("offset_nm", "shown"), [(1.5, True), (2.5, False)])
def test_ctr_beside_the_route_is_shown_only_within_2_nm(offset_nm: float, shown: bool) -> None:
    profile = build_profile(route_north_of_square(offset_nm, 2500), [airspace(**CTR)], 1013.25)

    assert bool(profile.segments) is shown


@pytest.mark.parametrize(
    ("overrides", "kind"),
    [
        ({"type": "TMZ", "icao_class": None}, "requirement"),
        ({"type": "ATZ", "icao_class": "G"}, "info"),
        ({"type": "R", "icao_class": None}, "clearance_required"),
    ],
)
def test_segment_kind_is_the_airspace_category(overrides: dict, kind: str) -> None:
    space = airspace(lower=ft_gnd(0), upper=ft_msl(5000), **overrides)

    [segment] = build_profile(route_through_centre(3000), [space], qnh_hpa=1013.25).segments

    assert segment.kind == kind


def test_airspace_that_never_produces_an_event_is_not_shown() -> None:
    class_e = airspace(type="CTA", icao_class="E", lower=ft_gnd(0), upper=ft_msl(5000))

    assert build_profile(route_through_centre(3000), [class_e], qnh_hpa=1013.25).segments == []


def test_ground_referenced_limit_is_marked_approximate() -> None:
    space = airspace(type="CTR", lower=ft_gnd(0), upper=ft_gnd(3000))

    [segment] = build_profile(route_through_centre(2500), [space], qnh_hpa=1013.25).segments

    assert segment.approximate is True


def test_entering_twice_gives_two_segments_in_route_order() -> None:
    east_centre = offset(*CENTER, 90, 40_000)
    west = airspace(id="A1", name="West", type="CTR", lower=ft_gnd(0), upper=ft_msl(3500))
    east = airspace(
        id="A2",
        name="East",
        type="CTR",
        lower=ft_gnd(0),
        upper=ft_msl(3500),
        polygon_lonlat=square_ring(*east_centre, HALF_SIZE_M),
    )
    long_route = Route(
        waypoints=[
            Waypoint(name="S", lon=offset(*CENTER, 270, 30_000)[0], lat=CENTER[1], alt_ft=2500),
            Waypoint(name="E", lon=offset(*east_centre, 90, 30_000)[0], lat=CENTER[1], alt_ft=2500),
        ]
    )

    segments = build_profile(long_route, [east, west], qnh_hpa=1013.25).segments

    assert [s.airspace_id for s in segments] == ["A1", "A2"]
    assert segments[0].end_nm < segments[1].start_nm


def test_invalid_parameters_are_rejected() -> None:
    with pytest.raises(ValueError, match="QNH"):
        build_profile(route_through_centre(3000), [airspace()], qnh_hpa=29.92)
