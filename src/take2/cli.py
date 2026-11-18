"""Command line interface: ``take2 <command>`` (SPEC.md section 8).

Training tool only, not for navigation. Each command is a function on ``app``; errors the
user can fix are printed as one ``Error: ...`` line on stderr with exit code 1, never as a
traceback.
"""

import math
from collections import Counter
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from take2.altimetry import check_qnh_hpa
from take2.analysis import DEFAULT_LATERAL_MARGIN_NM, DEFAULT_VERTICAL_MARGIN_FT, analyze_route
from take2.io.openaip import (
    ATTRIBUTION,
    OpenAipFetchError,
    OpenAipFileError,
    fetch_airspaces,
    load_openaip_file,
    normalize_country,
    parse_openaip_airspaces,
    read_api_key,
    save_openaip_file,
)
from take2.io.routes import RouteImportError, load_route
from take2.models import Event

DATA_DIR = Path("data")  # default location of downloaded data, relative to the current dir
DEFAULT_AIRSPACE_FILE = DATA_DIR / "ro.json"
MAX_SHOWN_PROBLEMS = 5  # skipped openAIP items listed after a download
MIN_CRUISE_FT = -1_500.0  # below sea level exists (Dead Sea about -1,400 ft)
MAX_CRUISE_FT = 60_000.0
MAX_NAME_CHARS = 32  # airspace names in the event table
NOT_FOR_NAVIGATION = (
    "Training tool only. Not for navigation: use official AIP data and approved tools "
    "for real flights."
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    # Never print local variables in a traceback: one of them can hold the API key.
    pretty_exceptions_show_locals=False,
)


@app.callback()
def main() -> None:
    """Take2: practise VFR route planning around airspace. Training only, not for navigation."""


@app.command("fetch-airspace")
def fetch_airspace(
    country: Annotated[str, typer.Option(help="ISO 3166-1 alpha-2 country code, for example RO.")],
    output: Annotated[
        Path | None,
        typer.Option(help="File to write. Default: data/<country>.json, e.g. data/ro.json."),
    ] = None,
) -> None:
    """Download the airspaces of one country from openAIP into a JSON file.

    The API key is read from the OPENAIP_API_KEY environment variable or from a .env file
    in the current directory.
    """
    # Each failure is turned into a message inside its except block and _fail() is called
    # after it, so typer.Exit carries no __context__ (whose traceback frames hold the key).
    failure: str | None = None
    try:
        country = normalize_country(country)
    except ValueError as exc:
        failure = str(exc)
    _fail_if(failure)

    api_key: str | None = None
    try:
        api_key = read_api_key()
    except (OSError, ValueError) as exc:
        failure = f"cannot read the openAIP API key: {exc}"
    _fail_if(failure)
    if api_key is None:
        _fail(
            "no openAIP API key found. Set OPENAIP_API_KEY in the environment or in a .env "
            "file in the current directory (see .env.example)."
        )

    items: list[dict[str, object]] = []
    try:
        items = fetch_airspaces(country, api_key)
    except (OpenAipFetchError, ValueError) as exc:  # ValueError: a key that cannot be sent
        failure = str(exc)  # the message already says when to check OPENAIP_API_KEY
    del api_key  # not needed any more
    _fail_if(failure)
    if not items:
        _fail(f"openAIP returned no airspaces for {country}; nothing was saved")

    path = output if output is not None else DATA_DIR / f"{country.lower()}.json"
    try:
        save_openaip_file(path, items, country=country)
    except OpenAipFileError as exc:
        failure = f"cannot save {path}: {exc}"
    except OSError as exc:
        failure = f"cannot save {path}: {exc.strerror or exc}"
    _fail_if(failure)

    result = parse_openaip_airspaces(items)
    typer.echo(f"Saved {len(items)} openAIP airspaces for {country} to {path}.")
    if result.skipped:
        typer.echo(f"{_items(result.skipped)} cannot be used by Take2 and will be skipped.")
    if result.defaulted:
        typer.echo(
            f"{_items(result.defaulted)} will be used with a default for a field Take2 cannot read."
        )
    shown = result.problems[:MAX_SHOWN_PROBLEMS]
    for problem in shown:
        typer.echo(f"  {problem}")
    hidden = result.skipped + result.defaulted - len(shown)
    if hidden > 0:
        typer.echo(f"  ... and {hidden} more")
    typer.echo(ATTRIBUTION)


@app.command("check")
def check(
    route_file: Annotated[
        Path,
        typer.Argument(help="Route file: .gpx, .pln (MSFS) or .csv (name,lat,lon,alt_ft)."),
    ],
    qnh: Annotated[float, typer.Option(help="QNH of the day in hPa, for example 1013.")],
    cruise: Annotated[
        float,
        typer.Option(help="Cruise altitude in ft AMSL, used for waypoints without an altitude."),
    ],
    airspace: Annotated[
        Path, typer.Option(help="openAIP airspace file written by take2 fetch-airspace.")
    ] = DEFAULT_AIRSPACE_FILE,
) -> None:
    """Check a route against the airspace of the day and list the events."""
    failure: str | None = None
    try:
        check_qnh_hpa(qnh)
    except ValueError as exc:
        failure = str(exc)
    _fail_if(failure)
    if not (math.isfinite(cruise) and MIN_CRUISE_FT <= cruise <= MAX_CRUISE_FT):
        _fail(
            f"cruise altitude {cruise} ft is not plausible: expected {MIN_CRUISE_FT:,.0f} to "
            f"{MAX_CRUISE_FT:,.0f} ft AMSL"
        )
    if not airspace.exists():
        _fail(
            f"no airspace data at {airspace.name}: download it with "
            "`take2 fetch-airspace --country RO`, or give another file with --airspace"
        )

    data = None
    try:
        data = load_openaip_file(airspace)
    except OpenAipFileError as exc:
        failure = f"cannot read {airspace.name}: {exc}"
    _fail_if(failure)
    route = None
    try:
        route = load_route(route_file, cruise_alt_ft=cruise)
    except RouteImportError as exc:
        failure = f"cannot read {route_file.name}: {exc}"
    _fail_if(failure)
    events: list[Event] = []
    try:
        events = analyze_route(route, data.airspaces, qnh)
    except ValueError as exc:
        failure = str(exc)
    _fail_if(failure)

    _say(f"Take2 check: {route_file.name}, QNH {qnh:.2f} hPa, cruise {cruise:.0f} ft")
    loaded = f"Airspace: {airspace.name}, {_count(len(data.airspaces), 'airspace')} loaded"
    if data.skipped:
        loaded += (
            f"; {_count(data.skipped, 'airspace item')} could not be used and "
            f"{'was' if data.skipped == 1 else 'were'} skipped"
        )
    if data.defaulted:
        loaded += (
            f"; {_count(data.defaulted, 'airspace item')} "
            f"{'was' if data.defaulted == 1 else 'were'} read with a default"
        )
    _say(loaded)
    _say("")
    if not data.airspaces:
        _say(
            "WARNING: no usable airspace was loaded, so nothing was checked. Download the data "
            "again with `take2 fetch-airspace --country RO`."
        )
    if events:
        for line in _event_table(events):
            _say(line)
        _say("")
    if data.airspaces:
        for line in _summary(events):
            _say(line)
    _say(NOT_FOR_NAVIGATION)
    _say(ATTRIBUTION)


def _event_table(events: list[Event]) -> list[str]:
    """Aligned rows: number, kind, airspace, extent, minimum distances."""
    header = ("#", "Kind", "Airspace", "From NM", "To NM", "Lateral NM", "Vertical ft")
    rows = [
        (
            str(number),
            event.kind,
            _printable(event.airspace_name)[:MAX_NAME_CHARS],
            f"{event.start_nm:.1f}",
            f"{event.end_nm:.1f}",
            f"{event.min_lateral_nm:.2f}",
            f"{event.min_vertical_ft:.0f}" + ("~" if event.approximate else ""),
        )
        for number, event in enumerate(events, start=1)
    ]
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    left_aligned = {1, 2}

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(
            cell.ljust(width) if i in left_aligned else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(cells, widths, strict=True))
        ).rstrip()

    return [line(header), *(line(row) for row in rows)]


def _summary(events: list[Event]) -> list[str]:
    """The counts per kind and what they mean for the plan."""
    if not events:
        return [
            "No events against the airspace Take2 evaluates (classes A to D, P, R, TMZ, RMZ, D, "
            "ATZ) in the loaded data: the route keeps the Take 2 margins "
            f"({DEFAULT_LATERAL_MARGIN_NM:g} NM laterally, {DEFAULT_VERTICAL_MARGIN_FT:g} ft "
            "vertically) to them. Other airspace types and areas outside the data are not checked."
        ]
    counts = Counter(event.kind for event in events)
    lines = [
        "Summary: "
        + ", ".join(
            [
                _count(counts["INFRINGEMENT"], "infringement"),
                _count(counts["WARNING"], "warning"),
                _count(counts["REQUIREMENT"], "requirement"),
                f"{counts['INFO']} info",
            ]
        )
        + "."
    ]
    if counts["INFRINGEMENT"]:
        lines.append(
            "The route enters airspace that needs a clearance or permission: change the route, "
            "or plan to obtain it before the flight; prohibited areas cannot be entered."
        )
    if counts["WARNING"]:
        lines.append(
            "Warnings: the route does not keep the Take 2 margins "
            f"({DEFAULT_LATERAL_MARGIN_NM:g} NM, {DEFAULT_VERTICAL_MARGIN_FT:g} ft)."
        )
    if counts["REQUIREMENT"]:
        lines.append("Requirements: a transponder or radio requirement applies where listed.")
    if any(event.approximate for event in events):
        lines.append("~ approximate: a ground-referenced limit with terrain assumed at 0 ft.")
    return lines


def _say(line: str) -> None:
    """Print one line of output, made safe for a terminal."""
    typer.echo(_printable(line))


def _printable(text: str) -> str:
    """Replace characters a terminal could interpret (escape sequences, controls) with '?'."""
    return "".join(c if c.isprintable() else "?" for c in text)


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _items(count: int) -> str:
    return f"{count} item" if count == 1 else f"{count} items"


def _fail(message: str) -> NoReturn:
    """Print ``Error: message`` on stderr and exit with code 1."""
    typer.echo(f"Error: {_printable(message)}", err=True)
    raise typer.Exit(code=1)


def _fail_if(message: str | None) -> None:
    """``_fail(message)`` when there is a message; call it outside any except block."""
    if message is not None:
        _fail(message)
