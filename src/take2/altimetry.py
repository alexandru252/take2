"""Altimetry conversions (SPEC.md section 7.1).

Uses the ISA troposphere model with no temperature correction (out of scope for v0.1).
All altitudes are in feet; "altitude on QNH" means what an altimeter set to QNH indicates.
"""

import math
from typing import assert_never

from take2.models import LimitReference, VerticalLimit

STANDARD_PRESSURE_HPA = 1013.25
# Sea-level pressures ever recorded lie between about 870 and 1084 hPa. Outside this range the
# value was almost certainly typed in another unit (29.92 inHg, 101325 Pa).
MIN_PLAUSIBLE_QNH_HPA = 850.0
MAX_PLAUSIBLE_QNH_HPA = 1100.0
FT_PER_M = 3.28084
FT_PER_FL = 100.0

_ISA_PRESSURE_ALTITUDE_SCALE_FT = 145366.45
_ISA_PRESSURE_ALTITUDE_EXPONENT = 0.190284

_FT_PER_UNIT = {"FT": 1.0, "M": FT_PER_M, "FL": FT_PER_FL}


def pressure_altitude_of_level_ft(p_hpa: float) -> float:
    """Return the ISA pressure altitude (ft) of the pressure level ``p_hpa``.

    Zero at 1013.25 hPa, negative for higher pressures.
    """
    if not (math.isfinite(p_hpa) and p_hpa > 0):
        raise ValueError(f"pressure must be a positive finite number, got {p_hpa} hPa")
    ratio = p_hpa / STANDARD_PRESSURE_HPA
    return _ISA_PRESSURE_ALTITUDE_SCALE_FT * (1 - ratio**_ISA_PRESSURE_ALTITUDE_EXPONENT)


def check_qnh_hpa(qnh_hpa: float) -> float:
    """Return ``qnh_hpa`` if it is a plausible QNH, else raise ``ValueError``."""
    if not (math.isfinite(qnh_hpa) and MIN_PLAUSIBLE_QNH_HPA <= qnh_hpa <= MAX_PLAUSIBLE_QNH_HPA):
        raise ValueError(
            f"QNH {qnh_hpa} hPa is not plausible: expected {MIN_PLAUSIBLE_QNH_HPA:.0f} to "
            f"{MAX_PLAUSIBLE_QNH_HPA:.0f} hPa (29.92 inHg is 1013 hPa)"
        )
    return qnh_hpa


def fl_to_altitude_on_qnh_ft(fl: float, qnh_hpa: float) -> float:
    """Return the altitude (ft) on QNH of flight level ``fl``.

    An altimeter set to QNH reads ``H(p) - H(QNH)``, so a flight level sits lower than its
    nominal value when QNH is below 1013.25 hPa and higher when QNH is above it.
    """
    return fl * FT_PER_FL - pressure_altitude_of_level_ft(qnh_hpa)


def limit_to_altitude_ft(
    limit: VerticalLimit, qnh_hpa: float, ground_elev_ft: float = 0.0
) -> tuple[float, bool]:
    """Convert a published vertical limit to an altitude (ft) on the day's QNH.

    Returns ``(alt_ft, approximate)``:

    - ``STD`` limits are pressure altitudes, converted with ``qnh_hpa``.
    - ``MSL`` limits are already altitudes on QNH.
    - ``GND`` limits are added to ``ground_elev_ft`` (0 ft when no terrain data) and are
      marked approximate. A ``GND`` limit of 0 (surface) is exact: the airspace reaches
      the ground wherever the ground is.

    Raises ``ValueError`` for an ``FL`` unit with a reference other than ``STD``.
    """
    if limit.unit == "FL" and limit.reference is not LimitReference.STD:
        raise ValueError(
            f"a flight level limit must use the STD reference, got {limit.reference.value}"
        )
    value_ft = limit.value * _FT_PER_UNIT[limit.unit]

    match limit.reference:
        case LimitReference.STD:
            return value_ft - pressure_altitude_of_level_ft(qnh_hpa), False
        case LimitReference.MSL:
            return value_ft, False
        case LimitReference.GND:
            return ground_elev_ft + value_ft, value_ft != 0
        case _:
            assert_never(limit.reference)
