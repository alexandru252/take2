"""Route import from GPX, MSFS ``.pln`` and CSV files (SPEC.md section 7.2).

Every loader returns a validated ``Route``: at least 2 waypoints, latitude in [-90, 90],
longitude in [-180, 180]. Waypoints without an altitude get the cruise altitude.

Route files are untrusted input (later milestones accept uploads), so every parser enforces a
size limit and XML files are checked for nesting depth and namespace declarations before
gpxpy or ElementTree builds a tree from them.
"""

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import gpxpy
import gpxpy.gpx
from pydantic import ValidationError

from take2.altimetry import FT_PER_M
from take2.models import MAX_ROUTE_WAYPOINTS, Route, Waypoint

# Limits for untrusted route files. Real routes are far smaller: a 1 Hz GPX track of a
# 3-hour flight is about 1 MB, and real GPX/PLN files nest about 6 to 12 elements deep.
MAX_ROUTE_FILE_SIZE = 10_000_000  # bytes for files, characters for text
MAX_XML_DEPTH = 64
MAX_XML_NAMESPACE_DECLARATIONS = 32
_ERROR_PREVIEW_CHARS = 40

# (name or None, lat, lon, alt_ft or None) as read from a file, before validation.
type RawPoint = tuple[str | None, float, float, float | None]

_DMS_RE = re.compile(
    r"""^\s*([NSEW])\s*(\d+(?:\.\d+)?)\s*°\s*(\d+(?:\.\d+)?)\s*'\s*(\d+(?:\.\d+)?)\s*"\s*$"""
)


class RouteImportError(ValueError):
    """A route file could not be read, or its content is not a valid route."""


def load_route(path: str | Path, cruise_alt_ft: float) -> Route:
    """Load a route file, choosing the format from the extension (.gpx, .pln, .csv)."""
    path = Path(path)
    parser = _PARSERS_BY_SUFFIX.get(path.suffix.lower())
    if parser is None:
        raise RouteImportError(
            f"unsupported route file type {_preview(path.suffix)!r}: expected .gpx, .pln or .csv"
        )
    # is_file() is False for directories, devices and FIFOs, which could hang or never end.
    if not path.is_file():
        raise RouteImportError(f"route file not found: {_preview(path.name)}")
    size_bytes = path.stat().st_size
    if size_bytes > MAX_ROUTE_FILE_SIZE:
        raise RouteImportError(
            f"route file is too large: {size_bytes:,} bytes (limit {MAX_ROUTE_FILE_SIZE:,})"
        )
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RouteImportError(f"{_preview(path.name)} is not a UTF-8 text file") from exc
    return parser(text, cruise_alt_ft)


def parse_csv(text: str, cruise_alt_ft: float) -> Route:
    """Parse CSV with header ``name,lat,lon,alt_ft``; ``alt_ft`` may be empty or absent."""
    _check_size(text)
    records = _csv_records(text)
    first = next(records, None)
    if first is None:
        raise RouteImportError("empty CSV: expected the header name,lat,lon,alt_ft")
    _, header = first
    columns = {title.strip().lower(): index for index, title in enumerate(header)}
    missing = [title for title in ("name", "lat", "lon") if title not in columns]
    if missing:
        raise RouteImportError(
            f"CSV header is missing {', '.join(missing)}: expected name,lat,lon,alt_ft"
        )

    raw: list[RawPoint] = []
    for line, row in records:
        if not any(cell.strip() for cell in row):
            continue

        def cell(title: str, row: list[str] = row) -> str:
            index = columns.get(title)
            return row[index].strip() if index is not None and index < len(row) else ""

        alt_text = cell("alt_ft")
        raw.append(
            (
                cell("name"),
                _csv_float(cell("lat"), "lat", line),
                _csv_float(cell("lon"), "lon", line),
                _csv_float(alt_text, "alt_ft", line) if alt_text else None,
            )
        )
    return _build_route(raw, cruise_alt_ft)


def parse_gpx(text: str, cruise_alt_ft: float) -> Route:
    """Parse GPX: route points if present, otherwise track points (elevation m -> ft).

    The points of all routes (or of all tracks and segments) are joined in file order.
    """
    _check_size(text)
    _check_xml_structure(text, "GPX")
    try:
        gpx = gpxpy.parse(text)
    except (gpxpy.gpx.GPXException, ValueError) as exc:
        raise RouteImportError(f"invalid GPX file: {_preview(str(exc), 200)}") from exc

    points = [point for rte in gpx.routes for point in rte.points]
    if not points:
        points = [point for trk in gpx.tracks for seg in trk.segments for point in seg.points]
    raw: list[RawPoint] = [
        (
            point.name,
            point.latitude,
            point.longitude,
            None if point.elevation is None else point.elevation * FT_PER_M,
        )
        for point in points
    ]
    return _build_route(raw, cruise_alt_ft)


def parse_pln(text: str, cruise_alt_ft: float) -> Route:
    """Parse an MSFS flight plan: one waypoint per ``ATCWaypoint`` element."""
    _check_size(text)
    _check_xml_structure(text, "PLN")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RouteImportError(f"invalid PLN file (XML error): {exc}") from exc

    raw: list[RawPoint] = []
    for index, element in enumerate(root.iter("ATCWaypoint"), start=1):
        position = element.findtext("WorldPosition")
        if position is None:
            raise RouteImportError(f"ATCWaypoint {index}: missing WorldPosition")
        try:
            lat, lon, alt_ft = parse_world_position(position)
        except RouteImportError as exc:
            raise RouteImportError(f"ATCWaypoint {index}: {exc}") from exc
        raw.append((element.get("id"), lat, lon, alt_ft))
    return _build_route(raw, cruise_alt_ft)


def parse_world_position(text: str) -> tuple[float, float, float | None]:
    """Parse an MSFS ``WorldPosition`` into ``(lat, lon, alt_ft)``.

    Format: ``N44° 34' 21.56",E26° 5' 5.06",+004500.00`` (DMS, DMS, feet). The altitude
    part is optional and returned as ``None`` when absent. South and west are negative.
    """
    parts = [part.strip() for part in text.split(",")]
    if len(parts) not in (2, 3):
        raise RouteImportError(f"invalid WorldPosition {_preview(text)!r}: expected lat,lon[,alt]")
    lat = _parse_dms(parts[0], hemispheres="NS")
    lon = _parse_dms(parts[1], hemispheres="EW")
    alt_ft = None
    if len(parts) == 3 and parts[2]:
        try:
            alt_ft = float(parts[2])
        except ValueError as exc:
            raise RouteImportError(
                f"invalid WorldPosition altitude {_preview(parts[2])!r}"
            ) from exc
    return lat, lon, alt_ft


def _parse_dms(text: str, hemispheres: str) -> float:
    """Parse ``N44° 34' 21.56"`` into signed decimal degrees."""
    axis = "latitude" if hemispheres == "NS" else "longitude"
    match = _DMS_RE.match(text)
    if match is None or match.group(1) not in hemispheres:
        raise RouteImportError(f"invalid {axis} {_preview(text)!r}: expected e.g. N44° 34' 21.56\"")
    hemisphere, degrees, minutes, seconds = match.groups()
    if float(minutes) >= 60 or float(seconds) >= 60:
        raise RouteImportError(
            f"invalid {axis} {_preview(text)!r}: minutes and seconds must be < 60"
        )
    value = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    return -value if hemisphere in "SW" else value


def _csv_records(text: str) -> Iterator[tuple[int, list[str]]]:
    """Yield ``(line number, fields)`` per CSV record; csv errors become RouteImportError."""
    reader = csv.reader(io.StringIO(text.removeprefix("\ufeff")))
    while True:
        try:
            fields = next(reader)
        except StopIteration:
            return
        except csv.Error as exc:
            raise RouteImportError(f"CSV line {reader.line_num}: {exc}") from exc
        yield reader.line_num, fields


def _csv_float(text: str, column: str, line: int) -> float:
    try:
        return float(text)
    except ValueError as exc:
        raise RouteImportError(
            f"CSV line {line}: {column} {_preview(text)!r} is not a number"
        ) from exc


def _build_route(raw: Iterable[RawPoint], cruise_alt_ft: float) -> Route:
    """Validate raw points into a ``Route``, filling missing names and altitudes."""
    raw = list(raw)
    if len(raw) < 2:
        raise RouteImportError(f"a route needs at least 2 waypoints, found {len(raw)}")
    if len(raw) > MAX_ROUTE_WAYPOINTS:
        raise RouteImportError(f"too many waypoints: {len(raw):,} (limit {MAX_ROUTE_WAYPOINTS:,})")
    waypoints = []
    for index, (name, lat, lon, alt_ft) in enumerate(raw, start=1):
        name = name or f"WP{index}"
        try:
            waypoints.append(
                Waypoint(
                    name=name, lat=lat, lon=lon, alt_ft=cruise_alt_ft if alt_ft is None else alt_ft
                )
            )
        except ValidationError as exc:
            raise RouteImportError(
                f"waypoint {index} ({_preview(name)!r}): {_describe(exc)}"
            ) from exc
    try:
        return Route(waypoints=waypoints)
    except ValidationError as exc:
        raise RouteImportError(f"invalid route: {_describe(exc)}") from exc


def _describe(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )


def _preview(text: str, limit: int = _ERROR_PREVIEW_CHARS) -> str:
    """Shorten user-supplied text before echoing it in an error message."""
    return text if len(text) <= limit else text[:limit] + "..."


def _check_size(text: str) -> None:
    if len(text) > MAX_ROUTE_FILE_SIZE:
        raise RouteImportError(
            f"route file is too large: {len(text):,} characters (limit {MAX_ROUTE_FILE_SIZE:,})"
        )


def _check_xml_structure(text: str, file_type: str) -> None:
    """Reject XML that is malformed, nested too deeply or declares too many namespaces.

    Runs before gpxpy, which deep-copies nested ``<extensions>`` recursively (a stack
    overflow that crashes the process) and scans namespace declarations with a regex that is
    quadratic in their number. The pull parser used here is iterative, so it is safe on
    deeply nested input, and the stdlib expat parser refuses entity-expansion attacks.
    """
    if text.count("xmlns") > MAX_XML_NAMESPACE_DECLARATIONS:
        raise RouteImportError(
            f"{file_type} file declares too many XML namespaces "
            f"(limit {MAX_XML_NAMESPACE_DECLARATIONS})"
        )
    parser = ET.XMLPullParser(events=("start", "end"))
    depth = 0
    chunk_chars = 65_536
    try:
        for offset in range(0, len(text), chunk_chars):
            parser.feed(text[offset : offset + chunk_chars])
            for event, _ in parser.read_events():
                depth += 1 if event == "start" else -1
                if depth > MAX_XML_DEPTH:
                    raise RouteImportError(
                        f"{file_type} file is nested more than {MAX_XML_DEPTH} elements deep"
                    )
        parser.close()
    except ET.ParseError as exc:
        raise RouteImportError(f"invalid {file_type} file (XML error): {exc}") from exc


_PARSERS_BY_SUFFIX: dict[str, Callable[[str, float], Route]] = {
    ".csv": parse_csv,
    ".gpx": parse_gpx,
    ".pln": parse_pln,
}
