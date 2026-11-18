"""openAIP airspace data (SPEC.md section 5, milestone 5).

``fetch_airspaces`` downloads the raw airspace items of one country from the openAIP API,
``parse_openaip_airspaces`` converts items into ``Airspace`` models, and
``load_openaip_file`` reads them from a saved JSON file.

All of it treats the data as untrusted. An item that is malformed, uses a code Take2 does
not know, or fails model validation is skipped and counted instead of failing the whole
file, and no JSON value can make the conversion crash. A download is bounded in pages,
bytes and time, and the API key never appears in a URL, an error message or a log line.

The openAIP endpoint and codes come from the official OpenAPI schema
(https://api.core.openaip.net/api/system/specs/v1/schema.json) and the airspace response
schema (https://api.core.openaip.net/api/schemas/response/airspace/airspace-schema.json).

Airspace data © openAIP contributors (openaip.net), CC BY-NC 4.0.
"""

import contextlib
import json
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import ValidationError

from take2 import __version__
from take2.models import Airspace, LimitReference, VerticalLimit

ATTRIBUTION = "Airspace data © openAIP contributors (openaip.net), CC BY-NC 4.0"
OPENAIP_API_URL = "https://api.core.openaip.net/api"
API_KEY_HEADER = "x-openaip-api-key"
PAGE_SIZE = 1000  # items requested per page (the openAIP ``limit`` parameter)
# Bounds for a download, so a broken or hostile server cannot loop or exhaust memory.
MAX_PAGES = 100
MAX_PAGE_BYTES = 50_000_000  # one page body as received (compressed bodies are refused)
MAX_DOWNLOAD_BYTES = 100_000_000  # all pages together; equal to the file limit below
MAX_PAGE_SECONDS = 120.0  # wall-clock time for one page, however slowly it trickles in
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=10.0)  # seconds per connect, read, write
MAX_API_KEY_LENGTH = 512
API_KEY_ENV_VAR = "OPENAIP_API_KEY"
MAX_ENV_FILE_SIZE = 1_000_000  # bytes; a .env file is a few lines

# Generous on purpose (room for large countries); it bounds what a hostile file can cost.
MAX_OPENAIP_FILE_SIZE = 100_000_000  # bytes
MAX_REPORTED_PROBLEMS = 20  # skipped items are all counted, but only these are described
MAX_TYPE_CODE = 999  # openAIP type codes are small; larger numbers are treated as malformed
_ERROR_PREVIEW_CHARS = 40
_COUNTRY_RE = re.compile(r"[A-Za-z]{2}")  # used with fullmatch: no trailing newline
_API_KEY_RE = re.compile(r"[!-~]+")  # visible ASCII: valid in a header, nothing to escape
# One .env assignment: optional "export", a variable name, "=", the rest of the line.
_ENV_LINE_RE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*")
# Starts a comment in an unquoted value: "#" at its start or after whitespace ("a#b" is a value).
_ENV_COMMENT_RE = re.compile(r"(?:^|\s+)#")
_clock = time.monotonic  # replaced in tests

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


class OpenAipFetchError(RuntimeError):
    """Downloading from the openAIP API failed (HTTP error, network error, bad response)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code  # the HTTP status, when the server answered


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


def save_openaip_file(path: str | Path, items: list[dict[str, object]], *, country: str) -> None:
    """Write raw openAIP items to a file that ``load_openaip_file`` reads back.

    The file is a UTF-8 JSON object: ``attribution`` (the openAIP licence line),
    ``country``, ``fetchedAt`` (UTC, ISO 8601) and ``items`` exactly as given. It is written
    to a temporary file in the same directory and then renamed over ``path``, so an existing
    file is either fully replaced or left as it was. Missing parent directories are created.

    Raises ``OpenAipFileError`` if the items are not standard JSON or would make a file
    larger than ``MAX_OPENAIP_FILE_SIZE`` (nothing is written), and ``OSError`` if writing
    fails.
    """
    path = Path(path)
    document = {
        "attribution": ATTRIBUTION,
        "country": normalize_country(country),
        "fetchedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }
    try:
        text = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise OpenAipFileError(f"the airspace items cannot be written as JSON: {exc}") from None
    data = text.encode("utf-8") + b"\n"
    if len(data) > MAX_OPENAIP_FILE_SIZE:
        raise OpenAipFileError(
            f"the airspace data is larger than {MAX_OPENAIP_FILE_SIZE:,} bytes "
            "and could not be read back"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def read_api_key(env_file: Path = Path(".env")) -> str | None:
    """The openAIP API key, or None when none is configured.

    The ``OPENAIP_API_KEY`` environment variable wins when it is set and not blank.
    Otherwise ``env_file`` is read if it is a regular file: simple ``KEY=VALUE`` lines, an
    optional ``export`` prefix, optional single or double quotes around the value, ``#``
    comment lines and `` # comments`` after an unquoted value. The last assignment wins; an
    empty value means no key. There are no escapes or variable expansion.

    Raises ``ValueError`` (never quoting the value) if the file is larger than
    ``MAX_ENV_FILE_SIZE``, is not UTF-8, or the key's value has an unclosed quote, and
    ``OSError`` if it cannot be read.
    """
    from_environment = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if from_environment:
        return from_environment
    if not env_file.is_file():  # also keeps a FIFO from blocking the read below
        return None
    name = _preview(env_file.name)
    with env_file.open("rb") as file:
        data = file.read(MAX_ENV_FILE_SIZE + 1)
    if len(data) > MAX_ENV_FILE_SIZE:
        raise ValueError(f"{name} is larger than {MAX_ENV_FILE_SIZE:,} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{name} is not a UTF-8 text file") from None
    value: str | None = None
    for line in text.splitlines():
        match = _ENV_LINE_RE.fullmatch(line)
        if match is not None and match.group(1) == API_KEY_ENV_VAR:
            value = _env_value(match.group(2), name)
    return value or None


def _env_value(raw: str, file_name: str) -> str:
    """The value part of a ``.env`` line: unquoted, without a trailing comment."""
    if raw[:1] in ('"', "'"):
        end = raw.find(raw[0], 1)
        if end == -1:
            raise ValueError(f"{API_KEY_ENV_VAR} in {file_name} has no closing quote")
        return raw[1:end]
    return _ENV_COMMENT_RE.split(raw, maxsplit=1)[0].strip()


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


def normalize_country(country: str) -> str:
    """Validate an ISO 3166-1 alpha-2 country code (two ASCII letters); return it upper-cased."""
    if not _COUNTRY_RE.fullmatch(country):
        raise ValueError(
            f"invalid country {_preview(country, 10)!r}: expected a two-letter ISO 3166-1 "
            "country code such as RO"
        )
    return country.upper()


def fetch_airspaces(
    country: str, api_key: str, *, client: httpx.Client | None = None
) -> list[dict[str, object]]:
    """Download every airspace item of one country from the openAIP API.

    Requests ``GET /airspaces`` page by page until a page has no ``nextPage``, and returns
    the raw items in order. The key goes in the ``x-openaip-api-key`` header, never in the
    URL, and redirects are never followed (httpx would forward that header to any host).
    Every request uses ``REQUEST_TIMEOUT``, also on a caller's ``client``; a caller's client
    is left open. A download is bounded by ``MAX_PAGES``, ``MAX_PAGE_BYTES``,
    ``MAX_DOWNLOAD_BYTES`` and ``MAX_PAGE_SECONDS``.

    Raises ``ValueError`` for an invalid country or an unusable key (before any request) and
    ``OpenAipFetchError`` when the download fails. No message contains the key.
    """
    country = normalize_country(country)
    _check_api_key(api_key)
    if client is not None:
        return _fetch_pages(client, country, api_key)
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False) as own_client:
        return _fetch_pages(own_client, country, api_key)


def _check_api_key(api_key: str) -> None:
    """Reject a key that cannot be sent as a header value, without echoing it."""
    if not api_key:
        raise ValueError("the openAIP API key is empty")
    if len(api_key) > MAX_API_KEY_LENGTH:
        raise ValueError(f"the openAIP API key is longer than {MAX_API_KEY_LENGTH} characters")
    if not _API_KEY_RE.fullmatch(api_key):
        raise ValueError(
            "the openAIP API key contains spaces, control or non-ASCII characters; "
            "check that it was copied correctly"
        )


def _fetch_pages(client: httpx.Client, country: str, api_key: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    downloaded_bytes = 0
    for page in range(1, MAX_PAGES + 1):
        data = _download_page(client, country, api_key, page, downloaded_bytes)
        downloaded_bytes += len(data)
        page_items, has_next_page = _read_page(data, page)
        items.extend(page_items)
        if not has_next_page:
            return items
    raise OpenAipFetchError(
        f"openAIP returned more than {MAX_PAGES} pages; stopped to avoid an endless download"
    )


def _download_page(
    client: httpx.Client, country: str, api_key: str, page: int, downloaded_bytes: int
) -> bytes:
    """One page body, read in a stream and bounded in size and time."""
    total_left = MAX_DOWNLOAD_BYTES - downloaded_bytes
    limit_bytes = min(MAX_PAGE_BYTES, total_left)
    deadline = _clock() + MAX_PAGE_SECONDS
    try:
        with client.stream(
            "GET",
            f"{OPENAIP_API_URL}/airspaces",
            params={"country": country, "page": page, "limit": PAGE_SIZE},
            headers={
                API_KEY_HEADER: api_key,
                "Accept": "application/json",
                # No compression: httpx inflates each received chunk before the size check
                # could see it, so a small compressed body could take gigabytes of memory.
                "Accept-Encoding": "identity",
                "User-Agent": f"take2/{__version__}",
            },
            timeout=REQUEST_TIMEOUT,
            follow_redirects=False,
        ) as response:
            _check_status(response.status_code)
            _check_not_compressed(response.headers.get("content-encoding", ""), page)
            declared = response.headers.get("content-length", "")
            if declared.isascii() and declared.isdigit() and int(declared) > limit_bytes:
                raise _too_large(page, limit_bytes, total_left)
            body = bytearray()
            # httpx chooses its decoder from the same Content-Encoding header checked above,
            # so these are the bytes received, not an inflated version of them.
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > limit_bytes:
                    raise _too_large(page, limit_bytes, total_left)
                if _clock() > deadline:
                    raise OpenAipFetchError(
                        f"openAIP sent page {page} too slowly (more than "
                        f"{MAX_PAGE_SECONDS:.0f} s); try again later",
                        status_code=response.status_code,
                    )
            return bytes(body)
    except httpx.TimeoutException:
        raise OpenAipFetchError("openAIP did not answer in time; try again later") from None
    except httpx.HTTPError as exc:
        raise OpenAipFetchError(
            f"cannot reach openAIP: {_describe_http_error(exc, api_key)}"
        ) from None


def _check_status(status: int) -> None:
    if status == 200:
        return
    if status in (401, 403):
        raise OpenAipFetchError(f"openAIP rejected the API key (HTTP {status})", status)
    hint = {
        # Observed on the live API: an unknown key gets 404 ("Failed to load user
        # permissions"), not 401/403. The URL is fixed, so the key is the likely cause.
        404: f"openAIP answers 404 for an API key it does not know; check {API_KEY_ENV_VAR}",
        429: "too many requests; try again later",
    }.get(status)
    if hint is None and status >= 500:
        hint = "openAIP server error; try again later"
    message = f"openAIP request failed with HTTP {status}"
    raise OpenAipFetchError(f"{message} ({hint})" if hint else message, status)


def _check_not_compressed(content_encoding: str, page: int) -> None:
    """Refuse a compressed body: identity was requested, but a server or proxy may ignore it."""
    if content_encoding.strip().lower() in ("", "identity"):
        return
    raise OpenAipFetchError(
        f"openAIP sent page {page} with Content-Encoding {_preview(content_encoding, 20)!r} "
        "although an uncompressed response was requested; refusing to decompress it",
        status_code=200,
    )


def _too_large(page: int, limit_bytes: int, total_left: int) -> OpenAipFetchError:
    if total_left < MAX_PAGE_BYTES:  # the total, not the page, was the tighter limit
        message = f"the openAIP download is larger than {MAX_DOWNLOAD_BYTES:,} bytes in total"
    else:
        message = f"openAIP page {page} is larger than {limit_bytes:,} bytes"
    return OpenAipFetchError(message, status_code=200)


def _read_page(data: bytes, page: int) -> tuple[list[dict[str, object]], bool]:
    """The items of one page, and whether another page follows."""
    try:
        document = _parse_json(data)
    except ValueError as exc:
        raise OpenAipFetchError(f"openAIP page {page} is {exc}", status_code=200) from None
    if not isinstance(document, dict):
        raise OpenAipFetchError(
            f"unexpected openAIP response on page {page}: expected a JSON object", 200
        )
    items = document.get("items")
    if not isinstance(items, list):
        raise OpenAipFetchError(f"unexpected openAIP response on page {page}: no 'items' list", 200)
    if not all(isinstance(item, dict) for item in items):
        raise OpenAipFetchError(
            f"unexpected openAIP response on page {page}: every item must be a JSON object", 200
        )
    next_page = document.get("nextPage")
    if next_page is None:
        return items, False
    # page + 1 >= 2, so booleans (equal to 0 and 1) can never pass this check either.
    if not isinstance(next_page, int) or next_page != page + 1:
        raise OpenAipFetchError(
            f"unexpected openAIP response on page {page}: unexpected nextPage "
            f"({_json_type(next_page)}, expected {page + 1} or none)",
            200,
        )
    return items, True


def _describe_http_error(exc: httpx.HTTPError, api_key: str) -> str:
    """The error's kind and a short, redacted detail: httpx messages can quote headers."""
    detail = _preview(str(exc).replace(api_key, "[redacted]"), 80)
    name = type(exc).__name__
    return f"{name} ({detail})" if detail else name


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
