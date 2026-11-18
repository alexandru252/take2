"""Tests for take2.io.routes (SPEC.md section 7.2)."""

import os
import shutil
import threading
from pathlib import Path

import pytest

from take2.io import routes
from take2.io.routes import (
    RouteImportError,
    load_route,
    parse_csv,
    parse_gpx,
    parse_pln,
    parse_world_position,
)
from take2.models import MAX_ROUTE_WAYPOINTS

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
        "N44° 60' 0.00\",E26° 0' 0.00\",+000100.00",  # minutes exactly 60
        "N44° 30' 60.00\",E26° 0' 0.00\",+000100.00",  # seconds exactly 60
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


# --- input handling found in review (QA) -------------------------------------------


def gpx_document(body: str) -> str:
    return f'<gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">{body}</gpx>'


def test_csv_columns_are_matched_by_header_name() -> None:
    route = parse_csv("name,lon,lat,alt_ft\nA,26.0,44.5,\nB,26.1,44.6,\n", cruise_alt_ft=CRUISE_FT)

    assert [(w.lat, w.lon) for w in route.waypoints] == [(44.5, 26.0), (44.6, 26.1)]


@pytest.mark.parametrize(
    ("parser", "text"),
    [
        pytest.param(parse_csv, "name,lat,lon,alt_ft\nA,44.5,26.0,0\nB,44.6,26.1,0\n", id="csv"),
        pytest.param(
            parse_gpx,
            gpx_document(
                '<rte><rtept lat="44.5" lon="26.0"><ele>0</ele></rtept>'
                '<rtept lat="44.6" lon="26.1"><ele>0</ele></rtept></rte>'
            ),
            id="gpx",
        ),
        pytest.param(
            parse_pln,
            pln_document(
                "N44° 30' 0.00\",E26° 0' 0.00\",+000000.00",
                "N44° 36' 0.00\",E26° 6' 0.00\",+000000.00",
            ),
            id="pln",
        ),
    ],
)
def test_zero_altitude_is_kept_not_replaced_by_cruise(parser, text: str) -> None:
    route = parser(text, cruise_alt_ft=CRUISE_FT)

    assert [w.alt_ft for w in route.waypoints] == [0.0, 0.0]


def test_gpx_with_empty_route_falls_back_to_track_points() -> None:
    text = gpx_document(
        '<rte></rte><trk><trkseg><trkpt lat="44.5" lon="26.0"/><trkpt lat="44.6" lon="26.1"/>'
        "</trkseg></trk>"
    )

    assert [(w.lat, w.lon) for w in parse_gpx(text, CRUISE_FT).waypoints] == [
        (44.5, 26.0),
        (44.6, 26.1),
    ]


def test_gpx_with_several_routes_joins_their_points_in_file_order() -> None:
    text = gpx_document(
        '<rte><rtept lat="44.1" lon="26.0"/><rtept lat="44.2" lon="26.0"/></rte>'
        '<rte><rtept lat="44.3" lon="26.0"/><rtept lat="44.4" lon="26.0"/></rte>'
    )

    assert [w.lat for w in parse_gpx(text, CRUISE_FT).waypoints] == [44.1, 44.2, 44.3, 44.4]


def test_load_route_rejects_a_non_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "latin1.csv"
    path.write_bytes(b"name,lat,lon,alt_ft\nA\xb0,44.5,26.0,\nB,44.6,26.1,\n")

    with pytest.raises(RouteImportError, match="latin1.csv"):
        load_route(path, cruise_alt_ft=CRUISE_FT)


# --- hostile input (security review) -----------------------------------------------

BILLION_LAUGHS_DTD = (
    '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa">'
    + "".join(f'<!ENTITY {chr(98 + i)} "{("&" + chr(97 + i) + ";") * 10}">' for i in range(9))
    + "]>"
)


def test_gpx_rejects_billion_laughs() -> None:
    body = (
        '<rte><rtept lat="44.5" lon="26.0"><name>&j;</name></rtept>'
        '<rtept lat="44.6" lon="26.1"/></rte>'
    )

    with pytest.raises(RouteImportError):
        parse_gpx(BILLION_LAUGHS_DTD + gpx_document(body), cruise_alt_ft=CRUISE_FT)


def test_pln_rejects_billion_laughs() -> None:
    text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\"", "N44° 36' 0.00\",E26° 6' 0.00\"")
    text = text.replace('<?xml version="1.0" encoding="UTF-8"?>', BILLION_LAUGHS_DTD)
    text = text.replace('id="P1"', "").replace("N44° 30' 0.00\",E26° 0' 0.00\"", "&j;")

    with pytest.raises(RouteImportError):
        parse_pln(text, cruise_alt_ft=CRUISE_FT)


def external_entity_dtd(tmp_path: Path, content: str) -> str:
    """DTD whose entity &j; points to a local file with valid route content."""
    secret = tmp_path / "secret.txt"
    secret.write_text(content, encoding="utf-8")
    return f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY j SYSTEM "{secret.as_uri()}">]>'


def test_gpx_does_not_read_external_entities(tmp_path: Path) -> None:
    # A parser that resolved &j; would load this route with the name "SECRET-NAME".
    dtd = external_entity_dtd(tmp_path, "SECRET-NAME")
    body = (
        '<rte><rtept lat="44.5" lon="26.0"><name>&j;</name></rtept>'
        '<rtept lat="44.6" lon="26.1"/></rte>'
    )

    with pytest.raises(RouteImportError) as excinfo:
        parse_gpx(dtd + gpx_document(body), cruise_alt_ft=CRUISE_FT)

    assert "SECRET" not in str(excinfo.value)


def test_pln_does_not_read_external_entities(tmp_path: Path) -> None:
    # A parser that resolved &j; would read a valid WorldPosition from the file and succeed.
    dtd = external_entity_dtd(tmp_path, "N44° 30' 0.00\",E26° 0' 0.00\"")
    text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\"", "N44° 36' 0.00\",E26° 6' 0.00\"")
    text = text.replace('<?xml version="1.0" encoding="UTF-8"?>', dtd)
    text = text.replace("N44° 30' 0.00\",E26° 0' 0.00\"", "&j;")

    with pytest.raises(RouteImportError) as excinfo:
        parse_pln(text, cruise_alt_ft=CRUISE_FT)

    assert "44° 30" not in str(excinfo.value)


FLAT_ENTITY_DTD = f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY j "{"A" * 290}">]>'


@pytest.mark.parametrize("parser", [parse_gpx, parse_pln])
def test_any_doctype_is_rejected(parser) -> None:
    # A flat entity referenced many times stays under expat's amplification limit (about
    # 100x), so a 1 MB file could expand to hundreds of MB. Route files never need a DTD.
    if parser is parse_gpx:
        body = '<rte><rtept lat="44.5" lon="26.0"/><rtept lat="44.6" lon="26.1"/></rte>'
        text = FLAT_ENTITY_DTD + gpx_document(body)
    else:
        text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\"", "N44° 36' 0.00\",E26° 6' 0.00\"")
        text = text.replace('<?xml version="1.0" encoding="UTF-8"?>', FLAT_ENTITY_DTD)

    with pytest.raises(RouteImportError, match="DOCTYPE"):
        parser(text, cruise_alt_ft=CRUISE_FT)


def test_markup_hidden_in_a_comment_stays_a_comment() -> None:
    # Found in the security re-review: gpxpy rewrote ' xmlns="..."' in the raw text before
    # parsing, which turned this comment into real markup that the safety check never saw.
    text = (
        '<gpx version="1.1" creator="t"><rte><rtept lat="44.5" lon="26.0"/>'
        'T xmlns="<!--"<rtept lat="45.0" lon="27.0"/>-->'
        '<rtept lat="44.6" lon="26.1"/></rte></gpx>'
    )

    route = parse_gpx(text, cruise_alt_ft=CRUISE_FT)

    assert [(w.lat, w.lon) for w in route.waypoints] == [(44.5, 26.0), (44.6, 26.1)]


@pytest.mark.parametrize(
    "root",
    [
        '<gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">',
        '<gpx version="1.0" creator="t" xmlns="http://www.topografix.com/GPX/1/0">',
        '<gpx version="1.1" creator="t">',
    ],
    ids=["gpx-1.1", "gpx-1.0", "no-namespace"],
)
def test_gpx_versions_and_namespaces(root: str) -> None:
    text = (
        root + '<rte><rtept lat="44.5" lon="26.0"><ele>100</ele><name>A</name></rtept>'
        '<rtept lat="44.6" lon="26.1"/></rte></gpx>'
    )

    route = parse_gpx(text, cruise_alt_ft=CRUISE_FT)

    assert [(w.name, w.lat, w.lon) for w in route.waypoints] == [
        ("A", 44.5, 26.0),
        ("WP2", 44.6, 26.1),
    ]
    assert route.waypoints[0].alt_ft == pytest.approx(100 * FT_PER_M)


def test_gpx_points_inside_extensions_are_ignored() -> None:
    text = gpx_document(
        '<rte><rtept lat="44.5" lon="26.0"/><rtept lat="44.6" lon="26.1"/>'
        '<extensions><rtept lat="45.0" lon="27.0"/></extensions></rte>'
    )

    assert len(parse_gpx(text, cruise_alt_ft=CRUISE_FT).waypoints) == 2


def test_file_that_is_not_gpx_is_rejected() -> None:
    with pytest.raises(RouteImportError, match="gpx"):
        parse_gpx("<kml><Placemark/></kml>", cruise_alt_ft=CRUISE_FT)


@pytest.mark.parametrize("attributes", ['lon="26.0"', 'lat="north" lon="26.0"'])
def test_gpx_point_with_missing_or_bad_latitude_is_rejected(attributes: str) -> None:
    text = gpx_document(f'<rte><rtept {attributes}/><rtept lat="44.6" lon="26.1"/></rte>')

    with pytest.raises(RouteImportError, match="lat"):
        parse_gpx(text, cruise_alt_ft=CRUISE_FT)


def test_gpx_with_more_than_500_000_elements_is_rejected() -> None:
    # Root plus 500,000 empty elements is 500,001 elements, one over the documented limit.
    text = gpx_document("<a/>" * 500_000)

    with pytest.raises(RouteImportError, match="elements"):
        parse_gpx(text, cruise_alt_ft=CRUISE_FT)


@pytest.mark.parametrize(("count", "accepted"), [(64, True), (65, False)])
def test_attribute_limit_per_element_is_64(count: int, accepted: bool) -> None:
    attributes = " ".join(f'a{i}="v"' for i in range(count - 2))
    body = f'<rte><rtept lat="44.5" lon="26.0" {attributes}/><rtept lat="44.6" lon="26.1"/></rte>'
    text = gpx_document(body)

    if accepted:
        assert len(parse_gpx(text, cruise_alt_ft=CRUISE_FT).waypoints) == 2
    else:
        with pytest.raises(RouteImportError, match="attributes"):
            parse_gpx(text, cruise_alt_ft=CRUISE_FT)


@pytest.mark.parametrize(("depth", "accepted"), [(64, True), (65, False)])
def test_nesting_limit_is_64_elements(depth: int, accepted: bool) -> None:
    # gpx > rte > rtept > extensions is 4 levels; the rest are nested <x> elements.
    nested = "<x>" * (depth - 4) + "</x>" * (depth - 4)
    body = (
        f'<rte><rtept lat="44.5" lon="26.0"><extensions>{nested}</extensions></rtept>'
        '<rtept lat="44.6" lon="26.1"/></rte>'
    )
    text = gpx_document(body)

    if accepted:
        assert len(parse_gpx(text, cruise_alt_ft=CRUISE_FT).waypoints) == 2
    else:
        with pytest.raises(RouteImportError, match="nested"):
            parse_gpx(text, cruise_alt_ft=CRUISE_FT)


@pytest.mark.parametrize("parser", [parse_gpx, parse_pln])
def test_deep_nesting_after_a_large_token_is_rejected(parser) -> None:
    # Found in the QA re-review: expat may defer parsing after a large token until the end of
    # input, and a chunked check that ignored the final events let deep nesting through.
    hidden = '<y a="' + "v" * 200_000 + '"/>' + "<x>" * 1_000 + "</x>" * 1_000
    if parser is parse_gpx:
        text = gpx_document(
            f'<rte><rtept lat="44.5" lon="26.0"><extensions>{hidden}</extensions></rtept>'
            '<rtept lat="44.6" lon="26.1"/></rte>'
        )
    else:
        text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\"", "N44° 36' 0.00\",E26° 6' 0.00\"")
        text = text.replace("</FlightPlan.FlightPlan>", hidden + "</FlightPlan.FlightPlan>")

    with pytest.raises(RouteImportError, match="nested"):
        parser(text, cruise_alt_ft=CRUISE_FT)


def garmin_track(points: int) -> str:
    """A GPX 1.1 track shaped like a Garmin export: many namespaces, per-point extensions."""
    namespaces = " ".join(
        f'xmlns:ns{i}="http://www.example.com/xmlschemas/Ext{i}/v1"' for i in range(12)
    )
    root = (
        '<gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" '
        f"{namespaces}>"
    )
    trkpts = "".join(
        f'<trkpt lat="{44 + i * 1e-5:.6f}" lon="26.000000"><ele>300.0</ele>'
        "<time>2026-11-18T10:00:00Z</time><extensions><gpxtpx:TrackPointExtension>"
        "<gpxtpx:hr>95</gpxtpx:hr></gpxtpx:TrackPointExtension></extensions></trkpt>"
        for i in range(points)
    )
    return f"{root}<trk><name>Flight</name><trkseg>{trkpts}</trkseg></trk></gpx>"


def test_three_hour_1hz_garmin_track_loads(tmp_path: Path) -> None:
    path = tmp_path / "flight.gpx"
    path.write_text(garmin_track(10_800), encoding="utf-8")

    route = load_route(path, cruise_alt_ft=CRUISE_FT)

    assert len(route.waypoints) == 10_800
    assert route.waypoints[0].alt_ft == pytest.approx(300 * FT_PER_M)


def test_text_of_exactly_the_size_limit_is_accepted() -> None:
    base = gpx_document('<rte><rtept lat="44.5" lon="26.0"/><rtept lat="44.6" lon="26.1"/></rte>')
    text = base + " " * (10_000_000 - len(base))

    assert len(parse_gpx(text, cruise_alt_ft=CRUISE_FT).waypoints) == 2
    with pytest.raises(RouteImportError, match="too large"):
        parse_gpx(text + " ", cruise_alt_ft=CRUISE_FT)


def test_load_route_checks_the_size_before_decoding(tmp_path: Path) -> None:
    # 10,000,001 bytes that are not UTF-8: the size error must win over the decoding error.
    path = tmp_path / "big.csv"
    with path.open("wb") as file:
        file.write(b"\xff")
        file.truncate(10_000_001)

    with pytest.raises(RouteImportError, match="too large"):
        load_route(path, cruise_alt_ft=CRUISE_FT)


@pytest.mark.parametrize("parser", [parse_gpx, parse_pln])
def test_text_that_cannot_be_encoded_is_rejected(parser) -> None:
    # A lone surrogate cannot be encoded as UTF-8; it used to escape as UnicodeEncodeError.
    with pytest.raises(RouteImportError):
        parser("\ud800<gpx/>", cruise_alt_ft=CRUISE_FT)


def test_deeply_nested_gpx_is_rejected() -> None:
    # Deep nesting used to crash the process (stack overflow in a recursive copy in gpxpy).
    nested = "<x>" * 1_000 + "</x>" * 1_000
    body = (
        f'<rte><rtept lat="44.5" lon="26.0"><extensions>{nested}</extensions></rtept>'
        '<rtept lat="44.6" lon="26.1"/></rte>'
    )

    with pytest.raises(RouteImportError, match="nested"):
        parse_gpx(gpx_document(body), cruise_alt_ft=CRUISE_FT)


def test_deeply_nested_pln_is_rejected() -> None:
    nested = "<x>" * 1_000 + "</x>" * 1_000
    text = pln_document("N44° 30' 0.00\",E26° 0' 0.00\"", "N44° 36' 0.00\",E26° 6' 0.00\"")
    text = text.replace("</FlightPlan.FlightPlan>", nested + "</FlightPlan.FlightPlan>")

    with pytest.raises(RouteImportError, match="nested"):
        parse_pln(text, cruise_alt_ft=CRUISE_FT)


def test_gpx_declaring_a_namespace_on_every_point_is_accepted() -> None:
    # Some exporters repeat the extension namespace on every point; a long real track must
    # not be rejected for that (found in the code review of the first guard).
    ext = (
        "<extensions><gpxtpx:TrackPointExtension xmlns:gpxtpx="
        '"http://www.garmin.com/xmlschemas/TrackPointExtension/v1"/></extensions>'
    )
    points = "".join(f'<trkpt lat="44.{i:03d}" lon="26.0">{ext}</trkpt>' for i in range(200))

    route = parse_gpx(gpx_document(f"<trk><trkseg>{points}</trkseg></trk>"), CRUISE_FT)

    assert len(route.waypoints) == 200


@pytest.mark.parametrize(
    ("parser", "filename"),
    [(parse_csv, "route.csv"), (parse_gpx, "route.gpx"), (parse_pln, "route.pln")],
)
def test_text_larger_than_the_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch, parser, filename: str
) -> None:
    monkeypatch.setattr(routes, "MAX_ROUTE_FILE_SIZE", 50)
    text = (FIXTURES / filename).read_text(encoding="utf-8")

    with pytest.raises(RouteImportError, match="too large"):
        parser(text, cruise_alt_ft=CRUISE_FT)


def test_load_route_rejects_a_file_larger_than_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes, "MAX_ROUTE_FILE_SIZE", 50)

    with pytest.raises(RouteImportError, match="too large"):
        load_route(FIXTURES / "route.csv", cruise_alt_ft=CRUISE_FT)


@pytest.mark.parametrize("make", ["missing", "directory", "fifo"])
def test_load_route_rejects_a_path_that_is_not_a_file(tmp_path: Path, make: str) -> None:
    path = tmp_path / "route.csv"
    if make == "directory":
        path.mkdir()
    elif make == "fifo":
        os.mkfifo(path)  # opening it for reading must not block
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            load_route(path, cruise_alt_ft=CRUISE_FT)
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    # Run in a thread so a regression that blocks on the FIFO fails instead of hanging.
    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "load_route blocked"
    assert isinstance(outcome[0], RouteImportError)
    assert "not found" in str(outcome[0])


def test_route_with_too_many_waypoints_is_rejected() -> None:
    rows = "".join(f"P{i},44.5,26.0,\n" for i in range(MAX_ROUTE_WAYPOINTS + 1))

    with pytest.raises(RouteImportError, match="too many waypoints"):
        parse_csv("name,lat,lon,alt_ft\n" + rows, cruise_alt_ft=CRUISE_FT)


def test_csv_parser_errors_become_route_import_errors() -> None:
    # A bare carriage return inside an unquoted field makes the csv module raise _csv.Error.
    text = "name,lat,lon,alt_ft\nA\rB,44.5,26.0,\nC,44.6,26.1,\n"

    with pytest.raises(RouteImportError, match="CSV"):
        parse_csv(text, cruise_alt_ft=CRUISE_FT)


def test_error_messages_do_not_echo_huge_input() -> None:
    text = "name,lat,lon,alt_ft\nA," + "9" * 10_000 + "x,26.0,\nB,44.6,26.1,\n"

    with pytest.raises(RouteImportError) as excinfo:
        parse_csv(text, cruise_alt_ft=CRUISE_FT)

    assert len(str(excinfo.value)) < 200


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read files without permission")
def test_load_route_rejects_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "locked.csv"
    shutil.copy(FIXTURES / "route.csv", path)
    path.chmod(0)
    try:
        with pytest.raises(RouteImportError, match="locked.csv"):
            load_route(path, cruise_alt_ft=CRUISE_FT)
    finally:
        path.chmod(0o600)


def test_error_for_a_long_waypoint_name_is_short() -> None:
    text = "name,lat,lon,alt_ft\n" + "N" * 10_000 + ",95.0,26.0,\nB,44.6,26.1,\n"

    with pytest.raises(RouteImportError) as excinfo:
        parse_csv(text, cruise_alt_ft=CRUISE_FT)

    assert len(str(excinfo.value)) < 300


def test_error_for_a_long_world_position_is_short() -> None:
    with pytest.raises(RouteImportError) as excinfo:
        parse_world_position("N" * 10_000)

    assert len(str(excinfo.value)) < 300
