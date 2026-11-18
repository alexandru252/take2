"""Route import from GPX, MSFS ``.pln`` and CSV files (SPEC.md section 7.2).

Every loader returns a validated ``Route``: at least 2 waypoints, latitude in [-90, 90],
longitude in [-180, 180]. Waypoints without an altitude get the cruise altitude.
"""

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from pathlib import Path

import gpxpy
import gpxpy.gpx
from pydantic import ValidationError

from take2.altimetry import FT_PER_M
from take2.models import Route, Waypoint

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
            f"unsupported route file type {path.suffix!r}: expected .gpx, .pln or .csv"
        )
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise RouteImportError(f"{path.name} is not a UTF-8 text file") from exc
    return parser(text, cruise_alt_ft)


def parse_csv(text: str, cruise_alt_ft: float) -> Route:
    """Parse CSV with header ``name,lat,lon,alt_ft``; ``alt_ft`` may be empty or absent."""
    rows = csv.reader(io.StringIO(text.removeprefix("﻿")))
    header = next(rows, None)
    if header is None:
        raise RouteImportError("empty CSV: expected the header name,lat,lon,alt_ft")
    columns = {title.strip().lower(): index for index, title in enumerate(header)}
    missing = [title for title in ("name", "lat", "lon") if title not in columns]
    if missing:
        raise RouteImportError(
            f"CSV header is missing {', '.join(missing)}: expected name,lat,lon,alt_ft"
        )

    raw: list[RawPoint] = []
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue

        def cell(title: str, row: list[str] = row) -> str:
            index = columns.get(title)
            return row[index].strip() if index is not None and index < len(row) else ""

        alt_text = cell("alt_ft")
        raw.append(
            (
                cell("name"),
                _csv_float(cell("lat"), "lat", rows.line_num),
                _csv_float(cell("lon"), "lon", rows.line_num),
                _csv_float(alt_text, "alt_ft", rows.line_num) if alt_text else None,
            )
        )
    return _build_route(raw, cruise_alt_ft)


def parse_gpx(text: str, cruise_alt_ft: float) -> Route:
    """Parse GPX: route points if present, otherwise track points (elevation m -> ft)."""
    try:
        gpx = gpxpy.parse(text)
    except (gpxpy.gpx.GPXException, ValueError) as exc:
        raise RouteImportError(f"invalid GPX file: {exc}") from exc

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
        raise RouteImportError(f"invalid WorldPosition {text!r}: expected lat,lon[,alt]")
    lat = _parse_dms(parts[0], hemispheres="NS")
    lon = _parse_dms(parts[1], hemispheres="EW")
    alt_ft = None
    if len(parts) == 3 and parts[2]:
        try:
            alt_ft = float(parts[2])
        except ValueError as exc:
            raise RouteImportError(f"invalid WorldPosition altitude {parts[2]!r}") from exc
    return lat, lon, alt_ft


def _parse_dms(text: str, hemispheres: str) -> float:
    """Parse ``N44° 34' 21.56"`` into signed decimal degrees."""
    axis = "latitude" if hemispheres == "NS" else "longitude"
    match = _DMS_RE.match(text)
    if match is None or match.group(1) not in hemispheres:
        raise RouteImportError(f"invalid {axis} {text!r}: expected e.g. N44° 34' 21.56\"")
    hemisphere, degrees, minutes, seconds = match.groups()
    if float(minutes) >= 60 or float(seconds) >= 60:
        raise RouteImportError(f"invalid {axis} {text!r}: minutes and seconds must be < 60")
    value = float(degrees) + float(minutes) / 60 + float(seconds) / 3600
    return -value if hemisphere in "SW" else value


def _csv_float(text: str, column: str, line: int) -> float:
    try:
        return float(text)
    except ValueError as exc:
        raise RouteImportError(f"CSV line {line}: {column} {text!r} is not a number") from exc


def _build_route(raw: Iterable[RawPoint], cruise_alt_ft: float) -> Route:
    """Validate raw points into a ``Route``, filling missing names and altitudes."""
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
            raise RouteImportError(f"waypoint {index} ({name!r}): {_describe(exc)}") from exc
    try:
        return Route(waypoints=waypoints)
    except ValidationError as exc:
        raise RouteImportError(
            f"a route needs at least 2 waypoints, found {len(waypoints)}"
        ) from exc


def _describe(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
    )


_PARSERS_BY_SUFFIX: dict[str, Callable[[str, float], Route]] = {
    ".csv": parse_csv,
    ".gpx": parse_gpx,
    ".pln": parse_pln,
}
