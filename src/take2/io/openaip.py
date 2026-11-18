"""openAIP airspace data (SPEC.md section 5, milestone 5).

``parse_openaip_airspaces`` converts openAIP airspace items into ``Airspace`` models, and
``load_openaip_file`` reads them from a saved JSON file. Both treat the data as untrusted:
an item that is malformed, uses a code Take2 does not know, or fails model validation is
skipped and counted instead of failing the whole file, and no JSON value can make them crash.

The openAIP codes below come from the official response schema
(https://api.core.openaip.net/api/schemas/response/airspace/airspace-schema.json).

Airspace data © openAIP contributors (openaip.net), CC BY-NC 4.0.
"""

import json
import math
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from take2.models import Airspace, LimitReference, VerticalLimit

# A whole country is a few MB; the limit leaves room for large countries and future fields.
MAX_OPENAIP_FILE_SIZE = 100_000_000  # bytes
MAX_REPORTED_PROBLEMS = 20  # skipped items are all counted, but only these are described
MAX_TYPE_CODE = 999  # openAIP type codes are small; larger numbers are treated as malformed
_ERROR_PREVIEW_CHARS = 40

# openAIP airspace type code -> Take2 type string. P, R, D, CTR, TMZ, RMZ, TMA, ATZ and CTA
# are matched by the severity configuration, so they must keep these exact spellings.
TYPE_BY_CODE: dict[int, str] = {
    0: "OTHER",
    1: "R",  # Restricted
    2: "D",  # Danger
    3: "P",  # Prohibited
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
    15: "AWY",  # Airway
    16: "MTR",  # Military Training Route
    17: "ALERT",
    18: "WARNING",
    19: "PROTECTED",
    20: "HTZ",
    21: "GLIDING",  # Gliding Sector
    22: "TRP",  # Transponder Setting
    23: "TIZ",
    24: "TIA",
    25: "MTA",
    26: "CTA",
    27: "ACC",  # ACC Sector
    28: "SPORT",  # Aerial Sporting or Recreational Activity
    29: "LOW_OVERFLIGHT",  # Low Altitude Overflight Restriction
    30: "MRT",  # Military Route
    31: "TFR",  # TSA/TRA Feeding Route
    32: "VFR_SECTOR",
    33: "FIS",  # FIS Sector
    34: "LTA",
    35: "UTA",
    36: "MCTR",
}
# openAIP ICAO class code -> class letter; 8 is "Unclassified / Special Use Airspace".
ICAO_CLASS_BY_CODE: dict[int, str | None] = {
    0: "A",
    1: "B",
    2: "C",
    3: "D",
    4: "E",
    5: "F",
    6: "G",
    8: None,
}
UNIT_BY_CODE: dict[int, Literal["FT", "M", "FL"]] = {0: "M", 1: "FT", 6: "FL"}
REFERENCE_BY_CODE: dict[int, LimitReference] = {
    0: LimitReference.GND,
    1: LimitReference.MSL,
    2: LimitReference.STD,
}


class OpenAipFileError(ValueError):
    """A saved openAIP file could not be read, or its content is not openAIP data."""


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Airspaces converted from openAIP items, and the items that were skipped."""

    airspaces: list[Airspace]
    skipped: int  # number of items that could not be converted
    problems: list[str]  # why, for the first MAX_REPORTED_PROBLEMS skipped items


class _SkipItem(Exception):
    """Internal: the current item cannot be converted; the message says why."""


def parse_openaip_airspaces(items: Iterable[object]) -> ParseResult:
    """Convert openAIP airspace items to ``Airspace`` models, skipping unusable items.

    An item is skipped (and counted) when a field Take2 needs is missing or has the wrong
    JSON type, when it uses a unit, reference datum or ICAO class code Take2 does not know,
    when its geometry is not a single-ring Polygon, when it fails model validation, or when
    its ``_id`` was already seen. An unknown *type* code ``n`` is kept as ``"TYPE_n"``.
    Open rings are closed. The input is not modified.
    """
    airspaces: list[Airspace] = []
    problems: list[str] = []
    skipped = 0
    seen_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        try:
            airspace = _convert_item(item)
            if airspace.id in seen_ids:
                raise _SkipItem(f"duplicate _id {_preview(airspace.id)!r}")
        except _SkipItem as exc:
            skipped += 1
            if len(problems) < MAX_REPORTED_PROBLEMS:
                problems.append(f"item {index}{_describe_item(item)}: {exc}")
            continue
        seen_ids.add(airspace.id)
        airspaces.append(airspace)
    return ParseResult(airspaces=airspaces, skipped=skipped, problems=problems)


def load_openaip_file(path: str | Path) -> ParseResult:
    """Read a saved openAIP JSON file and convert its airspace items.

    The file holds either a list of openAIP items or an object with an ``items`` list (an
    openAIP API page, or the file written by ``take2 fetch-airspace``); other keys are
    ignored. It must be a regular UTF-8 file of at most ``MAX_OPENAIP_FILE_SIZE`` bytes.
    """
    path = Path(path)
    name = _preview(path.name)
    data = _read_openaip_file(path)
    try:
        document = _parse_json(data)
    except ValueError as exc:
        raise OpenAipFileError(f"{name} is {exc}") from None
    items = document.get("items") if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise OpenAipFileError(
            f"{name}: expected a list of openAIP items or an object with an 'items' list"
        )
    return parse_openaip_airspaces(items)


def _parse_json(data: bytes) -> object:
    """Decode strict JSON from UTF-8 bytes; raise ``ValueError`` with a short reason.

    Stricter than ``json.loads``: ``NaN``/``Infinity`` and numbers too large for a float are
    rejected, so everything accepted can be written back as standard JSON.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("not a UTF-8 text file") from None
    try:
        return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)
    except RecursionError:
        raise ValueError("not usable JSON: nested too deeply") from None
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})"
        ) from None
    except ValueError as exc:  # from the number hooks, or an integer with too many digits
        reason = str(exc).split(":", 1)[0]
        raise ValueError(f"not valid JSON ({_preview(reason, 60)})") from None


def _reject_constant(name: str) -> float:
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("number out of range")
    return value


def _convert_item(item: object) -> Airspace:
    """Convert one openAIP item; raise ``_SkipItem`` when it cannot be used."""
    if not isinstance(item, dict):
        raise _SkipItem(f"expected an object, got {_json_type(item)}")
    airspace_id = _text(item, "_id")
    name = _text(item, "name")
    type_code = _code(item, "type")
    if type_code > MAX_TYPE_CODE:
        raise _SkipItem(f"type {type_code} is not an openAIP type code")
    class_code = _code(item, "icaoClass")
    if class_code not in ICAO_CLASS_BY_CODE:
        raise _SkipItem(f"unknown icaoClass {class_code}")
    lower = _vertical_limit(item, "lowerLimit")
    upper = _vertical_limit(item, "upperLimit")
    ring = _polygon_ring(item.get("geometry"))
    try:
        return Airspace(
            id=airspace_id,
            name=name,
            type=TYPE_BY_CODE.get(type_code, f"TYPE_{type_code}"),
            icao_class=ICAO_CLASS_BY_CODE[class_code],
            lower=lower,
            upper=upper,
            polygon_lonlat=ring,
        )
    except ValidationError as exc:
        raise _SkipItem(_describe_validation_error(exc)) from None


def _text(item: dict[object, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise _SkipItem(f"{key} must be a non-empty string, got {_json_type(value)}")
    return value


def _code(container: dict[object, object], key: str) -> int:
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _SkipItem(f"{key} must be a code (an integer >= 0), got {_json_type(value)}")
    return value


def _number(value: object) -> float | None:
    """``value`` as a finite float, or None if it is not a JSON number (booleans are not)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer beyond the float range
        return None
    return number if math.isfinite(number) else None


def _vertical_limit(item: dict[object, object], key: str) -> VerticalLimit:
    limit = item.get(key)
    if not isinstance(limit, dict):
        raise _SkipItem(f"{key} must be an object, got {_json_type(limit)}")
    value = _number(limit.get("value"))
    if value is None:
        raise _SkipItem(f"{key}.value must be a finite number")
    unit_code = _code(limit, "unit")
    if unit_code not in UNIT_BY_CODE:
        raise _SkipItem(f"{key}: unknown unit {unit_code}")
    reference_code = _code(limit, "referenceDatum")
    if reference_code not in REFERENCE_BY_CODE:
        raise _SkipItem(f"{key}: unknown referenceDatum {reference_code}")
    return VerticalLimit(
        value=value, unit=UNIT_BY_CODE[unit_code], reference=REFERENCE_BY_CODE[reference_code]
    )


def _polygon_ring(geometry: object) -> list[tuple[float, float]]:
    """The single ring of a GeoJSON-style Polygon as (lon, lat) tuples, closed if open."""
    if not isinstance(geometry, dict):
        raise _SkipItem(f"geometry must be an object, got {_json_type(geometry)}")
    geometry_type = geometry.get("type")
    if geometry_type != "Polygon":
        shown = geometry_type if isinstance(geometry_type, str) else _json_type(geometry_type)
        raise _SkipItem(f"geometry type {_preview(shown)!r} is not supported: only Polygon")
    rings = geometry.get("coordinates")
    if not isinstance(rings, list) or len(rings) != 1 or not isinstance(rings[0], list):
        raise _SkipItem("a Polygon must have exactly one ring (holes are not supported)")
    ring: list[tuple[float, float]] = []
    for number, position in enumerate(rings[0], start=1):
        if not isinstance(position, list | tuple) or len(position) != 2:
            raise _SkipItem(f"position {number} must be [lon, lat]")
        lon, lat = _number(position[0]), _number(position[1])
        if lon is None or lat is None:
            raise _SkipItem(f"position {number} must hold two finite numbers")
        ring.append((lon, lat))
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _read_openaip_file(path: Path) -> bytes:
    """Read a regular file of at most ``MAX_OPENAIP_FILE_SIZE`` bytes.

    Same approach as the route reader: open once with ``O_NONBLOCK`` (a FIFO cannot block
    the open), check the descriptor is a regular file, then read a bounded number of bytes.
    """
    name = _preview(path.name)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise OpenAipFileError(f"openAIP file not found: {name}") from None
    except OSError as exc:
        raise OpenAipFileError(f"cannot open openAIP file {name}: {exc.strerror}") from None
    except ValueError:  # NUL byte or unencodable characters in the path
        raise OpenAipFileError(f"invalid openAIP file path: {name!r}") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OpenAipFileError(f"openAIP file not found or not a regular file: {name}")
        os.set_blocking(fd, True)  # O_NONBLOCK was only for opening; read normally
        file = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with file:
        try:
            data = file.read(MAX_OPENAIP_FILE_SIZE + 1)
        except OSError as exc:
            raise OpenAipFileError(f"cannot read openAIP file {name}: {exc.strerror}") from None
    if len(data) > MAX_OPENAIP_FILE_SIZE:
        raise OpenAipFileError(
            f"openAIP file {name} is too large: more than {MAX_OPENAIP_FILE_SIZE:,} bytes"
        )
    return data


def _describe_item(item: object) -> str:
    name = item.get("name") if isinstance(item, dict) else None
    return f" ({_preview(name)!r})" if isinstance(name, str) and name else ""


def _describe_validation_error(exc: ValidationError) -> str:
    """The first validation error only: a broken ring can produce thousands of them."""
    errors = exc.errors()
    first = errors[0]
    location = ".".join(str(part) for part in first["loc"])
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{location}: {first['msg']}{more}"


def _json_type(value: object) -> str:
    """The JSON name of a value's type, for error messages that must not echo the value."""
    match value:
        case None:
            return "null"
        case bool():
            return "a boolean"
        case int() | float():
            return "a number"
        case str():
            return "a string" if value else "an empty string"
        case list() | tuple():
            return "a list"
        case dict():
            return "an object"
        case _:
            return type(value).__name__


def _preview(text: str, limit: int = _ERROR_PREVIEW_CHARS) -> str:
    """Shorten untrusted text before echoing it in an error message."""
    return text if len(text) <= limit else text[:limit] + "..."
