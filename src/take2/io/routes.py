"""Route import from GPX, MSFS ``.pln`` and CSV files (SPEC.md section 7.2).

Every loader returns a validated ``Route``: at least 2 waypoints, latitude in [-90, 90],
longitude in [-180, 180]. Waypoints without an altitude get the cruise altitude.

Route files are untrusted input (later milestones accept uploads). Every parser enforces a
size limit, and XML (GPX, ``.pln``) is first checked by a streaming expat pass that builds no
tree (see ``_check_xml``). The exact same text is then parsed with ElementTree, so what is
checked is what is read. GPX is read directly from its ``rtept``/``trkpt`` elements; no
third-party GPX library is involved.
"""

import csv
import io
import os
import re
import stat
import xml.etree.ElementTree as ET
import xml.parsers.expat
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from pydantic import ValidationError

from take2.altimetry import FT_PER_M
from take2.models import MAX_ROUTE_WAYPOINTS, Route, Waypoint

# Limits for untrusted route files. Real routes are far smaller: a 1 Hz GPX track of a
# 3-hour flight is about 1 MB with fewer than 100,000 elements, and real GPX/PLN files nest
# about 6 to 12 elements deep with a handful of attributes per element.
MAX_ROUTE_FILE_SIZE = 10_000_000  # bytes for files, characters for text
MAX_XML_DEPTH = 64
MAX_XML_ELEMENTS = 500_000
MAX_XML_ATTRIBUTES = 64  # per element, namespace declarations included
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
    return parser(_read_route_file(path), cruise_alt_ft)


def _read_route_file(path: Path) -> str:
    """Read a regular file of at most ``MAX_ROUTE_FILE_SIZE`` bytes as UTF-8 text.

    The file is opened once and checked through its descriptor, so the path cannot be swapped
    for a device or FIFO between the check and the read. ``O_NONBLOCK`` keeps ``open`` from
    blocking on a FIFO, and the read is bounded even if the file grows meanwhile.
    """
    name = _preview(path.name)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError as exc:
        raise RouteImportError(f"route file not found: {name}") from exc
    except OSError as exc:
        raise RouteImportError(f"cannot open route file {name}: {exc.strerror}") from exc
    try:
        # Check the descriptor before fdopen, which itself rejects directories.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RouteImportError(f"route file not found or not a regular file: {name}")
        file = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with file:
        try:
            data = file.read(MAX_ROUTE_FILE_SIZE + 1)
        except OSError as exc:
            raise RouteImportError(f"cannot read route file {name}: {exc.strerror}") from exc
    if len(data) > MAX_ROUTE_FILE_SIZE:
        raise RouteImportError(f"route file is too large: more than {MAX_ROUTE_FILE_SIZE:,} bytes")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RouteImportError(f"{name} is not a UTF-8 text file") from exc


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
        if len(raw) > MAX_ROUTE_WAYPOINTS:  # stop early instead of reading every row
            raise _too_many_waypoints(len(raw))
    return _build_route(raw, cruise_alt_ft)


def parse_gpx(text: str, cruise_alt_ft: float) -> Route:
    """Parse GPX 1.0 or 1.1: route points if present, otherwise track points.

    Each ``rtept``/``trkpt`` gives ``lat``/``lon`` and optional ``<ele>`` (metres, converted to
    feet) and ``<name>``. The points of all routes (or of all tracks and segments) are joined
    in file order. Elements inside ``<extensions>`` are ignored.
    """
    _check_size(text)
    root = _parse_xml(text, "GPX")
    namespace, root_name = _split_tag(root.tag)
    if root_name != "gpx":
        raise RouteImportError(
            f"not a GPX file: the root element is {_preview(root_name)!r}, expected gpx"
        )

    def tag(name: str) -> str:
        return f"{{{namespace}}}{name}" if namespace else name

    points = [pt for rte in root.findall(tag("rte")) for pt in rte.findall(tag("rtept"))]
    if not points:
        points = [
            pt
            for trk in root.findall(tag("trk"))
            for seg in trk.findall(tag("trkseg"))
            for pt in seg.findall(tag("trkpt"))
        ]
    raw: list[RawPoint] = []
    for index, point in enumerate(points, start=1):
        ele_text = (point.findtext(tag("ele")) or "").strip()
        raw.append(
            (
                (point.findtext(tag("name")) or "").strip() or None,
                _gpx_float(point.get("lat"), "lat", index),
                _gpx_float(point.get("lon"), "lon", index),
                _gpx_float(ele_text, "ele", index) * FT_PER_M if ele_text else None,
            )
        )
    return _build_route(raw, cruise_alt_ft)


def parse_pln(text: str, cruise_alt_ft: float) -> Route:
    """Parse an MSFS flight plan: one waypoint per ``ATCWaypoint`` element."""
    _check_size(text)
    root = _parse_xml(text, "PLN")

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


def _gpx_float(text: str | None, field: str, index: int) -> float:
    if text is None:
        raise RouteImportError(f"GPX point {index}: missing {field}")
    try:
        return float(text)
    except ValueError as exc:
        raise RouteImportError(
            f"GPX point {index}: {field} {_preview(text)!r} is not a number"
        ) from exc


def _split_tag(tag: str) -> tuple[str, str]:
    """Split an ElementTree tag ``{namespace}name`` into ``(namespace, name)``."""
    if tag.startswith("{"):
        namespace, _, name = tag[1:].partition("}")
        return namespace, name
    return "", tag


def _too_many_waypoints(count: int) -> RouteImportError:
    return RouteImportError(f"too many waypoints: {count:,} (limit {MAX_ROUTE_WAYPOINTS:,})")


def _build_route(raw: Iterable[RawPoint], cruise_alt_ft: float) -> Route:
    """Validate raw points into a ``Route``, filling missing names and altitudes."""
    raw = list(raw)
    if len(raw) < 2:
        raise RouteImportError(f"a route needs at least 2 waypoints, found {len(raw)}")
    if len(raw) > MAX_ROUTE_WAYPOINTS:
        raise _too_many_waypoints(len(raw))
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


def _parse_xml(text: str, file_type: str) -> ET.Element:
    """Check XML with ``_check_xml``, then parse the same text with ElementTree."""
    _check_xml(text, file_type)
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:  # not expected after _check_xml, kept as a safety net
        raise RouteImportError(f"invalid {file_type} file (XML error): {exc}") from exc


def _check_xml(text: str, file_type: str) -> None:
    """Reject XML that Take2 does not parse, without building a tree.

    Rejects malformed XML, any DOCTYPE, nesting deeper than ``MAX_XML_DEPTH``, more than
    ``MAX_XML_ELEMENTS`` elements and more than ``MAX_XML_ATTRIBUTES`` attributes on one
    element. Rejecting every DOCTYPE removes all entity tricks (exponential or flat expansion,
    external entities); route files never need a DTD. expat reports elements through
    callbacks, so deep nesting cannot overflow the stack here.
    """
    parser = xml.parsers.expat.ParserCreate()
    depth = 0
    elements = 0

    def start_doctype(*_: object) -> None:
        raise RouteImportError(f"{file_type} file contains a DOCTYPE, which is not allowed")

    def start_element(_name: str, attributes: dict[str, str]) -> None:
        nonlocal depth, elements
        depth += 1
        elements += 1
        if depth > MAX_XML_DEPTH:
            raise RouteImportError(
                f"{file_type} file is nested more than {MAX_XML_DEPTH} elements deep"
            )
        if elements > MAX_XML_ELEMENTS:
            raise RouteImportError(f"{file_type} file has more than {MAX_XML_ELEMENTS:,} elements")
        if len(attributes) > MAX_XML_ATTRIBUTES:
            raise RouteImportError(
                f"{file_type} file has an element with more than {MAX_XML_ATTRIBUTES} attributes"
            )

    def end_element(_name: str) -> None:
        nonlocal depth
        depth -= 1

    parser.StartDoctypeDeclHandler = start_doctype
    parser.StartElementHandler = start_element
    parser.EndElementHandler = end_element
    try:
        parser.Parse(text, True)
    except xml.parsers.expat.ExpatError as exc:
        raise RouteImportError(f"invalid {file_type} file (XML error): {exc}") from exc
    except UnicodeEncodeError as exc:
        raise RouteImportError(f"{file_type} file contains text that is not valid UTF-8") from exc


_PARSERS_BY_SUFFIX: dict[str, Callable[[str, float], Route]] = {
    ".csv": parse_csv,
    ".gpx": parse_gpx,
    ".pln": parse_pln,
}
