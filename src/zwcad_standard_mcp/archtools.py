"""Pure-Python mirrors of the LISP tool maths (dry-run previews and unit tests).

The drawing work is done in LISP (lisp/mo_tools.lsp); these functions repeat the same formulas so a dry run
can report what will be drawn without touching ZWCAD.
"""

from __future__ import annotations

import math

# --------------------------------------------------------------------------- sun ruler (LinijkaSlonca)

EQUINOX = {"W": {"tz": 1.0, "eot": -0.123, "label": "21 marca"}, "J": {"tz": 2.0, "eot": 0.126, "label": "23 wrzesnia"}}


def sun_position(hour: float, phi: float, lam: float, equinox: str) -> tuple[float, float]:
    """(azimuth from north clockwise, altitude) in degrees at local clock time ``hour`` on the equinox."""
    eq = EQUINOX[equinox]
    ast = hour + (lam - 15.0 * eq["tz"]) / 15.0 + eq["eot"]
    omega = math.radians((ast - 12.0) * 15.0)
    rphi = math.radians(phi)
    alt = math.asin(max(-1.0, min(1.0, math.cos(rphi) * math.cos(omega))))
    cos_az = max(-1.0, min(1.0, -math.tan(alt) * math.tan(rphi)))
    az = math.degrees(math.acos(cos_az))
    if omega > 0:
        az = 360.0 - az
    return az, math.degrees(alt)


def shadow(hour: float, phi: float, lam: float, equinox: str, height_m: float) -> tuple[float, float]:
    """(CAD angle in degrees, length in metres) of the shadow of a ``height_m`` obstruction."""
    az, alt = sun_position(hour, phi, lam, equinox)
    length = height_m / math.tan(math.radians(alt)) if alt > 0 else 0.0
    return 90.0 - az, length


def time_for_direction(cad_angle_deg: float, phi: float, lam: float, equinox: str) -> float:
    """Local clock time at which the shadow points along ``cad_angle_deg`` (LinijkaSlonca get_shadow_data)."""
    eq = EQUINOX[equinox]
    az = (90.0 - cad_angle_deg) % 360.0
    rphi = math.radians(phi)
    alt = math.atan(-math.cos(math.radians(az)) / math.tan(rphi))
    cos_omega = max(-1.0, min(1.0, math.sin(alt) / math.cos(rphi)))
    omega = math.degrees(math.acos(cos_omega))
    ast = 12.0 - omega / 15.0 if az < 180.0 else 12.0 + omega / 15.0
    return ast - ((lam - 15.0 * eq["tz"]) / 15.0 + eq["eot"])


def fmt_time(hours: float) -> str:
    minutes = int(hours * 60.0 + 0.5)
    return f"{minutes // 60}:{minutes % 60:02d}"


def exposure_window(
    base: tuple[float, float],
    phi: float,
    lam: float,
    equinox: str,
    start_dir: tuple[float, float] | None = None,
    end_dir: tuple[float, float] | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict:
    def pick(direction: tuple[float, float] | None, given: float | None, default: float) -> float:
        if direction is not None and math.dist(base, direction) > 0:
            angle = math.degrees(math.atan2(direction[1] - base[1], direction[0] - base[0]))
            value = time_for_direction(angle, phi, lam, equinox)
        else:
            value = default if given is None else given
        return min(17.0, max(7.0, value))

    t1 = pick(start_dir, start_time, 7.0)
    t2 = pick(end_dir, end_time, 17.0)
    minutes = int(abs(t1 - t2) * 60.0 + 0.5)
    return {
        "start": fmt_time(min(t1, t2)),
        "end": fmt_time(max(t1, t2)),
        "duration_min": minutes,
        "duration": f"{minutes // 60}:{minutes % 60:02d}",
    }


# --------------------------------------------------------------------------- swept path (CAYENNE)

CAYENNE_E3 = {
    "NAME": "Porsche Cayenne E3 (bez skretnej tylnej osi)",
    "L": 2895.0, "FO": 990.0, "RO": 1045.0, "W": 1983.0, "WM": 2194.0, "MX": 1100.0, "MD": 230.0,
    "TRF": 1676.0, "TRR": 1676.0, "TW": 255.0, "TD": 763.0, "TC": 12100.0,
}


def vehicle(overrides: dict[str, float] | None = None) -> dict:
    data = dict(CAYENNE_E3)
    for key, value in (overrides or {}).items():
        if key not in data:
            raise ValueError(f"Unknown vehicle parameter: {key} (known: {', '.join(k for k in data if k != 'NAME')})")
        data[key] = value
    if data["TC"] * 0.5 <= data["L"]:
        raise ValueError("Turning circle TC must be larger than 2 x wheelbase L.")
    return data


def min_path_radius_mm(veh: dict, reverse: bool) -> float:
    """Minimum radius of the guided path: rear axle centre when reversing, front axle centre forward."""
    rr = math.sqrt(0.25 * veh["TC"] ** 2 - veh["L"] ** 2) - 0.5 * veh["TRF"]
    return rr if reverse else math.sqrt(rr * rr + veh["L"] ** 2)


def max_steer_deg(veh: dict) -> float:
    rr = math.sqrt(0.25 * veh["TC"] ** 2 - veh["L"] ** 2) - 0.5 * veh["TRF"]
    return math.degrees(math.atan2(veh["L"], rr))


# --------------------------------------------------------------------------- area field

UNIT_AREA_FACTOR = {4: 1e-6, 5: 1e-4, 6: 1.0}  # INSUNITS -> m² per drawing unit²


def area_field_format(insunits: int, decimals: int = 1, decimal_separator: str | None = None,
                      factor: float | None = None) -> str:
    """Field format like WklejPole: ``%lu2%pr1%ct8[.0001]`` (decimal, 1 decimal place, cm² → m²)."""
    if factor is None:
        factor = UNIT_AREA_FACTOR.get(insunits, 1.0)
    text = f"{factor:.10f}".rstrip("0").rstrip(".")
    if text.startswith("0."):
        text = text[1:]
    sep = "" if decimal_separator in (None, ".") else f"%ds{ord(decimal_separator)}"
    return f"%lu2%pr{int(decimals)}{sep}%ct8[{text}]"
