"""Tests for take2.altimetry, using the reference values from SPEC.md section 7.1."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from take2.altimetry import (
    fl_to_altitude_on_qnh_ft,
    limit_to_altitude_ft,
    pressure_altitude_of_level_ft,
)
from take2.models import LimitReference, VerticalLimit

TOLERANCE_FT = 0.5  # SPEC.md 7.1
FT_PER_M = 3.28084

# SPEC.md 7.1 reference table: QNH (hPa), H(QNH) ft, FL65 on QNH ft, FL95 on QNH ft.
# Never edit these values to make a test pass; report the discrepancy instead.
REFERENCE_TABLE = [
    pytest.param(1033.25, -541.7, 7041.7, 10041.7, id="QNH1033.25"),
    pytest.param(1023.00, -265.1, 6765.1, 9765.1, id="QNH1023"),
    pytest.param(1013.25, 0.0, 6500.0, 9500.0, id="QNH1013.25"),
    pytest.param(1003.00, 281.0, 6219.0, 9219.0, id="QNH1003"),
    pytest.param(1000.00, 363.6, 6136.4, 9136.4, id="QNH1000"),
    pytest.param(993.00, 557.3, 5942.7, 8942.7, id="QNH993"),
    pytest.param(983.00, 836.0, 5664.0, 8664.0, id="QNH983"),
]

flight_levels = st.floats(min_value=0.0, max_value=660.0, allow_nan=False)
qnh_values_hpa = st.floats(min_value=900.0, max_value=1100.0, allow_nan=False)


# --- pressure_altitude_of_level_ft -------------------------------------------------


@pytest.mark.parametrize(("qnh_hpa", "h_qnh_ft", "fl65_ft", "fl95_ft"), REFERENCE_TABLE)
def test_pressure_altitude_of_qnh_matches_reference(
    qnh_hpa: float, h_qnh_ft: float, fl65_ft: float, fl95_ft: float
) -> None:
    assert pressure_altitude_of_level_ft(qnh_hpa) == pytest.approx(h_qnh_ft, abs=TOLERANCE_FT)


def test_pressure_altitude_of_standard_pressure_is_exactly_zero() -> None:
    assert pressure_altitude_of_level_ft(1013.25) == 0.0


@pytest.mark.parametrize("p_hpa", [0.0, -1.0, -1013.25])
def test_pressure_altitude_rejects_non_positive_pressure(p_hpa: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        pressure_altitude_of_level_ft(p_hpa)


# --- fl_to_altitude_on_qnh_ft ------------------------------------------------------


@pytest.mark.parametrize(("qnh_hpa", "h_qnh_ft", "fl65_ft", "fl95_ft"), REFERENCE_TABLE)
def test_fl65_on_qnh_matches_reference(
    qnh_hpa: float, h_qnh_ft: float, fl65_ft: float, fl95_ft: float
) -> None:
    assert fl_to_altitude_on_qnh_ft(65, qnh_hpa) == pytest.approx(fl65_ft, abs=TOLERANCE_FT)


@pytest.mark.parametrize(("qnh_hpa", "h_qnh_ft", "fl65_ft", "fl95_ft"), REFERENCE_TABLE)
def test_fl95_on_qnh_matches_reference(
    qnh_hpa: float, h_qnh_ft: float, fl65_ft: float, fl95_ft: float
) -> None:
    assert fl_to_altitude_on_qnh_ft(95, qnh_hpa) == pytest.approx(fl95_ft, abs=TOLERANCE_FT)


@given(fl=flight_levels)
def test_fl_on_standard_qnh_equals_fl_times_100_exactly(fl: float) -> None:
    assert fl_to_altitude_on_qnh_ft(fl, 1013.25) == fl * 100


@given(
    fl=flight_levels,
    qnh_hpa=qnh_values_hpa,
    drop_hpa=st.floats(min_value=0.01, max_value=100.0, allow_nan=False),
)
def test_fl_on_qnh_decreases_when_qnh_decreases(fl: float, qnh_hpa: float, drop_hpa: float) -> None:
    lower_qnh_hpa = qnh_hpa - drop_hpa
    assert fl_to_altitude_on_qnh_ft(fl, lower_qnh_hpa) < fl_to_altitude_on_qnh_ft(fl, qnh_hpa)


# --- limit_to_altitude_ft ----------------------------------------------------------


def test_flight_level_limit_is_converted_with_qnh() -> None:
    limit = VerticalLimit(value=65, unit="FL", reference=LimitReference.STD)

    alt_ft, approximate = limit_to_altitude_ft(limit, qnh_hpa=993.0)

    assert alt_ft == pytest.approx(5942.7, abs=TOLERANCE_FT)
    assert approximate is False


def test_std_limit_in_feet_is_a_pressure_altitude() -> None:
    limit = VerticalLimit(value=6500, unit="FT", reference=LimitReference.STD)

    alt_ft, approximate = limit_to_altitude_ft(limit, qnh_hpa=993.0)

    assert alt_ft == pytest.approx(fl_to_altitude_on_qnh_ft(65, 993.0))
    assert approximate is False


@pytest.mark.parametrize("qnh_hpa", [983.0, 1013.25, 1033.25])
def test_msl_limit_in_feet_does_not_depend_on_qnh(qnh_hpa: float) -> None:
    limit = VerticalLimit(value=3500, unit="FT", reference=LimitReference.MSL)

    assert limit_to_altitude_ft(limit, qnh_hpa) == (3500.0, False)


def test_msl_limit_in_metres_is_converted_to_feet() -> None:
    limit = VerticalLimit(value=1000, unit="M", reference=LimitReference.MSL)

    alt_ft, approximate = limit_to_altitude_ft(limit, qnh_hpa=1013.25)

    assert alt_ft == pytest.approx(1000 * FT_PER_M)
    assert approximate is False


def test_gnd_limit_assumes_ground_at_zero_and_is_approximate() -> None:
    limit = VerticalLimit(value=1000, unit="FT", reference=LimitReference.GND)

    assert limit_to_altitude_ft(limit, qnh_hpa=1013.25) == (1000.0, True)


def test_gnd_limit_adds_given_ground_elevation() -> None:
    limit = VerticalLimit(value=1000, unit="FT", reference=LimitReference.GND)

    assert limit_to_altitude_ft(limit, qnh_hpa=1013.25, ground_elev_ft=450.0) == (1450.0, True)


def test_gnd_limit_in_metres_is_converted_to_feet() -> None:
    limit = VerticalLimit(value=300, unit="M", reference=LimitReference.GND)

    alt_ft, approximate = limit_to_altitude_ft(limit, qnh_hpa=1013.25)

    assert alt_ft == pytest.approx(300 * FT_PER_M)
    assert approximate is True


@pytest.mark.parametrize("ground_elev_ft", [0.0, 450.0])
def test_surface_limit_reaches_the_ground_exactly(ground_elev_ft: float) -> None:
    # GND/SFC with value 0 means the airspace reaches the ground, wherever the ground is,
    # so the result is the ground elevation and is not an approximation.
    limit = VerticalLimit(value=0, unit="FT", reference=LimitReference.GND)

    assert limit_to_altitude_ft(limit, 1013.25, ground_elev_ft=ground_elev_ft) == (
        ground_elev_ft,
        False,
    )


@pytest.mark.parametrize("reference", [LimitReference.MSL, LimitReference.GND])
def test_flight_level_unit_requires_std_reference(reference: LimitReference) -> None:
    limit = VerticalLimit(value=65, unit="FL", reference=reference)

    with pytest.raises(ValueError, match="STD"):
        limit_to_altitude_ft(limit, qnh_hpa=1013.25)
