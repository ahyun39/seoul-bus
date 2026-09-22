"""raw JSONL → raw_events → clean_events → 마트.

    python -m scripts.build_marts            # 새로 들어온 파일만 (증분)
    python -m scripts.build_marts --rebuild  # 정제 규칙을 고쳤을 때 전량 다시
    python -m scripts.build_marts --report   # 빌드 후 지표까지 출력

1. 멱등 — event_id PK 라 같은 파일을 다시 돌려도 raw 가 안 는다. 중복은 세어서 보고한다.
2. clean 은 매번 raw 전량에서 다시 만든다 → 정제 규칙 수정이 지난 데이터에도 적용된다.
3. 스키마 버전 차이는 _normalize 에서 한 번만 흡수한다. 마트 쿼리는 버전을 모른다.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.collector import delay_ms, parse_ts   # 수집이 정한 시각 해석을 그대로 쓴다
from app.config import DATA_DIR, EVENT_DIR, KST

ROOT = Path(__file__).resolve().parent.parent
MARTS_SQL = ROOT / "sql" / "marts.sql"
ANALYTICS_DB = DATA_DIR / "analytics.db"

# 1.0.0 은 api.call/refresh 를 product 로 섞어 보냈다(event_type 은 1.1.0 부터 서버가 채운다).
SYSTEM_EVENTS = {"api.call", "refresh"}


def _normalize(row: dict) -> dict:
    """버전 차이를 흡수해 clean_events 한 행으로.

    1.0.0 → 1.2.0 변경분:
      intent            → entry_intent / current_intent   (최초 의도는 복원 불가 → NULL)
      clock_skew_ms     → queue_delay_ms / ingest_delay_ms (원본 시각에서 다시 계산)
      target_type/_id   → route_id / ars
      search.click(from=route_map) → nav.click            (검색 퍼널에서 분리)
      seq(페이지 로드 단위) → seq(세션 단위)               (구버전은 gap 계산 금지)
    """
    ver = row.get("schema_version") or "1.0.0"
    old = ver < "1.1.0"
    name = row["event_name"]

    route_id, ars, station_id = row.get("route_id"), row.get("ars"), row.get("station_id")
    if old and row.get("target_id"):
        # 이름 없는 식별자 → 이름 있는 자리. 이 해석 규칙이 필요한 것 자체가 폐기 이유였다.
        if row.get("target_type") == "route":
            route_id = row["target_id"]
        else:
            ars = row["target_id"]          # 클릭이 남긴 값은 언제나 ARS 였다
    if old and name == "search.click" and row.get("from") == "route_map":
        name = "nav.click"                  # 검색 결과 클릭이 아니다 — 퍼널에서 빼야 한다

    ingest = parse_ts(row.get("ingest_ts"))
    total = delay_ms(row.get("ingest_ts"), row.get("event_ts"))
    return {
        "event_id": row["event_id"],
        "event_date": ingest.astimezone(KST).strftime("%Y-%m-%d") if ingest else None,
        "event_ts": row.get("event_ts"),
        "sent_ts": row.get("sent_ts"),
        "ingest_ts": row.get("ingest_ts"),
        "schema_version": ver,
        "sdk_version": row.get("sdk_version"),
        "service": row.get("service"),
        # source 는 클라이언트의 data_source 와 이름도 값('live')도 겹친다.
        # raw 는 두고 clean 에서만 이름을 분명히 한다.
        "ingest_source": row.get("source"),
        "batch_trigger": row.get("batch_trigger"),
        "batch_attempt": row.get("batch_attempt"),
        "client_dropped": row.get("client_dropped"),
        "event_name": name,
        "event_type": row.get("event_type") or ("system" if name in SYSTEM_EVENTS else "product"),
        "session_id": row["session_id"],
        "seq": row["seq"] if isinstance(row.get("seq"), int) else None,
        "seq_trustworthy": 0 if ver < "1.2.0" else 1,
        "entry_intent": row.get("entry_intent"),
        "current_intent": row.get("current_intent") or row.get("intent"),
        "search_id": row.get("search_id"),
        "search_id_estimated": 0,
        "search_type": row.get("search_type"),
        "query": row.get("query"),
        "result_count": row.get("result_count"),
        "result_status": row.get("result_status"),
        "data_source": row.get("data_source"),
        "position": row.get("position"),
        "route_id": route_id,
        "ars": ars,
        "station_id": station_id,
        "endpoint": row.get("endpoint"),
        "status": row.get("status"),
        "error_code": row.get("error_code"),
        "cache": row.get("cache"),
        "latency_ms": row.get("latency_ms"),
        "data_age_sec": row.get("data_age_sec"),
        "trigger": row.get("trigger"),
        "visible_ms": row.get("visible_ms"),
        # from 은 예약어라 컬럼명을 바꿔 담는다
        "from_screen": row.get("from"),
        "next_mode": row.get("next_mode"),
        "control": row.get("control"),
        "value": row.get("value") if isinstance(row.get("value"), str) else None,
        "queue_delay_ms": row.get("queue_delay_ms", delay_ms(row.get("sent_ts"), row.get("event_ts"))),
        "ingest_delay_ms": row.get("ingest_delay_ms", delay_ms(row.get("ingest_ts"), row.get("sent_ts"))),
        "device": row.get("device"),
        # 시계 이상 판정은 수집 서버가 이미 했다(event_ts_suspect) — 여기서 또 하면
        # 한쪽 임계치만 바뀌었을 때 갈라진다. 값이 없는 구버전만 계산한다.
        "ts_suspect": int(row["event_ts_suspect"]) if "event_ts_suspect" in row
                      else int(total is not None and abs(total) > 86_400_000),
    }


# 검색 결과로 이어지는 이벤트들
_FOLLOWS_SEARCH = {"search.click", "route.view", "station.view"}


def _attribute_searches(rows: list[dict]) -> int:
    """search_id 가 없던 시절(1.2.0 이전) 데이터에 검색 단위를 복원한다.

    세션 안에서 seq 순으로 훑어 직전 search.query 에 이어 붙이고, 추정임을
    search_id_estimated=1 로 표시한다. nav.click·intent.select 가 나오면 끊는다 —
    안 끊으면 검색 한 번에 조회 다섯 건이 달라붙어 퍼널이 부푼다.
    """
    filled = 0
    for row in sorted(rows, key=lambda r: (r["session_id"], r["seq"] or 0)):
        name = row["event_name"]
        if name == "search.query":
            if not row["search_id"]:
                row["search_id"] = "est-" + row["event_id"][:8]
                row["search_id_estimated"] = 1
                filled += 1
            _attribute_searches.current = row["search_id"]
            _attribute_searches.estimated = row["search_id_estimated"]
        elif name in ("nav.click", "intent.select"):
            _attribute_searches.current = None          # 검색 흐름을 벗어났다
        elif name in _FOLLOWS_SEARCH and not row["search_id"]:
            cur = getattr(_attribute_searches, "current", None)
            if cur:
                row["search_id"] = cur
                row["search_id_estimated"] = 1
                filled += 1
    _attribute_searches.current = None
    return filled


def build(db_path: Path = ANALYTICS_DB, rebuild: bool = False) -> dict:
    conn = sqlite3.connect(db_path)
    conn.executescript(MARTS_SQL.read_text(encoding="utf-8"))

    known = {name: size for name, size in
             conn.execute("SELECT name, size FROM ingested_files")}

    read = inserted = broken = skipped = 0
    for path in sorted(EVENT_DIR.glob("events-*.jsonl")):
        size = path.stat().st_size
        # 크기가 같으면 이미 다 읽은 파일. PK 가 막아주긴 하지만 그러면
        # '중복 도착'과 '재적재'가 한 숫자로 섞인다.
        if not rebuild and known.get(path.name) == size:
            skipped += 1
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            read += 1
            try:
                row = json.loads(line)
                key = row["event_id"]
            except (ValueError, KeyError):
                broken += 1            # 파싱 불가 · 키 없음. 세어서 드러낸다.
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO raw_events (event_id, ingest_ts, src_file, payload) VALUES (?,?,?,?)",
                (key, row.get("ingest_ts"), path.name, line))
            inserted += cur.rowcount   # 0 이면 이미 있던 것 = 재전송으로 두 번 도착

        conn.execute("""INSERT INTO ingested_files (name, size, rows, ingested_at)
                        VALUES (?,?,?,datetime('now'))
                        ON CONFLICT(name) DO UPDATE SET
                          size=excluded.size, rows=excluded.rows, ingested_at=excluded.ingested_at""",
                     (path.name, size, path.read_text(encoding="utf-8").count("\n")))

    # ── DLQ 도 적재 — SQL 로 못 보면 스키마가 어긋난 순간을 뒤늦게 안다.
    dlq_rows = 0
    for path in sorted(EVENT_DIR.glob("dlq-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                broken += 1
                continue
            raw = row.get("raw") if isinstance(row.get("raw"), dict) else None
            received = parse_ts(row.get("received_at"))
            errors = row.get("errors") or []
            cur = conn.execute(
                """INSERT OR IGNORE INTO dlq_events
                   (received_at, event_date, event_id, event_name, session_id, reason, errors, raw, src_file)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (row.get("received_at"),
                 received.astimezone(KST).strftime("%Y-%m-%d") if received else "unknown",
                 row.get("event_id"),
                 (raw or {}).get("event_name"),
                 (raw or {}).get("session_id"),
                 errors[0] if errors else "unknown",
                 json.dumps(errors, ensure_ascii=False),
                 json.dumps(raw, ensure_ascii=False) if raw else None,
                 path.name))
            dlq_rows += cur.rowcount

    # ponytail: clean 은 매번 raw 전량에서 재생성한다. 규칙 수정이 소급 적용되는 대신
    # raw 크기에 선형으로 느려진다 — 수천만 건이 되면 event_date 파티션 단위로 바꿀 것.
    cols = list(_normalize({"event_id": "x", "event_name": "refresh", "session_id": "s"}).keys())
    rows = [_normalize(json.loads(p)) for (p,) in conn.execute("SELECT payload FROM raw_events")]
    _attribute_searches(rows)
    conn.execute("DELETE FROM clean_events")      # raw 에서 매번 다시 만든다
    conn.executemany(
        f"INSERT INTO clean_events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [tuple(r[c] for c in cols) for r in rows])
    conn.commit()

    total_raw = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
    out = {"read": read, "new": inserted, "duplicates": read - inserted - broken,
           "unparsable": broken, "skipped_files": skipped, "dlq_new": dlq_rows,
           "raw_total": total_raw, "clean": len(rows)}
    # 실행 이력. 중복 도착률은 적재 순간에만 알 수 있고 테이블에 흔적이 안 남는다.
    conn.execute("""INSERT INTO ingest_runs (run_at, read_rows, new_rows, duplicates, unparsable)
                    VALUES (datetime('now'), ?, ?, ?, ?)""",
                 (out["read"], out["new"], out["duplicates"], out["unparsable"]))
    conn.commit()
    conn.close()
    return out


def report(db_path: Path = ANALYTICS_DB) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = lambda sql: [dict(r) for r in conn.execute(sql)]

    print("\n── 검색 퍼널 ─────────────────────────────")
    # 검색 단위로 센다. search_id 가 중복되면 SUM() 이 같은 클릭을 여러 번 세어
    # 클릭률이 100% 를 넘는다(실제로 발생).
    f = q("""SELECT COUNT(DISTINCT search_id) n,
                    COUNT(DISTINCT CASE WHEN clicked = 1 THEN search_id END) clicked,
                    COUNT(DISTINCT CASE WHEN clicked_first = 1 THEN search_id END) first_,
                    COUNT(DISTINCT CASE WHEN views > 0 THEN search_id END) views
             FROM fact_search""")[0]
    if f["n"]:
        pct = lambda x: f"{100.0 * (x or 0) / f['n']:5.1f}%"
        print(f"  search.query   {f['n']:4}")
        print(f"  search.click   {f['clicked'] or 0:4}  {pct(f['clicked'])}")
        print(f"  1위 결과 클릭   {f['first_'] or 0:4}  {pct(f['first_'])}")
        print(f"  조회 도달      {f['views'] or 0:4}  {pct(f['views'])}")

    print("\n── 검색 결과 상태 ─────────────────────────")
    for r in q("""SELECT COALESCE(result_status,'(미기록)') s, COUNT(*) n,
                         ROUND(100.0*COUNT(*)/(SELECT COUNT(*) FROM fact_search),1) pct
                  FROM fact_search GROUP BY 1 ORDER BY n DESC"""):
        print(f"  {r['s']:12} {r['n']:4}  {r['pct']:5}%")

    print("\n── 의도별 세션 ───────────────────────────")
    for r in q("""SELECT COALESCE(entry_intent,'(미선택)') i, COUNT(*) n,
                         ROUND(AVG(duration_sec)) dur, ROUND(AVG(searches),1) s, ROUND(AVG(views),1) v
                  FROM fact_session GROUP BY 1 ORDER BY n DESC"""):
        print(f"  {r['i']:12} 세션 {r['n']:3}  평균 {r['dur']:4}초  검색 {r['s']}  조회 {r['v']}")

    print("\n── 외부 호출 ─────────────────────────────")
    for r in q("SELECT * FROM fact_api_call ORDER BY event_date, calls DESC"):
        print(f"  {r['event_date']} {r['endpoint']:18} {r['cache']:5} {r['calls']:3}회  "
              f"지연 {r['min_latency_ms']:4}~{r['max_latency_ms']:4}ms (평균 {r['avg_latency_ms']:4})  "
              f"오류 {r['errors']}  신선도불명 {r['unknown_freshness']}")

    print("\n── 수집 파이프라인 ───────────────────────")
    for r in q("SELECT * FROM fact_pipeline_health ORDER BY event_date"):
        print(f"  {r['event_date']}  이벤트 {r['events']:5}  세션 {r['sessions']:3}  "
              f"큐대기 평균 {r['avg_queue_delay_ms']}ms(최대 {r['max_queue_delay_ms']})  "
              f"전송 평균 {r['avg_ingest_delay_ms']}ms  시계이상 {r['ts_suspect']}")

    rej = q("""SELECT event_name, reason, rejected, replayable FROM fact_rejection
               ORDER BY rejected DESC LIMIT 5""")
    if rej:
        print("\n── 거부된 이벤트 ─────────────────────────")
        for r in rej:
            print(f"  {r['event_name']:16} {r['reason'][:38]:40} {r['rejected']:3}건 (재처리 가능 {r['replayable']})")

    batches = q("""SELECT trigger, COUNT(*) n, SUM(events) ev, MAX(attempt) max_attempt
                   FROM fact_batch WHERE trigger IS NOT NULL GROUP BY trigger ORDER BY n DESC""")
    if batches:
        print("\n── 전송 단위 ─────────────────────────────")
        for r in batches:
            print(f"  {r['trigger']:10} {r['n']:4}회 · 이벤트 {r['ev']:5}건 · 최대 시도 {r['max_attempt']}")

    print("\n── 데이터 품질 ───────────────────────────")
    dup = q("""SELECT COUNT(*) n FROM (SELECT search_id FROM clean_events
                WHERE event_name = 'search.query' AND search_id IS NOT NULL
                GROUP BY search_id HAVING COUNT(*) > 1)""")[0]["n"]
    est = q("SELECT COUNT(*) n FROM clean_events WHERE search_id_estimated = 1")[0]["n"]
    susp = q("SELECT COUNT(*) n FROM clean_events WHERE ts_suspect = 1")[0]["n"]
    print(f"  search_id 중복(검색 2건이 같은 ID)  {dup}건" + ("  ← 수집 쪽 결함" if dup else ""))
    print(f"  search_id 추정으로 채운 이벤트       {est}건 (스키마 1.2.0 이전 데이터)")
    print(f"  클라이언트 시계 이상                {susp}건")

    gaps = q("""SELECT COUNT(*) n, SUM(seq_gap) g FROM fact_session WHERE seq_trustworthy = 1""")[0]
    untrusted = q("SELECT COUNT(*) n FROM fact_session WHERE seq_trustworthy = 0")[0]["n"]
    print(f"\n── 유실 추정 ─────────────────────────────")
    print(f"  gap 계산 가능 세션 {gaps['n']}개 · 빠진 seq {gaps['g'] or 0}건")
    if untrusted:
        print(f"  스키마 1.2.0 이전이라 gap 을 셀 수 없는 세션 {untrusted}개 (seq 가 페이지 로드 단위였음)")
    conn.close()


if __name__ == "__main__":
    out = build(rebuild="--rebuild" in sys.argv)
    print(f"[build_marts] 읽음 {out['read']} · 신규 {out['new']} · 재도착 {out['duplicates']} · "
          f"파싱불가 {out['unparsable']} · 건너뛴 파일 {out['skipped_files']} "
          f"→ raw {out['raw_total']} · clean {out['clean']} · DLQ {out['dlq_new']}")
    if "--report" in sys.argv:
        report()
