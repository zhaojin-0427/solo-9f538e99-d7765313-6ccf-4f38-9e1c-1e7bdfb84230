"""Ingestion, storage and incremental recalculation.

Data model
----------
Raw input is kept exactly as uploaded:

* ``contracts`` – one row per contract version; a correction updates the
  row in place (the batch log preserves the audit trail), a void sets
  ``voided_at`` and leaves the original row downloadable.
* ``claims`` – one row per claim.

Derived data is kept separately and is *always* rebuilt from the raw rows
inside the same transaction, so raw records and extraction results become
visible together:

* ``parts`` – merged, contiguous protection parts per vehicle, with the
  waiting-period snapshot used to build them.
* ``month_results`` – the extraction result: per vehicle and calendar
  month, payable days and the included claim numbers.

Incremental strategy
--------------------
A batch collects the vehicles it touches.  For each such vehicle the
relevant months are found from the contract range (arithmetic month
listing, never day-by-day), from already stored months, and from claim
dates.  Parts are rebuilt only when the *set* of active contracts for that
vehicle changed; a claim-only batch reuses the existing parts.  Vehicles
not named by the batch are never touched.

Crash safety
------------
The whole batch runs inside ``BEGIN IMMEDIATE`` … ``COMMIT``.  Results are
written last; any validation or DB error rolls everything back and the
previous ``month_results`` stay downloadable.  The only way to lose the
previous results is a hard crash mid-transaction, which SQLite itself
rolls back on the next open.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from datetime import date, datetime, timezone
from typing import Iterable

from .compute import (
    DEFAULT_WAITING_DAYS,
    Span,
    build_spans,
    is_effective,
    month_covered_by,
    month_has_coverage,
    payable_days,
    waiting_days,
)

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS contracts (
    id            INTEGER PRIMARY KEY,
    contract_no   TEXT NOT NULL,
    vehicle_no    TEXT NOT NULL,
    start_date    TEXT NOT NULL,          -- YYYY-MM-DD, inclusive
    end_date      TEXT NOT NULL,          -- YYYY-MM-DD, exclusive
    voided_at     TEXT,                   -- NULL while active
    voided_batch  INTEGER,
    source_batch  INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contracts_vehicle ON contracts(vehicle_no);

CREATE TABLE IF NOT EXISTS claims (
    id            INTEGER PRIMARY KEY,
    claim_no      TEXT NOT NULL,
    vehicle_no    TEXT NOT NULL,
    fault_date    TEXT,                   -- YYYY-MM-DD, nullable
    source_batch  INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_vehicle ON claims(vehicle_no);
CREATE INDEX IF NOT EXISTS idx_claims_no ON claims(claim_no);

CREATE TABLE IF NOT EXISTS parts (
    id            INTEGER PRIMARY KEY,
    vehicle_no    TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    waiting_days  INTEGER NOT NULL,
    computed_at   TEXT NOT NULL,
    UNIQUE(vehicle_no, seq)
);

CREATE TABLE IF NOT EXISTS month_results (
    id            INTEGER PRIMARY KEY,
    vehicle_no    TEXT NOT NULL,
    month         TEXT NOT NULL,          -- YYYY-MM
    payable_days  INTEGER NOT NULL,
    waiting_days  INTEGER NOT NULL,
    claim_numbers TEXT NOT NULL DEFAULT '',
    computed_at   TEXT NOT NULL,
    UNIQUE(vehicle_no, month)
);
CREATE INDEX IF NOT EXISTS idx_results_vehicle ON month_results(vehicle_no);

CREATE TABLE IF NOT EXISTS batches (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,          -- contracts | claims
    mode          TEXT NOT NULL,          -- upload | correct | void
    label         TEXT NOT NULL,
    waiting_days  INTEGER NOT NULL,
    vehicles      TEXT NOT NULL DEFAULT '',
    rebuilt       TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,          -- committed
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Logical backups: the extraction state as it was immediately before a
-- batch started.  Written inside the same transaction as the batch, so a
-- rolled-back batch leaves no orphan snapshot, and a failed batch can
-- always still be compared against / downloaded from the prior state.
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    label         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    payload       TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ValidationError(ValueError):
    """Raised for bad CSV content; carries the offending line number."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(f"第 {line} 行: {message}" if line else message)


class FleetStore:
    def __init__(self, db_path: str, waiting_days: int = DEFAULT_WAITING_DAYS):
        self.db_path = db_path
        self.waiting_days = waiting_days
        directory = os.path.dirname(os.path.abspath(db_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._init()

    # -- connections -----------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('waiting_days', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(self.waiting_days),),
            )

    # ------------------------------------------------------------------
    # CSV parsing
    # ------------------------------------------------------------------
    CONTRACT_HEADERS = ("合同号", "车号", "起日", "止日")
    CLAIM_HEADERS = ("索赔号", "车号", "故障日")

    @staticmethod
    def parse_contracts(text: str) -> list[dict]:
        return _parse_csv(text, FleetStore.CONTRACT_HEADERS, keep_fault_date=False)

    @staticmethod
    def parse_claims(text: str) -> list[dict]:
        return _parse_csv(text, FleetStore.CLAIM_HEADERS, keep_fault_date=True)

    # ------------------------------------------------------------------
    # Public mutation API.  Every method validates first (so a bad file
    # never opens a transaction) and then runs one atomic transaction.
    # ------------------------------------------------------------------
    def import_contracts(self, rows: list[dict], label: str, mode: str = "upload") -> dict:
        self._check_contract_rows(rows)
        vehicles = sorted({r["vehicle_no"] for r in rows})
        return self._write_batch("contracts", mode, label, vehicles,
                                 lambda conn, batch, now: self._insert_contracts(conn, rows, batch, now))

    def import_claims(self, rows: list[dict], label: str, mode: str = "upload") -> dict:
        self._check_claim_rows(rows)
        vehicles = sorted({r["vehicle_no"] for r in rows})
        return self._write_batch("claims", mode, label, vehicles,
                                 lambda conn, batch, now: self._insert_claims(conn, rows, batch, now))

    def correct_contract(self, contract_id: int, row: dict) -> dict:
        self._check_contract_rows([row])
        with self._transaction() as conn:
            target = self._active_contract(conn, contract_id)
            vehicles = sorted({target["vehicle_no"], row["vehicle_no"]})
            batch = self._open_batch(conn, "contracts", "correct",
                                     f"更正合同 #{contract_id}", vehicles)
            conn.execute(
                "UPDATE contracts SET contract_no=?, vehicle_no=?, start_date=?, end_date=?, "
                "source_batch=? WHERE id=?",
                (row["contract_no"], row["vehicle_no"], row["start_date"], row["end_date"],
                 batch, contract_id),
            )
            rebuilt = self._recompute(conn, vehicles, batch)
            self._close_batch(conn, batch, rebuilt)
        return {"batch": batch, "vehicles": vehicles, "rebuilt": rebuilt}

    def correct_claim(self, claim_id: int, row: dict) -> dict:
        self._check_claim_rows([row])
        with self._transaction() as conn:
            target = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if target is None:
                raise ValidationError(f"索赔 {claim_id} 不存在")
            vehicles = sorted({target["vehicle_no"], row["vehicle_no"]})
            batch = self._open_batch(conn, "claims", "correct",
                                     f"更正索赔 #{claim_id}", vehicles)
            conn.execute(
                "UPDATE claims SET claim_no=?, vehicle_no=?, fault_date=?, source_batch=? "
                "WHERE id=?",
                (row["claim_no"], row["vehicle_no"], row["fault_date"], batch, claim_id),
            )
            rebuilt = self._recompute(conn, vehicles, batch)
            self._close_batch(conn, batch, rebuilt)
        return {"batch": batch, "vehicles": vehicles, "rebuilt": rebuilt}

    def void_contract(self, contract_id: int) -> dict:
        with self._transaction() as conn:
            target = self._active_contract(conn, contract_id)
            batch = self._open_batch(conn, "contracts", "void",
                                     f"撤销合同 #{contract_id}", [target["vehicle_no"]])
            conn.execute(
                "UPDATE contracts SET voided_at=?, voided_batch=? WHERE id=?",
                (utcnow(), batch, contract_id),
            )
            rebuilt = self._recompute(conn, [target["vehicle_no"]], batch)
            self._close_batch(conn, batch, rebuilt)
        return {"batch": batch, "vehicles": [target["vehicle_no"]], "rebuilt": rebuilt}

    # ------------------------------------------------------------------
    # Transaction plumbing
    # ------------------------------------------------------------------
    class _Transaction:
        def __init__(self, store: "FleetStore"):
            self.store = store
            self.conn = store.connect()

        def __enter__(self) -> sqlite3.Connection:
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb) -> bool:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")
                # Re-raise unknown DB errors with a clear message.
                if isinstance(exc, sqlite3.Error):
                    raise RuntimeError(f"数据库写入失败，原取数结果保持不变: {exc}") from exc
            self.conn.close()
            return False

    def _transaction(self) -> "FleetStore._Transaction":
        return FleetStore._Transaction(self)

    def _write_batch(self, kind, mode, label, vehicles, writer) -> dict:
        with self._transaction() as conn:
            batch = self._open_batch(conn, kind, mode, label, vehicles)
            writer(conn, batch, utcnow())
            rebuilt = self._recompute(conn, vehicles, batch)
            self._close_batch(conn, batch, rebuilt)
        return {"batch": batch, "vehicles": vehicles, "rebuilt": rebuilt}

    def _open_batch(self, conn, kind, mode, label, vehicles) -> int:
        cur = conn.execute(
            "INSERT INTO batches(kind, mode, label, waiting_days, vehicles, status, created_at) "
            "VALUES (?,?,?,?,?, 'committed', ?)",
            (kind, mode, label, self.waiting_days, ",".join(sorted(vehicles)), utcnow()),
        )
        batch = cur.lastrowid
        # Freeze the extraction results as they stood before this batch.
        # It is written in the same transaction: rollback removes it too,
        # and "still downloadable" always refers to the last committed state.
        conn.execute(
            "INSERT INTO snapshots(batch_id, kind, label, created_at, payload) VALUES (?,?,?,?,?)",
            (batch, kind, f"批次 #{batch}（{label}）之前", utcnow(), self._snapshot_payload(conn)),
        )
        return batch

    @staticmethod
    def _snapshot_payload(conn) -> str:
        rows = conn.execute(
            "SELECT vehicle_no, month, payable_days, waiting_days, claim_numbers, computed_at "
            "FROM month_results ORDER BY vehicle_no, month"
        ).fetchall()
        return _results_csv(rows)

    @staticmethod
    def _close_batch(conn, batch: int, rebuilt: list[str]) -> None:
        conn.execute("UPDATE batches SET rebuilt=? WHERE id=?", (",".join(rebuilt), batch))

    def _insert_contracts(self, conn, rows, batch, now) -> None:
        conn.executemany(
            "INSERT INTO contracts(contract_no, vehicle_no, start_date, end_date, "
            "source_batch, created_at) VALUES (?,?,?,?,?,?)",
            [(r["contract_no"], r["vehicle_no"], r["start_date"], r["end_date"], batch, now)
             for r in rows],
        )

    def _insert_claims(self, conn, rows, batch, now) -> None:
        conn.executemany(
            "INSERT INTO claims(claim_no, vehicle_no, fault_date, source_batch, created_at) "
            "VALUES (?,?,?,?,?)",
            [(r["claim_no"], r["vehicle_no"], r["fault_date"], batch, now) for r in rows],
        )

    @staticmethod
    def _active_contract(conn, contract_id: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM contracts WHERE id=? AND voided_at IS NULL", (contract_id,)
        ).fetchone()
        if row is None:
            raise ValidationError(f"合同 {contract_id} 不存在或已撤销")
        return row

    # ------------------------------------------------------------------
    # Incremental recalculation
    # ------------------------------------------------------------------
    def _recompute(self, conn, vehicles: Iterable[str], batch: int) -> list[str]:
        rebuilt = []
        for vehicle_no in vehicles:
            if self._recompute_vehicle(conn, vehicle_no, batch):
                rebuilt.append(vehicle_no)
        return rebuilt

    def _recompute_vehicle(self, conn, vehicle_no: str, batch: int) -> bool:
        """Rebuild parts and months for one vehicle.

        Returns True when the merged parts changed (the expensive path).
        Claim-only changes still refresh the affected months but reuse the
        stored parts.  A vehicle with no contracts and no stored parts is a
        no-op: uploading a claim for an uncovered vehicle must not create
        empty parts or result rows.
        """
        stored = self._stored_parts(conn, vehicle_no)
        active = conn.execute(
            "SELECT start_date, end_date FROM contracts WHERE vehicle_no=? AND voided_at IS NULL",
            (vehicle_no,),
        ).fetchall()
        fresh = build_spans(
            [(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])) for r in active],
            self.waiting_days,
        )
        if stored is None and not fresh:
            return False
        parts_changed = _spans_signature(stored or []) != _spans_signature(fresh)
        if not parts_changed:
            parts = stored
        else:
            parts = fresh
            conn.execute("DELETE FROM parts WHERE vehicle_no=?", (vehicle_no,))
            now = utcnow()
            for seq, span in enumerate(parts):
                conn.execute(
                    "INSERT INTO parts(vehicle_no, seq, start_date, end_date, waiting_days, computed_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (vehicle_no, seq, span.start.isoformat(), span.end.isoformat(),
                     span.waiting_days, now),
                )

        # Months this batch may affect:
        #  * every month already on record (so stale months can be removed),
        #  * every month covered by the current parts,
        #  * every month a claim falls into.
        # Stored months are a handful per vehicle (a 10-year contract is
        # only 120 of them), and all counts come from date arithmetic, so
        # nothing is expanded day by day and nothing re-reads contracts.
        months: set[str] = {r["month"] for r in conn.execute(
            "SELECT month FROM month_results WHERE vehicle_no=?", (vehicle_no,))}
        months.update(month_covered_by(parts))
        claim_dates = self._claim_dates(conn, vehicle_no)
        months.update(_month_of(fault_day) for _no, fault_day in claim_dates if fault_day)

        results = self._build_month_rows(parts, months, claim_dates)
        self._write_month_rows(conn, vehicle_no, results, months)
        return parts_changed
    def _stored_parts(self, conn, vehicle_no: str) -> list[Span] | None:
        rows = conn.execute(
            "SELECT start_date, end_date, waiting_days FROM parts WHERE vehicle_no=? ORDER BY seq",
            (vehicle_no,),
        ).fetchall()
        if not rows:
            return None
        return [
            Span(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]),
                 r["waiting_days"])
            for r in rows
        ]

    def _claim_dates(self, conn, vehicle_no: str):
        rows = conn.execute(
            "SELECT claim_no, fault_date FROM claims WHERE vehicle_no=? ORDER BY claim_no",
            (vehicle_no,),
        ).fetchall()
        out = []
        for r in rows:
            fd = _parse_date(r["fault_date"])
            out.append((r["claim_no"], fd))
        return out

    def _build_month_rows(self, parts, months, claim_dates):
        """Compute every result row in memory before writing anything."""
        # Bucket claims by their own month up front; each claim is then
        # examined once instead of once per affected month.
        by_month: dict[str, list[tuple[str, date]]] = {}
        for claim_no, fault_day in claim_dates:
            if fault_day is not None:
                by_month.setdefault(_month_of(fault_day), []).append((claim_no, fault_day))
        rows = []
        for month in sorted(months):
            days = payable_days(parts, month)
            waiting = waiting_days(parts, month)
            included = sorted(
                claim_no
                for claim_no, fault_day in by_month.get(month, [])
                if is_effective(parts, fault_day)
            )
            in_scope = days > 0 or waiting > 0 or month_has_coverage(parts, month) or included
            if not in_scope:
                # No coverage and no claim in this month: keep nothing.  (A
                # month can be in ``months`` merely because it was on record
                # before, e.g. after a contract void.)
                continue
            rows.append({
                "month": month,
                "payable_days": days,
                "waiting_days": waiting,
                "claim_numbers": ",".join(included),
            })
        return rows

    def _write_month_rows(self, conn, vehicle_no, results, months) -> None:
        keep = {r["month"] for r in results}
        now = utcnow()
        # Delete months that no longer exist, then upsert current ones.  This
        # is the only place month_results changes.
        for month in sorted(months):
            if month not in keep:
                conn.execute(
                    "DELETE FROM month_results WHERE vehicle_no=? AND month=?",
                    (vehicle_no, month),
                )
        for row in results:
            conn.execute(
                "INSERT INTO month_results(vehicle_no, month, payable_days, waiting_days, "
                "claim_numbers, computed_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(vehicle_no, month) DO UPDATE SET "
                "payable_days=excluded.payable_days, waiting_days=excluded.waiting_days, "
                "claim_numbers=excluded.claim_numbers, computed_at=excluded.computed_at",
                (vehicle_no, row["month"], row["payable_days"], row["waiting_days"],
                 row["claim_numbers"], now),
            )

    # ------------------------------------------------------------------
    # Read API used by the web layer
    # ------------------------------------------------------------------
    def vehicles(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT r.vehicle_no, COUNT(*) AS month_count, SUM(r.payable_days) AS payable_days, "
                "SUM(r.waiting_days) AS waiting_days, "
                "SUM(CASE WHEN r.claim_numbers <> '' THEN 1 ELSE 0 END) AS month_with_claims "
                "FROM month_results r GROUP BY r.vehicle_no ORDER BY r.vehicle_no"
            ).fetchall()

    def result_rows(self, vehicle_no: str, month: str | None = None,
                    year: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT month, payable_days, waiting_days, claim_numbers, computed_at "
               "FROM month_results WHERE vehicle_no=?")
        params: list = [vehicle_no]
        if month:
            sql += " AND month=?"
            params.append(month)
        if year:
            sql += " AND month LIKE ?"
            params.append(f"{year:04d}-%")
        sql += " ORDER BY month"
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def detail(self, vehicle_no: str) -> dict:
        with self.connect() as conn:
            contracts = conn.execute(
                "SELECT id, contract_no, vehicle_no, start_date, end_date, voided_at, source_batch "
                "FROM contracts WHERE vehicle_no=? ORDER BY start_date, id", (vehicle_no,)
            ).fetchall()
            claim_rows = conn.execute(
                "SELECT id, claim_no, vehicle_no, fault_date, source_batch FROM claims "
                "WHERE vehicle_no=? ORDER BY fault_date, claim_no", (vehicle_no,)
            ).fetchall()
            parts = self._stored_parts(conn, vehicle_no) or []
        included = set()
        with self.connect() as conn:
            for row in conn.execute(
                "SELECT claim_numbers FROM month_results WHERE vehicle_no=?", (vehicle_no,)
            ):
                included.update(_split_claims(row["claim_numbers"]))
        claims = [dict(c) for c in claim_rows]
        for c in claims:
            c["included"] = c["claim_no"] in included
        part_views = [
            {
                "start": p.start, "end": p.end, "waiting_days": p.waiting_days,
                "effective_start": p.effective_start,
                "payable": max(0, (p.end - p.effective_start).days),
            }
            for p in parts
        ]
        return {"contracts": contracts, "claims": claims, "parts": part_views}

    def claim_month(self, vehicle_no: str, claim_no: str, month: str) -> str | None:
        for row in self.result_rows(vehicle_no, month):
            if claim_no in _split_claims(row["claim_numbers"]):
                return month
        return None

    def batches(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()

    def snapshot(self, snapshot_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()

    def snapshots(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT s.id, s.batch_id, s.kind, s.label, s.created_at, b.mode "
                "FROM snapshots s LEFT JOIN batches b ON b.id = s.batch_id "
                "ORDER BY s.id DESC"
            ).fetchall()

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------
    def export_month_csv(self, month: str) -> str:
        return _results_csv(self.result_rows_for_month(month))

    def export_all_csv(self) -> str:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT vehicle_no, month, payable_days, waiting_days, claim_numbers, computed_at "
                "FROM month_results ORDER BY vehicle_no, month"
            ).fetchall()
        return _results_csv(rows)

    def result_rows_for_month(self, month: str) -> list[sqlite3.Row]:
        """Extraction for one natural month, only vehicles with a result."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT vehicle_no, month, payable_days, waiting_days, claim_numbers, computed_at "
                "FROM month_results WHERE month=? ORDER BY vehicle_no", (month,)
            ).fetchall()

    def known_months(self) -> list[str]:
        with self.connect() as conn:
            return [r["month"] for r in conn.execute(
                "SELECT DISTINCT month FROM month_results ORDER BY month DESC")]


    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    @staticmethod
    def _check_contract_rows(rows: list[dict]) -> None:
        if not rows:
            raise ValidationError("没有可导入的合同记录")
        for row in rows:
            if not row["contract_no"] or not row["vehicle_no"]:
                raise ValidationError("合同号与车号不能为空")
            if row["start_date"] is None or row["end_date"] is None:
                raise ValidationError("起日/止日格式应为 YYYY-MM-DD")
            if row["end_date"] <= row["start_date"]:
                raise ValidationError("止日必须晚于起日（止日当日不计入保障）")

    @staticmethod
    def _check_claim_rows(rows: list[dict]) -> None:
        if not rows:
            raise ValidationError("没有可导入的索赔记录")
        for row in rows:
            if not row["claim_no"] or not row["vehicle_no"]:
                raise ValidationError("索赔号与车号不能为空")
            if row["fault_date"] and not isinstance(row["fault_date"], date):
                raise ValidationError("故障日格式应为 YYYY-MM-DD")


def _parse_csv(text: str, headers: tuple[str, ...], keep_fault_date: bool) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValidationError("CSV 为空或没有表头")
    normalised = {_norm(h): h for h in reader.fieldnames if h}
    expected = [_norm(h) for h in headers]
    missing = [h for h in expected if h not in normalised]
    if missing:
        raise ValidationError(f"CSV 缺少列: {', '.join(missing)}")
    out = []
    for offset, raw in enumerate(reader, start=2):
        row = {norm: (raw.get(orig) or "").strip() for norm, orig in normalised.items()}
        record = {
            "contract_no": row.get("合同号", ""),
            "claim_no": row.get("索赔号", ""),
            "vehicle_no": row.get("车号", ""),
            "start_date": _parse_date(row.get("起日", "")),
            "end_date": _parse_date(row.get("止日", "")),
            "fault_date": _parse_date(row.get("故障日", "")) if keep_fault_date else "",
            "line": offset,
        }
        if not record["vehicle_no"] and not record["contract_no"] and not record["claim_no"]:
            continue  # blank line
        out.append(record)
    return out


def _norm(header: str) -> str:
    return (header or "").strip().lower()


def _parse_date(text: str | None) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _spans_signature(spans) -> tuple:
    return tuple((s.start, s.end, s.waiting_days) for s in spans)


def _split_claims(value: str | None) -> list[str]:
    return [c for c in (value or "").split(",") if c]


def _month_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _results_csv(rows) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["车号", "月份", "可赔天数", "等待期扣除天数", "计入索赔号", "计算时间"])
    for row in rows:
        writer.writerow([
            row["vehicle_no"], row["month"], row["payable_days"], row["waiting_days"],
            row["claim_numbers"], row["computed_at"],
        ])
    return buffer.getvalue()
