"""Tests for take2.io.openaip (SPEC.md section 5, milestone 5).

No test touches the network: HTTP goes through ``httpx.MockTransport``. Every airspace is
fictional, and the API key is a made-up string.
"""

import copy
import json
import os
import threading
import tracemalloc
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from take2.io import openaip
from take2.io.openaip import (
    ICAO_CLASS_BY_CODE,
    MAX_OPENAIP_FILE_SIZE,
    MAX_REPORTED_PROBLEMS,
    REFERENCE_BY_CODE,
    TYPE_BY_CODE,
    UNIT_BY_CODE,
    OpenAipFileError,
    ParseResult,
    load_openaip_file,
    parse_openaip_airspaces,
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


def test_fixture_converts_the_three_valid_airspaces_in_file_order() -> None:
    result = load_openaip_file(FIXTURE)

    assert [a.name for a in result.airspaces] == [
        "FICTIONAL ALPHA CTR",
        "FICTIONAL BRAVO TMA",
        "FICTIONAL CHARLIE TMZ",
    ]


def test_fixture_skips_and_counts_the_item_with_point_geometry() -> None:
    result = load_openaip_file(FIXTURE)

    assert result.skipped == 1
    (problem,) = result.problems
    assert problem.startswith("item 4 ('FICTIONAL DELTA BROKEN'): ")
    assert "Point" in problem


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


def test_integer_coordinates_are_accepted() -> None:
    ring = [[26, 44], [27, 44], [27, 45], [26, 45], [26, 44]]

    airspace = only_airspace(make_item(geometry=polygon(ring)))

    assert airspace.polygon_lonlat[1] == (27.0, 44.0)


# --- skipped items ----------------------------------------------------------------

BROKEN_ITEMS: dict[str, object] = {
    "item is a number": 42,
    "item is a string": "FICTIONAL",
    "item is a list": [],
    "item is null": None,
    "missing _id": make_item(_id=MISSING),
    "_id is a number": make_item(_id=123),
    "_id is empty": make_item(_id=""),
    "missing name": make_item(name=MISSING),
    "name is a list": make_item(name=["FICTIONAL"]),
    "name is empty": make_item(name=""),
    "missing type": make_item(type=MISSING),
    "type is a boolean": make_item(type=True),
    "type is a string": make_item(type="4"),
    "type is a decimal number": make_item(type=4.0),
    "type is negative": make_item(type=-1),
    "type is 1000 or more": make_item(type=1000),
    "missing icaoClass": make_item(icaoClass=MISSING),
    "icaoClass is null": make_item(icaoClass=None),
    "icaoClass 7 is not defined": make_item(icaoClass=7),
    "icaoClass is a boolean": make_item(icaoClass=False),
    "icaoClass is a letter": make_item(icaoClass="D"),
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


@pytest.mark.parametrize("item", BROKEN_ITEMS.values(), ids=BROKEN_ITEMS.keys())
def test_a_broken_item_is_skipped_and_counted(item: object) -> None:
    result = parse_openaip_airspaces([item])

    assert result.airspaces == []
    assert result.skipped == 1
    (problem,) = result.problems
    assert problem.startswith("item 1")


@pytest.mark.parametrize("item", BROKEN_ITEMS.values(), ids=BROKEN_ITEMS.keys())
def test_a_broken_item_does_not_stop_the_items_after_it(item: object) -> None:
    items = [make_item(_id="first"), item, make_item(_id="third")]

    result = parse_openaip_airspaces(items)

    assert [a.id for a in result.airspaces] == ["first", "third"]
    assert result.skipped == 1
    assert result.problems[0].startswith("item 2")


def test_the_problem_names_the_airspace_and_the_reason() -> None:
    item = make_item(name="FICTIONAL ECHO", icaoClass=7)

    (problem,) = parse_openaip_airspaces([item]).problems

    assert problem == "item 1 ('FICTIONAL ECHO'): unknown icaoClass 7"


def test_an_item_with_an_already_seen_id_is_skipped() -> None:
    items = [make_item(name="FICTIONAL FIRST"), make_item(name="FICTIONAL COPY")]

    result = parse_openaip_airspaces(items)

    assert [a.name for a in result.airspaces] == ["FICTIONAL FIRST"]
    assert result.skipped == 1
    assert "duplicate _id 'fictional-0001'" in result.problems[0]


def test_max_reported_problems_is_20() -> None:
    assert MAX_REPORTED_PROBLEMS == 20


def test_only_the_first_problems_are_kept_but_every_skip_is_counted() -> None:
    items = [make_item(_id=f"broken-{i}", type="x") for i in range(50)]

    result = parse_openaip_airspaces(items)

    assert result.skipped == 50
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
    assert parse_openaip_airspaces([]) == ParseResult(airspaces=[], skipped=0, problems=[])


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
