"""Vertical profile data, ready for a chart (SPEC.md section 7.5).

- ``aircraft``: the planned altitude along the route as ``(distance_nm, alt_ft)`` points. Legs
  are flat (v0.1), so the line steps up or down at the waypoints.
- ``segments``: for each airspace that matters for the route (one that can produce an event
  and that the route comes within the lateral margin of), the stretches of route where the
  route is inside it or within the margin horizontally, with the airspace's lower and upper
  limit on the day's QNH, the bottom of the margin band under the lower limit, and its
  category (``kind``): the severity section that applies, ``clearance_required``,
  ``requirement`` or ``info``. What actually happens on the route is in the events.
"""

from dataclasses import dataclass
from itertools import pairwise

import numpy as np

from take2.analysis import (
    DEFAULT_LATERAL_MARGIN_NM,
    DEFAULT_VERTICAL_MARGIN_FT,
    MAX_EVENTS,
    SeverityRules,
    evaluate_route,
)
from take2.geo import DEFAULT_STEP_NM, RouteSample
from take2.models import Airspace, Route

# Upper bound on the bands drawn, like the event limit of the analysis.
MAX_SEGMENTS = MAX_EVENTS


@dataclass(frozen=True)
class ProfileSegment:
    """The vertical band of one airspace over a stretch of the route."""

    airspace_id: str
    airspace_name: str
    start_nm: float
    end_nm: float
    lower_ft: float  # lower limit on the day's QNH
    upper_ft: float  # upper limit on the day's QNH
    margin_bottom_ft: float  # bottom of the margin band drawn under the lower limit
    kind: str  # clearance_required, requirement or info
    approximate: bool  # a GND-referenced limit was used without terrain data


@dataclass(frozen=True)
class Profile:
    """Chart data: the aircraft line and the airspace bands along the route."""

    aircraft: list[tuple[float, float]]
    segments: list[ProfileSegment]


def build_profile(
    route: Route,
    airspaces: list[Airspace],
    qnh_hpa: float,
    *,
    rules: SeverityRules | None = None,
    lateral_margin_nm: float = DEFAULT_LATERAL_MARGIN_NM,
    vertical_margin_ft: float = DEFAULT_VERTICAL_MARGIN_FT,
    step_nm: float = DEFAULT_STEP_NM,
    ground_elev_ft: float = 0.0,
) -> Profile:
    """Build the vertical profile of a route.

    Raises ``ValueError`` like ``analyze_route``, and when there would be more than
    ``MAX_SEGMENTS`` segments.
    """
    evaluation = evaluate_route(
        route,
        airspaces,
        qnh_hpa,
        rules=rules,
        lateral_margin_nm=lateral_margin_nm,
        vertical_margin_ft=vertical_margin_ft,
        step_nm=step_nm,
        ground_elev_ft=ground_elev_ft,
    )
    dist_nm = np.array([s.dist_nm for s in evaluation.samples])
    segments: list[ProfileSegment] = []
    for track in evaluation.tracks:
        for first, last in _consecutive_runs(track.indices):
            segments.append(
                ProfileSegment(
                    airspace_id=track.airspace.id,
                    airspace_name=track.airspace.name,
                    start_nm=float(dist_nm[track.indices[first]]),
                    end_nm=float(dist_nm[track.indices[last]]),
                    lower_ft=track.lower_ft,
                    upper_ft=track.upper_ft,
                    margin_bottom_ft=track.lower_ft - vertical_margin_ft,
                    kind=track.category,
                    approximate=track.approximate,
                )
            )
            if len(segments) > MAX_SEGMENTS:
                raise ValueError(
                    f"the profile has more than {MAX_SEGMENTS:,} airspace segments; "
                    "simplify the route"
                )
    segments.sort(key=lambda s: (s.start_nm, s.end_nm, s.airspace_id))
    return Profile(aircraft=_aircraft_line(evaluation.samples), segments=segments)


def _aircraft_line(samples: list[RouteSample]) -> list[tuple[float, float]]:
    """Points of the planned altitude: the start, both sides of each step, and the end."""
    line = [(samples[0].dist_nm, samples[0].alt_ft)]
    for previous, sample in pairwise(samples):
        if sample.alt_ft != previous.alt_ft:
            line.append((sample.dist_nm, previous.alt_ft))
            line.append((sample.dist_nm, sample.alt_ft))
    last = (samples[-1].dist_nm, samples[-1].alt_ft)
    if line[-1] != last:
        line.append(last)
    return line


def _consecutive_runs(indices: np.ndarray) -> list[tuple[int, int]]:
    """``(first, last)`` positions of each run of consecutive sample indices."""
    if indices.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(indices) != 1) + 1
    starts = np.concatenate(([0], breaks))
    ends = np.concatenate((breaks - 1, [indices.size - 1]))
    return [(int(a), int(b)) for a, b in zip(starts, ends, strict=True)]
