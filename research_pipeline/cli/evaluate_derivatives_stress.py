"""Evaluate the frozen BTC derivatives-stress candidate across cloud shards."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Dict, Iterable, List

from google.cloud import storage

from ..advisory import build_exit_contract
from ..candidates.derivatives_stress import (
    PRODUCT_ID,
    build_candidate_decisions,
    evaluate_candidate,
)
from ..config import CostModel, load_config


def _bq_query(sql: str, *, location: str, max_rows: int = 500_000) -> List[dict]:
    executable = shutil.which("bq")
    if not executable:
        raise RuntimeError("bq CLI is required for cross-shard candidate evaluation")
    command = [executable]
    if executable.lower().endswith(".cmd"):
        sdk_root = Path(executable).resolve().parent.parent
        bq_script = sdk_root / "bin" / "bootstrapping" / "bq.py"
        bundled_python = (
            sdk_root / "platform" / "bundledpython" / "python.exe"
        )
        if bq_script.exists() and bundled_python.exists():
            command = [
                str(bundled_python),
                "-S",
                str(bq_script),
            ]
    result = subprocess.run(
        command + [
            "query",
            f"--location={location}",
            "--use_legacy_sql=false",
            "--format=json",
            f"--max_rows={max_rows}",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"BigQuery failed ({result.returncode}): "
            f"{(result.stderr or result.stdout)[-2000:]}"
        )
    return json.loads(result.stdout or "[]")


def _table(project: str, dataset: str, name: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
    for value in (project, dataset, name):
        if not value or any(char not in allowed for char in value):
            raise ValueError(f"unsafe BigQuery identifier: {value!r}")
    return f"`{project}.{dataset}.{name}`"


def _decision_sql(project: str, dataset: str) -> str:
    om = _table(project, dataset, "order_math_external")
    features = _table(project, dataset, "features_external")
    context = _table(project, dataset, "context_events_external")
    return f"""
WITH om_dedup AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT *,
      ROW_NUMBER() OVER (
        PARTITION BY product_id, event_time_us
        ORDER BY CASE WHEN flags='ok' THEN 0 ELSE 1 END, freshness_us, id DESC
      ) AS rn
    FROM {om}
    WHERE product_id='{PRODUCT_ID}'
  )
  WHERE rn=1 AND flags='ok'
),
feature_pivot AS (
  SELECT event_time_us,
    MAX(IF(name='realized_volatility', value, NULL)) AS realized_volatility,
    MAX(IF(name='signed_trade_flow', value, NULL)) AS signed_trade_flow,
    MAX(IF(name='trade_intensity', value, NULL)) AS trade_intensity
  FROM {features}
  WHERE product_id='{PRODUCT_ID}'
  GROUP BY event_time_us
),
context_dedup AS (
  SELECT * EXCEPT(rn) FROM (
    SELECT *,
      ROW_NUMBER() OVER (
        PARTITION BY source_id, native_id, vintage
        ORDER BY availability_time_us
      ) AS rn
    FROM {context}
    WHERE source_kind='funding_oi'
  )
  WHERE rn=1
),
oi AS (
  SELECT availability_time_us,
    CAST(JSON_VALUE(payload, '$.open_interest') AS FLOAT64) AS open_interest
  FROM context_dedup
  WHERE native_id LIKE '%:open_interest:%'
),
funding AS (
  SELECT availability_time_us,
    event_time_us AS funding_event_time_us,
    CAST(JSON_VALUE(payload, '$.funding_rate') AS FLOAT64) AS funding_rate,
    CAST(JSON_VALUE(payload, '$.mark_price') AS FLOAT64) AS mark_price
  FROM context_dedup
  WHERE native_id LIKE '%:funding:%'
),
oi_latest AS (
  SELECT om.event_time_us,
    ARRAY_AGG(
      IF(oi.open_interest IS NULL, NULL,
        STRUCT(oi.availability_time_us, oi.open_interest))
      IGNORE NULLS ORDER BY oi.availability_time_us DESC LIMIT 1
    )[SAFE_OFFSET(0)] AS state
  FROM om_dedup om
  LEFT JOIN oi
    ON oi.availability_time_us <= om.event_time_us
   AND oi.availability_time_us >= om.event_time_us - 300000000
  GROUP BY om.event_time_us
),
funding_latest AS (
  SELECT om.event_time_us,
    ARRAY_AGG(
      IF(funding.funding_rate IS NULL, NULL,
        STRUCT(
          funding.availability_time_us,
          funding.funding_event_time_us,
          funding.funding_rate,
          funding.mark_price
        ))
      IGNORE NULLS ORDER BY funding.availability_time_us DESC,
        funding.funding_event_time_us DESC LIMIT 1
    )[SAFE_OFFSET(0)] AS state
  FROM om_dedup om
  LEFT JOIN funding
    ON funding.availability_time_us <= om.event_time_us
   AND funding.availability_time_us >= om.event_time_us - 7200000000
  GROUP BY om.event_time_us
)
SELECT
  om.event_time_us, om.mid, om.spread_bps, om.queue_imbalance,
  om.multilevel_depth_imbalance, om.microprice_delta_bps,
  om.depth_imbalance_10bps, om.ofi_60s, om.mlofi_5_60s,
  om.bid_replenishment_ratio, om.ask_replenishment_ratio,
  oi_latest.state.open_interest AS open_interest,
  funding_latest.state.funding_event_time_us AS funding_event_time_us,
  funding_latest.state.funding_rate AS funding_rate,
  funding_latest.state.mark_price AS mark_price,
  feature_pivot.realized_volatility,
  feature_pivot.signed_trade_flow,
  feature_pivot.trade_intensity
FROM om_dedup om
LEFT JOIN oi_latest USING(event_time_us)
LEFT JOIN funding_latest USING(event_time_us)
LEFT JOIN feature_pivot USING(event_time_us)
ORDER BY event_time_us
"""


def _chunks(values: List[int], size: int = 400) -> Iterable[List[int]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _outcome_sql(
    project: str,
    dataset: str,
    times: List[int],
) -> str:
    quotes = _table(project, dataset, "quotes_external")
    values = ",".join(str(int(value)) for value in times)
    return f"""
WITH decisions AS (
  SELECT decision_time_us FROM UNNEST([{values}]) AS decision_time_us
),
targets AS (
  SELECT decision_time_us, 'entry' AS target_name,
    decision_time_us AS target_time_us FROM decisions
  UNION ALL
  SELECT decision_time_us, '1h', decision_time_us + 3600000000 FROM decisions
  UNION ALL
  SELECT decision_time_us, '4h', decision_time_us + 14400000000 FROM decisions
),
matched AS (
  SELECT
    targets.decision_time_us,
    targets.target_name,
    quotes.best_bid,
    quotes.best_ask,
    quotes.recv_time_us,
    ROW_NUMBER() OVER (
      PARTITION BY targets.decision_time_us, targets.target_name
      ORDER BY quotes.recv_time_us DESC, quotes.id DESC
    ) AS rn
  FROM targets
  JOIN {quotes} quotes
    ON quotes.product_id='{PRODUCT_ID}'
   AND quotes.recv_time_us <= targets.target_time_us
   AND quotes.recv_time_us >= targets.target_time_us - 2000000
  WHERE quotes.best_bid IS NOT NULL AND quotes.best_ask IS NOT NULL
    AND quotes.best_bid < quotes.best_ask
)
SELECT decision_time_us,
  MAX(IF(target_name='entry' AND rn=1, best_bid, NULL)) AS entry_bid,
  MAX(IF(target_name='entry' AND rn=1, best_ask, NULL)) AS entry_ask,
  MAX(IF(target_name='1h' AND rn=1, best_bid, NULL)) AS exit_bid_1h,
  MAX(IF(target_name='1h' AND rn=1, best_ask, NULL)) AS exit_ask_1h,
  MAX(IF(target_name='4h' AND rn=1, best_bid, NULL)) AS exit_bid_4h,
  MAX(IF(target_name='4h' AND rn=1, best_ask, NULL)) AS exit_ask_4h
FROM matched
GROUP BY decision_time_us
ORDER BY decision_time_us
"""


def _net_return(
    entry_bid: float,
    entry_ask: float,
    exit_bid: float,
    exit_ask: float,
    cm: CostModel,
    sensitivity: float,
) -> float:
    mid_entry = (entry_bid + entry_ask) / 2.0
    mid_exit = (exit_bid + exit_ask) / 2.0
    gross = mid_exit / mid_entry - 1.0
    spread = -(
        (entry_ask - mid_entry) / mid_entry
        + (mid_exit - exit_bid) / mid_exit
    )
    slippage = -(2 * cm.slippage_bps * sensitivity / 10_000.0)
    fee = -(2 * cm.taker_fee_bps * sensitivity / 10_000.0)
    adverse = -cm.adverse_selection_bps * sensitivity / 10_000.0
    return gross + spread + slippage + fee + adverse


def _fetch_outcomes(
    project: str,
    dataset: str,
    location: str,
    times: List[int],
    cm: CostModel,
) -> Dict[int, Dict[str, Dict[float, float]]]:
    output: Dict[int, Dict[str, Dict[float, float]]] = {}
    for batch in _chunks(times):
        rows = _bq_query(
            _outcome_sql(project, dataset, batch),
            location=location,
            max_rows=len(batch) + 10,
        )
        for row in rows:
            t = int(row["decision_time_us"])
            entry_bid = row.get("entry_bid")
            entry_ask = row.get("entry_ask")
            if entry_bid is None or entry_ask is None:
                continue
            for horizon in ("1h", "4h"):
                exit_bid = row.get(f"exit_bid_{horizon}")
                exit_ask = row.get(f"exit_ask_{horizon}")
                if exit_bid is None or exit_ask is None:
                    continue
                output.setdefault(t, {}).setdefault(horizon, {})
                for sensitivity in (1.0, 2.0):
                    output[t][horizon][sensitivity] = _net_return(
                        float(entry_bid),
                        float(entry_ask),
                        float(exit_bid),
                        float(exit_ask),
                        cm,
                        sensitivity,
                    )
    return output


def _path_outcome_sql(
    project: str,
    dataset: str,
    decisions: List[dict],
) -> str:
    quotes = _table(project, dataset, "quotes_external")
    values = ",\n".join(
        "STRUCT("
        f"{int(item['event_time_us'])} AS decision_time_us, "
        f"{float(item['stop_price']):.12f} AS stop_price, "
        f"{float(item['target_price']):.12f} AS target_price"
        ")"
        for item in decisions
    )
    return f"""
WITH decisions AS (
  SELECT * FROM UNNEST([{values}])
),
entry_candidates AS (
  SELECT
    d.decision_time_us, q.best_bid, q.best_ask, q.recv_time_us,
    ROW_NUMBER() OVER (
      PARTITION BY d.decision_time_us
      ORDER BY q.recv_time_us DESC, q.id DESC
    ) AS rn
  FROM decisions d
  JOIN {quotes} q
    ON q.product_id='{PRODUCT_ID}'
   AND q.recv_time_us <= d.decision_time_us
   AND q.recv_time_us >= d.decision_time_us - 2000000
  WHERE q.best_bid IS NOT NULL AND q.best_ask IS NOT NULL
    AND q.best_bid < q.best_ask
),
entry_quote AS (
  SELECT * EXCEPT(rn) FROM entry_candidates WHERE rn=1
),
trigger_candidates AS (
  SELECT
    d.decision_time_us, q.best_bid, q.best_ask, q.recv_time_us,
    IF(q.best_bid <= d.stop_price, 'STOP_LOSS', 'TAKE_PROFIT') AS reason,
    ROW_NUMBER() OVER (
      PARTITION BY d.decision_time_us
      ORDER BY q.recv_time_us ASC, q.id ASC
    ) AS rn
  FROM decisions d
  JOIN {quotes} q
    ON q.product_id='{PRODUCT_ID}'
   AND q.recv_time_us > d.decision_time_us
   AND q.recv_time_us <= d.decision_time_us + 14400000000
  WHERE q.best_bid IS NOT NULL AND q.best_ask IS NOT NULL
    AND q.best_bid < q.best_ask
    AND (q.best_bid <= d.stop_price OR q.best_bid >= d.target_price)
),
trigger_quote AS (
  SELECT * EXCEPT(rn) FROM trigger_candidates WHERE rn=1
),
time_candidates AS (
  SELECT
    d.decision_time_us, q.best_bid, q.best_ask, q.recv_time_us,
    ROW_NUMBER() OVER (
      PARTITION BY d.decision_time_us
      ORDER BY q.recv_time_us DESC, q.id DESC
    ) AS rn
  FROM decisions d
  JOIN {quotes} q
    ON q.product_id='{PRODUCT_ID}'
   AND q.recv_time_us <= d.decision_time_us + 14400000000
   AND q.recv_time_us >= d.decision_time_us + 14398000000
  WHERE q.best_bid IS NOT NULL AND q.best_ask IS NOT NULL
    AND q.best_bid < q.best_ask
),
time_quote AS (
  SELECT * EXCEPT(rn) FROM time_candidates WHERE rn=1
)
SELECT
  d.decision_time_us,
  d.stop_price,
  d.target_price,
  e.best_bid AS entry_bid,
  e.best_ask AS entry_ask,
  COALESCE(t.best_bid, x.best_bid) AS exit_bid,
  COALESCE(t.best_ask, x.best_ask) AS exit_ask,
  COALESCE(t.recv_time_us, x.recv_time_us) AS exit_time_us,
  COALESCE(t.reason, 'TIME_STOP') AS reason
FROM decisions d
LEFT JOIN entry_quote e USING(decision_time_us)
LEFT JOIN trigger_quote t USING(decision_time_us)
LEFT JOIN time_quote x USING(decision_time_us)
ORDER BY d.decision_time_us
"""


def _fetch_path_outcomes(
    project: str,
    dataset: str,
    location: str,
    signals: List[dict],
    cm: CostModel,
) -> Dict[int, Dict[Any, Any]]:
    contracts = []
    for signal in signals:
        contract = build_exit_contract(signal)
        contracts.append({
            "event_time_us": int(signal["event_time_us"]),
            "stop_price": contract["stop_price"],
            "target_price": contract["target_price"],
        })
    output: Dict[int, Dict[Any, Any]] = {}
    for batch_start in range(0, len(contracts), 50):
        batch = contracts[batch_start:batch_start + 50]
        rows = _bq_query(
            _path_outcome_sql(project, dataset, batch),
            location=location,
            max_rows=len(batch) + 10,
        )
        for row in rows:
            required = ("entry_bid", "entry_ask", "exit_bid", "exit_ask")
            if any(row.get(key) is None for key in required):
                continue
            event_time_us = int(row["decision_time_us"])
            result: Dict[Any, Any] = {
                "reason": row.get("reason"),
                "exit_time_us": (
                    int(row["exit_time_us"])
                    if row.get("exit_time_us") is not None else None
                ),
                "stop_price": float(row["stop_price"]),
                "target_price": float(row["target_price"]),
            }
            for sensitivity in (1.0, 2.0):
                result[sensitivity] = _net_return(
                    float(row["entry_bid"]),
                    float(row["entry_ask"]),
                    float(row["exit_bid"]),
                    float(row["exit_ask"]),
                    cm,
                    sensitivity,
                )
            output[event_time_us] = result
    return output


def _stage_json(report: dict, stage: str) -> dict:
    try:
        return json.loads(report["stages"][stage]["stdout"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return {}


def _report_outage_ok(report: dict) -> bool:
    derive = _stage_json(report, "derive")
    try:
        return int(
            derive.get("health", {}).get("counts", {}).get("gaps", 1)
        ) == 0
    except (TypeError, ValueError):
        return False


def _report_storage_ok(report: dict, *, current: bool = False) -> bool:
    if not current:
        return report.get("status") == "UPLOADED_AND_VERIFIED"
    upload = report.get("stages", {}).get("upload", {})
    mirror = report.get("stages", {}).get("query_mirror", {})
    required_tables = {"order_math", "quotes", "context_events"}
    return (
        upload.get("returncode") == 0
        and int(mirror.get("objects_verified", 0)) > 0
        and required_tables <= set(mirror.get("tables", []))
    )


def _operational_gates(
    *,
    bucket: str | None,
    prefix: str,
    project: str,
    current_verification_passed: bool,
    current_report: dict | None = None,
) -> tuple[Dict[str, bool], dict]:
    gates = {
        "replay_parity": bool(current_verification_passed),
        "freshness": False,
        "outage": False,
        "storage": False,
    }
    evidence = {
        "reports_reviewed": 0,
        "latest_report": None,
        "current_shard_included": bool(current_report),
    }
    reports = []
    if bucket:
        client = storage.Client(project=project)
        for blob in client.list_blobs(bucket, prefix=prefix.strip("/") + "/"):
            if not blob.name.endswith("/reports/shard_result.json"):
                continue
            try:
                reports.append(json.loads(blob.download_as_text()))
            except Exception:
                continue
    reports = sorted(
        reports,
        key=lambda report: report.get("ended_at_utc", ""),
    )[-20:]
    historical_reports = list(reports)
    if current_report:
        reports.append(current_report)
    evidence["reports_reviewed"] = len(reports)
    if not reports:
        return gates, evidence
    latest = current_report or reports[-1]
    evidence["latest_report"] = {
        "shard_id": latest.get("shard_id"),
        "status": latest.get("status"),
        "ended_at_utc": latest.get("ended_at_utc")
        or datetime.now(timezone.utc).isoformat(),
    }
    try:
        freshness_time = (
            latest.get("ended_at_utc")
            or latest.get("started_at_utc")
        )
        ended = datetime.fromisoformat(
            freshness_time.replace("Z", "+00:00")
        )
        gates["freshness"] = (
            datetime.now(timezone.utc) - ended
        ).total_seconds() <= 8 * 3600
    except Exception:
        gates["freshness"] = False
    gates["storage"] = (
        all(_report_storage_ok(report) for report in historical_reports)
        and (
            _report_storage_ok(current_report, current=True)
            if current_report else bool(historical_reports)
        )
    )
    gates["outage"] = all(_report_outage_ok(report) for report in reports)
    return gates, evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="bitwise-trader")
    parser.add_argument("--dataset", default="crypto_research")
    parser.add_argument("--location", default="us-east1")
    parser.add_argument("--config", default=None)
    parser.add_argument("--bucket", default=None)
    parser.add_argument(
        "--reports-prefix", default="coinbase/BTC-USD/shards"
    )
    parser.add_argument("--current-verification-passed", action="store_true")
    parser.add_argument("--current-shard-report", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    rows = _bq_query(
        _decision_sql(args.project, args.dataset),
        location=args.location,
    )
    candidate = build_candidate_decisions(rows)
    union_times = sorted({
        int(item["event_time_us"])
        for values in (
            list(candidate["decisions"].values())
            + list(candidate.get("baselines", {}).values())
        )
        for item in values
    })
    outcomes = _fetch_outcomes(
        args.project,
        args.dataset,
        args.location,
        union_times,
        CostModel.from_config(cfg),
    ) if union_times else {}
    promotable_signals = {
        int(item["event_time_us"]): dict(item)
        for variant in ("combined_balanced_v1", "combined_strict_v1")
        for item in candidate["decisions"].get(variant, [])
    }
    path_outcomes = _fetch_path_outcomes(
        args.project,
        args.dataset,
        args.location,
        list(promotable_signals.values()),
        CostModel.from_config(cfg),
    ) if promotable_signals else {}
    current_report = None
    if args.current_shard_report:
        current_report = json.loads(
            Path(args.current_shard_report).read_text(encoding="utf-8")
        )
    operational, operational_evidence = _operational_gates(
        bucket=args.bucket,
        prefix=args.reports_prefix,
        project=args.project,
        current_verification_passed=args.current_verification_passed,
        current_report=current_report,
    )
    result = evaluate_candidate(
        candidate,
        outcomes,
        path_outcomes=path_outcomes,
        operational_gates=operational,
    )
    result["generated_at"] = int(time.time())
    result["data_backend"] = {
        "project": args.project,
        "dataset": args.dataset,
        "location": args.location,
        "decision_rows": len(rows),
        "outcome_rows": len(outcomes),
        "path_outcome_rows": len(path_outcomes),
    }
    result["operational_evidence"] = operational_evidence
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["evidence_status"] == "EVIDENCE_PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
