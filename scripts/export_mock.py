"""실데이터 캐시 → 목업 고정 데이터(app/mock_data.json).

정적 마스터(노선·정류장 순서)는 변하지 않으므로 한 번 받아두면 키 없이 다시 써도
거짓이 아니다 — 그래서 키가 없는 사람도 합성이 아닌 진짜 노선도를 본다.
굳히는 것은 정적뿐이고 버스 위치·도착 시각은 목업이 계속 합성한다.

    python -m scripts.preload          # 실데이터로 마스터를 채우고
    python -m scripts.export_mock      # 파일로 굳힌다
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import DATA_DIR, PRELOAD_ROUTES

ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = DATA_DIR / "bus_cache.db"          # 읽기만 한다
OUT = ROOT / "app" / "mock_data.json"


def export(targets: list[str] | None = None) -> dict:
    targets = targets or PRELOAD_ROUTES
    if not LIVE_DB.exists():
        raise SystemExit(f"[export_mock] 실데이터 캐시가 없습니다: {LIVE_DB}\n"
                         f"  먼저 python -m scripts.preload 를 실행하세요.")

    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    routes, missing = [], []

    for number in targets:
        row = conn.execute("SELECT * FROM routes WHERE no = ?", (number,)).fetchone()
        if row is None:
            missing.append(number)
            continue
        stops = conn.execute(
            """SELECT seq, station_id, ars, name, direction, is_turn
               FROM route_stations WHERE route_id = ? ORDER BY seq""",
            (row["route_id"],)).fetchall()
        if not stops:
            missing.append(number)
            continue
        routes.append({
            "route_id": row["route_id"], "no": row["no"], "type": row["type"],
            "from": row["from_stop"], "to": row["to_stop"],
            "first": row["first_bus"], "last": row["last_bus"],
            "interval": row["interval"], "company": row["company"],
            "stations": [
                {"seq": s["seq"], "station_id": s["station_id"], "ars": s["ars"],
                 "name": s["name"], "direction": s["direction"], "is_turn": bool(s["is_turn"])}
                for s in stops
            ],
        })
    conn.close()

    payload = {
        "_note": "실데이터에서 뽑은 정적 마스터(노선·정류장 순서). 키 없이 목업을 실제처럼 보이게 한다. "
                 "버스 위치·도착 시각은 여기 없다 — 그건 목업이 매번 합성한다.",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "routes": routes,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"routes": [r["no"] for r in routes],
            "stations": sum(len(r["stations"]) for r in routes),
            "missing": missing, "bytes": OUT.stat().st_size}


if __name__ == "__main__":
    out = export()
    print(f"[export_mock] {OUT.relative_to(ROOT)} · 노선 {out['routes']} · "
          f"정류장 {out['stations']}행 · {out['bytes']:,} bytes")
    if out["missing"]:
        print(f"  ⚠ 캐시에 없어 건너뛴 노선: {out['missing']}")
        print(f"    python -m scripts.preload {' '.join(out['missing'])}  로 채운 뒤 다시 실행하세요.")
        print("    (채우지 않으면 그 노선은 목업의 합성 데이터로 계속 동작합니다)")
