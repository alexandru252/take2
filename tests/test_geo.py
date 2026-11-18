"""Tests for take2.geo, using the reference values from SPEC.md section 7.3."""

import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pyproj import Geod

from take2.geo import (
    M_PER_NM,
    MAX_ROUTE_NM,
    MAX_ROUTE_SAMPLES,
    LocalProjection,
    densify_route,
    geodesic_distance_m,
    lateral_distance_nm,
    route_midpoint,
)
from take2.models import Route, Waypoint

TOLERANCE_M = 1.0  # SPEC.md 7.3
TOLERANCE_NM = TOLERANCE_M / 1852
STEP_NM = 0.25

# Independent WGS84 geodesic used only to build test geometry.
_GEOD = Geod(ellps="WGS84")


def offset(lon: float, lat: float, azimuth_deg: float, dist_m: float) -> tuple[float, float]:
    """Point reached from (lon, lat) along a geodesic with the given azimuth and length."""
    lon2, lat2, _ = _GEOD.fwd(lon, lat, azimuth_deg, dist_m)
    return lon2, lat2


def square_ring(lon: float, lat: float, half_size_m: float) -> list[tuple[float, float]]:
    """Closed lon/lat ring of a square centred on (lon, lat), edges on meridians and parallels."""
    _, lat_n = offset(lon, lat, 0, half_size_m)
    _, lat_s = offset(lon, lat, 180, half_size_m)
    lon_e, _ = offset(lon, lat, 90, half_size_m)
    lon_w, _ = offset(lon, lat, 270, half_size_m)
    return [(lon_w, lat_s), (lon_e, lat_s), (lon_e, lat_n), (lon_w, lat_n), (lon_w, lat_s)]


def make_route(*points: tuple[float, float, float | None]) -> Route:
    """Route from (lon, lat, alt_ft) tuples."""
    return Route(
        waypoints=[
            Waypoint(name=f"P{i}", lon=lon, lat=lat, alt_ft=alt_ft)
            for i, (lon, lat, alt_ft) in enumerate(points)
        ]
    )


# --- geodesic distance: SPEC.md 7.3 reference values -------------------------------

REFERENCE_DISTANCES = [
    pytest.param((26.0, 44.0), (26.0, 45.0), 111_122.0, 60.001, id="meridian-44N-45N"),
    pytest.param((26.0, 44.5), (27.0, 44.5), 79_529.1, 42.942, id="parallel-44.5N-26E-27E"),
]


@pytest.mark.parametrize(("start", "end", "dist_m", "dist_nm"), REFERENCE_DISTANCES)
def test_geodesic_distance_matches_reference(
    start: tuple[float, float], end: tuple[float, float], dist_m: float, dist_nm: float
) -> None:
    assert geodesic_distance_m(*start, *end) == pytest.approx(dist_m, abs=TOLERANCE_M)


@pytest.mark.parametrize(("start", "end", "dist_m", "dist_nm"), REFERENCE_DISTANCES)
def test_reference_distance_in_nautical_miles(
    start: tuple[float, float], end: tuple[float, float], dist_m: float, dist_nm: float
) -> None:
    assert geodesic_distance_m(*start, *end) / M_PER_NM == pytest.approx(dist_nm, abs=TOLERANCE_NM)


def test_nautical_mile_is_1852_metres() -> None:
    assert M_PER_NM == 1852.0


# --- route densification -----------------------------------------------------------


def test_densified_leg_has_a_sample_at_least_every_quarter_nm() -> None:
    samples = densify_route(make_route((26.0, 44.0, 3000), (26.0, 45.0, 3000)))

    gaps_nm = [b.dist_nm - a.dist_nm for a, b in zip(samples, samples[1:], strict=False)]
    assert max(gaps_nm) <= STEP_NM
    assert len(samples) == math.ceil(111_122.0 / M_PER_NM / STEP_NM) + 1


def test_densified_leg_samples_are_evenly_spaced() -> None:
    samples = densify_route(make_route((26.0, 44.0, 3000), (26.0, 45.0, 3000)))

    gaps_nm = [b.dist_nm - a.dist_nm for a, b in zip(samples, samples[1:], strict=False)]
    assert max(gaps_nm) - min(gaps_nm) == pytest.approx(0.0, abs=1e-9)


def test_densified_leg_starts_and_ends_on_the_waypoints() -> None:
    samples = densify_route(make_route((26.0, 44.0, 3000), (26.0, 45.0, 3000)))

    assert (samples[0].lon, samples[0].lat, samples[0].dist_nm) == (26.0, 44.0, 0.0)
    assert (samples[-1].lon, samples[-1].lat) == pytest.approx((26.0, 45.0), abs=1e-12)
    assert samples[-1].dist_nm == pytest.approx(60.001, abs=TOLERANCE_NM)


def test_densified_samples_lie_on_the_geodesic() -> None:
    start, end = (26.0, 44.5), (27.0, 44.5)
    samples = densify_route(make_route((*start, 3000), (*end, 3000)))
    total_m = samples[-1].dist_nm * M_PER_NM

    for sample in samples[::20]:
        from_start_m = geodesic_distance_m(*start, sample.lon, sample.lat)
        to_end_m = geodesic_distance_m(sample.lon, sample.lat, *end)
        assert from_start_m == pytest.approx(sample.dist_nm * M_PER_NM, abs=0.01)
        assert from_start_m + to_end_m == pytest.approx(total_m, abs=0.01)


def test_cumulative_distance_continues_across_legs() -> None:
    a, b, c = (26.0, 44.0), (26.0, 44.5), (26.5, 44.5)
    samples = densify_route(make_route((*a, 2000), (*b, 2500), (*c, 3000)))
    leg1_nm = geodesic_distance_m(*a, *b) / M_PER_NM
    leg2_nm = geodesic_distance_m(*b, *c) / M_PER_NM

    at_b = [s for s in samples if (s.lon, s.lat) == b]
    assert len(at_b) == 1
    assert at_b[0].dist_nm == pytest.approx(leg1_nm, abs=1e-9)
    assert samples[-1].dist_nm == pytest.approx(leg1_nm + leg2_nm, abs=1e-9)


def test_altitude_along_a_leg_is_the_leg_start_altitude() -> None:
    a, b, c = (26.0, 44.0), (26.0, 44.5), (26.5, 44.5)
    samples = densify_route(make_route((*a, 2000), (*b, 2500), (*c, 3000)))
    leg1_nm = geodesic_distance_m(*a, *b) / M_PER_NM

    assert {s.alt_ft for s in samples if s.dist_nm < leg1_nm} == {2000.0}
    # From waypoint B to the route end, including the last point: leg B-C is flown at 2500 ft.
    assert {s.alt_ft for s in samples if s.dist_nm >= leg1_nm} == {2500.0}


def test_leg_shorter_than_one_step_has_only_its_end_points() -> None:
    start = (26.0, 44.5)
    end = offset(*start, 90, 0.1 * M_PER_NM)

    samples = densify_route(make_route((*start, 2000), (*end, 2000)))

    assert [s.dist_nm for s in samples] == pytest.approx([0.0, 0.1], abs=TOLERANCE_NM)


def test_zero_length_leg_adds_no_duplicate_samples() -> None:
    samples = densify_route(make_route((26.0, 44.0, 2000), (26.0, 44.0, 2500), (26.0, 44.1, 2500)))

    dists_nm = [s.dist_nm for s in samples]
    assert all(b > a for a, b in zip(dists_nm, dists_nm[1:], strict=False))
    assert samples[0].alt_ft == 2500.0


def test_repeated_last_waypoint_adds_no_duplicate_sample() -> None:
    # Found by hypothesis: the geodesic distance between these identical points is not
    # exactly 0 (about 2e-12 m), which used to create a second sample at the same distance.
    route = make_route((22.0, 43.5, 3000), (22.0, 44.171875, 3000), (22.0, 44.171875, 3000))

    dists_nm = [s.dist_nm for s in densify_route(route)]

    assert all(b > a for a, b in zip(dists_nm, dists_nm[1:], strict=False))


def test_densify_requires_an_altitude_on_every_leg_start() -> None:
    route = make_route((26.0, 44.0, 2000), (26.0, 44.5, None), (26.5, 44.5, 3000))

    with pytest.raises(ValueError, match="P1"):
        densify_route(route)


@pytest.mark.parametrize("step_nm", [0.0, -0.25, math.inf, math.nan])
def test_densify_rejects_invalid_step(step_nm: float) -> None:
    route = make_route((26.0, 44.0, 2000), (26.0, 44.5, 2000))

    with pytest.raises(ValueError, match="step"):
        densify_route(route, step_nm=step_nm)


@given(
    points=st.lists(
        st.tuples(
            st.floats(min_value=22.0, max_value=28.0),  # lon
            st.floats(min_value=43.5, max_value=48.0),  # lat
        ),
        min_size=2,
        max_size=4,
    )
)
def test_densified_route_property(points: list[tuple[float, float]]) -> None:
    route = make_route(*[(lon, lat, 3000.0) for lon, lat in points])
    legs_nm = sum(
        geodesic_distance_m(*p, *q) / M_PER_NM for p, q in zip(points, points[1:], strict=False)
    )
    assume(abs(legs_nm - MAX_ROUTE_NM) > 1e-6)  # avoid rounding right at the limit
    if legs_nm > MAX_ROUTE_NM:
        with pytest.raises(ValueError, match="NM long"):
            densify_route(route)
        return

    samples = densify_route(route)

    dists_nm = [s.dist_nm for s in samples]
    gaps_nm = [b - a for a, b in zip(dists_nm, dists_nm[1:], strict=False)]
    assert dists_nm[0] == 0.0
    assert all(0.0 < gap <= STEP_NM + 1e-9 for gap in gaps_nm)
    assert dists_nm[-1] == pytest.approx(legs_nm, abs=1e-6)


# --- route midpoint and local projection -------------------------------------------


def test_route_midpoint_is_halfway_along_a_single_leg() -> None:
    start, end = (26.0, 44.0), (26.0, 45.0)
    mid = route_midpoint(make_route((*start, 3000), (*end, 3000)))

    half_m = geodesic_distance_m(*start, *end) / 2
    assert geodesic_distance_m(*start, *mid) == pytest.approx(half_m, abs=TOLERANCE_M)
    assert geodesic_distance_m(*mid, *end) == pytest.approx(half_m, abs=TOLERANCE_M)


def test_route_midpoint_is_measured_along_the_route_not_between_the_ends() -> None:
    # A to B is 10 km, B to C is 30 km: halfway (20 km) is 10 km past B on leg B-C.
    a = (26.0, 44.5)
    b = offset(*a, 90, 10_000)
    c = offset(*b, 0, 30_000)
    mid = route_midpoint(make_route((*a, 3000), (*b, 3000), (*c, 3000)))

    assert geodesic_distance_m(*b, *mid) == pytest.approx(10_000, abs=TOLERANCE_M)
    assert geodesic_distance_m(*mid, *c) == pytest.approx(20_000, abs=TOLERANCE_M)


def test_route_midpoint_of_an_out_and_back_route_is_the_turning_point() -> None:
    a, b = (26.0, 44.5), (26.5, 44.7)
    mid = route_midpoint(make_route((*a, 3000), (*b, 3000), (*a, 3000)))

    assert geodesic_distance_m(*mid, *b) == pytest.approx(0.0, abs=TOLERANCE_M)


def test_projection_maps_its_centre_to_the_origin() -> None:
    projection = LocalProjection(center_lon=26.0, center_lat=44.5)

    assert projection.to_xy_m(26.0, 44.5) == pytest.approx((0.0, 0.0), abs=1e-6)


@pytest.mark.parametrize(("start", "end", "dist_m", "dist_nm"), REFERENCE_DISTANCES)
def test_projection_preserves_distance_from_its_centre(
    start: tuple[float, float], end: tuple[float, float], dist_m: float, dist_nm: float
) -> None:
    # Azimuthal equidistant: distances from the centre are true geodesic distances.
    projection = LocalProjection(center_lon=start[0], center_lat=start[1])

    x_m, y_m = projection.to_xy_m(*end)

    assert math.hypot(x_m, y_m) == pytest.approx(dist_m, abs=TOLERANCE_M)


def test_projection_has_east_as_x_and_north_as_y() -> None:
    projection = LocalProjection(center_lon=26.0, center_lat=44.5)

    east_x, east_y = projection.to_xy_m(*offset(26.0, 44.5, 90, 5_000))
    north_x, north_y = projection.to_xy_m(*offset(26.0, 44.5, 0, 5_000))

    assert (east_x, east_y) == pytest.approx((5_000, 0), abs=TOLERANCE_M)
    assert (north_x, north_y) == pytest.approx((0, 5_000), abs=TOLERANCE_M)


def test_projection_for_route_is_centred_halfway_along_the_legs() -> None:
    # L-shaped route: 10 km east, then 30 km north. Halfway (20 km) is 10 km north of B,
    # computed here independently of the production midpoint code.
    a = (26.0, 44.5)
    b = offset(*a, 90, 10_000)
    c = offset(*b, 0, 30_000)
    expected_centre = offset(*b, 0, 10_000)

    projection = LocalProjection.for_route(make_route((*a, 3000), (*b, 3000), (*c, 3000)))

    assert projection.to_xy_m(*expected_centre) == pytest.approx((0.0, 0.0), abs=TOLERANCE_M)


# --- lateral distance --------------------------------------------------------------

CENTER = (26.0, 44.5)
HALF_SIZE_M = 10_000  # 20 km x 20 km square, as in SPEC.md 7.4


@pytest.fixture
def projection() -> LocalProjection:
    return LocalProjection(center_lon=CENTER[0], center_lat=CENTER[1])


def test_lateral_distance_is_zero_inside_the_polygon(projection: LocalProjection) -> None:
    polygon = projection.polygon(square_ring(*CENTER, HALF_SIZE_M))

    for point in [CENTER, offset(*CENTER, 45, 9_000), offset(*CENTER, 200, 5_000)]:
        assert lateral_distance_nm(polygon, projection.point(*point)) == 0.0


def test_lateral_distance_is_zero_on_the_boundary(projection: LocalProjection) -> None:
    ring = square_ring(*CENTER, HALF_SIZE_M)
    polygon = projection.polygon(ring)

    assert lateral_distance_nm(polygon, projection.point(*ring[1])) == 0.0


@pytest.mark.parametrize("offset_nm", [0.1, 1.5, 2.0, 2.5, 10.0])
def test_lateral_distance_outside_an_edge(projection: LocalProjection, offset_nm: float) -> None:
    ring = square_ring(*CENTER, HALF_SIZE_M)
    polygon = projection.polygon(ring)
    east_edge_mid = offset(*CENTER, 90, HALF_SIZE_M)
    point = offset(*east_edge_mid, 90, offset_nm * M_PER_NM)

    assert lateral_distance_nm(polygon, projection.point(*point)) == pytest.approx(
        offset_nm, abs=TOLERANCE_NM
    )


def test_lateral_distance_outside_a_corner_is_the_distance_to_the_corner(
    projection: LocalProjection,
) -> None:
    ring = square_ring(*CENTER, HALF_SIZE_M)
    polygon = projection.polygon(ring)
    north_east_corner = ring[2]
    point = offset(*north_east_corner, 45, 2 * M_PER_NM)

    assert lateral_distance_nm(polygon, projection.point(*point)) == pytest.approx(
        2.0, abs=TOLERANCE_NM
    )


# --- review follow-ups: limits, independent oracles, harder geometry ---------------


@pytest.mark.parametrize(("start", "end", "dist_m", "dist_nm"), REFERENCE_DISTANCES)
def test_densified_route_length_matches_reference(
    start: tuple[float, float], end: tuple[float, float], dist_m: float, dist_nm: float
) -> None:
    samples = densify_route(make_route((*start, 3000), (*end, 3000)))

    assert samples[-1].dist_nm == pytest.approx(dist_nm, abs=TOLERANCE_NM)


def test_densify_rejects_a_route_longer_than_the_limit() -> None:
    # 40N to 59N along a meridian is about 1,140 NM.
    route = make_route((20.0, 40.0, 3000), (20.0, 59.0, 3000))

    with pytest.raises(ValueError, match=f"{MAX_ROUTE_NM:.0f} NM"):
        densify_route(route)


def test_densify_rejects_a_step_that_would_create_too_many_samples() -> None:
    # 60 NM at 0.0001 NM would be 600,000 samples.
    route = make_route((26.0, 44.0, 3000), (26.0, 45.0, 3000))

    with pytest.raises(ValueError, match="samples"):
        densify_route(route, step_nm=0.0001)


def test_sample_limit_allows_the_longest_route_at_the_default_step() -> None:
    assert MAX_ROUTE_NM / STEP_NM < MAX_ROUTE_SAMPLES


def local_point(x_m: float, y_m: float) -> tuple[float, float]:
    """WGS84 point at (x_m east, y_m north) in the AEQD plane centred on CENTER.

    AEQD keeps azimuth and distance from its centre exact, so this is exact, and polygons
    built from such points have known planar coordinates in that projection.
    """
    return offset(*CENTER, math.degrees(math.atan2(x_m, y_m)), math.hypot(x_m, y_m))


def test_lateral_distance_in_a_concave_notch_is_the_distance_to_the_boundary(
    projection: LocalProjection,
) -> None:
    # U-shaped airspace, 20 x 20 km, with an 8 km wide notch open to the north. A point in
    # the notch is outside, 4 km from both arms. A convex hull would wrongly give 0.
    ring_xy = [
        (-10_000, -10_000),
        (10_000, -10_000),
        (10_000, 10_000),
        (4_000, 10_000),
        (4_000, -5_000),
        (-4_000, -5_000),
        (-4_000, 10_000),
        (-10_000, 10_000),
        (-10_000, -10_000),
    ]
    polygon = projection.polygon([local_point(x, y) for x, y in ring_xy])

    distance_nm = lateral_distance_nm(polygon, projection.point(*local_point(0, 5_000)))

    assert distance_nm == pytest.approx(4_000 / M_PER_NM, abs=TOLERANCE_NM)


def test_lateral_distance_is_accurate_200_km_from_the_projection_centre(
    projection: LocalProjection,
) -> None:
    # Edge on the central meridian 200 km north of the centre; the point lies 1.5 NM east of
    # it, across the radial direction, where AEQD distortion is largest.
    edge_point = offset(*CENTER, 0, 200_000)
    _, lat_s = offset(*edge_point, 180, 5_000)
    _, lat_n = offset(*edge_point, 0, 5_000)
    lon = edge_point[0]
    ring = [(lon - 0.1, lat_s), (lon, lat_s), (lon, lat_n), (lon - 0.1, lat_n), (lon - 0.1, lat_s)]
    point = offset(*edge_point, 90, 1.5 * M_PER_NM)

    distance_nm = lateral_distance_nm(projection.polygon(ring), projection.point(*point))

    assert distance_nm == pytest.approx(1.5, abs=TOLERANCE_NM)
