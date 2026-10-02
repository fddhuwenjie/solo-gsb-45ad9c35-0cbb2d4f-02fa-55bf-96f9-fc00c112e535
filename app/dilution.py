"""Deterministic dilution-gradient planning for an existing arrangement.

Given the target layout (sample → well, as produced by the arrangement
endpoint), the stocks of every source sample (concentration and the volume
available for sharing across targets) and a list of target wells (each with
its requested final concentration and final volume), this module emits a
deterministic, step-by-step single-stage pipetting plan:

* every target well receives one source-stock transfer (possibly zero, when
  the requested concentration is 0) and one diluent transfer;
* every non-zero transfer is an integer multiple of the pipette resolution
  and at least the minimum pipette volume; the well final volume must itself
  be a resolution multiple and not exceed the well capacity;
* the realized concentration ``c0 * v_stock / V`` must lie within the
  requested relative tolerance (or the absolute tolerance, whichever bound is
  looser for that well) of the target concentration.

Rounding model
--------------
The final volume ``V`` must already be a resolution multiple; it is never
rounded. The stock volume is the integer multiple ``n * resolution`` closest
to the ideal ``V * c_target / c0`` among the multiples that satisfy the
capacity and minimum-volume rules, choosing the one with the smallest
realized concentration error (ties resolved toward the smaller stock volume
for determinism). The diluent volume is ``V - v_stock`` and is therefore
automatically on the resolution grid.

Stock inventories are shared: the stock volumes of every target fed by one
source are summed and checked against that source's available volume. If
*any* well cannot meet its tolerance/capacity/minimum-volume constraints, or
*any* source is short, no plan is produced at all — a single 422 lists every
reason found. The computation contains no randomness or iteration over
environment state, so identical inputs always yield an identical plan.
"""
from __future__ import annotations

import math

from .planner import InfeasibleArrangement, _resolve_dimensions
from .plates import parse_well, well_name
from .schemas import (
    DilutionPlanRequest,
    VIOLATION_DILUTION_MIN_VOLUME_VIOLATION,
    VIOLATION_DILUTION_SOURCE_DUPLICATED,
    VIOLATION_DILUTION_SOURCE_INSUFFICIENT,
    VIOLATION_DILUTION_SOURCE_UNKNOWN,
    VIOLATION_DILUTION_TARGET_CONCENTRATION_INVALID,
    VIOLATION_DILUTION_TARGET_WELL_DUPLICATED,
    VIOLATION_DILUTION_TARGET_WELL_NOT_IN_LAYOUT,
    VIOLATION_DILUTION_TOLERANCE_UNREACHABLE,
    VIOLATION_DILUTION_VOLUME_INVALID,
    VIOLATION_DILUTION_WELL_CAPACITY_EXCEEDED,
    VIOLATION_WELL_REFERENCE_INVALID,
)

# Floating-point slack used for grid-alignment and bound checks.
_EPS = 1e-9

# Output volume decimals: enough to represent realistic resolution grids
# (0.001 uL) while hiding binary float noise (e.g. 50.00000000000001).
_VOL_DECIMALS = 6


class DilutionPlanInvalid(Exception):
    """The dilution request cannot be fulfilled as stated (HTTP 422).

    Every structural or physical problem found is listed; no partial plan is
    ever generated.
    """

    def __init__(self, categories: list[str], messages: list[str]):
        self.categories = categories
        self.messages = messages
        super().__init__("; ".join(messages) if messages else "invalid dilution plan request")


def _round_vol(v: float) -> float:
    return round(v, _VOL_DECIMALS)


def _nearest_grid_integer(value: float, resolution: float) -> tuple[int, int]:
    """Return (floor, ceil) tick indices around ``value / resolution``."""
    q = value / resolution
    lo = math.floor(q + _EPS)
    hi = math.ceil(q - _EPS)
    return lo, hi


def _plan_well(target, source, req) -> dict:
    """Compute the chosen stock/diluent volumes for one target well.

    Raises ``DilutionPlanInvalid`` (single message) when the well cannot be
    filled within the well's own constraints (capacity, minimum volume,
    rounding tolerance). Shared-stock shortages are handled by the caller.
    """
    V = target.final_volume
    res = req.pipette_resolution
    vmin = req.min_pipette_volume
    c0 = source["concentration"]
    ct = target.target_concentration

    # Final volume must lie on the resolution grid and within the well.
    if abs(V / res - round(V / res)) > 1e-6:
        raise DilutionPlanInvalid(
            [VIOLATION_DILUTION_VOLUME_INVALID],
            [
                f"well {target.well}: final volume {_round_vol(V)} is not an integer "
                f"multiple of the pipette resolution {_round_vol(res)}"
            ],
        )
    K = int(round(V / res))
    if V > req.well_capacity + _EPS:
        raise DilutionPlanInvalid(
            [VIOLATION_DILUTION_WELL_CAPACITY_EXCEEDED],
            [
                f"well {target.well}: final volume {_round_vol(V)} exceeds the well "
                f"capacity {_round_vol(req.well_capacity)}"
            ],
        )

    # Pure diluent wells (target concentration 0): the only transfer is the
    # diluent, which must itself be pipettable.
    if ct == 0.0:
        if V < vmin - _EPS:
            raise DilutionPlanInvalid(
                [VIOLATION_DILUTION_MIN_VOLUME_VIOLATION],
                [
                    f"well {target.well}: diluent volume {_round_vol(V)} is below the "
                    f"minimum pipette volume {_round_vol(vmin)}"
                ],
            )
        return {
            "n_stock": 0,
            "stock_volume": 0.0,
            "diluent_volume": V,
            "actual_concentration": 0.0,
            "abs_error": 0.0,
            "rel_error": 0.0,
        }

    if ct > c0 * (1.0 + 1e-9):
        raise DilutionPlanInvalid(
            [VIOLATION_DILUTION_TARGET_CONCENTRATION_INVALID],
            [
                f"well {target.well}: target concentration {ct} exceeds the source "
                f"'{source['id']}' concentration {c0} (single-stage dilution cannot "
                f"concentrate the stock)"
            ],
        )

    ideal = V * ct / c0
    flo, cei = _nearest_grid_integer(ideal, res)

    # Feasible tick range:
    #   n = 0        only allowed when the diluent transfer V alone is pipettable;
    #   n >= n_lo    every non-zero stock draw must reach the minimum volume;
    #   n <= K       stock never exceeds the final volume (non-negative diluent);
    # and the diluent transfer, when non-zero, must also reach the minimum.
    n_lo = math.ceil(vmin / res - _EPS)
    candidates: set[int] = set()
    if V >= vmin - _EPS:
        candidates.add(0)
    # Boundary indices plus the grid points bracketing the ideal volume cover
    # every nearest feasible point for the monotone concentration error.
    boundary = {n_lo, K, max(n_lo, flo), min(K, cei), flo, cei}
    for n in boundary:
        if n_lo <= n <= K:
            d = V - n * res
            # d ~ 0 covers the pure-stock corner where grid rounding leaves a
            # few ulps of binary-float noise (e.g. V=0.3, resolution=0.1).
            if n == 0 or abs(d) <= _EPS or d >= vmin - _EPS:
                candidates.add(n)

    best = None
    for n in candidates:
        vs = n * res
        actual = c0 * vs / V
        abs_err = abs(actual - ct)
        rel_err = abs_err / ct
        key = (abs_err, n)  # smallest error; deterministic tie -> less stock
        if best is None or key < best[0]:
            best = (key, n, vs, actual, abs_err, rel_err)

    if best is None:
        # No pipettable split exists: even the full volume is below the
        # minimum draw (covers both the pure-stock and pure-diluent corners).
        raise DilutionPlanInvalid(
            [VIOLATION_DILUTION_MIN_VOLUME_VIOLATION],
            [
                f"well {target.well}: final volume {_round_vol(V)} is below the minimum "
                f"pipette volume {_round_vol(vmin)}, so neither the stock nor the "
                f"diluent transfer can be pipetted"
            ],
        )

    _, n, vs, actual, abs_err, rel_err = best
    diluent = V - vs
    if abs(diluent) <= _EPS:  # normalize binary-float noise at the pure-stock corner
        diluent = 0.0
    bound = max(req.relative_tolerance, req.absolute_tolerance / ct)
    if rel_err > bound + _EPS:
        raise DilutionPlanInvalid(
            [VIOLATION_DILUTION_TOLERANCE_UNREACHABLE],
            [
                f"well {target.well}: no resolution-grid pipetting reaches the target "
                f"concentration within tolerance; best achievable relative error "
                f"{rel_err:.6g} > {req.relative_tolerance:.6g} (absolute tolerance "
                f"{_round_vol(req.absolute_tolerance)}, requested {ct}, best {actual:.6g})"
            ],
        )

    return {
        "n_stock": n,
        "stock_volume": vs,
        "diluent_volume": diluent,
        "actual_concentration": actual,
        "abs_error": abs_err,
        "rel_error": rel_err,
    }


def plan_dilution(req: DilutionPlanRequest) -> dict:
    """Validate the request and produce the deterministic dilution plan."""
    try:
        rows, cols = _resolve_dimensions(req.plate)
    except InfeasibleArrangement as exc:
        raise DilutionPlanInvalid(exc.categories, exc.messages) from exc

    categories: list[str] = []
    messages: list[str] = []

    def add(category: str, msg: str) -> None:
        if category not in categories:
            categories.append(category)
        messages.append(msg)

    # --- layout (sample -> well) -------------------------------------------
    well_owner: dict[tuple[int, int], str] = {}
    sample_well: dict[str, tuple[int, int]] = {}
    for sid, raw in req.layout.items():
        try:
            pos = parse_well(raw, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in well_owner:
            add(
                VIOLATION_DILUTION_TARGET_WELL_DUPLICATED,
                f"layout well {well_name(*pos)} assigned to both "
                f"'{well_owner[pos]}' and '{sid}'",
            )
        else:
            well_owner[pos] = sid
        sample_well[sid] = pos

    # --- sources ------------------------------------------------------------
    sources: dict[str, dict] = {}
    for s in req.sources:
        if s.id in sources:
            add(
                VIOLATION_DILUTION_SOURCE_DUPLICATED,
                f"source '{s.id}' is listed more than once",
            )
            continue
        if s.id not in sample_well:
            add(
                VIOLATION_DILUTION_SOURCE_UNKNOWN,
                f"source '{s.id}' is not present in the layout",
            )
        sources[s.id] = {
            "id": s.id,
            "pos": sample_well.get(s.id),
            "concentration": s.concentration,
            "available_volume": s.available_volume,
        }

    # min_pipette_volume must not exceed the resolution grid's smallest
    # positive draw that any feasible well could use.
    if req.min_pipette_volume > req.well_capacity + _EPS:
        add(
            VIOLATION_DILUTION_MIN_VOLUME_VIOLATION,
            f"minimum pipette volume {_round_vol(req.min_pipette_volume)} exceeds the "
            f"well capacity {_round_vol(req.well_capacity)}",
        )

    # --- targets ------------------------------------------------------------
    well_plans: list[dict] = []  # sorted to row-major order before output

    seen_wells: set[tuple[int, int]] = set()
    for t in req.targets:
        try:
            pos = parse_well(t.well, rows, cols)
        except ValueError as exc:
            add(VIOLATION_WELL_REFERENCE_INVALID, str(exc))
            continue
        if pos in seen_wells:
            add(
                VIOLATION_DILUTION_TARGET_WELL_DUPLICATED,
                f"target well {well_name(*pos)} appears more than once",
            )
            continue
        seen_wells.add(pos)
        owner = well_owner.get(pos)
        if owner is None:
            add(
                VIOLATION_DILUTION_TARGET_WELL_NOT_IN_LAYOUT,
                f"target well {well_name(*pos)} is not occupied in the layout",
            )
            continue
        if owner not in sources:
            add(
                VIOLATION_DILUTION_SOURCE_UNKNOWN,
                f"well {well_name(*pos)} is occupied by '{owner}', whose source "
                f"concentration/available volume were not supplied",
            )
            continue
        well_plans.append({"pos": pos, "target": t, "source": sources[owner], "owner": owner})

    # Per-well physical/rounding feasibility; every failure is collected.
    for item in well_plans:
        try:
            item["plan"] = _plan_well(item["target"], item["source"], req)
        except DilutionPlanInvalid as exc:
            for c in exc.categories:
                if c not in categories:
                    categories.append(c)
            messages.extend(exc.messages)
            item["plan"] = None

    # --- shared inventory ---------------------------------------------------
    # Only meaningful when every well has a plan; otherwise the request is
    # already rejected and stock arithmetic would be misleading.
    stock_use: dict[str, float] = {}
    if all(item.get("plan") is not None for item in well_plans):
        for item in well_plans:
            p = item["plan"]
            if p["stock_volume"] > 0:
                stock_use[item["owner"]] = stock_use.get(item["owner"], 0.0) + p["stock_volume"]
        for sid in sorted(stock_use):
            used = stock_use[sid]
            avail = sources[sid]["available_volume"]
            if used > avail + _EPS:
                add(
                    VIOLATION_DILUTION_SOURCE_INSUFFICIENT,
                    f"source '{sid}': {len([i for i in well_plans if i['owner'] == sid])} "
                    f"target well(s) need {_round_vol(used)} in total but only "
                    f"{_round_vol(avail)} is available "
                    f"(short by {_round_vol(used - avail)})",
                )

    if categories:
        raise DilutionPlanInvalid(categories, messages)

    return _build_response(req, rows, cols, well_plans, sources, stock_use)


def _build_response(req, rows, cols, well_plans, sources, stock_use) -> dict:
    # Phase 1: dispense diluent into every well (row-major order).
    # Phase 2: transfer source stock into every well needing it.
    instructions: list[dict] = []
    ordered = sorted(well_plans, key=lambda i: i["pos"])
    step = 1
    for item in ordered:
        p, t = item["plan"], item["target"]
        if p["diluent_volume"] > 0:
            instructions.append(
                {
                    "step": step,
                    "phase": "dispense_diluent",
                    "target_well": well_name(*item["pos"]),
                    "source_id": None,
                    "reagent": req.diluent,
                    "volume": _round_vol(p["diluent_volume"]),
                }
            )
            step += 1
    for item in ordered:
        p = item["plan"]
        if p["stock_volume"] > 0:
            instructions.append(
                {
                    "step": step,
                    "phase": "transfer_stock",
                    "target_well": well_name(*item["pos"]),
                    "source_id": item["owner"],
                    "reagent": None,
                    "volume": _round_vol(p["stock_volume"]),
                }
            )
            step += 1

    wells_out = []
    for item in ordered:
        p, t = item["plan"], item["target"]
        V = p["stock_volume"] + p["diluent_volume"]
        wells_out.append(
            {
                "well": well_name(*item["pos"]),
                "source_id": item["owner"],
                "target_concentration": t.target_concentration,
                "actual_concentration": _round_vol(p["actual_concentration"]),
                "absolute_error": _round_vol(p["abs_error"]),
                "relative_error": round(p["rel_error"], 12),
                "final_volume": _round_vol(V),
                "stock_volume": _round_vol(p["stock_volume"]),
                "diluent_volume": _round_vol(p["diluent_volume"]),
            }
        )

    sources_out = []
    for sid in sorted(sources):
        used = stock_use.get(sid, 0.0)
        avail = sources[sid]["available_volume"]
        sources_out.append(
            {
                "source_id": sid,
                "well": well_name(*sources[sid]["pos"]),
                "concentration": sources[sid]["concentration"],
                "available_volume": _round_vol(avail),
                "used_volume": _round_vol(used),
                "remaining_volume": _round_vol(avail - used),
            }
        )

    return {
        "plate": {"type": req.plate.type, "rows": rows, "columns": cols},
        "parameters": {
            "diluent": req.diluent,
            "well_capacity": _round_vol(req.well_capacity),
            "pipette_resolution": _round_vol(req.pipette_resolution),
            "min_pipette_volume": _round_vol(req.min_pipette_volume),
            "relative_tolerance": req.relative_tolerance,
            "absolute_tolerance": _round_vol(req.absolute_tolerance),
        },
        "wells": wells_out,
        "sources": sources_out,
        "instruction_count": len(instructions),
        "instructions": instructions,
        "verification": {
            "all_concentrations_within_tolerance": all(
                w["absolute_error"]
                <= max(
                    req.relative_tolerance * w["target_concentration"],
                    req.absolute_tolerance,
                )
                + 1e-9
                for w in wells_out
            ),
            "all_volumes_on_resolution_grid": all(
                abs(w["stock_volume"] / req.pipette_resolution
                    - round(w["stock_volume"] / req.pipette_resolution)) <= 1e-6
                and abs(w["diluent_volume"] / req.pipette_resolution
                    - round(w["diluent_volume"] / req.pipette_resolution)) <= 1e-6
                for w in wells_out
            ),
            "all_wells_within_capacity": all(
                w["final_volume"] <= req.well_capacity + 1e-9 for w in wells_out
            ),
            "all_draws_above_minimum": all(
                ins["volume"] >= req.min_pipette_volume - 1e-9 for ins in instructions
            ),
            "instruction_volumes_conserved": all(
                abs((w["stock_volume"] + w["diluent_volume"]) - w["final_volume"]) <= 1e-9
                for w in wells_out
            ),
            "all_sources_sufficient": all(
                s["remaining_volume"] >= -1e-9 for s in sources_out
            ),
        },
    }
