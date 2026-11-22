"""Flask UI for the fleet actuary: import contracts/claims and extract
monthly payable days plus included claim numbers.

Run directly:  python app.py
or via gunicorn: gunicorn -w 2 -b 0.0.0.0:8000 'app:app'
"""

from __future__ import annotations

import io
import os
from datetime import date

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from fleet_app.compute import DEFAULT_WAITING_DAYS
from fleet_app.db import FleetStore, ValidationError


PACKAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_app")


def create_app(data_dir: str | None = None, waiting_days: int | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(PACKAGE_DIR, "templates"),
        static_folder=os.path.join(PACKAGE_DIR, "static"),
    )
    app.secret_key = os.environ.get("FLEET_SECRET_KEY", "dev-secret-change-me")
    data_dir = data_dir or os.environ.get(
        "FLEET_DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    )
    db_path = os.path.join(data_dir, "fleet.sqlite3")
    days = int(os.environ.get("FLEET_WAITING_DAYS", waiting_days or DEFAULT_WAITING_DAYS))
    store = FleetStore(db_path, days)
    app.config["FLEET_STORE"] = store

    # ------------------------------------------------------------------
    def get_store() -> FleetStore:
        return app.config["FLEET_STORE"]

    def read_upload(field: str) -> str:
        uploaded = request.files.get(field)
        if not uploaded or uploaded.filename == "":
            raise ValidationError("请选择要上传的 CSV 文件")
        raw = uploaded.read()
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw.decode("gbk")

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    @app.get("/")
    def index():
        s = get_store()
        return render_template(
            "index.html",
            vehicles=s.vehicles(),
            batches=s.batches(),
            snapshots=s.snapshots(),
            waiting_days=s.waiting_days,
            today=date.today().isoformat(),
        )

    # ------------------------------------------------------------------
    # Imports
    # ------------------------------------------------------------------
    @app.post("/import/contracts")
    def upload_contracts():
        try:
            text = read_upload("file")
            rows = get_store().parse_contracts(text)
            result = get_store().import_contracts(
                rows, label=request.form.get("label") or "上传合同", mode="upload"
            )
        except (ValidationError, RuntimeError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        flash(
            f"合同导入成功：批次 #{result['batch']}，涉及 {len(result['vehicles'])} 辆车，"
            f"重算 {len(result['rebuilt'])} 辆。",
            "ok",
        )
        return redirect(url_for("index"))

    @app.post("/import/claims")
    def upload_claims():
        try:
            text = read_upload("file")
            rows = get_store().parse_claims(text)
            result = get_store().import_claims(
                rows, label=request.form.get("label") or "上传索赔", mode="upload"
            )
        except (ValidationError, RuntimeError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        flash(
            f"索赔导入成功：批次 #{result['batch']}，涉及 {len(result['vehicles'])} 辆车，"
            f"重算 {len(result['rebuilt'])} 辆。",
            "ok",
        )
        return redirect(url_for("index"))

    # ------------------------------------------------------------------
    # Corrections / void
    # ------------------------------------------------------------------
    @app.post("/contracts/<int:contract_id>/correct")
    def correct_contract(contract_id: int):
        row = {
            "contract_no": (request.form.get("contract_no") or "").strip(),
            "vehicle_no": (request.form.get("vehicle_no") or "").strip(),
            "start_date": _form_date(request.form.get("start_date")),
            "end_date": _form_date(request.form.get("end_date")),
        }
        try:
            result = get_store().correct_contract(contract_id, row)
        except (ValidationError, RuntimeError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("vehicle", vehicle_no=row["vehicle_no"] or "?"))
        flash(
            f"合同 #{contract_id} 已更正（批次 #{result['batch']}，"
            f"重算 {len(result['vehicles'])} 辆车）。",
            "ok",
        )
        return redirect(url_for("vehicle", vehicle_no=row["vehicle_no"]))

    @app.post("/contracts/<int:contract_id>/void")
    def void_contract(contract_id: int):
        back = request.form.get("vehicle_no") or "?"
        try:
            result = get_store().void_contract(contract_id)
        except (ValidationError, RuntimeError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("vehicle", vehicle_no=back))
        flash(f"合同 #{contract_id} 已撤销（批次 #{result['batch']}）。", "ok")
        return redirect(url_for("vehicle", vehicle_no=back))

    @app.post("/claims/<int:claim_id>/correct")
    def correct_claim(claim_id: int):
        row = {
            "claim_no": (request.form.get("claim_no") or "").strip(),
            "vehicle_no": (request.form.get("vehicle_no") or "").strip(),
            "fault_date": _form_date(request.form.get("fault_date")),
        }
        try:
            result = get_store().correct_claim(claim_id, row)
        except (ValidationError, RuntimeError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("vehicle", vehicle_no=row["vehicle_no"] or "?"))
        flash(
            f"索赔 #{claim_id} 已更正（批次 #{result['batch']}，"
            f"重算 {len(result['vehicles'])} 辆车）。",
            "ok",
        )
        return redirect(url_for("vehicle", vehicle_no=row["vehicle_no"]))

    # ------------------------------------------------------------------
    # Monthly extraction (natural month)
    # ------------------------------------------------------------------
    @app.get("/extract")
    def extract():
        s = get_store()
        month = request.args.get("month") or date.today().strftime("%Y-%m")
        rows = s.result_rows_for_month(month)
        totals = {
            "days": sum(r["payable_days"] for r in rows),
            "waiting": sum(r["waiting_days"] for r in rows),
            "claims": sum(len(_split_claims(r["claim_numbers"])) for r in rows),
            "vehicles": len(rows),
        }
        return render_template(
            "extract.html",
            month=month,
            rows=rows,
            totals=totals,
            months=s.known_months(),
        )

    @app.get("/extract/download")
    def download_extract():
        s = get_store()
        month = request.args.get("month") or date.today().strftime("%Y-%m")
        text = s.export_month_csv(month)
        return send_file(
            io.BytesIO(text.encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"车队可赔天数_{month}.csv",
        )

    @app.get("/download/current")
    def download_current():
        s = get_store()
        return send_file(
            io.BytesIO(s.export_all_csv().encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name="车队取数结果_全部月份.csv",
        )

    @app.get("/snapshots/<int:snapshot_id>/download")
    def download_snapshot(snapshot_id: int):
        s = get_store()
        snap = s.snapshot(snapshot_id)
        if snap is None:
            flash("该快照不存在", "error")
            return redirect(url_for("index"))
        return send_file(
            io.BytesIO(snap["payload"].encode("utf-8-sig")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"上一版取数结果_批次前_{snapshot_id}.csv",
        )

    # ------------------------------------------------------------------
    # Vehicle detail
    # ------------------------------------------------------------------
    @app.get("/vehicle/<vehicle_no>")
    def vehicle(vehicle_no: str):
        s = get_store()
        detail = s.detail(vehicle_no)
        months = s.result_rows(vehicle_no)
        return render_template(
            "vehicle.html", vehicle_no=vehicle_no, months=months, parts=detail["parts"],
            contracts=detail["contracts"], claims=detail["claims"],
        )

    @app.get("/batches")
    def batches():
        return render_template("batches.html", batches=get_store().batches())

    @app.post("/snapshots/<int:snapshot_id>/download")
    def download_snapshot_post(snapshot_id: int):
        return download_snapshot(snapshot_id)

    return app


def _form_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _split_claims(value: str) -> list[str]:
    return [c for c in (value or "").split(",") if c]


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
