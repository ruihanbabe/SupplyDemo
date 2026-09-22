"""Authorised live check against the real distributor APIs.

Its own command rather than part of any test suite. `make test` must not depend on
whether the developer holds credentials, and must not spend a daily quota to assert a
parser; D14's fourth layer is explicitly the one that touches real external systems and
needs a person to say go.

Run with: make probe-suppliers MPN=IRFZ44NPBF
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

from infrastructure.database import ROOT, database_url
from persistence.procurement import ProcurementRepository
from tools.catalog_tools import ToolContext
from tools.sourcing_tools import PROVIDERS, compare_supplier_offers


def _load_supplier_credentials() -> list[str]:
    """Lift supplier variables out of .env into this process.

    Only supplier ones: the child gets a filtered environment either way, but there is
    no reason for this process to import credentials it will never pass on.
    """
    loaded = []
    for key, value in dotenv_values(ROOT / ".env").items():
        if value and key.startswith("SUPPLYAGENT_SUPPLIER_"):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对真实分销商 API 跑一次只读查询")
    parser.add_argument("--mpn", default="IRFZ44NPBF")
    parser.add_argument("--keep", action="store_true",
                        help="保留本次写入的 Run 与证据；默认回滚，不污染数据库")
    args = parser.parse_args(argv)

    loaded = _load_supplier_credentials()
    print(f"凭据变量 {len(loaded)} 个已载入（值不打印）")

    engine = create_engine(database_url(), hide_parameters=True)
    with engine.connect() as connection:
        transaction = connection.begin()
        run_id = uuid4()
        connection.execute(
            text("INSERT INTO run(run_id, trigger_kind, state) VALUES (:r,'user','analyzing')"),
            {"r": run_id})
        context = ToolContext(repository=ProcurementRepository(connection), run_id=run_id)
        started = time.monotonic()
        outcome = compare_supplier_offers({"mpn": args.mpn}, context)
        elapsed = time.monotonic() - started
        content = outcome.content or {}

        print(f"\n{args.mpn}  →  {outcome.status}  provenance={outcome.provenance}  "
              f"{elapsed:.1f}s")
        if outcome.message:
            print(f"  {outcome.message}")
        for row in content.get("rows", []):
            if row["status"] in ("ok", "partial"):
                print(f"\n  {row['provider']:10} {row['status']:7} {row['provenance']}")
                print(f"    SKU {row['distributor_sku']}   匹配 {row['match_status']}")
                print(f"    库存 {row['stock_qty']}   交期 {row['lead_time_days']} 天   "
                      f"单价 {row['unit_price_at_moq']} {row['price_currency'] or ''}   "
                      f"MOQ {row['moq']}")
                print(f"    生命周期 {row.get('lifecycle_status')}")
                print(f"    来源原文 {row.get('raw')}")
            else:
                print(f"\n  {row['provider']:10} {row['status']:7} "
                      f"{row.get('error_code','')}  {row.get('message','')}")
        print(f"\n  库存分歧 {content.get('stock_disagreement')}   "
              f"币种 {content.get('currencies')}   可比价 {content.get('prices_comparable')}")
        evidence = connection.scalar(
            text("SELECT count(*) FROM evidence WHERE run_id = :r"), {"r": run_id})
        print(f"  写入证据 {evidence} 条")

        if args.keep:
            transaction.commit()
            print(f"  已保留，run_id = {run_id}")
        else:
            transaction.rollback()
            print("  已回滚（加 --keep 可保留）")
    missing = [name for name in PROVIDERS
               if all(row["provider"] != name for row in content.get("rows", []))]
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
