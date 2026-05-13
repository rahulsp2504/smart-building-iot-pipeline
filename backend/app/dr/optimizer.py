"""
DR Load Shedding Optimizer
===========================
Computes a zone-level setpoint plan that meets a kW reduction target
while respecting comfort constraints.

Algorithm: comfort-constrained greedy
  1. Rank zones by occupancy_ratio ascending (shed from emptiest first).
  2. For each zone, compute max_sheddable_kW within comfort bounds.
  3. Allocate shed target greedily until met or all zones exhausted.
  4. Output: list of ZoneAction with setpoint_delta_c per zone.

Energy model (inverse of simulator):
  Raising cooling setpoint by ΔT reduces HVAC energy by:
    ΔkW ≈ ΔT × hvac_sensitivity_factor
  Calibrated per zone based on area and thermal mass.
"""

from dataclasses import dataclass
from typing import List, Optional

from . import comfort as comfort_checker

# kW saved per °C of setpoint raise, per zone (calibrated to simulator model)
HVAC_SENSITIVITY = {
    "zone_1": 0.9,   # Conference Room A — small, responsive
    "zone_2": 2.2,   # Open Office B     — large, most DR potential
    "zone_3": 0.7,   # Lab C             — small, sensitive equipment
    "zone_4": 1.1,   # Lobby             — medium
}

MAX_SETPOINT_RAISE_C = 3.0   # absolute max per zone per event


@dataclass
class ZoneAction:
    zone_id:            str
    zone_name:          str
    predicted_occupancy: int
    occupancy_ratio:    float
    kw_before:          float
    setpoint_delta_c:   float        # °C to raise cooling setpoint
    kw_projected:       float        # estimated kW after action
    kw_shed:            float        # projected reduction
    comfort_bound_hit:  bool
    skip_reason:        Optional[str]


@dataclass
class SheddingPlan:
    target_kw:          float
    projected_kw_shed:  float
    target_met:         bool
    zones:              List[ZoneAction]
    setpoint_writes:    List[dict]   # ready for BACnet client


def compute(
    target_kw: float,
    zone_states: dict,    # {zone_id: {temperature, co2, humidity, energy_kw, cooling_setpoint}}
    predictions: dict,    # {zone_id: {predicted_occupancy, occupancy_ratio, ...}}
    zone_meta: dict,      # {zone_id: {zone_name, capacity}}
) -> SheddingPlan:
    """
    zone_states  — latest sensor snapshot per zone
    predictions  — ML output from predictor.predict_all()
    zone_meta    — static zone info
    """

    # --- Step 1: rank zones by occupancy_ratio ascending ---
    ranked = sorted(
        predictions.keys(),
        key=lambda zid: predictions[zid].get("occupancy_ratio", 1.0)
    )

    actions: List[ZoneAction] = []
    writes:  List[dict]        = []
    remaining_kw = target_kw

    for zid in ranked:
        pred   = predictions[zid]
        state  = zone_states.get(zid, {})
        meta   = zone_meta.get(zid, {})
        sens   = HVAC_SENSITIVITY.get(zid, 1.0)

        occ_ratio    = pred.get("occupancy_ratio", 0.5)
        current_temp = state.get("temperature", 22.0)
        current_co2  = state.get("co2", 500.0)
        current_hum  = state.get("humidity", 45.0)
        current_sp   = state.get("cooling_setpoint", 24.0)
        current_kw   = state.get("energy_kw", 3.0)

        # --- Step 2: compute allowable setpoint raise ---
        allowed, reason = comfort_checker.can_raise_setpoint(
            current_temp, current_sp, MAX_SETPOINT_RAISE_C, current_co2
        )

        if not allowed:
            actions.append(ZoneAction(
                zone_id=zid,
                zone_name=meta.get("zone_name", zid),
                predicted_occupancy=int(pred.get("predicted_occupancy", 0)),
                occupancy_ratio=occ_ratio,
                kw_before=current_kw,
                setpoint_delta_c=0.0,
                kw_projected=current_kw,
                kw_shed=0.0,
                comfort_bound_hit=True,
                skip_reason=reason,
            ))
            continue

        if remaining_kw <= 0:
            actions.append(ZoneAction(
                zone_id=zid, zone_name=meta.get("zone_name", zid),
                predicted_occupancy=int(pred.get("predicted_occupancy", 0)),
                occupancy_ratio=occ_ratio,
                kw_before=current_kw,
                setpoint_delta_c=0.0,
                kw_projected=current_kw,
                kw_shed=0.0,
                comfort_bound_hit=False,
                skip_reason="target already met",
            ))
            continue

        # How much can we shed from this zone?
        max_raise     = min(MAX_SETPOINT_RAISE_C, (comfort_checker.TEMP_MAX - 0.5) - current_temp)
        max_raise     = max(0.0, max_raise)
        max_shed      = max_raise * sens

        # How much do we need?
        needed_raise  = remaining_kw / sens
        actual_raise  = min(needed_raise, max_raise)
        actual_raise  = round(actual_raise, 1)

        if actual_raise < 0.1:
            actions.append(ZoneAction(
                zone_id=zid, zone_name=meta.get("zone_name", zid),
                predicted_occupancy=int(pred.get("predicted_occupancy", 0)),
                occupancy_ratio=occ_ratio,
                kw_before=current_kw,
                setpoint_delta_c=0.0,
                kw_projected=current_kw,
                kw_shed=0.0,
                comfort_bound_hit=False,
                skip_reason="insufficient headroom for meaningful shed",
            ))
            continue

        kw_shed        = actual_raise * sens
        remaining_kw  -= kw_shed
        new_sp         = round(current_sp + actual_raise, 1)

        actions.append(ZoneAction(
            zone_id=zid,
            zone_name=meta.get("zone_name", zid),
            predicted_occupancy=int(pred.get("predicted_occupancy", 0)),
            occupancy_ratio=occ_ratio,
            kw_before=current_kw,
            setpoint_delta_c=actual_raise,
            kw_projected=round(current_kw - kw_shed, 3),
            kw_shed=round(kw_shed, 3),
            comfort_bound_hit=False,
            skip_reason=None,
        ))

        # BACnet write spec (resolved to object instance in BACnet client)
        writes.append({
            "zone_id":         zid,
            "property_name":   "cooling_setpoint",
            "new_setpoint":    new_sp,
            "setpoint_delta":  actual_raise,
        })

    projected = sum(a.kw_shed for a in actions)
    return SheddingPlan(
        target_kw=target_kw,
        projected_kw_shed=round(projected, 3),
        target_met=(projected >= target_kw * 0.85),   # 85% tolerance
        zones=actions,
        setpoint_writes=writes,
    )
