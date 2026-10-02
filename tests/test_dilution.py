"""Dilution-plan endpoint tests: stock sharing, rounding/tolerance, capacity,
minimum pipette volume, itemized 422 semantics and determinism."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PLATE = {"type": "24"}  # 4 rows x 6 cols: A1..D6


def _payload(**overrides):
    p = {
        "plate": PLATE,
        "layout": {"S1": "A1", "S2": "A2", "S3": "B1"},
        "sources": [
            {"id": "S1", "concentration": 100.0, "available_volume": 100.0},
            {"id": "S2", "concentration": 80.0, "available_volume": 60.0},
            {"id": "S3", "concentration": 50.0, "available_volume": 30.0},
        ],
        "targets": [
            {"well": "A1", "target_concentration": 50.0, "final_volume": 100.0},
            {"well": "A2", "target_concentration": 20.0, "final_volume": 100.0},
            {"well": "B1", "target_concentration": 10.0, "final_volume": 100.0},
        ],
        "well_capacity": 200.0,
        "diluent": "PBS",
        "pipette_resolution": 1.0,
        "min_pipette_volume": 2.0,
        "relative_tolerance": 0.01,
        "absolute_tolerance": 0.5,
    }
    p.update(overrides)
    return p


# --------------------------------------------------------------------- 200


def test_basic_plan_volumes_and_concentrations():
    r = client.post("/api/v1/arrangements/dilution-plan", json=_payload())
    assert r.status_code == 200
    body = r.json()

    wells = {w["well"]: w for w in body["wells"]}
    assert set(wells) == {"A1", "A2", "B1"}
    # A1: 100 * v/100 = 50  -> 50 stock + 50 diluent
    assert wells["A1"]["stock_volume"] == 50.0
    assert wells["A1"]["diluent_volume"] == 50.0
    assert wells["A1"]["actual_concentration"] == 50.0
    # A2: 80 * v/100 = 20   -> 25 stock + 75 diluent
    assert wells["A2"]["stock_volume"] == 25.0
    assert wells["A2"]["actual_concentration"] == 20.0
    # B1: 50 * v/100 = 10   -> 20 stock + 80 diluent
    assert wells["B1"]["stock_volume"] == 20.0
    assert wells["B1"]["actual_concentration"] == 10.0
    for w in wells.values():
        assert w["final_volume"] == 100.0
        assert w["stock_volume"] + w["diluent_volume"] == 100.0
        assert w["relative_error"] == 0.0

    src = {s["source_id"]: s for s in body["sources"]}
    assert src["S1"]["used_volume"] == 50.0
    assert src["S1"]["remaining_volume"] == 50.0
    assert src["S2"]["remaining_volume"] == 35.0
    assert src["S3"]["remaining_volume"] == 10.0

    assert body["verification"] == {
        "all_concentrations_within_tolerance": True,
        "all_volumes_on_resolution_grid": True,
        "all_wells_within_capacity": True,
        "all_draws_above_minimum": True,
        "instruction_volumes_conserved": True,
        "all_sources_sufficient": True,
    }


def test_instructions_are_diluent_phase_then_stock_phase_row_major():
    body = client.post("/api/v1/arrangements/dilution-plan", json=_payload()).json()
    assert body["instruction_count"] == 6
    steps = body["instructions"]
    assert [s["step"] for s in steps] == [1, 2, 3, 4, 5, 6]
    # Phase 1: diluent first, wells in row-major order (A1, A2, B1).
    assert [s["phase"] for s in steps[:3]] == ["dispense_diluent"] * 3
    assert [s["target_well"] for s in steps[:3]] == ["A1", "A2", "B1"]
    assert all(s["reagent"] == "PBS" and s["source_id"] is None for s in steps[:3])
    # Phase 2: stock transfers in the same deterministic order.
    assert [s["phase"] for s in steps[3:]] == ["transfer_stock"] * 3
    assert [s["target_well"] for s in steps[3:]] == ["A1", "A2", "B1"]
    assert [s["source_id"] for s in steps[3:]] == ["S1", "S2", "S3"]
    assert all(s["reagent"] is None for s in steps[3:])
    # Each instruction volume sums back to its well.
    for w in body["wells"]:
        ins = [s for s in steps if s["target_well"] == w["well"]]
        assert sum(s["volume"] for s in ins) == w["final_volume"]


def test_determinism():
    b1 = client.post("/api/v1/arrangements/dilution-plan", json=_payload()).json()
    b2 = client.post("/api/v1/arrangements/dilution-plan", json=_payload()).json()
    assert b1 == b2


def test_input_key_order_does_not_change_plan():
    # Same data, different JSON key/list ordering -> identical plan.
    p = _payload()
    p["sources"] = list(reversed(p["sources"]))
    p["targets"] = list(reversed(p["targets"]))
    body = client.post("/api/v1/arrangements/dilution-plan", json=p).json()
    ref = client.post("/api/v1/arrangements/dilution-plan", json=_payload()).json()
    assert body["wells"] == ref["wells"]
    assert body["instructions"] == ref["instructions"]
    assert {s["source_id"]: s["remaining_volume"] for s in body["sources"]} == {
        s["source_id"]: s["remaining_volume"] for s in ref["sources"]
    }


def test_well_names_case_insensitive():
    p = _payload()
    p["layout"] = {"S1": "a1", "S2": "A2", "S3": "B1"}
    p["targets"][0] = {"well": "a1", "target_concentration": 50.0, "final_volume": 100.0}
    r = client.post("/api/v1/arrangements/dilution-plan", json=p)
    assert r.status_code == 200
    assert {w["well"] for w in r.json()["wells"]} == {"A1", "A2", "B1"}


def test_rounding_to_resolution_grid():
    # 0.5 uL grid; ideal stock for c=33.3333 in 100 is 33.3333 -> 33.5.
    p = _payload(
        targets=[{"well": "A1", "target_concentration": 33.3333, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
        pipette_resolution=0.5,
        min_pipette_volume=1.0,
    )
    body = client.post("/api/v1/arrangements/dilution-plan", json=p).json()
    w = body["wells"][0]
    assert w["stock_volume"] == 33.5
    assert w["diluent_volume"] == 66.5
    assert abs(w["actual_concentration"] - 33.5) < 1e-9
    assert w["relative_error"] <= 0.01


def test_relative_error_exactly_at_tolerance_passes():
    # c0=100, V=100, grid 1: ct=50.5 -> stock 50 or 51 both give 1% error,
    # tie resolved towards the smaller stock volume.
    p = _payload(
        targets=[{"well": "A1", "target_concentration": 50.5, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
        pipette_resolution=1.0,
        min_pipette_volume=1.0,
        relative_tolerance=0.01,
    )
    body = client.post("/api/v1/arrangements/dilution-plan", json=p).json()
    w = body["wells"][0]
    assert w["stock_volume"] == 50.0
    assert abs(w["relative_error"] - 0.01 / 1.01) < 1e-6


def test_absolute_tolerance_rescues_otherwise_infeasible_well():
    # Ideal stock 6 uL; minimum draw 10 forces stock to 10 -> actual 10
    # (relative error ~67%), but an absolute tolerance of 4.5 covers |10-6|=4
    # while pure diluent (actual 0, error 6) stays out of tolerance.
    p = _payload(
        targets=[{"well": "A1", "target_concentration": 6.0, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
        pipette_resolution=1.0,
        min_pipette_volume=10.0,
        relative_tolerance=0.01,
        absolute_tolerance=4.5,
    )
    body = client.post("/api/v1/arrangements/dilution-plan", json=p).json()
    w = body["wells"][0]
    assert w["stock_volume"] == 10.0
    assert w["actual_concentration"] == 10.0
    assert w["absolute_error"] == 4.0


def test_zero_target_concentration_is_pure_diluent():
    p = _payload(
        targets=[{"well": "A1", "target_concentration": 0.0, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
    )
    body = client.post("/api/v1/arrangements/dilution-plan", json=p).json()
    w = body["wells"][0]
    assert w["stock_volume"] == 0.0
    assert w["diluent_volume"] == 100.0
    assert w["actual_concentration"] == 0.0
    assert body["sources"][0]["used_volume"] == 0.0
    assert body["sources"][0]["remaining_volume"] == 100.0
    assert len(body["instructions"]) == 1
    assert body["instructions"][0]["phase"] == "dispense_diluent"


def test_target_equal_to_source_concentration_is_pure_stock():
    p = _payload(
        targets=[{"well": "A1", "target_concentration": 100.0, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
    )
    body = client.post("/api/v1/arrangements/dilution-plan", json=p).json()
    w = body["wells"][0]
    assert w["stock_volume"] == 100.0
    assert w["diluent_volume"] == 0.0
    assert len(body["instructions"]) == 1
    assert body["instructions"][0]["phase"] == "transfer_stock"


def test_final_volume_may_equal_capacity():
    p = _payload(
        well_capacity=100.0,
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
    )
    assert client.post("/api/v1/arrangements/dilution-plan", json=p).status_code == 200


def test_binary_float_grid_corner_pure_stock():
    # 0.3/0.1 is 2.999... in binary float; the pure-stock corner must still
    # resolve to stock 0.3 + diluent 0 rather than a spurious negative/overflow.
    p = _payload(
        well_capacity=1.0,
        pipette_resolution=0.1,
        min_pipette_volume=0.1,
        sources=[{"id": "S1", "concentration": 10.0, "available_volume": 1.0}],
        targets=[{"well": "A1", "target_concentration": 10.0, "final_volume": 0.3}],
    )
    body = client.post("/api/v1/arrangements/dilution-plan", json=p).json()
    w = body["wells"][0]
    assert w["stock_volume"] == 0.3
    assert w["diluent_volume"] == 0.0
    assert all(body["verification"].values())


def test_layout_may_be_a_superset_of_targets():
    # S2/S3 remain on the plate but are not part of this gradient.
    p = _payload(targets=[{"well": "A1", "target_concentration": 50.0, "final_volume": 100.0}])
    r = client.post("/api/v1/arrangements/dilution-plan", json=p)
    assert r.status_code == 200
    assert [w["well"] for w in r.json()["wells"]] == ["A1"]


# --------------------------------------------------------------------- 422


def _categories(payload):
    r = client.post("/api/v1/arrangements/dilution-plan", json=payload)
    assert r.status_code == 422, r.text
    body = r.json()
    assert "wells" not in body and "instructions" not in body
    return body["infeasibility_categories"], body["messages"]


def test_shared_inventory_shortfall_is_itemized():
    p = _payload(
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 49.0}],
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume": 100.0}],
    )
    cats, msgs = _categories(p)
    assert cats == ["DILUTION_SOURCE_INSUFFICIENT"]
    assert "S1" in msgs[0] and "49.0" in msgs[0] and "short by 1.0" in msgs[0]


def test_well_capacity_exceeded():
    p = _payload(
        well_capacity=99.0,
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
    )
    cats, _ = _categories(p)
    assert cats == ["DILUTION_WELL_CAPACITY_EXCEEDED"]


def test_final_volume_off_resolution_grid():
    p = _payload(
        pipette_resolution=3.0,
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
    )
    cats, _ = _categories(p)
    assert cats == ["DILUTION_VOLUME_INVALID"]


def test_tolerance_unreachable_after_rounding():
    # Want 1 in 100 on a 10 uL grid: closest is stock 10 -> actual 10 (900%).
    p = _payload(
        pipette_resolution=10.0,
        targets=[{"well": "A1", "target_concentration": 1.0, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
    )
    cats, msgs = _categories(p)
    assert cats == ["DILUTION_TOLERANCE_UNREACHABLE"]
    assert "A1" in msgs[0]


def test_minimum_pipette_volume_violation_when_below_min():
    p = _payload(
        pipette_resolution=1.0,
        min_pipette_volume=10.0,
        targets=[{"well": "A1", "target_concentration": 50.0, "final_volume": 5.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
    )
    cats, _ = _categories(p)
    assert cats == ["DILUTION_MIN_VOLUME_VIOLATION"]


def test_target_above_source_concentration_rejected():
    p = _payload(
        targets=[{"well": "A1", "target_concentration": 150.0, "final_volume": 100.0}],
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
    )
    cats, _ = _categories(p)
    assert cats == ["DILUTION_TARGET_CONCENTRATION_INVALID"]


def test_structural_problems_all_reported_together():
    p = _payload(
        sources=[
            {"id": "S1", "concentration": 100.0, "available_volume": 100.0},
            {"id": "S1", "concentration": 100.0, "available_volume": 100.0},
            {"id": "GHOST", "concentration": 100.0, "available_volume": 100.0},
        ],
        targets=[
            {"well": "A1", "target_concentration": 50.0, "final_volume": 100.0},
            {"well": "a1", "target_concentration": 50.0, "final_volume": 100.0},
            {"well": "C3", "target_concentration": 50.0, "final_volume": 100.0},
            {"well": "Z9", "target_concentration": 50.0, "final_volume": 100.0},
        ],
    )
    cats, _ = _categories(p)
    assert "DILUTION_SOURCE_DUPLICATED" in cats
    assert "DILUTION_SOURCE_UNKNOWN" in cats
    assert "DILUTION_TARGET_WELL_DUPLICATED" in cats
    assert "DILUTION_TARGET_WELL_NOT_IN_LAYOUT" in cats
    assert "WELL_REFERENCE_INVALID" in cats


def test_target_source_not_supplied():
    # A2 is occupied by S2 in the layout but no source record is given.
    p = _payload(
        sources=[{"id": "S1", "concentration": 100.0, "available_volume": 100.0}],
        targets=[{"well": "A2", "target_concentration": 20.0, "final_volume": 100.0}],
    )
    cats, _ = _categories(p)
    assert cats == ["DILUTION_SOURCE_UNKNOWN"]


def test_no_partial_plan_on_mixed_well_failures():
    # A1 fails tolerance; B1 is fine on its own. The whole request is rejected.
    p = _payload(
        pipette_resolution=10.0,
        targets=[
            {"well": "A1", "target_concentration": 1.0, "final_volume": 100.0},
            {"well": "B1", "target_concentration": 0.0, "final_volume": 100.0},
        ],
        sources=[
            {"id": "S1", "concentration": 100.0, "available_volume": 100.0},
            {"id": "S3", "concentration": 50.0, "available_volume": 30.0},
        ],
    )
    cats, _ = _categories(p)
    assert cats == ["DILUTION_TOLERANCE_UNREACHABLE"]


def test_request_validation_error_is_422():
    r = client.post("/api/v1/arrangements/dilution-plan", json={"layout": {}})
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]


def test_extra_field_rejected():
    r = client.post("/api/v1/arrangements/dilution-plan", json=_payload(extra=1))
    assert r.status_code == 422
    assert r.json()["infeasibility_categories"] == ["REQUEST_VALIDATION_ERROR"]
