"""Tests for take2.io.openaip (SPEC.md section 5, milestone 5).

No test touches the network: HTTP goes through ``httpx.MockTransport``. Every airspace is
fictional, and the API key is a made-up string.
"""

import copy
import itertools
import json
import logging
import os
import threading
import time
import traceback
import tracemalloc
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

import take2
from take2.io import openaip
from take2.io.openaip import (
    API_KEY_ENV_VAR,
    ATTRIBUTION,
    ICAO_CLASS_BY_CODE,
    MAX_DOWNLOAD_BYTES,
    MAX_ENV_FILE_SIZE,
    MAX_OPENAIP_FILE_SIZE,
    MAX_PAGE_BYTES,
    MAX_PAGE_SECONDS,
    MAX_PAGES,
    MAX_REPORTED_PROBLEMS,
    PAGE_SIZE,
    REFERENCE_BY_CODE,
    REQUEST_TIMEOUT,
    TYPE_BY_CODE,
    UNIT_BY_CODE,
    OpenAipFetchError,
    OpenAipFileError,
    ParseResult,
    fetch_airspaces,
    load_openaip_file,
    normalize_country,
    parse_openaip_airspaces,
    read_api_key,
    save_openaip_file,
)
from take2.models import Airspace, LimitReference, VerticalLimit

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "openaip_airspaces.json"
SQUARE = [[26.0, 44.4], [26.1, 44.4], [26.1, 44.5], [26.0, 44.5], [26.0, 44.4]]
MISSING = object()  # marks a field that make_item removes

# The type strings from the openAIP code list. P, R, D, CTR, TMZ, RMZ, TMA, ATZ and CTA must
# be exactly these: config/severity.yaml (milestone 6) matches on them.
EXPECTED_TYPES = {
    0: "OTHER",
    1: "R",
    2: "D",
    3: "P",
    4: "CTR",
    5: "TMZ",
    6: "RMZ",
    7: "TMA",
    8: "TRA",
    9: "TSA",
    10: "FIR",
    11: "UIR",
    12: "ADIZ",
    13: "ATZ",
    14: "MATZ",
    15: "AWY",
    16: "MTR",
    17: "ALERT",
    18: "WARNING",
    19: "PROTECTED",
    20: "HTZ",
    21: "GLIDING",
    22: "TRP",
    23: "TIZ",
    24: "TIA",
    25: "MTA",
    26: "CTA",
    27: "ACC",
    28: "SPORT",
    29: "LOW_OVERFLIGHT",
    30: "MRT",
    31: "TFR",
    32: "VFR_SECTOR",
    33: "FIS",
    34: "LTA",
    35: "UTA",
    36: "MCTR",
}
EXPECTED_CLASSES = {0: "A", 1: "B", 2: "C", 3: "D", 4: "E", 5: "F", 6: "G", 8: None}


def make_item(**overrides: object) -> dict[str, object]:
    """A valid openAIP item (fictional class D TMA, FL65 to FL195); MISSING drops a field."""
    item: dict[str, object] = {
        "_id": "fictional-0001",
        "name": "FICTIONAL TEST TMA",
        "type": 7,
        "icaoClass": 3,
        "geometry": {"type": "Polygon", "coordinates": [copy.deepcopy(SQUARE)]},
        "lowerLimit": {"value": 65, "unit": 6, "referenceDatum": 2},
        "upperLimit": {"value": 195, "unit": 6, "referenceDatum": 2},
    }
    item.update(overrides)
    return {key: value for key, value in item.items() if value is not MISSING}


def polygon(*rings: object) -> dict[str, object]:
    return {"type": "Polygon", "coordinates": list(rings)}


def limit(value: object = 65, unit: object = 6, reference: object = 2) -> dict[str, object]:
    return {"value": value, "unit": unit, "referenceDatum": reference}


def only_airspace(item: object) -> Airspace:
    result = parse_openaip_airspaces([item])
    assert result.skipped == 0, result.problems
    (airspace,) = result.airspaces
    return airspace


def by_name(result: ParseResult, name: str) -> Airspace:
    (airspace,) = [a for a in result.airspaces if a.name == name]
    return airspace


def write_json(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# --- the fictional fixture --------------------------------------------------------


def test_fixture_converts_the_four_usable_airspaces_in_file_order() -> None:
    result = load_openaip_file(FIXTURE)

    assert [a.name for a in result.airspaces] == [
        "FICTIONAL ALPHA CTR",
        "FICTIONAL BRAVO TMA",
        "FICTIONAL CHARLIE TMZ",
        "FICTIONAL ECHO P",
    ]
    assert result.defaulted == 0


def test_fixture_skips_and_counts_the_item_with_point_geometry() -> None:
    result = load_openaip_file(FIXTURE)

    assert result.skipped == 1
    (problem,) = result.problems
    assert problem == (
        "item 4 ('FICTIONAL DELTA BROKEN') skipped: geometry type 'Point' is not supported "
        "(only Polygon)"
    )


def test_fixture_ctr_ignores_fields_take2_does_not_model() -> None:
    # The CTR item also has lowerLimitMin, upperLimitMax, hoursOfOperation and audit fields.
    ctr = by_name(load_openaip_file(FIXTURE), "FICTIONAL ALPHA CTR")

    assert ctr.lower.value == 0
    assert ctr.upper.value == 3500


def test_fixture_prohibited_area_without_icao_class_is_kept_unclassified() -> None:
    # icaoClass is optional in the openAIP schema; a P area without it must not vanish.
    area = by_name(load_openaip_file(FIXTURE), "FICTIONAL ECHO P")

    assert area.type == "P"
    assert area.icao_class is None
    assert area.lower == VerticalLimit(value=0, unit="FT", reference=LimitReference.GND)
    # A very high flight level stands in for an unlimited ("UNL") upper limit.
    assert area.upper == VerticalLimit(value=999, unit="FL", reference=LimitReference.STD)


def test_fixture_ctr_is_class_d_from_the_surface_to_3500_ft_msl() -> None:
    ctr = by_name(load_openaip_file(FIXTURE), "FICTIONAL ALPHA CTR")

    assert ctr.id == "fictional-alpha-ctr"
    assert ctr.type == "CTR"
    assert ctr.icao_class == "D"
    assert ctr.lower == VerticalLimit(value=0, unit="FT", reference=LimitReference.GND)
    assert ctr.upper == VerticalLimit(value=3500, unit="FT", reference=LimitReference.MSL)
    assert ctr.fictional is False  # the loader cannot know; openAIP data is real data


def test_fixture_ctr_ring_is_closed_by_the_loader() -> None:
    ctr = by_name(load_openaip_file(FIXTURE), "FICTIONAL ALPHA CTR")

    # The file lists four positions without repeating the first one.
    assert ctr.polygon_lonlat == [
        (26.0, 44.45),
        (26.2, 44.45),
        (26.2, 44.6),
        (26.0, 44.6),
        (26.0, 44.45),
    ]


def test_fixture_tma_is_class_d_from_fl65_to_fl195_and_keeps_its_closed_ring() -> None:
    tma = by_name(load_openaip_file(FIXTURE), "FICTIONAL BRAVO TMA")

    assert tma.type == "TMA"
    assert tma.icao_class == "D"
    assert tma.lower == VerticalLimit(value=65, unit="FL", reference=LimitReference.STD)
    assert tma.upper == VerticalLimit(value=195, unit="FL", reference=LimitReference.STD)
    assert len(tma.polygon_lonlat) == 5  # already closed: not closed a second time
    assert tma.polygon_lonlat[0] == tma.polygon_lonlat[-1] == (25.8, 44.3)


def test_fixture_tmz_is_unclassified_with_limits_in_metres() -> None:
    tmz = by_name(load_openaip_file(FIXTURE), "FICTIONAL CHARLIE TMZ")

    assert tmz.type == "TMZ"
    assert tmz.icao_class is None
    assert tmz.lower == VerticalLimit(value=300, unit="M", reference=LimitReference.GND)
    assert tmz.upper == VerticalLimit(value=1500, unit="M", reference=LimitReference.MSL)


# --- code tables ------------------------------------------------------------------


def test_type_table_covers_every_documented_openaip_type_code() -> None:
    assert TYPE_BY_CODE == EXPECTED_TYPES


def test_icao_class_table() -> None:
    assert ICAO_CLASS_BY_CODE == EXPECTED_CLASSES


def test_unit_table() -> None:
    assert UNIT_BY_CODE == {0: "M", 1: "FT", 6: "FL"}


def test_reference_datum_table() -> None:
    assert REFERENCE_BY_CODE == {
        0: LimitReference.GND,
        1: LimitReference.MSL,
        2: LimitReference.STD,
    }


@pytest.mark.parametrize(("code", "expected"), EXPECTED_TYPES.items())
def test_each_type_code_becomes_its_type_string(code: int, expected: str) -> None:
    assert only_airspace(make_item(type=code)).type == expected


@pytest.mark.parametrize(("code", "expected"), [(37, "TYPE_37"), (999, "TYPE_999")])
def test_an_unknown_type_code_is_kept_as_type_n(code: int, expected: str) -> None:
    assert only_airspace(make_item(type=code)).type == expected


@pytest.mark.parametrize(("code", "expected"), EXPECTED_CLASSES.items())
def test_each_icao_class_code_becomes_its_class(code: int, expected: str | None) -> None:
    assert only_airspace(make_item(icaoClass=code)).icao_class == expected


@pytest.mark.parametrize(
    ("unit_code", "reference_code", "unit", "reference"),
    [
        (0, 0, "M", LimitReference.GND),
        (1, 1, "FT", LimitReference.MSL),
        (6, 2, "FL", LimitReference.STD),
    ],
)
def test_each_unit_and_reference_code_is_converted_for_both_limits(
    unit_code: int, reference_code: int, unit: str, reference: LimitReference
) -> None:
    item = make_item(
        lowerLimit=limit(10, unit_code, reference_code),
        upperLimit=limit(20, unit_code, reference_code),
    )

    airspace = only_airspace(item)

    assert airspace.lower == VerticalLimit(value=10, unit=unit, reference=reference)
    assert airspace.upper == VerticalLimit(value=20, unit=unit, reference=reference)


def test_limit_values_may_be_decimal_numbers() -> None:
    airspace = only_airspace(make_item(upperLimit=limit(1066.8, 0, 1)))

    assert airspace.upper.value == 1066.8


def test_lower_and_upper_limits_are_not_swapped() -> None:
    item = make_item(lowerLimit=limit(1500, 1, 1), upperLimit=limit(95, 6, 2))

    airspace = only_airspace(item)

    assert (airspace.lower.value, airspace.lower.unit) == (1500, "FT")
    assert (airspace.upper.value, airspace.upper.unit) == (95, "FL")


# --- rings ------------------------------------------------------------------------


def test_an_open_ring_of_three_positions_becomes_a_closed_triangle() -> None:
    ring = [[26.0, 44.4], [26.1, 44.4], [26.05, 44.5]]

    airspace = only_airspace(make_item(geometry=polygon(ring)))

    assert airspace.polygon_lonlat == [(26.0, 44.4), (26.1, 44.4), (26.05, 44.5), (26.0, 44.4)]


def test_an_out_of_range_ring_is_skipped_at_its_first_bad_position_without_building_it() -> None:
    ring = [[999.0, 1.0]] * 20_000  # every position out of range
    item = make_item(geometry=polygon(ring))

    tracemalloc.start()
    try:
        result = parse_openaip_airspaces([item])
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result.problems == [
        "item 1 ('FICTIONAL TEST TMA') skipped: position 1 is out of range "
        "(lon -180 to 180, lat -90 to 90)"
    ]
    # One validation error per vertex (what pydantic would build) costs megabytes here.
    assert peak_bytes < 500_000


@pytest.mark.parametrize("position", [[180.5, 44.0], [-180.5, 44.0], [26.0, 90.5], [26.0, -90.5]])
def test_the_first_out_of_range_position_is_named(position: list[float]) -> None:
    ring = [[26.0, 44.4], [26.1, 44.4], position, [26.0, 44.5], [26.0, 44.4]]

    (problem,) = parse_openaip_airspaces([make_item(geometry=polygon(ring))]).problems

    assert problem.endswith("skipped: position 3 is out of range (lon -180 to 180, lat -90 to 90)")


def test_coordinates_exactly_on_the_range_limits_are_accepted() -> None:
    ring = [[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]

    airspace = only_airspace(make_item(geometry=polygon(ring)))

    assert airspace.polygon_lonlat[2] == (180.0, 90.0)


def test_a_validation_error_is_described_by_its_first_error_and_the_count() -> None:
    with pytest.raises(ValidationError) as excinfo:
        VerticalLimit(value="x", unit="Q", reference="Z")  # type: ignore[arg-type]

    description = openaip._describe_validation_error(excinfo.value)

    assert description.startswith("value: Input should be a valid number")
    assert description.endswith(" (and 2 more)")


def test_integer_coordinates_are_accepted() -> None:
    ring = [[26, 44], [27, 44], [27, 45], [26, 45], [26, 44]]

    airspace = only_airspace(make_item(geometry=polygon(ring)))

    assert airspace.polygon_lonlat[1] == (27.0, 44.0)


# --- skipped items: the limits or the geometry cannot be read ----------------------

SKIPPED_ITEMS: dict[str, object] = {
    "item is a number": 42,
    "item is a string": "FICTIONAL",
    "item is a list": [],
    "item is null": None,
    "missing lowerLimit": make_item(lowerLimit=MISSING),
    "missing upperLimit": make_item(upperLimit=MISSING),
    "lowerLimit is a list": make_item(lowerLimit=[65, 6, 2]),
    "unit 2 is not defined": make_item(lowerLimit=limit(unit=2)),
    "unit is a boolean": make_item(lowerLimit=limit(unit=True)),
    "missing unit": make_item(lowerLimit={"value": 65, "referenceDatum": 2}),
    "referenceDatum 3 is not defined": make_item(upperLimit=limit(reference=3)),
    "missing referenceDatum": make_item(upperLimit={"value": 195, "unit": 6}),
    "missing value": make_item(upperLimit={"unit": 6, "referenceDatum": 2}),
    "value is a string": make_item(upperLimit=limit(value="195")),
    "value is a boolean": make_item(upperLimit=limit(value=True)),
    "value is null": make_item(upperLimit=limit(value=None)),
    "value is too large for a float": make_item(upperLimit=limit(value=10**400)),
    "value is not finite": make_item(upperLimit=limit(value=float("inf"))),
    "missing geometry": make_item(geometry=MISSING),
    "geometry is a list": make_item(geometry=[SQUARE]),
    "geometry type Point": make_item(geometry={"type": "Point", "coordinates": [26.0, 44.4]}),
    "geometry type MultiPolygon": make_item(
        geometry={"type": "MultiPolygon", "coordinates": [[SQUARE]]}
    ),
    "missing geometry type": make_item(geometry={"coordinates": [SQUARE]}),
    "missing coordinates": make_item(geometry={"type": "Polygon"}),
    "coordinates is an object": make_item(geometry={"type": "Polygon", "coordinates": {}}),
    "no ring": make_item(geometry=polygon()),
    "two rings (a hole)": make_item(geometry=polygon(SQUARE, SQUARE)),
    "ring is a string": make_item(geometry=polygon("26.0 44.4")),
    "ring is a flat list of numbers": make_item(geometry=polygon([26.0, 44.4, 26.1, 44.4])),
    "position has three numbers": make_item(
        geometry=polygon([[26.0, 44.4, 100.0], *SQUARE[1:4], [26.0, 44.4, 100.0]])
    ),
    "position has one number": make_item(geometry=polygon([[26.0], *SQUARE[1:]])),
    "coordinate is a string": make_item(geometry=polygon([["26.0", 44.4], *SQUARE[1:]])),
    "coordinate is a boolean": make_item(geometry=polygon([[True, 44.4], *SQUARE[1:]])),
    "coordinate is too large for a float": make_item(
        geometry=polygon([[10**400, 44.4], *SQUARE[1:]])
    ),
    "coordinate is not finite": make_item(geometry=polygon([[float("nan"), 44.4], *SQUARE[1:]])),
    "longitude above 180": make_item(
        geometry=polygon([[181.0, 44.4], *SQUARE[1:4], [181.0, 44.4]])
    ),
    "latitude below -90": make_item(geometry=polygon([[26.0, -91.0], *SQUARE[1:4], [26.0, -91.0]])),
    "ring of two positions": make_item(geometry=polygon([[26.0, 44.4], [26.1, 44.4]])),
    "closed ring of three positions": make_item(
        geometry=polygon([[26.0, 44.4], [26.1, 44.4], [26.0, 44.4]])
    ),
}


@pytest.mark.parametrize("item", SKIPPED_ITEMS.values(), ids=SKIPPED_ITEMS.keys())
def test_an_item_whose_limits_or_geometry_cannot_be_read_is_skipped(item: object) -> None:
    result = parse_openaip_airspaces([item])

    assert result.airspaces == []
    assert (result.skipped, result.defaulted) == (1, 0)
    (problem,) = result.problems
    assert problem.startswith("item 1")
    assert " skipped: " in problem


@pytest.mark.parametrize("item", SKIPPED_ITEMS.values(), ids=SKIPPED_ITEMS.keys())
def test_a_skipped_item_does_not_stop_the_items_after_it(item: object) -> None:
    items = [make_item(_id="first"), item, make_item(_id="third")]

    result = parse_openaip_airspaces(items)

    assert [a.id for a in result.airspaces] == ["first", "third"]
    assert result.skipped == 1
    assert result.problems[0].startswith("item 2")


# --- kept items: a field that only describes the airspace gets a default ------------

# (item, attribute, expected value, problem line expected?)
DEFAULTED_ITEMS: dict[str, tuple[object, str, object, bool]] = {
    "missing icaoClass": (make_item(icaoClass=MISSING), "icao_class", None, False),
    "icaoClass is null": (make_item(icaoClass=None), "icao_class", None, False),
    "icaoClass 7 is not defined": (make_item(icaoClass=7), "icao_class", None, True),
    "icaoClass is a boolean": (make_item(icaoClass=False), "icao_class", None, True),
    "icaoClass is a letter": (make_item(icaoClass="D"), "icao_class", None, True),
    "icaoClass is negative": (make_item(icaoClass=-1), "icao_class", None, True),
    "icaoClass is a decimal number": (make_item(icaoClass=3.0), "icao_class", None, True),
    "missing type": (make_item(type=MISSING), "type", "OTHER", True),
    "type is null": (make_item(type=None), "type", "OTHER", True),
    "type is a boolean": (make_item(type=True), "type", "OTHER", True),
    "type is a string": (make_item(type="4"), "type", "OTHER", True),
    "type is a decimal number": (make_item(type=4.0), "type", "OTHER", True),
    "type is negative": (make_item(type=-1), "type", "OTHER", True),
    "type is 1000 or more": (make_item(type=1000), "type", "OTHER", True),
    "missing name": (make_item(name=MISSING), "name", "fictional-0001", True),
    "name is a list": (make_item(name=["FICTIONAL"]), "name", "fictional-0001", True),
    "name is empty": (make_item(name=""), "name", "fictional-0001", True),
    "name is blank": (make_item(name="   "), "name", "fictional-0001", True),
    "missing _id": (make_item(_id=MISSING), "id", "openaip-item-1", True),
    "_id is a number": (make_item(_id=123), "id", "openaip-item-1", True),
    "_id is empty": (make_item(_id=""), "id", "openaip-item-1", True),
}


@pytest.mark.parametrize(
    ("item", "attribute", "expected", "reported"),
    DEFAULTED_ITEMS.values(),
    ids=DEFAULTED_ITEMS.keys(),
)
def test_an_unreadable_descriptive_field_gets_a_default_instead_of_dropping_the_airspace(
    item: object, attribute: str, expected: object, reported: bool
) -> None:
    result = parse_openaip_airspaces([item])

    (airspace,) = result.airspaces
    assert getattr(airspace, attribute) == expected
    assert result.skipped == 0
    assert result.defaulted == (1 if reported else 0)
    assert len(result.problems) == (1 if reported else 0)
    if reported:
        assert result.problems[0].startswith("item 1")
        assert " skipped" not in result.problems[0]


def test_an_unknown_icao_class_is_reported_and_read_as_unclassified() -> None:
    item = make_item(name="FICTIONAL ECHO", icaoClass=7)

    (problem,) = parse_openaip_airspaces([item]).problems

    assert problem == (
        "item 1 ('FICTIONAL ECHO'): icaoClass is not a known openAIP code (7), read as unclassified"
    )


def test_a_missing_type_is_reported_and_read_as_other() -> None:
    (problem,) = parse_openaip_airspaces([make_item(type=MISSING)]).problems

    assert problem == "item 1 ('FICTIONAL TEST TMA'): no type, read as OTHER"


def test_an_item_without_id_or_name_is_named_after_its_generated_id() -> None:
    result = parse_openaip_airspaces([make_item(), make_item(_id=MISSING, name=MISSING)])

    second = result.airspaces[1]
    assert (second.id, second.name) == ("openaip-item-2", "openaip-item-2")
    assert result.problems == [
        "item 2: no usable _id, read as 'openaip-item-2'; no usable name, read as 'openaip-item-2'"
    ]


def test_all_problems_of_one_item_are_on_one_line_and_it_counts_once() -> None:
    result = parse_openaip_airspaces([make_item(type="x", icaoClass="y")])

    assert result.defaulted == 1
    (problem,) = result.problems
    assert "type" in problem
    assert "icaoClass" in problem


@pytest.mark.parametrize(
    "item",
    [
        make_item(type=10**4000),
        make_item(icaoClass=10**4000),
        make_item(lowerLimit=limit(unit=10**4000)),
        make_item(upperLimit=limit(reference=10**4000)),
    ],
    ids=["type", "icaoClass", "unit", "referenceDatum"],
)
def test_a_huge_integer_code_is_not_echoed(item: object) -> None:
    (problem,) = parse_openaip_airspaces([item]).problems

    assert "a huge number" in problem
    assert len(problem) < 200


# --- duplicate ids ----------------------------------------------------------------


def test_an_exact_duplicate_is_skipped() -> None:
    items = [make_item(), make_item()]

    result = parse_openaip_airspaces(items)

    assert len(result.airspaces) == 1
    assert result.skipped == 1
    assert result.problems == [
        "item 2 ('FICTIONAL TEST TMA') skipped: same _id and content as item 1"
    ]


def test_a_different_airspace_with_an_already_used_id_is_kept_under_a_new_id() -> None:
    items = [make_item(name="FICTIONAL FIRST"), make_item(name="FICTIONAL OTHER")]

    result = parse_openaip_airspaces(items)

    assert [(a.id, a.name) for a in result.airspaces] == [
        ("fictional-0001", "FICTIONAL FIRST"),
        ("fictional-0001#2", "FICTIONAL OTHER"),
    ]
    assert (result.skipped, result.defaulted) == (0, 1)
    assert result.problems == [
        "item 2 ('FICTIONAL OTHER'): _id 'fictional-0001' already used by item 1, "
        "read as 'fictional-0001#2'"
    ]


def test_new_ids_for_repeated_ids_stay_unique() -> None:
    items = [make_item(name=f"FICTIONAL {n}") for n in range(4)] + [
        make_item(_id="fictional-0001#3", name="FICTIONAL REAL")
    ]

    ids = [a.id for a in parse_openaip_airspaces(items).airspaces]

    assert ids[:4] == ["fictional-0001", "fictional-0001#2", "fictional-0001#3", "fictional-0001#4"]
    assert len(set(ids)) == 5  # the real "#3" id that came later is renamed too


def test_a_new_id_never_takes_an_id_a_real_item_already_has() -> None:
    items = [
        make_item(_id="fictional-0001#2", name="FICTIONAL REAL"),
        make_item(name="FICTIONAL A"),
        make_item(name="FICTIONAL B"),
    ]

    ids = [a.id for a in parse_openaip_airspaces(items).airspaces]

    assert ids == ["fictional-0001#2", "fictional-0001", "fictional-0001#3"]


def test_an_id_first_seen_on_a_skipped_item_is_still_free() -> None:
    items = [make_item(geometry=MISSING), make_item()]

    result = parse_openaip_airspaces(items)

    assert [a.id for a in result.airspaces] == ["fictional-0001"]
    assert result.defaulted == 0


def test_many_items_with_the_same_id_take_linear_time() -> None:
    # Renaming by trying #2, #3, ... from the start each time would take about 2 * 10**8
    # steps here (tens of seconds); the per-id counter keeps it to one step per item.
    items = [make_item(name=f"FICTIONAL {n}") for n in range(20_000)]

    started = time.perf_counter()
    result = parse_openaip_airspaces(items)

    assert len({a.id for a in result.airspaces}) == 20_000
    assert time.perf_counter() - started < 5.0


# --- problem reporting --------------------------------------------------------------


def test_max_reported_problems_is_20() -> None:
    assert MAX_REPORTED_PROBLEMS == 20


def test_only_the_first_problems_are_kept_but_every_item_is_counted() -> None:
    items = [make_item(_id=f"broken-{i}", geometry=MISSING) for i in range(30)] + [
        make_item(_id=f"odd-{i}", type="x") for i in range(30)
    ]

    result = parse_openaip_airspaces(items)

    assert (result.skipped, result.defaulted) == (30, 30)
    assert len(result.problems) == MAX_REPORTED_PROBLEMS
    assert result.problems[-1].startswith(f"item {MAX_REPORTED_PROBLEMS} ")


def test_a_problem_does_not_echo_a_huge_name() -> None:
    item = make_item(name="N" * 10_000, geometry=MISSING)

    (problem,) = parse_openaip_airspaces([item]).problems

    assert len(problem) < 200


def test_a_problem_for_a_huge_invalid_ring_is_short() -> None:
    ring = [[500.0 + i, 44.4] for i in range(10_000)]
    item = make_item(geometry=polygon(ring))

    (problem,) = parse_openaip_airspaces([item]).problems

    assert len(problem) < 200


def test_no_items_give_an_empty_result() -> None:
    assert parse_openaip_airspaces([]) == ParseResult(
        airspaces=[], skipped=0, problems=[], defaulted=0
    )


def test_the_input_items_are_not_modified() -> None:
    item = make_item(geometry=polygon([[26.0, 44.4], [26.1, 44.4], [26.05, 44.5]]))
    before = copy.deepcopy(item)

    parse_openaip_airspaces([item])

    assert item == before  # closing the ring must not append to the caller's list


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(max_size=8),
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=8), children, max_size=5)
    ),
    max_leaves=25,
)


@given(items=st.lists(json_values, max_size=5))
def test_arbitrary_json_items_are_converted_or_skipped_never_raised(items: list[object]) -> None:
    result = parse_openaip_airspaces(items)

    assert len(result.airspaces) + result.skipped == len(items)


@given(
    field=st.sampled_from(
        ["_id", "name", "type", "icaoClass", "geometry", "lowerLimit", "upperLimit"]
    ),
    value=json_values,
)
def test_any_value_in_any_top_level_field_is_converted_or_skipped(
    field: str, value: object
) -> None:
    result = parse_openaip_airspaces([make_item(**{field: value})])

    assert len(result.airspaces) + result.skipped == 1


@given(
    path=st.sampled_from(
        [
            ("geometry", "type"),
            ("geometry", "coordinates"),
            ("lowerLimit", "value"),
            ("lowerLimit", "unit"),
            ("upperLimit", "referenceDatum"),
        ]
    ),
    value=json_values,
)
def test_any_value_in_any_nested_field_is_converted_or_skipped(
    path: tuple[str, str], value: object
) -> None:
    item = make_item()
    parent, key = path
    item[parent][key] = value  # type: ignore[index]

    result = parse_openaip_airspaces([item])

    assert len(result.airspaces) + result.skipped == 1


# --- load_openaip_file ------------------------------------------------------------


def test_load_reads_a_plain_list_of_items(tmp_path: Path) -> None:
    path = write_json(tmp_path / "ro.json", [make_item()])

    assert [a.id for a in load_openaip_file(path).airspaces] == ["fictional-0001"]


def test_load_accepts_a_string_path(tmp_path: Path) -> None:
    path = write_json(tmp_path / "ro.json", [make_item()])

    assert len(load_openaip_file(str(path)).airspaces) == 1


def test_load_ignores_other_keys_of_an_object(tmp_path: Path) -> None:
    document = {"attribution": "x", "country": "RO", "items": [make_item()], "nextPage": 2}
    path = write_json(tmp_path / "ro.json", document)

    assert len(load_openaip_file(path).airspaces) == 1


def test_load_accepts_a_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps([make_item()]).encode())

    assert len(load_openaip_file(path).airspaces) == 1


@pytest.mark.parametrize(
    "document",
    [{"page": 1}, {"items": {"a": 1}}, {"items": None}, "items", 42, None],
    ids=["no items", "items is an object", "items is null", "string", "number", "null"],
)
def test_load_rejects_json_that_is_not_a_list_or_an_object_with_items(
    tmp_path: Path, document: object
) -> None:
    path = write_json(tmp_path / "odd.json", document)

    with pytest.raises(OpenAipFileError, match="expected a list of openAIP items"):
        load_openaip_file(path)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "not valid JSON"),
        ('[{"_id": "a",}]', "not valid JSON"),
        ('[{"value": NaN}]', "not valid JSON"),
        ('[{"value": Infinity}]', "not valid JSON"),
        ('[{"value": 1e999}]', "not valid JSON"),
        ("[" + "1" * 5_000 + "]", "not valid JSON"),
        ("[" * 100_000, "nested too deeply"),
    ],
    ids=["empty", "trailing comma", "NaN", "Infinity", "overflowing float", "huge int", "deep"],
)
def test_load_rejects_invalid_json_with_a_short_message(
    tmp_path: Path, text: str, match: str
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(OpenAipFileError, match=match) as excinfo:
        load_openaip_file(path)

    assert "bad.json" in str(excinfo.value)
    assert len(str(excinfo.value)) < 200


def test_load_rejects_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "utf16.json"
    path.write_bytes('[{"name": "FICTIONAL ȘOIM"}]'.encode("utf-16"))

    with pytest.raises(OpenAipFileError, match="utf16.json is not a UTF-8 text file"):
        load_openaip_file(path)


@pytest.mark.parametrize("make", ["missing", "directory", "fifo"])
def test_load_rejects_a_path_that_is_not_a_regular_file(tmp_path: Path, make: str) -> None:
    path = tmp_path / "ro.json"
    if make == "directory":
        path.mkdir()
    elif make == "fifo":
        os.mkfifo(path)  # opening it for reading must not block
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            load_openaip_file(path)
            outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    # Run in a thread so a regression that blocks on the FIFO fails instead of hanging.
    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "load_openaip_file blocked"
    assert isinstance(outcome[0], OpenAipFileError)
    assert "not found" in str(outcome[0])


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read files without permission")
def test_load_rejects_an_unreadable_file(tmp_path: Path) -> None:
    path = write_json(tmp_path / "locked.json", [make_item()])
    path.chmod(0)
    try:
        with pytest.raises(OpenAipFileError, match="cannot open openAIP file locked.json"):
            load_openaip_file(path)
    finally:
        path.chmod(0o600)


def test_max_openaip_file_size_is_100_000_000_bytes() -> None:
    assert MAX_OPENAIP_FILE_SIZE == 100_000_000


@pytest.mark.parametrize(("extra", "accepted"), [(0, True), (1, False)])
def test_load_accepts_a_file_of_exactly_the_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: int, accepted: bool
) -> None:
    monkeypatch.setattr(openaip, "MAX_OPENAIP_FILE_SIZE", 5_000)
    data = json.dumps([make_item()]).encode()
    path = tmp_path / "padded.json"
    path.write_bytes(data + b" " * (5_000 - len(data) + extra))

    if accepted:
        assert len(load_openaip_file(path).airspaces) == 1
    else:
        with pytest.raises(OpenAipFileError, match="too large"):
            load_openaip_file(path)


def test_load_rejects_a_huge_file_without_reading_all_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openaip, "MAX_OPENAIP_FILE_SIZE", 1_000_000)
    path = tmp_path / "huge.json"
    with path.open("wb") as file:
        file.truncate(200_000_000)  # sparse: no disk space used

    tracemalloc.start()
    try:
        with pytest.raises(OpenAipFileError, match="too large"):
            load_openaip_file(path)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 5_000_000


# --- fetch_airspaces: the openAIP API through httpx.MockTransport -------------------

FAKE_KEY = "fictional-test-key-0123456789abcdef"  # never a real key
Handler = Callable[[httpx.Request], httpx.Response]


def page_body(items: list[object], page: int, pages: int) -> dict[str, object]:
    body: dict[str, object] = {
        "page": page,
        "limit": PAGE_SIZE,
        "totalCount": len(items),
        "totalPages": pages,
        "items": items,
    }
    if page < pages:
        body["nextPage"] = page + 1
    return body


class FakeOpenAip:
    """Serves the given pages like the openAIP API and records every request."""

    def __init__(self, *pages: list[object]) -> None:
        self.pages = pages
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=page_body(self.pages[page - 1], page, len(self.pages)))


def mock_client(handler: Handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def fetch_with(handler: Handler, country: str = "RO", api_key: str = FAKE_KEY) -> list[object]:
    with mock_client(handler) as client:
        return fetch_airspaces(country, api_key, client=client)


def respond(response: httpx.Response) -> Handler:
    """A handler that answers every request with the same response."""
    return lambda request: response


def raise_error(error: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


def assert_key_not_shown(exc: BaseException) -> None:
    shown = str(exc) + repr(exc) + "".join(traceback.format_exception(exc))
    assert FAKE_KEY not in shown


def test_fetch_requests_pages_until_the_last_one_and_keeps_the_order() -> None:
    server = FakeOpenAip(
        [make_item(_id="a"), make_item(_id="b")], [make_item(_id="c")], [make_item(_id="d")]
    )

    items = fetch_with(server)

    assert [item["_id"] for item in items] == ["a", "b", "c", "d"]  # type: ignore[index]
    assert [r.url.params["page"] for r in server.requests] == ["1", "2", "3"]


def test_fetch_a_single_page_without_next_page() -> None:
    server = FakeOpenAip([make_item()])

    assert len(fetch_with(server)) == 1
    assert len(server.requests) == 1


def test_fetch_an_empty_country() -> None:
    assert fetch_with(FakeOpenAip([])) == []


def test_fetch_calls_get_airspaces_with_country_page_and_limit() -> None:
    server = FakeOpenAip([])

    fetch_with(server)

    (request,) = server.requests
    assert request.method == "GET"
    assert (request.url.scheme, request.url.host, request.url.path) == (
        "https",
        "api.core.openaip.net",
        "/api/airspaces",
    )
    assert dict(request.url.params) == {"country": "RO", "page": "1", "limit": "1000"}


def test_fetch_sends_the_key_in_the_header_and_never_in_the_url() -> None:
    server = FakeOpenAip([make_item(_id="a")], [make_item(_id="b")])

    fetch_with(server)

    for request in server.requests:
        assert request.headers["x-openaip-api-key"] == FAKE_KEY
        assert FAKE_KEY not in str(request.url)
        assert "apiKey" not in request.url.params


def test_fetch_identifies_itself_and_asks_for_json() -> None:
    server = FakeOpenAip([])

    fetch_with(server)

    (request,) = server.requests
    assert request.headers["user-agent"] == f"take2/{take2.__version__}"
    assert request.headers["accept"] == "application/json"


def test_every_request_has_explicit_timeouts() -> None:
    server = FakeOpenAip([], [])

    fetch_with(server)

    for request in server.requests:
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }


def test_request_timeout_values() -> None:
    timeout = REQUEST_TIMEOUT

    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (10.0, 30.0, 30.0, 30.0)


def test_fetch_without_a_client_uses_its_own_with_timeouts_and_no_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[httpx.Client, dict[str, object]]] = []
    real_client = httpx.Client

    def make_client(**kwargs: object) -> httpx.Client:
        client = real_client(transport=httpx.MockTransport(FakeOpenAip([make_item()])), **kwargs)
        created.append((client, kwargs))
        return client

    monkeypatch.setattr(openaip.httpx, "Client", make_client)

    assert len(fetch_airspaces("RO", FAKE_KEY)) == 1

    ((client, kwargs),) = created
    assert kwargs["timeout"] == REQUEST_TIMEOUT
    assert kwargs["follow_redirects"] is False
    assert client.is_closed


def test_fetch_leaves_a_callers_client_open() -> None:
    with mock_client(FakeOpenAip([])) as client:
        fetch_airspaces("RO", FAKE_KEY, client=client)

        assert not client.is_closed


@pytest.mark.parametrize("country", ["ro", "Ro", "RO"])
def test_fetch_upper_cases_the_country(country: str) -> None:
    server = FakeOpenAip([])

    fetch_with(server, country=country)

    assert server.requests[0].url.params["country"] == "RO"


@pytest.mark.parametrize(
    "country",
    ["", "R", "ROU", "R1", "..", "R/", "RÖ", "ＲＯ", " RO", "RO\n", "R\x00"],
    ids=repr,
)
def test_an_invalid_country_is_rejected_before_any_request(country: str) -> None:
    server = FakeOpenAip([])

    with pytest.raises(ValueError, match="two-letter ISO 3166-1 country code"):
        fetch_with(server, country=country)

    assert server.requests == []


def test_normalize_country() -> None:
    assert normalize_country("ro") == "RO"


def test_an_invalid_country_message_is_short() -> None:
    with pytest.raises(ValueError) as excinfo:
        normalize_country("X" * 10_000)

    assert len(str(excinfo.value)) < 150


@pytest.mark.parametrize(
    "api_key",
    ["", "has space", "line\nbreak", "tab\tkey", "ünïcödé", "k" * 513],
    ids=["empty", "space", "newline", "tab", "non-ASCII", "too long"],
)
def test_an_unusable_api_key_is_rejected_before_any_request(api_key: str) -> None:
    server = FakeOpenAip([])

    with pytest.raises(ValueError, match="API key") as excinfo:
        fetch_with(server, api_key=api_key)

    assert server.requests == []
    if api_key:
        assert api_key not in str(excinfo.value)


def test_an_api_key_of_512_visible_characters_is_accepted() -> None:
    key = ("".join(chr(code) for code in range(0x21, 0x7F)) * 6)[:512]  # all visible ASCII
    server = FakeOpenAip([])

    fetch_with(server, api_key=key)

    assert server.requests[0].headers["x-openaip-api-key"] == key


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_api_key_is_reported_with_the_status(status: int) -> None:
    with pytest.raises(OpenAipFetchError) as excinfo:
        fetch_with(respond(httpx.Response(status, json={"message": "invalid key"})))

    assert str(excinfo.value) == f"openAIP rejected the API key (HTTP {status})"
    assert excinfo.value.status_code == status
    assert_key_not_shown(excinfo.value)


def test_a_404_points_at_the_api_key() -> None:
    # Observed on the live API (2026-11-18): an unknown key gets HTTP 404 with
    # {"message": "Failed to load user permissions. Not Found", "code": "app/not-found"};
    # no key at all gets 403.
    body = {"message": "Failed to load user permissions. Not Found", "code": "app/not-found"}

    with pytest.raises(OpenAipFetchError) as excinfo:
        fetch_with(respond(httpx.Response(404, json=body)))

    assert str(excinfo.value) == (
        "openAIP request failed with HTTP 404 (openAIP answers 404 for an API key it does "
        "not know; check OPENAIP_API_KEY)"
    )
    assert excinfo.value.status_code == 404


@pytest.mark.parametrize("status", [404, 429, 500, 502, 503])
def test_other_http_errors_are_reported_with_the_status(status: int) -> None:
    with pytest.raises(OpenAipFetchError, match=f"HTTP {status}") as excinfo:
        fetch_with(respond(httpx.Response(status, text="<html>error</html>")))

    assert excinfo.value.status_code == status


def test_an_http_error_on_a_later_page_fails_the_whole_fetch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=page_body([make_item()], 1, 2))
        return httpx.Response(500)

    with pytest.raises(OpenAipFetchError, match="HTTP 500"):
        fetch_with(handler)


def test_a_redirect_is_not_followed_so_the_key_cannot_leave_openaip() -> None:
    server_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        server_requests.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/steal"})

    with pytest.raises(OpenAipFetchError, match="HTTP 302") as excinfo:
        fetch_with(handler)

    assert [r.url.host for r in server_requests] == ["api.core.openaip.net"]
    assert excinfo.value.status_code == 302


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (b"<html>maintenance</html>", "not valid JSON"),
        (b"", "not valid JSON"),
        (b'{"items": [NaN]}', "not valid JSON"),
        (b"\xff\xfe{}", "not a UTF-8 text file"),
        (b"[" * 100_000, "nested too deeply"),
        (b"[]", "expected a JSON object"),
        (b'"items"', "expected a JSON object"),
        (b'{"page": 1}', "no 'items' list"),
        (b'{"items": {"a": 1}}', "no 'items' list"),
        (b'{"items": [1, 2]}', "every item must be a JSON object"),
        (b'{"items": [], "nextPage": 1}', "unexpected nextPage"),
        (b'{"items": [], "nextPage": 3}', "unexpected nextPage"),
        (b'{"items": [], "nextPage": "2"}', "unexpected nextPage"),
        (b'{"items": [], "nextPage": true}', "unexpected nextPage"),
        (b'{"items": [], "nextPage": false}', "unexpected nextPage"),
        (b'{"items": [], "nextPage": 0}', "unexpected nextPage"),
        (b'{"items": [], "nextPage": 2.0}', "unexpected nextPage"),
    ],
    ids=[
        "html",
        "empty body",
        "NaN",
        "UTF-16",
        "deep nesting",
        "list",
        "string",
        "no items",
        "items is an object",
        "items are numbers",
        "nextPage repeats the page",
        "nextPage skips a page",
        "nextPage is a string",
        "nextPage is true",
        "nextPage is false",
        "nextPage is 0",
        "nextPage is a decimal",
    ],
)
def test_a_malformed_response_is_reported_not_raised_raw(content: bytes, match: str) -> None:
    with pytest.raises(OpenAipFetchError, match=match) as excinfo:
        fetch_with(respond(httpx.Response(200, content=content)))

    assert excinfo.value.status_code == 200
    assert len(str(excinfo.value)) < 200


def test_next_page_null_means_the_last_page() -> None:
    body = {"items": [make_item()], "nextPage": None}

    assert len(fetch_with(respond(httpx.Response(200, json=body)))) == 1


def test_download_limits_have_their_documented_values() -> None:
    assert (MAX_PAGES, PAGE_SIZE, MAX_PAGE_SECONDS) == (100, 1000, 120.0)
    assert (MAX_PAGE_BYTES, MAX_DOWNLOAD_BYTES) == (50_000_000, 100_000_000)
    assert MAX_DOWNLOAD_BYTES <= MAX_OPENAIP_FILE_SIZE  # a download can always be read back


def test_a_server_that_never_reaches_the_last_page_is_stopped_after_100_pages() -> None:
    requests: list[httpx.Request] = []

    def endless(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"items": [], "nextPage": page + 1})

    with pytest.raises(OpenAipFetchError, match="more than 100 pages"):
        fetch_with(endless)

    assert len(requests) == MAX_PAGES


def test_exactly_100_pages_are_accepted() -> None:
    server = FakeOpenAip(*([make_item(_id=f"p{page}")] for page in range(1, MAX_PAGES + 1)))

    assert len(fetch_with(server)) == MAX_PAGES


def counting_stream(chunk: bytes, chunks: int | None) -> tuple[Iterator[bytes], list[int]]:
    """A streamed body of ``chunks`` copies of ``chunk`` (endless if None); counts reads."""
    sent = [0]

    def generate() -> Iterator[bytes]:
        for _ in itertools.repeat(None) if chunks is None else range(chunks):
            sent[0] += 1
            yield chunk

    return generate(), sent


def test_a_declared_content_length_over_the_page_limit_is_rejected_before_reading() -> None:
    body, sent = counting_stream(b"x" * 1000, 10)
    response = httpx.Response(
        200, headers={"content-length": str(MAX_PAGE_BYTES + 1)}, content=body
    )

    with pytest.raises(OpenAipFetchError, match="larger than 50,000,000 bytes"):
        fetch_with(respond(response))

    assert sent[0] == 0


def test_fetch_asks_for_an_uncompressed_body() -> None:
    server = FakeOpenAip([], [])

    fetch_with(server)

    for request in server.requests:
        assert request.headers["accept-encoding"] == "identity"


def gzip_bomb(decompressed_bytes: int) -> bytes:
    """A gzip stream of ``decompressed_bytes`` spaces, built without holding them in memory."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, 31)  # wbits 31: gzip container
    chunk = b" " * 1_000_000
    parts = [compressor.compress(chunk) for _ in range(decompressed_bytes // len(chunk))]
    parts.append(compressor.compress(b" " * (decompressed_bytes % len(chunk))))
    parts.append(compressor.flush())
    return b"".join(parts)


def test_a_compressed_page_is_rejected_before_anything_is_decompressed() -> None:
    bomb = gzip_bomb(MAX_PAGE_BYTES + 10_000_000)  # 60 MB of JSON whitespace when inflated
    assert len(bomb) < 200_000  # what travels over the wire is small
    # Streamed in network-sized chunks, as a real server sends it (a bytes body would make
    # httpx.Response inflate it here, in the test, before the fetch even starts).
    chunks = (bomb[i : i + 65_536] for i in range(0, len(bomb), 65_536))
    response = httpx.Response(200, headers={"content-encoding": "gzip"}, content=chunks)

    tracemalloc.start()
    try:
        with pytest.raises(OpenAipFetchError, match="Content-Encoding 'gzip'") as excinfo:
            fetch_with(respond(response))
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 5_000_000  # the 60 MB were never inflated
    assert excinfo.value.status_code == 200
    assert_key_not_shown(excinfo.value)


@pytest.mark.parametrize(
    "encoding", ["gzip", "deflate", "br", "zstd", "GZIP", "gzip, identity", "x-custom"]
)
def test_any_content_encoding_other_than_identity_is_rejected(encoding: str) -> None:
    # A streamed body: httpx.Response would try to decode a bytes body right here.
    content = iter([b'{"items": []}'])
    response = httpx.Response(200, headers={"content-encoding": encoding}, content=content)

    with pytest.raises(OpenAipFetchError, match="Content-Encoding"):
        fetch_with(respond(response))


def test_a_second_content_encoding_header_is_not_overlooked() -> None:
    headers = [("content-encoding", "identity"), ("content-encoding", "gzip")]
    response = httpx.Response(200, headers=headers, content=iter([gzip_bomb(10)]))

    with pytest.raises(OpenAipFetchError, match="Content-Encoding 'identity, gzip'"):
        fetch_with(respond(response))


@pytest.mark.parametrize("encoding", ["identity", " Identity ", ""])
def test_an_identity_content_encoding_is_accepted(encoding: str) -> None:
    body = json.dumps({"items": [make_item()]}).encode()
    response = httpx.Response(200, headers={"content-encoding": encoding}, content=body)

    assert len(fetch_with(respond(response))) == 1


def test_an_error_status_is_reported_even_with_a_compressed_body() -> None:
    response = httpx.Response(401, headers={"content-encoding": "gzip"}, content=gzip_bomb(10))

    with pytest.raises(OpenAipFetchError, match=r"rejected the API key \(HTTP 401\)"):
        fetch_with(respond(response))


def test_an_endless_streamed_page_is_cut_off_at_the_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(openaip, "MAX_PAGE_BYTES", 1_000)
    body, sent = counting_stream(b"x" * 100, None)  # no content-length, never ends

    with pytest.raises(OpenAipFetchError, match="larger than 1,000 bytes"):
        fetch_with(respond(httpx.Response(200, content=body)))

    assert sent[0] <= 11


@pytest.mark.parametrize(("extra", "accepted"), [(0, True), (1, False)])
def test_a_page_of_exactly_the_page_limit_is_accepted(
    monkeypatch: pytest.MonkeyPatch, extra: int, accepted: bool
) -> None:
    monkeypatch.setattr(openaip, "MAX_PAGE_BYTES", 5_000)
    data = json.dumps({"items": [make_item()]}).encode()
    body, _ = counting_stream(data + b" " * (5_000 - len(data) + extra), 1)
    handler = respond(httpx.Response(200, content=body))

    if accepted:
        assert len(fetch_with(handler)) == 1
    else:
        with pytest.raises(OpenAipFetchError, match="larger than 5,000 bytes"):
            fetch_with(handler)


def test_all_pages_together_are_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    page = json.dumps(page_body([make_item()], 1, 2)).encode()
    monkeypatch.setattr(openaip, "MAX_DOWNLOAD_BYTES", len(page) * 3 // 2)

    def handler(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params["page"])
        return httpx.Response(200, json=page_body([make_item(_id=f"p{number}")], number, 5))

    with pytest.raises(OpenAipFetchError, match="download is larger than"):
        fetch_with(handler)


def test_a_page_that_trickles_in_too_slowly_is_abandoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = itertools.count(start=0.0, step=100.0)  # every clock reading is 100 s later
    monkeypatch.setattr(openaip, "_clock", lambda: next(ticks))
    body, sent = counting_stream(b" ", 100)

    with pytest.raises(OpenAipFetchError, match="too slowly"):
        fetch_with(respond(httpx.Response(200, content=body)))

    assert sent[0] < 100


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectTimeout("timed out"), httpx.ReadTimeout("timed out"), httpx.PoolTimeout("")],
    ids=["connect", "read", "pool"],
)
def test_a_timeout_is_reported(error: Exception) -> None:
    with pytest.raises(OpenAipFetchError, match="openAIP did not answer in time") as excinfo:
        fetch_with(raise_error(error))

    assert excinfo.value.status_code is None


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("[Errno 8] nodename nor servname provided"),
        httpx.RemoteProtocolError("peer closed connection"),
        httpx.DecodingError("incorrect header check"),
    ],
    ids=["connect", "protocol", "decoding"],
)
def test_a_network_error_is_reported_with_its_kind(error: Exception) -> None:
    with pytest.raises(OpenAipFetchError, match="cannot reach openAIP") as excinfo:
        fetch_with(raise_error(error))

    assert type(error).__name__ in str(excinfo.value)
    assert excinfo.value.status_code is None


def test_a_network_error_that_quotes_the_key_is_redacted() -> None:
    error = httpx.LocalProtocolError(f"Illegal header value b'{FAKE_KEY}'")

    with pytest.raises(OpenAipFetchError) as excinfo:
        fetch_with(raise_error(error))

    assert "[redacted]" in str(excinfo.value)
    assert_key_not_shown(excinfo.value)


@pytest.mark.parametrize(
    "handler",
    [
        respond(httpx.Response(401)),
        respond(httpx.Response(500, text=FAKE_KEY)),
        respond(httpx.Response(200, content=b"not json " + FAKE_KEY.encode())),
        raise_error(httpx.ConnectError(f"refused for {FAKE_KEY}")),
        raise_error(httpx.ReadTimeout(f"slow {FAKE_KEY}")),
    ],
    ids=["401", "500 echoing the key", "bad JSON echoing the key", "connect", "timeout"],
)
def test_no_failure_shows_the_key_in_its_message_repr_or_traceback(handler: Handler) -> None:
    with pytest.raises(OpenAipFetchError) as excinfo:
        fetch_with(handler)

    assert_key_not_shown(excinfo.value)
    # No chained httpx exception (which holds the request and its headers) is displayed.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None or excinfo.value.__suppress_context__


def test_logging_during_a_fetch_never_shows_the_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    fetch_with(FakeOpenAip([make_item(_id="a")], [make_item(_id="b")]))

    assert caplog.records  # httpx logs each request, so the check below is not vacuous
    assert FAKE_KEY not in caplog.text


# --- save_openaip_file ------------------------------------------------------------


def test_a_saved_file_loads_back_into_the_same_airspaces(tmp_path: Path) -> None:
    items = [make_item(_id="a"), make_item(_id="b", type=4), make_item(_id="c", geometry=None)]
    path = tmp_path / "ro.json"

    save_openaip_file(path, items, country="RO")

    result = load_openaip_file(path)
    assert [a.id for a in result.airspaces] == ["a", "b"]
    assert result.skipped == 1  # the raw items are saved as they came, broken ones included


def test_a_saved_file_carries_the_attribution_country_and_raw_items(tmp_path: Path) -> None:
    items = [make_item(name="FICTIONAL ȘOIM CTR")]
    path = tmp_path / "ro.json"

    save_openaip_file(path, items, country="ro")

    data = path.read_bytes()
    assert "FICTIONAL ȘOIM CTR".encode() in data  # UTF-8, not \u escapes
    document = json.loads(data)
    assert document["attribution"] == ATTRIBUTION
    assert document["country"] == "RO"
    assert document["items"] == items
    fetched_at = datetime.fromisoformat(document["fetchedAt"])
    assert fetched_at.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - fetched_at) < timedelta(minutes=5)


def test_the_attribution_text() -> None:
    assert ATTRIBUTION == "Airspace data © openAIP contributors (openaip.net), CC BY-NC 4.0"


def test_save_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "data" / "nested" / "ro.json"

    save_openaip_file(path, [make_item()], country="RO")

    assert len(load_openaip_file(path).airspaces) == 1


def test_save_replaces_an_existing_file_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "ro.json"
    path.write_text("old content", encoding="utf-8")

    save_openaip_file(path, [make_item()], country="RO")

    assert len(load_openaip_file(path).airspaces) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["ro.json"]


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_a_failed_save_keeps_the_old_file_and_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    path = tmp_path / "ro.json"
    path.write_text("old content", encoding="utf-8")

    def fail(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(openaip.os, failing, fail)

    with pytest.raises(OSError, match="No space left"):
        save_openaip_file(path, [make_item()], country="RO")

    assert path.read_text(encoding="utf-8") == "old content"
    assert [p.name for p in tmp_path.iterdir()] == ["ro.json"]


def test_saving_onto_a_directory_fails_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "ro.json"
    path.mkdir()

    with pytest.raises(OSError):
        save_openaip_file(path, [make_item()], country="RO")

    assert [p.name for p in tmp_path.iterdir()] == ["ro.json"]


@pytest.mark.parametrize(
    "items",
    [[{"value": float("nan")}], [{"value": {1, 2}}]],
    ids=["NaN", "a set"],
)
def test_items_that_are_not_standard_json_are_not_saved(tmp_path: Path, items: list) -> None:
    path = tmp_path / "ro.json"

    with pytest.raises(OpenAipFileError, match="cannot be written as JSON"):
        save_openaip_file(path, items, country="RO")

    assert not path.exists()


def test_items_nested_too_deeply_are_not_saved(tmp_path: Path) -> None:
    nested: list[object] = []
    for _ in range(100_000):
        nested = [nested]
    path = tmp_path / "ro.json"

    with pytest.raises(OpenAipFileError, match="cannot be written as JSON"):
        save_openaip_file(path, [{"geometry": nested}], country="RO")

    assert not path.exists()


def test_data_too_large_to_read_back_is_not_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openaip, "MAX_OPENAIP_FILE_SIZE", 1_000)
    path = tmp_path / "ro.json"

    with pytest.raises(OpenAipFileError, match="larger than 1,000 bytes"):
        save_openaip_file(path, [make_item(_id=f"i{n}") for n in range(10)], country="RO")

    assert not path.exists()


def test_save_rejects_an_invalid_country(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="two-letter"):
        save_openaip_file(tmp_path / "x.json", [], country="ROU")


# --- read_api_key -----------------------------------------------------------------


@pytest.fixture
def no_key_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)


def env_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_environment_variable_wins_over_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAIP_API_KEY", "key-from-environment")

    assert read_api_key(env_file(tmp_path, "OPENAIP_API_KEY=key-from-file\n")) == (
        "key-from-environment"
    )


def test_surrounding_whitespace_of_the_environment_variable_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAIP_API_KEY", "  key-from-environment\n")

    assert read_api_key(tmp_path / ".env") == "key-from-environment"


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_environment_variable_falls_back_to_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("OPENAIP_API_KEY", value)

    assert read_api_key(env_file(tmp_path, "OPENAIP_API_KEY=key-from-file\n")) == "key-from-file"


@pytest.mark.usefixtures("no_key_in_environment")
def test_no_environment_variable_and_no_env_file_gives_none(tmp_path: Path) -> None:
    assert read_api_key(tmp_path / ".env") is None


@pytest.mark.usefixtures("no_key_in_environment")
def test_the_default_env_file_is_dot_env_in_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file(tmp_path, "OPENAIP_API_KEY=key-from-cwd\n")
    monkeypatch.chdir(tmp_path)

    assert read_api_key() == "key-from-cwd"


ENV_FILES = {
    "plain": ("OPENAIP_API_KEY=abc123\n", "abc123"),
    "no final newline": ("OPENAIP_API_KEY=abc123", "abc123"),
    "spaces around =": ("OPENAIP_API_KEY = abc123  \n", "abc123"),
    "leading spaces": ("   OPENAIP_API_KEY=abc123\n", "abc123"),
    "export prefix": ("export OPENAIP_API_KEY=abc123\n", "abc123"),
    "export with tabs": ("export\t\tOPENAIP_API_KEY=abc123\n", "abc123"),
    "double quotes": ('OPENAIP_API_KEY="abc123"\n', "abc123"),
    "single quotes": ("OPENAIP_API_KEY='abc123'\n", "abc123"),
    "quotes keep inner spaces": ('OPENAIP_API_KEY=" abc 123 "\n', " abc 123 "),
    "comment after the value": ("OPENAIP_API_KEY=abc123 # my key\n", "abc123"),
    "comment after quotes": ('OPENAIP_API_KEY="abc#123"   # my key\n', "abc#123"),
    "hash inside a value": ("OPENAIP_API_KEY=abc#123\n", "abc#123"),
    "commented-out line": ("# OPENAIP_API_KEY=old\nOPENAIP_API_KEY=new\n", "new"),
    "other keys around": ("A=1\nOPENAIP_API_KEY=abc123\nB=2\n", "abc123"),
    "last assignment wins": ("OPENAIP_API_KEY=first\nOPENAIP_API_KEY=second\n", "second"),
    "CRLF line endings": ("A=1\r\nOPENAIP_API_KEY=abc123\r\n", "abc123"),
    "blank lines": ("\n\n   \nOPENAIP_API_KEY=abc123\n\n", "abc123"),
    "value with =": ("OPENAIP_API_KEY=abc=123==\n", "abc=123=="),
    "only similar names": ("OPENAIP_API_KEY_OLD=x\nMY_OPENAIP_API_KEY=y\n", None),
    "empty value": ("OPENAIP_API_KEY=\n", None),
    "empty quotes": ('OPENAIP_API_KEY=""\n', None),
    "only a comment": ("OPENAIP_API_KEY= # fill in\n", None),
    "no equals sign": ("OPENAIP_API_KEY\n", None),
    "colon instead of =": ("OPENAIP_API_KEY: abc123\n", None),
    "empty file": ("", None),
    "later empty assignment clears it": ("OPENAIP_API_KEY=abc\nOPENAIP_API_KEY=\n", None),
}


@pytest.mark.usefixtures("no_key_in_environment")
@pytest.mark.parametrize(("text", "expected"), ENV_FILES.values(), ids=ENV_FILES.keys())
def test_env_file_lines(tmp_path: Path, text: str, expected: str | None) -> None:
    assert read_api_key(env_file(tmp_path, text)) == expected


@pytest.mark.usefixtures("no_key_in_environment")
def test_env_file_with_a_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfOPENAIP_API_KEY=abc123\n")

    assert read_api_key(path) == "abc123"


@pytest.mark.usefixtures("no_key_in_environment")
@pytest.mark.parametrize("text", ['OPENAIP_API_KEY="abc123\n', "OPENAIP_API_KEY='abc123\n"])
def test_an_unclosed_quote_is_an_error_that_does_not_echo_the_value(
    tmp_path: Path, text: str
) -> None:
    with pytest.raises(ValueError, match="closing quote") as excinfo:
        read_api_key(env_file(tmp_path, text))

    assert "abc123" not in str(excinfo.value)


@pytest.mark.usefixtures("no_key_in_environment")
def test_an_env_file_that_is_not_utf8_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(b"OPENAIP_API_KEY=abc\xff\n")

    with pytest.raises(ValueError, match=r"\.env is not a UTF-8 text file") as excinfo:
        read_api_key(path)

    assert "abc" not in str(excinfo.value)


@pytest.mark.usefixtures("no_key_in_environment")
def test_an_env_file_larger_than_1_mb_is_an_error(tmp_path: Path) -> None:
    path = env_file(tmp_path, "#" * MAX_ENV_FILE_SIZE + "\nOPENAIP_API_KEY=abc\n")

    with pytest.raises(ValueError, match="larger than 1,000,000 bytes"):
        read_api_key(path)


@pytest.mark.usefixtures("no_key_in_environment")
def test_a_huge_env_file_is_rejected_without_reading_all_of_it(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    with path.open("wb") as file:
        file.truncate(200_000_000)  # sparse: no disk space used

    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="larger than"):
            read_api_key(path)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak_bytes < 5_000_000


def test_max_env_file_size_is_1_000_000_bytes() -> None:
    assert MAX_ENV_FILE_SIZE == 1_000_000


@pytest.mark.usefixtures("no_key_in_environment")
@pytest.mark.parametrize("make", ["directory", "fifo"])
def test_an_env_path_that_is_not_a_regular_file_is_ignored(tmp_path: Path, make: str) -> None:
    path = tmp_path / ".env"
    path.mkdir() if make == "directory" else os.mkfifo(path)
    outcome: list[object] = []

    worker = threading.Thread(target=lambda: outcome.append(read_api_key(path)), daemon=True)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "read_api_key blocked"
    assert outcome == [None]
