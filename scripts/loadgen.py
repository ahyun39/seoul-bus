"""부하 생성 — 파이프라인이 어느 규모에서 어떻게 동작하는지 재현 가능하게 측정한다.

여기서 나오는 숫자는 '처리 성능'이지 '사용자 행동'이 아니다(합성 데이터).
수집 경로(collector.accept)와 적재 경로(build_marts.build)를 그대로 통과시키므로
검증·부분 수용·증분 적재·마트 생성이 전부 실제 코드로 실행된다.

    python -m scripts.loadgen              # 10만 건
    python -m scripts.loadgen 1000000      # 100만 건
    python -m scripts.loadgen 50000 --keep # 결과 디렉터리 유지

기본은 임시 디렉터리에서 돌고 지운다 — 합성 데이터가 실측 지표에 섞이지 않게.
"""

from __future__ import annotations

import random
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import collector
from scripts import build_marts

BATCH = 50          # SDK 의 MAX_BATCH 와 같은 크기로 보낸다
EVENTS = ["search.query", "search.click", "route.view", "station.view", "api.call", "refresh"]


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def run(n: int, keep: bool = False) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="busload-"))
    collector.EVENT_DIR = tmp
    collector._counts["events"] = 0
    build_marts.EVENT_DIR = tmp
    db = tmp / "analytics.db"
    rng = random.Random(42)                      # 같은 입력 → 같은 결과
    base = datetime(2026, 9, 10, tzinfo=timezone.utc)

    t0 = time.perf_counter()
    batch, session, seq = [], "s-0", 0
    for i in range(n):
        if i % 40 == 0:                          # 세션당 40 이벤트
            session, seq = f"s-{i // 40}", 0
        seq += 1
        ts = base + timedelta(seconds=i * 3)
        batch.append({
            "event_id": f"e{i:08d}", "event_name": rng.choice(EVENTS),
            "event_ts": _iso(ts), "session_id": session, "seq": seq,
            "device": "pc", "screen_w": 1512,
            "entry_intent": "where_bus", "current_intent": "where_bus",
            "search_id": f"sq-{i // 4}", "search_type": "route", "query": "4312",
            "result_count": 1, "result_status": "success", "data_source": "cache",
            "latency_ms": rng.randint(1, 300),
        })
        if len(batch) == BATCH:
            collector.accept({"write_key": "wk_seoul_bus_demo", "schema_version": "1.3.0",
                              "sdk_version": "0.1.0", "trigger": "count", "attempt": 1,
                              "dropped_count": 0, "sent_ts": _iso(ts + timedelta(seconds=2)),
                              "events": batch}, "203.0.113.10")
            batch = []
    collect_sec = time.perf_counter() - t0
    landing = sum(f.stat().st_size for f in tmp.glob("events-*.jsonl"))

    t0 = time.perf_counter()
    out = build_marts.build(db)
    build_sec = time.perf_counter() - t0

    conn = sqlite3.connect(db)
    queries = {}
    for name, sql in [
        ("fact_search", "SELECT COUNT(DISTINCT search_id), SUM(clicked) FROM fact_search"),
        ("fact_session", "SELECT COUNT(*), SUM(seq_gap) FROM fact_session"),
        ("fact_pipeline_health", "SELECT COUNT(*) FROM fact_pipeline_health"),
    ]:
        t = time.perf_counter()
        conn.execute(sql).fetchone()
        queries[name] = (time.perf_counter() - t) * 1000
    conn.close()

    result = {
        "events": out["clean"], "collect_sec": collect_sec, "build_sec": build_sec,
        "landing_mb": landing / 1e6, "db_mb": db.stat().st_size / 1e6,
        "queries_ms": queries, "dir": str(tmp),
    }
    if not keep:
        shutil.rmtree(tmp, ignore_errors=True)
        result["dir"] = None
    return result


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    n = int(args[0]) if args else 100_000
    r = run(n, keep="--keep" in sys.argv)
    print(f"[loadgen] 합성 이벤트 {r['events']:,}건 — 이 숫자는 파이프라인 성능이지 사용자 행동이 아닙니다\n")
    print(f"  수집 (검증 + landing)   {r['collect_sec']:6.1f}초   {r['events']/r['collect_sec']:>10,.0f} 건/초")
    print(f"  적재 (raw → clean → mart) {r['build_sec']:6.1f}초   {r['events']/r['build_sec']:>10,.0f} 건/초")
    print(f"  landing 파일             {r['landing_mb']:6.1f}MB")
    print(f"  analytics.db             {r['db_mb']:6.1f}MB   (landing 대비 {r['db_mb']/r['landing_mb']:.1f}배)")
    print("  마트 조회")
    for name, ms in r["queries_ms"].items():
        print(f"    {name:22} {ms:7.0f}ms")
    if r["dir"]:
        print(f"\n  결과 디렉터리: {r['dir']}")
