"""수집 상태 점검 — 로그가 제대로 쌓이고 있는지 본다.

수집이 멈추면 예외도 실패 응답도 없다. 그 조용함은 '오늘 사용자가 없었다'와
구분되지 않으므로 따로 봐야 한다. 마트가 있어야만 점검이 되면 적재 정지 자체를
놓치기 때문에 1단계는 적재 배치 없이 돈다.

    1단계  landing 파일 : 마지막 수집 시각 · 물량 변화 · 스키마 버전 · DLQ
    2단계  마트        : 중복 · seq gap · 시계 이상 (build_marts 이후)

    python -m scripts.dq_check              # 둘 다
    python -m scripts.dq_check --landing    # 1단계만

임계치를 넘으면 사유를 한 줄로 찍고 exit 1 — cron/systemd timer 에 그대로 건다.
로그는 앱이 도는 host 에 쌓이므로 CI 스케줄로는 점검할 수 없다.

    # crontab -e  — 매시 정각
    0 * * * * cd /path/to/seoul-bus-log && .venv/bin/python -m scripts.dq_check --landing

    # 하루 한 번은 적재까지 하고 전체 점검
    30 4 * * * cd /path/to/seoul-bus-log && .venv/bin/python -m scripts.build_marts \
               && .venv/bin/python -m scripts.dq_check
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from app.config import DATA_DIR, EVENT_DIR, EVENT_SCHEMA_VERSION, KST

ANALYTICS_DB = DATA_DIR / "analytics.db"

# '이 값을 넘으면 사람이 봐야 한다'는 선. 0 으로 두면 매일 울리고 아무도 안 본다.
THRESHOLDS = {
    "stale_hours": 24,         # 이 시간 넘게 새 이벤트가 없으면 수집이 끊겼을 수 있다
    "volume_drop": 0.5,        # 전일 대비 이 비율 미만이면 물량이 급감한 것
    "dlq_rate": 0.005,         # 0.5% 초과 → 클라이언트와 서버의 스키마가 어긋났다
    "duplicate_rate": 0.01,    # 1% 초과 → 같은 이벤트가 반복 도착한다
    "seq_gap_rate": 0.02,      # 2% 초과 → 유실 또는 클라이언트 드롭
    "ts_suspect_rate": 0.01,   # 1% 초과 → 시계가 어긋난 클라이언트가 많다
}


def _rows(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _kst_day(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def check_landing(now: datetime | None = None) -> list[tuple]:
    """landing 파일만 보고 판단한다 — 품질이 아니라 '들어오고 있나'."""
    now = now or datetime.now(KST)
    files = sorted(EVENT_DIR.glob("events-*.jsonl"))
    out: list[tuple] = []

    if not files:
        return [("수집 파일", 0, 0, "❌", "events-*.jsonl 이 하나도 없습니다 — 한 번도 수집되지 않았거나 경로가 다릅니다")]

    # 마지막 이벤트 시각은 mtime 이 아니라 ingest_ts 로 본다(건드리기만 해도 mtime 은 오른다).
    latest = max((r.get("ingest_ts") or "") for r in _rows(files[-1]))
    last_dt = datetime.fromisoformat(latest.replace("Z", "+00:00")).astimezone(KST) if latest else None
    hours = (now - last_dt).total_seconds() / 3600 if last_dt else 1e9
    out.append(("마지막 수집", round(hours, 1), THRESHOLDS["stale_hours"],
                "❌" if hours > THRESHOLDS["stale_hours"] else "✓",
                f"{last_dt:%Y-%m-%d %H:%M} KST" if last_dt else "이벤트 없음"))

    # 날짜별 물량 — 전일 대비 급감
    per_day: dict[str, int] = {}
    versions: dict[str, int] = {}
    for f in files:
        for r in _rows(f):
            day = _kst_day(r.get("ingest_ts"))
            if day:
                per_day[day] = per_day.get(day, 0) + 1
            versions[r.get("schema_version") or "(없음)"] = versions.get(r.get("schema_version") or "(없음)", 0) + 1

    days = sorted(per_day)
    if len(days) >= 2:
        today, yesterday = per_day[days[-1]], per_day[days[-2]]
        ratio = today / max(1, yesterday)
        out.append(("전일 대비 물량", round(ratio, 2), THRESHOLDS["volume_drop"],
                    "❌" if ratio < THRESHOLDS["volume_drop"] else "✓",
                    f"{days[-1]} {today}건 vs {days[-2]} {yesterday}건"))
    else:
        out.append(("전일 대비 물량", None, None, "—", "비교할 이전 날짜가 없습니다"))

    # 스키마 버전 — 배포가 섞이면 지표의 분모가 조용히 달라진다
    unknown = {v: n for v, n in versions.items() if v > EVENT_SCHEMA_VERSION}
    out.append(("스키마 버전", len(versions), None, "❌" if unknown else "✓",
                " · ".join(f"{v} {n}건" for v, n in sorted(versions.items()))
                + (f"  ← 서버({EVENT_SCHEMA_VERSION})보다 높은 버전이 있습니다" if unknown else "")))

    # 거부율 — 스키마가 어긋나면 여기부터 오른다
    total = sum(per_day.values())
    dlq = sum(1 for f in EVENT_DIR.glob("dlq-*.jsonl") for _ in _rows(f))
    rate = dlq / max(1, total + dlq)
    out.append(("거부율", round(rate, 4), THRESHOLDS["dlq_rate"],
                "❌" if rate > THRESHOLDS["dlq_rate"] else "✓", f"DLQ {dlq}행 / 전체 {total + dlq}건"))
    return out


def check_marts() -> list[tuple]:
    """적재된 데이터의 품질. build_marts 이후에만 의미가 있다."""
    if not ANALYTICS_DB.exists():
        return [("마트", None, None, "—", f"{ANALYTICS_DB.name} 없음 — python -m scripts.build_marts 를 먼저")]

    conn = sqlite3.connect(ANALYTICS_DB)
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    events = one("SELECT COUNT(*) FROM clean_events")
    if not events:
        conn.close()
        return [("마트", 0, None, "—", "적재된 이벤트가 없습니다")]

    read = one("SELECT COALESCE(SUM(read_rows), 0) FROM ingest_runs")
    dups = one("SELECT COALESCE(SUM(duplicates), 0) FROM ingest_runs")
    gap = one("SELECT COALESCE(SUM(seq_gap), 0) FROM fact_session WHERE seq_trustworthy = 1")
    gap_ev = one("SELECT COALESCE(SUM(events_all), 0) FROM fact_session WHERE seq_trustworthy = 1")
    drop = one("SELECT COALESCE(SUM(client_dropped), 0) FROM fact_session")
    suspect = one("SELECT COALESCE(SUM(ts_suspect), 0) FROM clean_events")
    dup_sid = one("""SELECT COUNT(*) FROM (SELECT search_id FROM clean_events
                     WHERE event_name='search.query' AND search_id IS NOT NULL
                     GROUP BY search_id HAVING COUNT(*) > 1)""")
    conn.close()

    def row(name, value, limit, note):
        return (name, round(value, 4), limit, "❌" if limit is not None and value > limit else "✓", note)

    return [
        ("적재 이벤트", events, None, "✓", f"읽은 줄 {read}"),
        row("중복 도착률", dups / max(1, read), THRESHOLDS["duplicate_rate"], f"재도착 {dups}건"),
        row("seq gap 비율", gap / max(1, gap_ev), THRESHOLDS["seq_gap_rate"],
            f"빠진 seq {gap}건 · 그중 클라이언트가 버린 것 {drop}건"),
        row("시계 이상 비율", suspect / max(1, events), THRESHOLDS["ts_suspect_rate"], f"{suspect}건"),
        ("search_id 중복", dup_sid, 0, "❌" if dup_sid else "✓", "같은 ID 로 검색이 두 번 — 수집 쪽 결함"),
    ]


def report(landing_only: bool = False) -> bool:
    failed = False
    print(f"[dq_check] {datetime.now(KST):%Y-%m-%d %H:%M} KST\n")
    print("── 수집되고 있는가 (landing) ─────────────")
    sections = [check_landing()] if landing_only else [check_landing(), check_marts()]
    for i, rows in enumerate(sections):
        if i == 1:
            print("\n── 제대로 쌓였는가 (mart) ───────────────")
        for name, value, limit, mark, note in rows:
            failed = failed or mark == "❌"
            shown = "—" if value is None else f"{value:>10}"
            cap = f"(한도 {limit})" if limit is not None else ""
            print(f"  {name:14} {shown} {cap:12} {mark}  {note}")
    return failed


if __name__ == "__main__":
    bad = report(landing_only="--landing" in sys.argv)
    if bad:
        print("\n  수집에 사람이 봐야 할 변화가 있습니다.")
        sys.exit(1)
    print("\n  이상 없음.")
