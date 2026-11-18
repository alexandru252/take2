"""Tests for take2.io.routes (SPEC.md section 7.2)."""

import shutil
from pathlib import Path

import pytest

from take2.io.routes import (
    RouteImportError,
    load_route,
    parse_csv,
    parse_gpx,
    parse_pln,
    parse_world_position,
)

FIXTURES = Path(__file__).parent / "fixtures"
CRUISE_FT = 4500.0
FT_PER_M = 3.28084


def dms(degrees: float, minutes: float, seconds: float) -> float:
    return degrees + minutes / 60 + seconds / 3600


def pln_document(*world_positions: str) -> str:
    waypoints = "".join(
        f'<ATCWaypoint id="P{i}"><WorldPosition>{pos}</WorldPosition></ATCWaypoint>'
        for i, pos in enumerate(world_positions, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><SimBase.Document><FlightPlan.FlightPlan>'
        f"{waypoints}</FlightPlan.FlightPlan></SimBase.Document>"
    )


# --- load_route: format dispatch ---------------------------------------------------


@pytest.mark.parametrize("filename", ["route.csv", "route.gpx", "route.pln"])
def test_load_route_reads_each_supported_format(filename: str) -> None:
    route = load_route(FIXTURES / filename, cruise_alt_ft=CRUISE_FT)

    assert [w.name for w in route.waypoints] == ["ALPHA", "BRAVO", "CHARLIE"]


def test_load_route_extension_is_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "ROUTE.CSV"
    shutil.copy(FIXTURES / "route.csv", path)

    assert len(load_route(path, cruise_alt_ft=CRUISE_FT).waypoints) == 3


def test_load_route_accepts_a_string_path() -> None:
    route = load_route(str(FIXTURES / "route.csv"), cruise_alt_ft=CRUISE_FT)

    assert len(route.waypoints) == 3


def test_load_route_rejects_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "route.kml"
    path.write_text("<kml/>")

    with pytest.raises(RouteImportError, match="unsupported"):
        load_route(path, cruise_alt_ft=CRUISE_FT)


def test_route_import_error_is_a_value_error() -> None:
    assert issubclass(RouteImportError, ValueError)


# --- CSV ---------------------------------------------------------------------------


def test_csv_reads_names_coordinates_and_altitudes() -> None:
    route = load_route(FIXTURES / "route.csv", cruise_alt_ft=CRUISE_FT)

    assert [(w.lat, w.lon) for w in route.waypoints] == [(44.4, 25.9), (44.5, 26.0), (44.6, 26.1)]
    assert [w.alt_ft for w in route.waypoints] == [2500.0, CRUISE_FT, 3000.0]


def test_csv_without_alt_ft_column_uses_cruise_altitude() -> None:
    route = parse_csv("name,lat,lon\nA,44.5,26.0\nB,44.6,26.1\n", cruise_alt_ft=CRUISE_FT)

    assert [w.alt_ft for w in route.waypoints] == [CRUISE_FT, CRUISE_FT]


def test_csv_ignores_bom_whitespace_and_blank_lines() -> None:
    text = "﻿name, lat, lon, alt_ft\n ALPHA , 44.5 , 26.0 , 1000 \n\nBRAVO,44.6,26.1,\n"

    route = parse_csv(text, cruise_alt_ft=CRUISE_FT)

    assert [(w.name, w.lat, w.lon, w.alt_ft) for w in route.waypoints] == [
        ("ALPHA", 44.5, 26.0, 1000.0),
        ("BRAVO", 44.6, 26.1, CRUISE_FT),
    ]


def test_csv_missing_required_column_is_rejected() -> None:
    with pytest.raises(RouteImportError, match="lon"):
        parse_csv("name,lat,alt_ft\nA,44.5,1000\nB,44.6,1000\n", cruise_alt_ft=CRUISE_FT)


def test_csv_empty_file_is_rejected() -> None:
    with pytest.raises(RouteImportError, match="header"):
        parse_csv("", cruise_alt_ft=CRUISE_FT)


def test_csv_non_numeric_value_reports_the_line() -> None:
    text = "name,lat,lon,alt_ft\nA,44.5,26.0,\nB,north,26.1,\n"

    with pytest.raises(RouteImportError, match="line 3"):
        parse_csv(text, cruise_alt_ft=CRUISE_FT)


# --- GPX ---------------------------------------------------------------------------


def test_gpx_prefers_route_points_over_track_points() -> None:
    route = load_route(FIXTURES / "route.gpx", cruise_alt_ft=CRUISE_FT)

    assert [(w.lat, w.lon) for w in route.waypoints] == [(44.4, 25.9), (44.5, 26.0), (44.6, 26.1)]


def test_gpx_elevation_in_metres_is_converted_to_feet() -> None:
    route = load_route(FIXTURES / "route.gpx", cruise_alt_ft=CRUISE_FT)

    assert [w.alt_ft for w in route.waypoints] == pytest.approx(
        [762 * FT_PER_M, CRUISE_FT, 914.4 * FT_PER_M]
    )


def test_gpx_falls_back_to_track_points_across_segments() -> None:
    route = load_route(FIXTURES / "track.gpx", cruise_alt_ft=CRUISE_FT)

    assert [(w.lat, w.lon) for w in route.waypoints] == [(44.4, 25.9), (44.45, 25.95), (44.5, 26.0)]
    assert [w.alt_ft for w in route.waypoints] == pytest.approx(
        [500 * FT_PER_M, 600 * FT_PER_M, CRUISE_FT]
    )


def test_gpx_unnamed_points_get_sequential_names() -> None:
    route = load_route(FIXTURES / "track.gpx", cruise_alt_ft=CRUISE_FT)

    assert [w.name for w in route.waypoints] == ["WP1", "WP2", "WP3"]


def test_gpx_without_route_or_track_points_is_rejected() -> None:
    text = '<gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1"></gpx>'

    with pytest.raises(RouteImportError, match="at least 2"):
        parse_gpx(text, cruise_alt_ft=CRUISE_FT)


def test_gpx_malformed_xml_is_rejected() -> None:
    with pytest.raises(RouteImportError, match="GPX"):
        parse_gpx("<gpx><rte>", cruise_alt_ft=CRUISE_FT)


# --- MSFS .pln ---------------------------------------------------------------------


def test_parse_world_position_reads_the_spec_example() -> None:
    lat, lon, alt_ft = parse_world_position("N44° 34' 21.56\",E26° 5' 5.06\",+004500.00")

    assert lat == pytest.approx(dms(44, 34, 21.56), abs=1e-12)
    assert lon == pytest.approx(dms(26, 5, 5.06), abs=1e-12)
    assert alt_ft == 4500.0


def test_parse_world_position_south_and_west_are_negative() -> None:
    lat, lon, alt_ft = parse_world_position("S33° 56' 46.00\",W118° 24' 29.00\",-000010.00")

    assert lat == pytest.approx(-dms(33, 56, 46.0), abs=1e-12)
    assert lon == pytest.approx(-dms(118, 24, 29.0), abs=1e-12)
    assert alt_ft == -10.0


def test_parse_world_position_without_altitude() -> None:
    assert parse_world_position("N44° 30' 0.00\",E26° 0' 0.00\"") == (44.5, 26.0, None)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "44.5,26.0,+000100.00",  # decimal degrees, not DMS
        "E44° 30' 0.00\",N26° 0' 0.00\",+000100.00",  # hemispheres swapped
        "N44° 75' 0.00\",E26° 0' 0.00\",+000100.00",  # minutes >= 60
        "N44° 30' 61.00\",E26° 0' 0.00\",+000100.00",  # seconds >= 60
        "N44° 30' 0.00\",E26° 0' 0.00\",high",  # altitude not a number
    ],
)
def test_parse_world_position_rejects_malformed_text(text: str) -> None:
    with pytest.raises(RouteImportError):
        parse_world_position(text)


def test_pln_reads_atc_waypoints() -> None:
    route = load_route(FIXTURES / "route.pln", cruise_alt_ft=CRUISE_FT)

    lats = [w.lat for w in route.waypoints]
    lons = [w.lon for w in route.waypoints]
    assert lats == pytest.approx([44.4, dms(44, 34, 21.56), 44.6], abs=1e-12)
    assert lons == pytest.approx([25.9, dms(26, 5, 5.06), 26.1], abs=1e-12)
    assert [w.alt_ft for w in route.waypoints] == [2500.0, 4500.0, 3000.0]


def test_pln_waypoint_without_altitude_uses_cruise_altitude() -> None:
    text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\"", "N44° 36' 0.00\",E26° 6' 0.00\"")

    route = parse_pln(text, cruise_alt_ft=CRUISE_FT)

    assert [w.alt_ft for w in route.waypoints] == [CRUISE_FT, CRUISE_FT]


def test_pln_bad_world_position_reports_the_waypoint() -> None:
    text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\",+1", "garbage")

    with pytest.raises(RouteImportError, match="ATCWaypoint 2"):
        parse_pln(text, cruise_alt_ft=CRUISE_FT)


def test_pln_waypoint_without_world_position_is_rejected() -> None:
    text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\"").replace(
        "</FlightPlan.FlightPlan>", '<ATCWaypoint id="X"></ATCWaypoint></FlightPlan.FlightPlan>'
    )

    with pytest.raises(RouteImportError, match="WorldPosition"):
        parse_pln(text, cruise_alt_ft=CRUISE_FT)


def test_pln_malformed_xml_is_rejected() -> None:
    with pytest.raises(RouteImportError, match="PLN"):
        parse_pln("<SimBase.Document><FlightPlan.FlightPlan>", cruise_alt_ft=CRUISE_FT)


# --- validation (all formats) ------------------------------------------------------


def test_route_with_a_single_waypoint_is_rejected() -> None:
    with pytest.raises(RouteImportError, match="at least 2"):
        parse_csv("name,lat,lon,alt_ft\nA,44.5,26.0,1000\n", cruise_alt_ft=CRUISE_FT)


@pytest.mark.parametrize(("lat", "lon", "field"), [(95.0, 26.0, "lat"), (44.5, -181.0, "lon")])
def test_out_of_range_coordinate_reports_the_waypoint(lat: float, lon: float, field: str) -> None:
    text = f"name,lat,lon,alt_ft\nA,44.5,26.0,\nB,{lat},{lon},\n"

    with pytest.raises(RouteImportError, match=rf"waypoint 2 \('B'\): {field}"):
        parse_csv(text, cruise_alt_ft=CRUISE_FT)


def test_out_of_range_pln_latitude_is_rejected() -> None:
    text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\"", "N95° 0' 0.00\",E26° 0' 0.00\"")

    with pytest.raises(RouteImportError, match="lat"):
        parse_pln(text, cruise_alt_ft=CRUISE_FT)
