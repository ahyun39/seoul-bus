"""이벤트 수집 — 브라우저 행동 로그를 검증해 JSONL 로 남긴다.

실패하면 안 되는 경로라 최대한 얇게: DB 도 파이프라인도 건드리지 않고 append 만 한다.
적재는 별도 프로세스가 닫힌 시간대 파일만 읽어 처리한다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import (EVENT_DIR, EVENT_SCHEMA_VERSION, MAX_COLLECT_BYTES,
                     MAX_COLLECT_EVENTS, WRITE_KEYS)

log = logging.getLogger("collector")

_lock = threading.Lock()

# 수집 현황 카운터. 시작 시 한 번만 세고 이후엔 증가분만 더한다(stats 참고).
_counts: dict[str, int | None] = {"events": None, "dlq": 0}

# 자유 텍스트 상한. 단건이 배치 상한을 넘겨 큐를 막는 것도 함께 막는다.
MAX_TEXT_LEN = 200

# 클라이언트 시각이 이만큼 어긋나면 표시만 한다 — 버리지는 않는다.
TS_SUSPECT_MS = 24 * 60 * 60 * 1000

ALLOWED_EVENTS = {
    "intent.select",    # 첫 화면에서 무엇을 하려는지 고름
    "search.query",     # 노선/정류장 검색
    "search.click",     # 검색 결과 선택
    "route.view",       # 노선도 조회
    "station.view",     # 정류장 상세 조회
    "refresh",          # 수동/자동 갱신
    "api.call",         # 외부 API 호출 관측
    "ui.toggle",        # 방향 전환 등
    "sample.pick",      # 데모의 예시 버스·정류장 칩을 눌러 검색
    "nav.click",        # 검색을 거치지 않은 이동(노선도에서 정류장 누르기 등)
    "page.leave",       # 탭 숨김/이탈 — 세션 길이를 알 수 있는 유일한 신호
}

# 사용자 행동(product) vs 시스템 관측(system) — 같은 파일이어도 분석 축이 다르다.
# 클라이언트 값을 믿지 않고 event_name 에서 서버가 도출한다.
EVENT_TYPES = {
    "api.call": "system",
    "refresh": "system",
}

REQUIRED = ("event_id", "event_name", "event_ts", "session_id")


class Rejected(Exception):
    """배치 전체를 못 받는 경우. 개별 이벤트 불량(DLQ)과는 다르다."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def resolve_service(batch: dict) -> str:
    """write_key 로 service 를 도출한다.

    본문의 service 를 믿으면 누구나 남의 서비스명으로 로그를 밀어넣는다.
    write_key 는 비밀이 아니지만 최소한 '등록된 발신자'인지는 가른다.
    """
    key = str(batch.get("write_key") or "").strip()
    service = WRITE_KEYS.get(key)
    if service is None:
        raise Rejected(401, f"unknown write_key: {key[:12] or '(없음)'}")
    return service


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def mask_ip(ip: str | None) -> str | None:
    """마지막 옥텟 마스킹. 대역 분석은 살리고 개별 식별은 막는다."""
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    return ".".join(parts[:3] + ["0"])


def parse_ts(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # 타임존 없는 값은 UTC 로 본다. aware-naive 를 그대로 빼면 TypeError 가 나고,
    # 그게 accept() 밖으로 나가면 한 건 때문에 배치 전체가 500 으로 사라진다.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def delay_ms(later, earlier) -> int | None:
    """두 시각의 차이(ms). 하나라도 못 읽으면 None.

    0 이 아니라 None 인 이유: '지연 없음'과 '알 수 없음'이 섞이면 p95 가 끌려 내려간다.
    scripts/build_marts.py 도 이 함수를 쓴다 — 계산을 두 곳에 두면 규칙이 갈라진다.
    """
    a, b = parse_ts(later), parse_ts(earlier)
    return None if a is None or b is None else int((a - b).total_seconds() * 1000)


def validate(event: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(event, dict):
        return ["record is not an object"]
    for field in REQUIRED:
        if not event.get(field):
            errors.append(f"missing required field: {field}")
    name = event.get("event_name")
    if name and name not in ALLOWED_EVENTS:
        errors.append(f"unknown event_name: {name}")
    # 타입까지 본다. event_id 가 숫자면 dedup 이 123 과 "123" 을 다른 키로 보고,
    # seq 가 문자열이면 gap 계산이 무너진다.
    for field in ("event_id", "event_name", "session_id"):
        if field in event and not isinstance(event[field], str):
            errors.append(f"{field} must be a string")
    if "seq" in event and not isinstance(event["seq"], int):
        errors.append("seq must be an integer")
    ts = event.get("event_ts")
    if ts and parse_ts(ts) is None:
        errors.append("event_ts is not a valid timestamp")
    return errors


_last_path: Path | None = None


def _path_for(dt: datetime) -> Path:
    """시간 단위 · 워커별로 파일을 끊는다.

    시간 단위: 적재 배치가 '아직 쓰이는 중인 파일'을 읽지 않게.
    워커별: 잠금이 프로세스 안에서만 유효해 공유하면 큰 배치가 쪼개져 줄이 섞인다.
    """
    return EVENT_DIR / f"events-{dt.strftime('%Y-%m-%d-%H')}-w{os.getpid()}.jsonl"


def _mark_closed(current: Path) -> None:
    """시간대가 넘어가면 직전 파일에 .closed 표시를 남긴다.

    파일명이 지난 시간대라는 것만으로는 후속 배치가 읽어도 되는지 알 수 없다.
    """
    global _last_path
    if _last_path and _last_path != current and _last_path.exists():
        _last_path.with_suffix(".jsonl.closed").touch()
    _last_path = current


def _dlq_path(dt: datetime) -> Path:
    return EVENT_DIR / f"dlq-{dt.strftime('%Y-%m-%d')}.jsonl"


def accept(batch: dict, client_ip: str | None, source: str = "live") -> dict:
    """유효한 것만 기록하고 나머지는 사유와 함께 DLQ 로 (부분 수용).

    전체를 거부하면 멀쩡한 이벤트가 불량 이웃 때문에 재시도되다 버려진다.
    """
    events = batch.get("events") or []
    if not isinstance(events, list):
        raise Rejected(400, "events is not a list")
    if len(events) > MAX_COLLECT_EVENTS:
        # 상한이 없으면 한 요청으로 메모리를 밀어 올릴 수 있다
        raise Rejected(413, f"too many events: {len(events)} > {MAX_COLLECT_EVENTS}")

    service = resolve_service(batch)          # 본문의 service 는 쓰지 않는다
    client_version = str(batch.get("schema_version") or "")
    if client_version and client_version != EVENT_SCHEMA_VERSION:
        # 거부는 안 한다 — 버전 차이로 버리면 배포 순서 때문에 데이터가 사라진다.
        log.warning("이벤트 스키마 버전 불일치: client=%s 서버=%s", client_version, EVENT_SCHEMA_VERSION)

    received = _now()
    ingest_ts = _iso(received)
    ip = mask_ip(client_ip)
    sent_ts = batch.get("sent_ts")

    # 지연 분해. clock_skew_ms 라는 이름을 버린 이유는 이 값에 시계 차이뿐 아니라
    # 큐 대기와 네트워크 지연이 섞이기 때문이다(시계 오차만 재려면 동기화 기준이 필요).
    #   queue_delay_ms  = sent_ts - event_ts   (브라우저 큐 체류, 이벤트별)
    #   ingest_delay_ms = ingest_ts - sent_ts  (전송 + 서버 수용, 배치별)
    ingest_delay_ms = delay_ms(ingest_ts, sent_ts)

    good: list[str] = []
    bad: list[dict] = []

    for index, event in enumerate(events):
        errors = validate(event)
        if errors:
            # dict 가 아닐 수 있다. .get 을 바로 부르면 AttributeError 로 배치가 죽는다.
            bad.append({
                "index": index,
                "event_id": event.get("event_id") if isinstance(event, dict) else None,
                "errors": errors,
                # 원본을 함께 남긴다. 사유만 남기면 재처리할 데이터가 없어 에러 로그일 뿐이다.
                "raw": event,
            })
            continue
        row = dict(event)
        for field in ("query",):
            if isinstance(row.get(field), str) and len(row[field]) > MAX_TEXT_LEN:
                row[field] = row[field][:MAX_TEXT_LEN]
                row[f"{field}_truncated"] = True
        total_delay = delay_ms(ingest_ts, event.get("event_ts"))
        row.update({
            "source": source,
            # 배치 단위 사실을 각 행에 복제한다(배치 키는 session_id + sent_ts).
            # landing 에 없으면 '몇 건을 버렸나·어떤 계기로·몇 번째 시도였나'를 되짚을 수 없다.
            "batch_trigger": batch.get("trigger"),
            "batch_attempt": batch.get("attempt"),
            "client_dropped": int(batch.get("dropped_count") or 0),
            # 시계가 크게 어긋난 이벤트. 버리지 않고 표시만 한다(이 값으로 파티션하면 미래 날짜가 생긴다).
            "event_ts_suspect": bool(total_delay is not None and abs(total_delay) > TS_SUSPECT_MS),
            "service": service,                       # write_key 에서 도출한 값
            "event_type": EVENT_TYPES.get(event.get("event_name"), "product"),
            "schema_version": client_version or EVENT_SCHEMA_VERSION,
            "sdk_version": batch.get("sdk_version", ""),
            "ingest_ts": ingest_ts,
            "sent_ts": sent_ts or ingest_ts,
            "queue_delay_ms": delay_ms(sent_ts, event.get("event_ts")),
            "ingest_delay_ms": ingest_delay_ms,
            "ip_masked": ip,
        })
        good.append(json.dumps(row, ensure_ascii=False))

    with _lock:
        if good:
            path = _path_for(received)
            _mark_closed(path)
            with open(path, "a", encoding="utf-8") as fp:
                fp.write("\n".join(good) + "\n")
            _scan_once()
            _counts["events"] = (_counts["events"] or 0) + len(good)
        if bad:
            with open(_dlq_path(received), "a", encoding="utf-8") as fp:
                for item in bad:
                    fp.write(json.dumps(
                        {"received_at": ingest_ts, "reason": "schema_violation", **item},
                        ensure_ascii=False) + "\n")
            _counts["dlq"] += len(bad)

    return {
        "accepted": len(good),
        "rejected": bad,
        "dropped_reported": int(batch.get("dropped_count") or 0),
        "server_ts": ingest_ts,
        "ingest_delay_ms": ingest_delay_ms,
    }


def _scan_once() -> None:
    """첫 호출에서만 파일을 센다. 이후에는 증가분만 더한다."""
    if _counts["events"] is not None:
        return
    _counts["events"] = sum(p.read_text(encoding="utf-8").count("\n")
                            for p in EVENT_DIR.glob("events-*.jsonl"))
    _counts["dlq"] = sum(p.read_text(encoding="utf-8").count("\n")
                         for p in EVENT_DIR.glob("dlq-*.jsonl"))


def stats() -> dict:
    """수집 현황. 전수 스캔하지 않는다.

    헬스체크가 누적 이벤트 수에 비례해 느려지면 데이터가 쌓일수록 먼저 타임아웃 난다.
    정확한 총계는 배치의 일이다.
    """
    _scan_once()
    files = sorted(EVENT_DIR.glob("events-*.jsonl"))
    return {"files": len(files), "events": _counts["events"], "dlq": _counts["dlq"],
            "latest_file": files[-1].name if files else None}
