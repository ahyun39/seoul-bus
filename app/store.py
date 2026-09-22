"""저장 계층 — 정적 마스터 캐시(SQLite)와 실시간 응답 캐시(메모리)."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import KST, CACHE_DB, DDL_PATH, LIVE_CACHE_MAX, LIVE_TTL_SEC

_lock = threading.Lock()
_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    """새 연결. 직접 쓰는 곳은 테스트뿐이고 앱은 db() 를 쓴다."""
    conn = sqlite3.connect(CACHE_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")   # 동시 쓰기 시 즉시 실패 대신 잠깐 기다린다
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """스레드마다 연결 하나를 재사용한다.

    sqlite3.Connection 의 `with` 는 트랜잭션만 관리하고 연결을 닫지 않는다 —
    `with connect()` 로 쓰면 쿼리마다 연결이 열리고 GC 에 맡겨진다.
    FastAPI 의 스레드풀은 크기가 제한적이라 스레드 로컬 재사용이 가장 단순하고 안 샌다.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = connect()
    yield conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """쓰기용. 커밋/롤백을 보장한다."""
    with db() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def close_thread_connection() -> None:
    """테스트나 종료 시 현재 스레드의 연결을 정리한다."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def init_db() -> None:
    close_thread_connection()          # CACHE_DB 가 바뀌었을 수 있다 (테스트)
    with tx() as conn:
        conn.executescript(Path(DDL_PATH).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 정적 마스터

def upsert_routes(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, tx() as conn:
        conn.executemany(
            """INSERT INTO routes (route_id,no,type,from_stop,to_stop,first_bus,last_bus,interval,company,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(route_id) DO UPDATE SET
                 no=excluded.no, type=excluded.type, from_stop=excluded.from_stop,
                 to_stop=excluded.to_stop, first_bus=excluded.first_bus, last_bus=excluded.last_bus,
                 interval=excluded.interval, company=excluded.company, updated_at=excluded.updated_at""",
            [(r["route_id"], r["no"], r["type"], r["from"], r["to"],
              r["first"], r["last"], r["interval"], r["company"], _now()) for r in rows if r.get("route_id")],
        )
    return len(rows)


def upsert_route_stations(route_id: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, tx() as conn:
        conn.executemany(
            """INSERT INTO route_stations (route_id,seq,station_id,ars,name,direction,is_turn,updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(route_id,seq) DO UPDATE SET
                 station_id=excluded.station_id, ars=excluded.ars, name=excluded.name,
                 direction=excluded.direction, is_turn=excluded.is_turn, updated_at=excluded.updated_at""",
            [(route_id, r["seq"], r["station_id"], r["ars"], r["name"], r["direction"],
              1 if r.get("is_turn") else 0, _now()) for r in rows],
        )
        conn.executemany(
            """INSERT INTO stations (station_id,ars,name,updated_at) VALUES (?,?,?,?)
               ON CONFLICT(station_id) DO UPDATE SET
                 ars=excluded.ars, name=excluded.name, updated_at=excluded.updated_at""",
            [(r["station_id"], r["ars"], r["name"], _now()) for r in rows if r.get("station_id")],
        )
    return len(rows)


def upsert_stations(rows: list[dict]) -> int:
    if not rows:
        return 0
    with _lock, tx() as conn:
        conn.executemany(
            """INSERT INTO stations (station_id,ars,name,updated_at) VALUES (?,?,?,?)
               ON CONFLICT(station_id) DO UPDATE SET
                 ars=excluded.ars, name=excluded.name, updated_at=excluded.updated_at""",
            [(r["station_id"], r["ars"], r["name"], _now()) for r in rows if r.get("station_id")],
        )
    return len(rows)


def search_routes(q: str, limit: int = 12) -> list[dict]:
    """노선번호 검색. 접두 일치 먼저, 부분 일치 뒤."""
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM routes
               WHERE no LIKE ? OR no LIKE ?
               ORDER BY CASE WHEN no LIKE ? THEN 0 ELSE 1 END, LENGTH(no), no
               LIMIT ?""",
            (q + "%", "%" + q + "%", q + "%", limit),
        ).fetchall()
    return [
        {"route_id": r["route_id"], "no": r["no"], "type": r["type"], "from": r["from_stop"],
         "to": r["to_stop"], "first": r["first_bus"], "last": r["last_bus"],
         "interval": r["interval"], "company": r["company"]}
        for r in rows
    ]


def get_route(route_id: str) -> dict | None:
    with db() as conn:
        r = conn.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
    if not r:
        return None
    return {"route_id": r["route_id"], "no": r["no"], "type": r["type"], "from": r["from_stop"],
            "to": r["to_stop"], "first": r["first_bus"], "last": r["last_bus"],
            "interval": r["interval"], "company": r["company"]}


def get_route_stations(route_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT seq,station_id,ars,name,direction,is_turn FROM route_stations WHERE route_id=? ORDER BY seq",
            (route_id,),
        ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        row["is_turn"] = bool(row.get("is_turn"))
        out.append(row)
    return out


def search_stations(q: str, limit: int = 15) -> list[dict]:
    """정류장 이름/ARS 검색. 방면을 함께 돌려준다.

    이름만으로는 고를 수 없다 — 같은 '가든파이브웍스동'이 ARS 24472/24473 으로
    나뉘는데 화면에는 똑같은 두 줄로 보인다. 방면은 route_stations 에서 모아 온다.
    적재된 노선이 없으면 빈 목록 — 지어내면 사용자가 반대편에서 버스를 기다린다.
    """
    with db() as conn:
        rows = conn.execute(
            """SELECT s.station_id, s.ars, s.name,
                      (SELECT GROUP_CONCAT(DISTINCT rs.direction) FROM route_stations rs
                        WHERE rs.ars = s.ars AND rs.direction IS NOT NULL AND rs.direction <> ''
                      ) AS directions
               FROM stations s
               WHERE s.name LIKE ? OR s.ars = ?
               ORDER BY CASE WHEN s.name LIKE ? THEN 0 ELSE 1 END, LENGTH(s.name), s.name
               LIMIT ?""",
            ("%" + q + "%", q, q + "%", limit),
        ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        # GROUP_CONCAT 구분자가 쉼표다. 방면 이름에 쉼표가 있으면 쪼개지지만 화면엔 나열될 뿐.
        raw = row.pop("directions") or ""
        row["directions"] = [d for d in raw.split(",") if d]
        out.append(row)
    return out


def get_station(station_id: str) -> dict | None:
    with db() as conn:
        r = conn.execute("SELECT station_id,ars,name FROM stations WHERE station_id=? OR ars=?",
                         (station_id, station_id)).fetchone()
    return dict(r) if r else None


def routes_through_station(ars: str) -> list[dict]:
    """'이 정류장을 지나는 노선' — 정적 캐시를 뒤집어 만든다. API 호출 0회.

    적재된 노선 범위 안에서만 정확하다는 한계는 응답에 함께 표시한다.
    """
    with db() as conn:
        # 한 노선이 같은 정류장을 두 번 지난다(상·하행이 한 seq 라 같은 ars 가 두 번).
        # 묶지 않으면 노선 하나가 두 줄로 세어져 '경유 노선 3개'가 실제로는 2개다.
        rows = conn.execute(
            """SELECT rs.route_id, MIN(rs.seq) AS seq, r.no, r.type, r.to_stop
               FROM route_stations rs JOIN routes r ON r.route_id = rs.route_id
               WHERE rs.ars = ? GROUP BY rs.route_id ORDER BY r.no""",
            (ars,),
        ).fetchall()
    return [{"route_id": r["route_id"], "seq": r["seq"], "no": r["no"],
             "type": r["type"], "to": r["to_stop"]} for r in rows]


# ---------------------------------------------------------------- API 사용량



def _kst_day() -> str:
    """한도는 KST 자정에 초기화된다. UTC 로 세면 오전 9시에 끊긴다."""
    return datetime.now(KST).strftime("%Y-%m-%d")


def record_api_call(endpoint: str, ok: bool = True) -> None:
    """외부 호출 1회 기록. 실패도 한도를 소모하므로 함께 센다."""
    with _lock, tx() as conn:
        conn.execute(
            """INSERT INTO api_usage (day, endpoint, calls, errors, updated_at)
               VALUES (?,?,1,?,?)
               ON CONFLICT(day, endpoint) DO UPDATE SET
                 calls = calls + 1,
                 errors = errors + excluded.errors,
                 updated_at = excluded.updated_at""",
            (_kst_day(), endpoint, 0 if ok else 1, _now()),
        )


def api_usage_today(budget: int = 1000) -> dict:
    day = _kst_day()
    with db() as conn:
        rows = conn.execute(
            "SELECT endpoint, calls, errors FROM api_usage WHERE day=? ORDER BY calls DESC",
            (day,),
        ).fetchall()
    total = sum(r["calls"] for r in rows)
    return {
        "day_kst": day,
        "total": total,
        "budget": budget,
        "remaining": max(0, budget - total),
        "used_pct": round(total / budget * 100, 1) if budget else 0.0,
        "errors": sum(r["errors"] for r in rows),
        "by_endpoint": [dict(r) for r in rows],
    }


def counts() -> dict:
    with db() as conn:
        return {
            "routes": conn.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
            "route_stations": conn.execute("SELECT COUNT(*) FROM route_stations").fetchone()[0],
            "stations": conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0],
        }


# ---------------------------------------------------------------- 실시간 캐시

class TTLCache:
    """실시간 호출용 짧은 메모리 캐시. 새로고침 연타가 외부 API 로 나가지 않게 막는다.

    주의한 두 가지:
      1. 나이는 float 로 비교한다. int 로 자르면 ttl=20 일 때 20.9초가 만료로 안 걸린다.
      2. 조회 시 만료 항목을 지우고 상한 초과분도 버린다 — 안 그러면 계속 쌓인다.
    """

    def __init__(self, ttl: int = LIVE_TTL_SEC, max_entries: int = LIVE_CACHE_MAX):
        self.ttl = ttl
        self.max_entries = max_entries
        self._data: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evicted = 0

    def get(self, key: str) -> tuple[Any, int] | None:
        """(값, 저장 후 경과 초) 또는 None."""
        now = time.time()
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                self.misses += 1
                return None
            stored_at, value = hit
            age = now - stored_at
            if age > self.ttl:                 # 만료 — 꺼내 쓰지 않고 바로 지운다
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)        # 최근 사용을 뒤로 (LRU)
            self.hits += 1
            return value, int(age)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)  # 가장 오래 안 쓴 것부터
                self.evicted += 1

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._data),
                "max_entries": self.max_entries,
                "ttl_sec": self.ttl,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
                "evicted": self.evicted,
            }


live_cache = TTLCache()
