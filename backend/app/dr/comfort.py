"""
Comfort Constraint Checker
===========================
Enforces ASHRAE 55 and 62.1 bounds.
The DR optimizer must call check() before issuing any setpoint change.
The BACnet device also enforces these independently (defense in depth).

Hard bounds (never violated, even during DR):
  Temperature : 18°C – 26°C  (ASHRAE 55 thermal comfort)
  CO₂         : ≤ 1000 ppm   (ASHRAE 62.1 ventilation)
  Humidity    : 30% – 65%    (ASHRAE 55)

Soft bounds (flagged as warnings, not blockers):
  Temperature : 20°C – 25°C
  CO₂         : ≤ 800 ppm
"""

from dataclasses import dataclass
from typing import Optional

# Hard bounds
TEMP_MIN  = 18.0
TEMP_MAX  = 26.0
CO2_MAX   = 1000.0
HUM_MIN   = 30.0
HUM_MAX   = 65.0

# Soft bounds
TEMP_SOFT_MIN = 20.0
TEMP_SOFT_MAX = 25.0
CO2_SOFT_MAX  = 800.0

# Maximum allowed setpoint raise per DR event per zone (°C)
MAX_SETPOINT_RAISE = 3.0


@dataclass
class ComfortStatus:
    zone_id:          str
    within_hard:      bool
    within_soft:      bool
    violations:       list
    warnings:         list
    temp_ok:          bool
    co2_ok:           bool
    humidity_ok:      bool
    current_temp:     float
    current_co2:      float
    current_humidity: float


def check(
    zone_id: str,
    current_temp: float,
    current_co2: float,
    current_humidity: float,
) -> ComfortStatus:
    violations = []
    warnings   = []

    temp_ok = True
    co2_ok  = True
    hum_ok  = True

    # Hard checks
    if not (TEMP_MIN <= current_temp <= TEMP_MAX):
        violations.append(f"Temperature {current_temp}°C outside hard bounds [{TEMP_MIN}–{TEMP_MAX}°C]")
        temp_ok = False
    if current_co2 > CO2_MAX:
        violations.append(f"CO₂ {current_co2:.0f} ppm exceeds ASHRAE 62.1 limit ({CO2_MAX} ppm)")
        co2_ok = False
    if not (HUM_MIN <= current_humidity <= HUM_MAX):
        violations.append(f"Humidity {current_humidity:.1f}% outside bounds [{HUM_MIN}–{HUM_MAX}%]")
        hum_ok = False

    # Soft checks (only if hard passed)
    if temp_ok and not (TEMP_SOFT_MIN <= current_temp <= TEMP_SOFT_MAX):
        warnings.append(f"Temperature {current_temp}°C outside optimal range [{TEMP_SOFT_MIN}–{TEMP_SOFT_MAX}°C]")
    if co2_ok and current_co2 > CO2_SOFT_MAX:
        warnings.append(f"CO₂ {current_co2:.0f} ppm above recommended limit ({CO2_SOFT_MAX} ppm)")

    return ComfortStatus(
        zone_id=zone_id,
        within_hard=(len(violations) == 0),
        within_soft=(len(warnings) == 0),
        violations=violations,
        warnings=warnings,
        temp_ok=temp_ok,
        co2_ok=co2_ok,
        humidity_ok=hum_ok,
        current_temp=current_temp,
        current_co2=current_co2,
        current_humidity=current_humidity,
    )


def can_raise_setpoint(
    current_temp: float,
    current_setpoint: float,
    proposed_raise: float,
    current_co2: float,
) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).
    Called before each BACnet WriteProperty during DR.
    """
    new_setpoint = current_setpoint + proposed_raise

    if new_setpoint > TEMP_MAX:
        return False, f"New setpoint {new_setpoint}°C would exceed hard ceiling {TEMP_MAX}°C"

    if proposed_raise > MAX_SETPOINT_RAISE:
        return False, f"Raise {proposed_raise}°C exceeds max allowed {MAX_SETPOINT_RAISE}°C per event"

    # If CO₂ already high, don't reduce ventilation further (heating setpoint raise)
    if current_co2 > CO2_SOFT_MAX:
        return False, f"CO₂ already {current_co2:.0f} ppm — setpoint raise deferred"

    # If temp already near ceiling, no headroom
    if current_temp > (TEMP_MAX - 1.0):
        return False, f"Temperature {current_temp}°C too close to ceiling — no headroom"

    return True, "ok"
