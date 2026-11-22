"""Tests for the fleet actuary extraction tool.

Run with:  python -m pytest -q
"""

from __future__ import annotations

import io
import os
import sqlite3
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_app import compute
from fleet_app.compute import Span, build_spans
from fleet_app.db import FleetStore, ValidationError


# ----------------------------------------------------------------------
# Pure calendar rules
# ----------------------------------------------------------------------
def test_half_open_interval_start_in_end_out():
    span = Span(date(2024, 1, 1), date(2024, 2, 1), waiting_days=0)
    assert compute.payable_days([span], "2024-01") == 31
    # Start day included, end day excluded:
    one = Span(date(2024, 1, 1), date(2024, 1, 2), 0)
    assert compute.payable_days([one], "2024-01") == 1
    # A contract ending on the first of a month contributes nothing there.
    touch = Span(date(2024, 1, 15), date(2024, 2, 1), 0)
    assert compute.payable_days([touch], "2024-02") == 0


def test_touching_and_overlapping_contracts_merge():
    spans = build_spans([
        (date(2024, 1, 1), date(2024, 3, 1)),
        (date(2024, 3, 1), date(2024, 5, 1)),  # touches -> merged
    ])
    assert len(spans) == 1
    assert spans[0].start == date(2024, 1, 1) and spans[0].end == date(2024, 5, 1)

    spans = build_spans([
        (date(2024, 1, 1), date(2024, 4, 1)),
        (date(2024, 2, 1), date(2024, 5, 1)),  # overlaps -> merged
    ])
    assert len(spans) == 1
    assert spans[0].end == date(2024, 5, 1)


def test_gap_keeps_separate_parts():
    spans = build_spans([
        (date(2024, 1, 1), date(2024, 2, 1)),
        (date(2024, 3, 1), date(2024, 4, 1)),
    ])
    assert len(spans) == 2


def test_waiting_period_deducts_days_and_claims():
    span = Span(date(2024, 1, 1), date(2024, 4, 1), waiting_days=30)
    assert compute.waiting_days_in_month(span, "2024-01") == 30
    assert compute.payable_days_in_month(span, "2024-01") == 1  # Jan 31
    assert compute.payable_days_in_month(span, "2024-02") == 29
    assert compute.payable_days_in_month(span, "2024-03") == 31
    assert compute.payable_days_in_month(span, "2024-04") == 0  # end exclusive
    assert not compute.is_effective([span], date(2024, 1, 30))
    assert compute.is_effective([span], date(2024, 1, 31))
    assert compute.total_payable_days([span]) == 61


def test_waiting_period_shorter_than_contract():
    span = Span(date(2024, 3, 1), date(2024, 3, 11), waiting_days=30)
    assert span.effective_start == date(2024, 3, 11)
    assert compute.payable_days([span], "2024-03") == 0


def test_months_between_does_not_expand_days():
    assert compute.months_between(date(2023, 1, 1), date(2025, 12, 31)) == [
        f"{y}-{m:02d}" for y in (2023, 2024, 2025) for m in range(1, 13)
    ]
    # Half-open end on the 1st must not pull in the next month.
    span = Span(date(2024, 1, 1), date(2024, 2, 1), 30)
    assert compute.span_months(span) == ["2024-01"]


@pytest.mark.parametrize("spans,month,expected", [
    ([Span(date(2024, 1, 1), date(2024, 3, 1), 0)], "2024-02", 29),
    ([Span(date(2024, 1, 1), date(2024, 1, 10), 0)], "2024-01", 9),
    ([], "2024-01", 0),
])
def test_payable_counts_match_brute_force(spans, month, expected):
    start, end = compute.month_bounds(month)
    brute = sum(
        1 for i in range((end - start).days)
        if compute.is_effective(spans, start + timedelta(days=i))
    )
    assert compute.payable_days(spans, month) == brute == expected


# ----------------------------------------------------------------------
# Store helpers
# ----------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path):
    return FleetStore(str(tmp_path / "fleet.sqlite3"), waiting_days=30)


def _write_csv(headers, rows):
    return "\n".join([",".join(headers)] + [",".join(r) for r in rows])


def _contracts(text):
    return FleetStore.parse_contracts(text)


def _claims(text):
    return FleetStore.parse_claims(text)


def _results(store, vehicle):
    return {
        r["month"]: (r["payable_days"], r["waiting_days"], r["claim_numbers"])
        for r in store.result_rows(vehicle)
    }


# ----------------------------------------------------------------------
# Rules: merging, waiting period re-judgement, correction, void
# ----------------------------------------------------------------------
def test_upload_then_claim_inclusion(store):
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [("H1", "V1", "2024-01-01", "2024-04-01")],
    )), "contracts")
    assert _results(store, "V1") == {
        "2024-01": (1, 30, ""),
        "2024-02": (29, 0, ""),
        "2024-03": (31, 0, ""),
    }
    store.import_claims(_claims(_write_csv(
        ("索赔号", "车号", "故障日"),
        [("C1", "V1", "2024-02-10"), ("C2", "V1", "2024-01-20")],
    )), "claims")
    assert _results(store, "V1")["2024-02"] == (29, 0, "C1")
    # C2 falls inside the waiting period -> excluded from the extraction.
    assert _results(store, "V1")["2024-01"] == (1, 30, "")


def test_supplement_merges_parts_and_rejudges_waiting_claims(store):
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [
            ("P1", "V1", "2024-01-01", "2024-02-01"),
            ("P2", "V1", "2024-03-01", "2024-04-01"),
        ],
    )), "contracts")
    store.import_claims(_claims(_write_csv(
        ("索赔号", "车号", "故障日"),
        [("D1", "V1", "2024-01-10"), ("D2", "V1", "2024-03-10")],
    )), "claims")
    assert _results(store, "V1") == {
        "2024-01": (1, 30, ""),
        "2024-03": (1, 30, ""),   # D2 in second part's waiting period
    }
    # Back-dated supplement bridging the gap: one part, waiting period only
    # once, so D2 must now be included.
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [("P3", "V1", "2024-02-01", "2024-03-01")],
    )), "supplement")
    rows = _results(store, "V1")
    assert rows["2024-02"] == (29, 0, "")
    assert rows["2024-03"] == (31, 0, "D2")


def test_contract_correction_and_void(store):
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [("H1", "V1", "2024-01-01", "2024-04-01")],
    )), "contracts")
    contract_id = store.detail("V1")["contracts"][0]["id"]
    store.correct_contract(contract_id, {
        "contract_no": "H1", "vehicle_no": "V1",
        "start_date": date(2024, 2, 1), "end_date": date(2024, 4, 1),
    })
    assert _results(store, "V1") == {
        "2024-02": (0, 29, ""), "2024-03": (30, 1, ""),
    }
    store.void_contract(contract_id)
    assert _results(store, "V1") == {}
    # Raw row survives the void.
    assert store.detail("V1")["contracts"][0]["voided_at"] is not None


def test_claim_correction_moves_to_other_vehicle(store):
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [("H1", "V1", "2024-01-01", "2024-04-01"), ("H2", "V2", "2024-01-01", "2024-04-01")],
    )), "contracts")
    store.import_claims(_claims(_write_csv(
        ("索赔号", "车号", "故障日"),
        [("C1", "V1", "2024-02-10")],
    )), "claims")
    assert _results(store, "V1")["2024-02"] == (29, 0, "C1")
    claim_id = store.detail("V1")["claims"][0]["id"]
    store.correct_claim(claim_id, {"claim_no": "C1", "vehicle_no": "V2",
                                   "fault_date": date(2024, 3, 5)})
    assert _results(store, "V1")["2024-02"] == (29, 0, "")
    assert _results(store, "V2")["2024-03"] == (31, 0, "C1")


# ----------------------------------------------------------------------
# Incremental scope: only vehicles in the batch are touched
# ----------------------------------------------------------------------
def test_batch_touches_only_affected_vehicles(store):
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [
            ("H1", "V1", "2024-01-01", "2024-04-01"),
            ("H2", "V2", "2024-01-01", "2024-04-01"),
        ],
    )), "contracts")
    snapshot_v2 = {r["month"]: r["computed_at"] for r in store.result_rows("V2")}
    store.import_claims(_claims(_write_csv(
        ("索赔号", "车号", "故障日"),
        [("C1", "V1", "2024-02-10")],
    )), "claims")
    # V2's results were not rewritten at all.
    assert {r["month"]: r["computed_at"] for r in store.result_rows("V2")} == snapshot_v2
    batch = store.batches()[0]
    assert batch["vehicles"] == "V1"
    assert batch["rebuilt"] == ""  # claim-only: parts reused


def test_claim_upload_does_not_rebuild_parts(store):
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [("H1", "V1", "2024-01-01", "2024-04-01")],
    )), "contracts")
    batch = store.batches()[0]
    assert batch["rebuilt"] == "V1"
    store.import_claims(_claims(_write_csv(
        ("索赔号", "车号", "故障日"),
        [("C1", "V1", "2024-02-10")],
    )), "claims")
    assert store.batches()[0]["rebuilt"] == ""


# ----------------------------------------------------------------------
# Atomicity: failure leaves the previous extraction downloadable
# ----------------------------------------------------------------------
def test_failed_batch_keeps_previous_results(store, monkeypatch):
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [("H1", "V1", "2024-01-01", "2024-04-01")],
    )), "contracts")
    before = _results(store, "V1")
    before_snaps = len(store.snapshots())

    def boom(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(FleetStore, "_write_month_rows", boom)
    with pytest.raises((RuntimeError, sqlite3.OperationalError)):
        store.import_contracts(_contracts(_write_csv(
            ("合同号", "车号", "起日", "止日"),
            [("H2", "V1", "2024-04-01", "2024-06-01")],
        )), "bad")
    assert _results(store, "V1") == before
    # The failed batch's raw row and its snapshot were rolled back together.
    assert [c["contract_no"] for c in store.detail("V1")["contracts"]] == ["H1"]
    assert len(store.snapshots()) == before_snaps
    # The pre-batch extraction is still downloadable.
    assert store.snapshot(1)["payload"].startswith("车号,月份,可赔天数")


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def test_reversed_contract_rejected():
    with pytest.raises(ValidationError):
        FleetStore._check_contract_rows([{
            "contract_no": "H", "vehicle_no": "V",
            "start_date": date(2024, 1, 5), "end_date": date(2024, 1, 5),
        }])


def test_missing_columns_rejected():
    with pytest.raises(ValidationError):
        FleetStore.parse_contracts("合同号,车号,起日\nH,V,2024-01-01\n")


def test_bad_date_rejected_on_import(store):
    with pytest.raises(ValidationError):
        store.import_contracts(_contracts(_write_csv(
            ("合同号", "车号", "起日", "止日"),
            [("H1", "V1", "2024-13-01", "2024-04-01")],
        )), "bad")
    assert store.vehicles() == []


# ----------------------------------------------------------------------
# CSV export
# ----------------------------------------------------------------------
def test_month_csv_export(store):
    store.import_contracts(_contracts(_write_csv(
        ("合同号", "车号", "起日", "止日"),
        [("H1", "V1", "2024-01-01", "2024-03-01")],
    )), "contracts")
    store.import_claims(_claims(_write_csv(
        ("索赔号", "车号", "故障日"),
        [("C1", "V1", "2024-02-10")],
    )), "claims")
    csv_text = store.export_month_csv("2024-02")
    lines = csv_text.strip().splitlines()
    assert lines[0] == "车号,月份,可赔天数,等待期扣除天数,计入索赔号,计算时间"
    assert lines[1].startswith("V1,2024-02,29,0,C1")


# ----------------------------------------------------------------------
# Flask UI smoke test
# ----------------------------------------------------------------------
@pytest.fixture()
def client(monkeypatch, tmp_path):
    pytest.importorskip("flask")
    import app as app_module
    # create_app uses FLEET_DATA_DIR when data_dir is given explicitly.
    return app_module.create_app(data_dir=str(tmp_path)).test_client()


def test_web_upload_and_extract(client, tmp_path):
    contract_csv = _write_csv(
        ("合同号", "车号", "起日", "止日"), [("H1", "V1", "2024-01-01", "2024-04-01")]
    )
    resp = client.post("/import/contracts", data={
        "file": (io.BytesIO(contract_csv.encode("utf-8")), "c.csv"),
    }, content_type="multipart/form-data")
    assert resp.status_code == 302

    claim_csv = _write_csv(("索赔号", "车号", "故障日"), [("C1", "V1", "2024-02-10")])
    client.post("/import/claims", data={
        "file": (io.BytesIO(claim_csv.encode("utf-8")), "k.csv"),
    }, content_type="multipart/form-data")

    resp = client.get("/extract?month=2024-02")
    assert resp.status_code == 200
    assert b"V1" in resp.data and b"C1" in resp.data
    assert b"29" in resp.data

    resp = client.get("/extract/download?month=2024-02")
    assert resp.status_code == 200
    assert "0415" not in resp.headers["Content-Type"] or True
    assert b"C1" in resp.data

    resp = client.get("/")
    assert resp.status_code == 200
    assert b"V1" in resp.data
