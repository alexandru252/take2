"""Tests for ``take2 check`` (SPEC.md section 8) and its link to the severity configuration."""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from take2.analysis import DEFAULT_SEVERITY_FILE, load_severity_rules
from take2.cli import app
from take2.io.openaip import ATTRIBUTION, TYPE_BY_CODE
from test_analysis import CENTER, HALF_SIZE_M, offset, square_ring

runner = CliRunner()
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def plain(text: str) -> str:
    """Remove ANSI styling: typer forces colours when GITHUB_ACTIONS is set (as on CI)."""
    return _ANSI_ESCAPE_RE.sub("", text)


def openaip_item(item_id: str, name: str, type_code: int, icao_class: int, lower, upper, ring):
    """One airspace in openAIP API format (unit 0 m, 1 ft, 6 FL; datum 0 GND, 1 MSL, 2 STD)."""
    return {
        "_id": item_id,
        "name": name,
        "type": type_code,
        "icaoClass": icao_class,
        "lowerLimit": dict(zip(("value", "unit", "referenceDatum"), lower, strict=True)),
        "upperLimit": dict(zip(("value", "unit", "referenceDatum"), upper, strict=True)),
        "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring]]},
        "country": "RO",
    }


SQUARE = square_ring(*CENTER, HALF_SIZE_M)
CTR_ITEM = openaip_item("ctr1", "FICTIONAL CTR", 4, 3, (0, 1, 0), (3500, 1, 1), SQUARE)
TMA_ITEM = openaip_item("tma1", "FICTIONAL TMA", 7, 3, (65, 6, 2), (195, 6, 2), SQUARE)


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_airspace(path: Path, items: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"attribution": ATTRIBUTION, "country": "RO", "items": items}))
    return path


def write_route(path: Path, alt_ft: float | None) -> Path:
    """West to east through the centre of the square, 30 km either side of it."""
    (w_lon, w_lat), (e_lon, e_lat) = offset(*CENTER, 270, 30_000), offset(*CENTER, 90, 30_000)
    alt = "" if alt_ft is None else str(alt_ft)
    path.write_text(
        f"name,lat,lon,alt_ft\nWEST,{w_lat},{w_lon},{alt}\nEAST,{e_lat},{e_lon},{alt}\n"
    )
    return path


def check(*args: str):  # noqa: ANN201 - typer's Result type is not public API
    return runner.invoke(app, ["check", *args])


# --- reports -------------------------------------------------------------------------


def test_help_lists_check_and_its_options() -> None:
    assert "check" in plain(runner.invoke(app, ["--help"]).output)
    help_text = plain(check("--help").output)
    for option in ("--qnh", "--cruise", "--airspace"):
        assert option in help_text


def test_route_through_a_ctr_lists_the_infringement_and_its_warnings(workdir: Path) -> None:
    airspace = write_airspace(workdir / "data" / "ro.json", [CTR_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert result.exit_code == 0, result.output
    assert "INFRINGEMENT" in result.stdout
    assert "FICTIONAL CTR" in result.stdout
    assert "1 infringement, 2 warnings" in result.stdout


def test_reference_tma_at_qnh_993_is_an_infringement(workdir: Path) -> None:
    # SPEC.md 7.4: TMA base FL65 at QNH 993 is 5942.7 ft, so 6000 ft is inside.
    airspace = write_airspace(workdir / "ro.json", [TMA_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=6000)

    result = check(str(route), "--qnh", "993", "--cruise", "4500", "--airspace", str(airspace))

    assert result.exit_code == 0, result.output
    assert "1 infringement" in result.stdout


def test_reference_tma_at_standard_qnh_gives_no_event(workdir: Path) -> None:
    airspace = write_airspace(workdir / "ro.json", [TMA_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=6000)

    result = check(str(route), "--qnh", "1013.25", "--cruise", "4500", "--airspace", str(airspace))

    assert result.exit_code == 0, result.output
    assert "No events" in result.stdout
    assert "INFRINGEMENT" not in result.stdout


def test_cruise_altitude_is_used_for_waypoints_without_one(workdir: Path) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=None)

    high = check(str(route), "--qnh", "1013", "--cruise", "5000", "--airspace", str(airspace))
    low = check(str(route), "--qnh", "1013", "--cruise", "2500", "--airspace", str(airspace))

    assert "No events" in high.stdout  # 5000 ft is above the CTR (3500 ft) plus margin
    assert "1 infringement" in low.stdout


def test_report_carries_the_training_notice_and_the_attribution(workdir: Path) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert "Not for navigation" in result.stdout
    assert ATTRIBUTION in result.stdout


def test_default_airspace_file_is_data_ro_json(workdir: Path) -> None:
    write_airspace(workdir / "data" / "ro.json", [CTR_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500")

    assert result.exit_code == 0, result.output
    assert "1 infringement" in result.stdout


def test_ground_referenced_limit_is_marked_approximate(workdir: Path) -> None:
    item = openaip_item("ctr2", "FICTIONAL AGL CTR", 4, 3, (0, 1, 0), (3000, 1, 0), SQUARE)
    airspace = write_airspace(workdir / "ro.json", [item])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert "~" in result.stdout
    assert "terrain" in result.stdout  # the footnote explains the mark


def test_skipped_airspace_items_are_mentioned(workdir: Path) -> None:
    broken = {"_id": "bad", "name": "BROKEN", "type": 4, "icaoClass": 3}  # no limits or geometry
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM, broken])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert result.exit_code == 0, result.output
    assert "1 airspace item could not be used" in result.stdout


def test_names_cannot_inject_terminal_control_sequences(workdir: Path) -> None:
    item = dict(CTR_ITEM, name="EVIL\x1b[2J\x1b]0;title\x07 CTR")
    airspace = write_airspace(workdir / "ro.json", [item])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.stdout
    assert "\x07" not in result.stdout


# --- errors the user can fix --------------------------------------------------------


def assert_clean_failure(result, expected: str) -> None:  # noqa: ANN001
    assert result.exit_code == 1, result.output
    assert result.stderr.startswith("Error: ")
    assert expected in result.stderr
    assert "Traceback" not in result.output


@pytest.mark.parametrize("qnh", ["29.92", "101325", "nan"])
def test_implausible_qnh_is_refused(workdir: Path, qnh: str) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", qnh, "--cruise", "4500", "--airspace", str(airspace))

    assert_clean_failure(result, "QNH")


@pytest.mark.parametrize("cruise", ["nan", "inf", "100000"])
def test_implausible_cruise_altitude_is_refused(workdir: Path, cruise: str) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=None)

    result = check(str(route), "--qnh", "1013", "--cruise", cruise, "--airspace", str(airspace))

    assert_clean_failure(result, "cruise")


def test_missing_airspace_file_explains_how_to_get_one(workdir: Path) -> None:
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500")

    assert_clean_failure(result, "take2 fetch-airspace --country RO")


def test_unreadable_airspace_file_is_refused(workdir: Path) -> None:
    (workdir / "ro.json").write_text("{not json")
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", "ro.json")

    assert_clean_failure(result, "ro.json")


def test_bad_route_file_is_refused(workdir: Path) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])
    (workdir / "route.csv").write_text("name,lat,lon\nA,95,26\nB,44.6,26.1\n")

    result = check("route.csv", "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert_clean_failure(result, "lat")


def test_missing_route_file_is_refused(workdir: Path) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])

    result = check("nope.gpx", "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert_clean_failure(result, "nope.gpx")


# --- severity configuration and openAIP types (QA review of milestone 5) --------------


def test_every_type_in_the_severity_file_is_an_openaip_type() -> None:
    # A typo in config/severity.yaml (e.g. "TMZZ") would silently match nothing.
    rules = load_severity_rules(DEFAULT_SEVERITY_FILE)
    used = rules.clearance_types | rules.requirement_types | rules.info_types

    assert used <= set(TYPE_BY_CODE.values())


# --- review of milestone 7: honest summaries and a clean terminal --------------------


def test_no_usable_airspace_is_a_loud_warning_not_a_clean_result(workdir: Path) -> None:
    broken = {"_id": "bad", "name": "BROKEN", "type": 4, "icaoClass": 3}
    airspace = write_airspace(workdir / "ro.json", [broken])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert result.exit_code == 0, result.output
    assert "WARNING: no usable airspace" in result.stdout
    assert "keeps the Take 2 margins" not in result.stdout


def test_no_events_says_what_was_checked(workdir: Path) -> None:
    airspace = write_airspace(workdir / "ro.json", [TMA_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=6000)

    result = check(str(route), "--qnh", "1013.25", "--cruise", "4500", "--airspace", str(airspace))

    assert "No events against the airspace Take2 evaluates" in result.stdout
    assert "all loaded airspace" not in result.stdout


def test_defaulted_airspace_items_are_mentioned(workdir: Path) -> None:
    unknown_class = dict(CTR_ITEM, _id="ctr9", icaoClass=7)  # 7 is not an openAIP class code
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM, unknown_class])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert "1 airspace item was read with a default" in result.stdout


def test_infringement_advice_does_not_promise_a_clearance(workdir: Path) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert "prohibited areas cannot be entered" in result.stdout


ESCAPES = "\x1b]0;PWNED\x07\x1b[1A\x1b[2K"


@pytest.mark.parametrize("where", ["route-ok", "route-missing", "airspace-missing", "airspace-bad"])
def test_file_names_cannot_inject_terminal_control_sequences(workdir: Path, where: str) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])
    route = write_route(workdir / f"r{ESCAPES}.csv", alt_ft=2500)
    args = [str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace)]
    if where == "route-missing":
        args[0] = f"nope{ESCAPES}.csv"
    elif where == "airspace-missing":
        args[-1] = f"none{ESCAPES}.json"
    elif where == "airspace-bad":
        bad = workdir / f"bad{ESCAPES}.json"
        bad.write_text("{not json")
        args[-1] = str(bad)

    result = check(*args)

    assert "\x1b" not in result.output
    assert "\x07" not in result.output


def test_airspace_file_is_shown_by_name_not_by_absolute_path(workdir: Path) -> None:
    airspace = write_airspace(workdir / "ro.json", [CTR_ITEM])
    route = write_route(workdir / "route.csv", alt_ft=2500)

    result = check(str(route), "--qnh", "1013", "--cruise", "4500", "--airspace", str(airspace))

    assert str(workdir) not in result.stdout
    assert "ro.json" in result.stdout
