"""Route analysis against airspaces (SPEC.md section 7.4).

For each sample of the densified route and each airspace, with the day's real lower and
upper limits ``L`` and ``U`` and the sample altitude ``a``:

- ``d_h``: lateral distance in NM, 0 inside the polygon. It is measured for the piece of route
  the sample stands for (half way to its neighbours, never across a waypoint), so a short
  incursion between two samples, such as clipping a corner, is not missed.
- ``d_v``: 0 when ``L <= a <= U``, otherwise ``L - a`` (below) or ``a - U`` (above), in ft.
- INSIDE when ``d_h == 0`` and ``d_v == 0``; CLOSE when not inside but within the expanded
  volume (``d_h <= lateral margin`` and ``L - margin <= a <= U + margin``); otherwise CLEAR.

States map to event kinds with ``config/severity.yaml``. Consecutive samples with the same
airspace and the same kind are merged into one ``Event``.

Airspaces are external data and routes may be uploaded, so work is bounded: airspaces that
can never produce an event are skipped, a spatial index limits distance computations to the
route pieces within the margin, and the number of events is capped.
"""

import functools
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np
import shapely
import yaml

from take2.altimetry import check_qnh_hpa, limit_to_altitude_ft
from take2.geo import DEFAULT_STEP_NM, M_PER_NM, LocalProjection, RouteSample, densify_route
from take2.io.files import read_text_file
from take2.models import Airspace, Event, Route

DEFAULT_LATERAL_MARGIN_NM = 2.0  # Take 2 rule
DEFAULT_VERTICAL_MARGIN_FT = 200.0  # Take 2 rule
DEFAULT_SEVERITY_FILE = Path(__file__).resolve().parents[2] / "config" / "severity.yaml"
MAX_EVENTS = 10_000
# Total route samples kept across all airspaces near the route (memory bound).
MAX_TRACK_SAMPLES = 2_000_000
# Airspaces wider than this are not supported: the far-field check below relies on it.
MAX_AIRSPACE_EXTENT_M = 1_500_000.0

_MAX_SEVERITY_FILE_BYTES = 100_000
_MAX_SEVERITY_DEPTH = 4  # mapping > section > key > list
_ICAO_CLASS_RE = re.compile(r"[A-G]")
_TYPE_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")


class State(StrEnum):
    """Position of a route sample relative to one airspace."""

    INSIDE = "INSIDE"
    CLOSE = "CLOSE"
    CLEAR = "CLEAR"


@dataclass(frozen=True)
class SeverityRules:
    """Which airspaces need a clearance, impose a requirement, or are information only."""

    clearance_classes: frozenset[str]
    clearance_types: frozenset[str]
    requirement_types: frozenset[str]
    info_types: frozenset[str]

    def category(self, airspace: Airspace) -> str | None:
        """The severity section that applies to an airspace, strictest first, or None."""
        if airspace.icao_class in self.clearance_classes or airspace.type in self.clearance_types:
            return "clearance_required"
        if airspace.type in self.requirement_types:
            return "requirement"
        if airspace.type in self.info_types:
            return "info"
        return None

    def kind(self, airspace: Airspace, state: State) -> str | None:
        """Event kind for an airspace in a given state, or None when it is not reported."""
        category = self.category(airspace)
        if state is State.CLEAR or category is None:
            return None
        if category == "clearance_required":
            return "INFRINGEMENT" if state is State.INSIDE else "WARNING"
        if state is State.INSIDE:
            return "REQUIREMENT" if category == "requirement" else "INFO"
        return None


def load_severity_rules(path: str | Path = DEFAULT_SEVERITY_FILE) -> SeverityRules:
    """Read and validate a severity file (see ``config/severity.yaml``).

    All three sections are required, ``clearance_required`` needs both classes and types,
    classes must be ICAO classes A to G and types upper-case names, because a missing or
    misspelt entry would silently switch detection off. YAML anchors, aliases and duplicate
    keys are refused.
    """
    path = Path(path)
    text = read_text_file(path, _MAX_SEVERITY_FILE_BYTES, label="severity file")
    content = _load_plain_yaml(text, path.name)

    keys = {"clearance_required": {"classes", "types"}, "requirement": {"types"}, "info": {"types"}}
    if not isinstance(content, dict) or set(content) != set(keys):
        raise ValueError(f"severity file {path.name} must have exactly the sections {sorted(keys)}")

    def names(section: str, key: str, pattern: re.Pattern[str]) -> frozenset[str]:
        body = content[section]
        if not isinstance(body, dict) or set(body) != keys[section]:
            raise ValueError(
                f"severity file {path.name}: section {section!r} must contain exactly "
                f"{' and '.join(sorted(keys[section]))}"
            )
        values = body[key]
        if not isinstance(values, list) or not all(
            isinstance(v, str) and pattern.fullmatch(v) for v in values
        ):
            raise ValueError(
                f"severity file {path.name}: {section}.{key} must be a list of "
                f"{'ICAO classes A to G' if key == 'classes' else 'upper-case type names'}"
            )
        return frozenset(values)

    return SeverityRules(
        clearance_classes=names("clearance_required", "classes", _ICAO_CLASS_RE),
        clearance_types=names("clearance_required", "types", _TYPE_NAME_RE),
        requirement_types=names("requirement", "types", _TYPE_NAME_RE),
        info_types=names("info", "types", _TYPE_NAME_RE),
    )


# Event kinds as small integer codes, so per-sample results stay compact numpy arrays.
KIND_NAMES: tuple[str | None, ...] = (None, "INFRINGEMENT", "WARNING", "REQUIREMENT", "INFO")
_INFRINGEMENT, _WARNING, _REQUIREMENT, _INFO = 1, 2, 3, 4


@dataclass(frozen=True)
class AirspaceTrack:
    """How one airspace relates to the route samples that come within the lateral margin.

    Only those samples are kept (``indices`` into the route samples, in increasing order),
    so memory grows with the part of the route near the airspace, not with the whole route.
    """

    airspace: Airspace
    category: str  # the severity section that applies: clearance_required, requirement or info
    lower_ft: float  # lower limit on the day's QNH
    upper_ft: float  # upper limit on the day's QNH
    approximate: bool  # a GND-referenced limit was used without terrain data
    indices: np.ndarray  # sample indices within the lateral margin
    lateral_nm: np.ndarray  # lateral distance at those samples (0 inside)
    vertical_ft: np.ndarray  # vertical distance at those samples (0 inside the band)
    kind_codes: np.ndarray  # event kind code at those samples (index into KIND_NAMES)


@dataclass(frozen=True)
class RouteEvaluation:
    """Samples of the route and the airspaces that matter for it."""

    samples: list[RouteSample]
    tracks: list[AirspaceTrack]


def evaluate_route(
    route: Route,
    airspaces: Iterable[Airspace],
    qnh_hpa: float,
    *,
    rules: SeverityRules | None = None,
    lateral_margin_nm: float = DEFAULT_LATERAL_MARGIN_NM,
    vertical_margin_ft: float = DEFAULT_VERTICAL_MARGIN_FT,
    step_nm: float = DEFAULT_STEP_NM,
    ground_elev_ft: float = 0.0,
) -> RouteEvaluation:
    """Evaluate every sample against every airspace that can produce an event.

    Airspaces are kept only if part of the route comes within ``lateral_margin_nm`` of them.
    Raises ``ValueError`` for an implausible QNH, negative or non-finite margins, a
    non-finite ground elevation, or more than ``MAX_TRACK_SAMPLES`` kept samples in total.
    """
    samples, tracks = _evaluate(
        route,
        airspaces,
        qnh_hpa,
        rules,
        lateral_margin_nm,
        vertical_margin_ft,
        step_nm,
        ground_elev_ft,
    )
    return RouteEvaluation(samples=samples, tracks=list(tracks))


def analyze_route(
    route: Route,
    airspaces: Iterable[Airspace],
    qnh_hpa: float,
    *,
    rules: SeverityRules | None = None,
    lateral_margin_nm: float = DEFAULT_LATERAL_MARGIN_NM,
    vertical_margin_ft: float = DEFAULT_VERTICAL_MARGIN_FT,
    step_nm: float = DEFAULT_STEP_NM,
    ground_elev_ft: float = 0.0,
) -> list[Event]:
    """Return the events of a route against airspaces, ordered by distance along the route.

    Raises ``ValueError`` like ``evaluate_route``, and as soon as there are more than
    ``MAX_EVENTS`` events (the remaining airspaces are then not evaluated).
    """
    samples, tracks = _evaluate(
        route,
        airspaces,
        qnh_hpa,
        rules,
        lateral_margin_nm,
        vertical_margin_ft,
        step_nm,
        ground_elev_ft,
    )
    dist_nm = np.array([s.dist_nm for s in samples])
    events: list[Event] = []
    for track in tracks:
        for code, first, last in _runs(track.indices, track.kind_codes):
            if code == 0:
                continue
            events.append(
                Event(
                    airspace_id=track.airspace.id,
                    airspace_name=track.airspace.name,
                    kind=KIND_NAMES[code],
                    start_nm=float(dist_nm[track.indices[first]]),
                    end_nm=float(dist_nm[track.indices[last]]),
                    min_lateral_nm=float(track.lateral_nm[first : last + 1].min()),
                    min_vertical_ft=float(track.vertical_ft[first : last + 1].min()),
                    approximate=track.approximate,
                )
            )
            if len(events) > MAX_EVENTS:
                raise ValueError(
                    f"the analysis found more than {MAX_EVENTS:,} events; simplify the route"
                )
    return sorted(events, key=lambda e: (e.start_nm, e.end_nm, e.airspace_id))


def _evaluate(
    route: Route,
    airspaces: Iterable[Airspace],
    qnh_hpa: float,
    rules: SeverityRules | None,
    lateral_margin_nm: float,
    vertical_margin_ft: float,
    step_nm: float,
    ground_elev_ft: float,
) -> tuple[list[RouteSample], Iterator[AirspaceTrack]]:
    """Check the parameters, sample the route, and return the samples with a lazy track stream.

    The tracks are produced one airspace at a time, so a caller can stop early.
    """
    check_qnh_hpa(qnh_hpa)
    for name, value in (
        ("lateral_margin_nm", lateral_margin_nm),
        ("vertical_margin_ft", vertical_margin_ft),
    ):
        if not (math.isfinite(value) and value >= 0):
            raise ValueError(f"{name} must be a finite number >= 0, got {value}")
    if not math.isfinite(ground_elev_ft):
        raise ValueError(f"ground_elev_ft must be finite, got {ground_elev_ft}")
    rules = rules or _default_rules()

    samples = densify_route(route, step_nm)
    projection = LocalProjection.for_route(route)
    xy_m = projection.to_xy_m_many([s.lon for s in samples], [s.lat for s in samples])
    pieces = _route_pieces(samples, xy_m)
    tree = shapely.STRtree(pieces)
    alt_ft = np.array([s.alt_ft for s in samples])
    margin_m = lateral_margin_nm * M_PER_NM
    # In AEQD the distance from the centre is the true geodesic distance, so an airspace whose
    # nearest vertex is farther than this cannot come within the margin of the route.
    reach_m = float(np.hypot(xy_m[:, 0], xy_m[:, 1]).max()) + margin_m + MAX_AIRSPACE_EXTENT_M

    def tracks() -> Iterator[AirspaceTrack]:
        kept_samples = 0
        for space in airspaces:
            category = rules.category(space)
            if category is None:
                continue  # this airspace never produces an event
            ring_m = projection.to_xy_m_many(*zip(*space.polygon_lonlat, strict=True))
            if np.hypot(ring_m[:, 0], ring_m[:, 1]).min() > reach_m:
                continue  # far away, possibly where the local projection is badly distorted
            polygon = shapely.Polygon(ring_m)
            # Candidates within the margin (widened slightly for rounding; exact test below).
            near = tree.query(polygon, predicate="dwithin", distance=margin_m * (1 + 1e-9) + 1e-6)
            if near.size == 0:
                continue
            near = np.sort(near)
            d_h_nm = shapely.distance(polygon, pieces[near]) / M_PER_NM
            within = d_h_nm <= lateral_margin_nm
            if not within.any():
                continue
            indices, d_h_nm = near[within], d_h_nm[within]
            kept_samples += indices.size
            if kept_samples > MAX_TRACK_SAMPLES:
                raise ValueError(
                    f"the route passes near too many airspaces: more than "
                    f"{MAX_TRACK_SAMPLES:,} samples to evaluate; simplify the route"
                )

            lower_ft, lower_approx = limit_to_altitude_ft(space.lower, qnh_hpa, ground_elev_ft)
            upper_ft, upper_approx = limit_to_altitude_ft(space.upper, qnh_hpa, ground_elev_ft)
            alt_near_ft = alt_ft[indices]
            d_v_ft = np.maximum(0.0, np.maximum(lower_ft - alt_near_ft, alt_near_ft - upper_ft))
            inside = (d_h_nm == 0.0) & (d_v_ft == 0.0)
            close = (
                ~inside
                & (alt_near_ft >= lower_ft - vertical_margin_ft)
                & (alt_near_ft <= upper_ft + vertical_margin_ft)
            )
            codes = np.zeros(indices.size, dtype=np.int8)
            if category == "clearance_required":
                codes[inside] = _INFRINGEMENT
                codes[close] = _WARNING
            else:
                codes[inside] = _REQUIREMENT if category == "requirement" else _INFO
            yield AirspaceTrack(
                airspace=space,
                category=category,
                lower_ft=lower_ft,
                upper_ft=upper_ft,
                approximate=lower_approx or upper_approx,
                indices=indices,
                lateral_nm=d_h_nm,
                vertical_ft=d_v_ft,
                kind_codes=codes,
            )

    return samples, tracks()


@functools.cache
def _default_rules() -> SeverityRules:
    return load_severity_rules(DEFAULT_SEVERITY_FILE)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys instead of keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load_plain_yaml(text: str, name: str) -> object:
    """Load small, plain YAML: no anchors or aliases, shallow nesting, unique keys.

    The event scan runs before any object is built, so a merge-key or alias bomb and deep
    nesting are refused without expanding them.
    """
    try:
        depth = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ValueError(f"severity file {name} must not use YAML anchors or aliases")
            if isinstance(event, yaml.MappingStartEvent | yaml.SequenceStartEvent):
                depth += 1
                if depth > _MAX_SEVERITY_DEPTH:
                    raise ValueError(f"severity file {name} is nested too deeply")
            elif isinstance(event, yaml.MappingEndEvent | yaml.SequenceEndEvent):
                depth -= 1
        # A SafeLoader subclass: it never builds Python objects from tags.
        return yaml.load(text, Loader=_UniqueKeySafeLoader)
    except (yaml.YAMLError, TypeError) as exc:
        raise ValueError(f"severity file {name} is not valid YAML") from exc


def _route_pieces(samples: list[RouteSample], xy_m: np.ndarray) -> np.ndarray:
    """One line per sample covering its part of the route, so the pieces tile the route.

    A piece runs from half way to the previous sample to half way to the next one, but stops
    at waypoints: a sample that starts a leg begins exactly there, and the last sample of a
    leg covers the leg up to its end. Each piece therefore lies on a single leg, at that
    leg's altitude.
    """
    count = len(samples)
    starts = xy_m.copy()
    ends = xy_m.copy()
    for i in range(count):
        if i > 0 and samples[i - 1].leg_index == samples[i].leg_index:
            starts[i] = (xy_m[i - 1] + xy_m[i]) / 2
        if i < count - 1:
            same_leg = samples[i + 1].leg_index == samples[i].leg_index
            ends[i] = (xy_m[i] + xy_m[i + 1]) / 2 if same_leg else xy_m[i + 1]
    return shapely.linestrings(np.stack([starts, xy_m, ends], axis=1))


def _runs(indices: np.ndarray, codes: np.ndarray) -> Iterator[tuple[int, int, int]]:
    """``(code, first, last)`` positions of each run of consecutive samples with one code.

    A run breaks where the sample indices are not consecutive (the route left the margin in
    between) or where the code changes.
    """
    if indices.size == 0:
        return
    breaks = np.flatnonzero((np.diff(indices) != 1) | (np.diff(codes) != 0)) + 1
    starts = np.concatenate(([0], breaks))
    ends = np.concatenate((breaks - 1, [indices.size - 1]))
    for first, last in zip(starts, ends, strict=True):
        yield int(codes[first]), int(first), int(last)
