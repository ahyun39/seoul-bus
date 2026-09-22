"""FastAPI 앱 — 화면 서빙 + 서울시 API 중계 + 로그 수집.

중계가 선택이 아닌 이유: 인증키 노출 · CORS 헤더 없음 · http 혼합 콘텐츠 차단.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import collector, mock, store
from .config import MAX_COLLECT_BYTES, STATIC_DIR, USE_MOCK, require_service_key
from .seoul_api import SeoulApiError, SeoulBusClient, data_age_seconds, normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

client = SeoulBusClient()


@asynccontextmanager
async def lifespan(_: FastAPI):
    require_service_key()       # 키가 없으면 여기서 멈춘다 — 조용히 목업으로 떨어지지 않는다
    store.init_db()
    client.on_call = store.record_api_call      # 계층을 넘지 않고 사용량만 전달
    if USE_MOCK:
        _seed_mock_master()
        log.warning("목업 모드입니다 — 화면의 모든 값이 합성 데이터입니다. 실데이터는 USE_MOCK 을 비우고 인증키를 넣으세요.")
    else:
        log.info("실데이터 모드. 정적 마스터 적재 현황: %s", store.counts())
    yield
    await client.aclose()


app = FastAPI(title="서울버스 로그 수집 데모", lifespan=lifespan)


def _seed_mock_master() -> None:
    """목업 모드도 같은 캐시 구조를 쓴다 — 코드 경로를 하나로 유지.

    건수만 보고 건너뛰지 않는다. 목업이 바뀌었는데 옛 행이 남으면 조용히 옛 종점이 나온다.
    """
    store.upsert_routes(mock.SEEDED)
    for route in mock.SEEDED:
        store.upsert_route_stations(route["route_id"], mock.route_stations(route["route_id"]))
    log.info("목업 마스터 적재 완료: %s", store.counts())


# ---------------------------------------------------------------- 실시간 조회

async def _live(kind: str, key: str, **params):
    """실시간 조회. TTL 캐시 우선, 없을 때만 외부 API.

    반환: (데이터, 관측정보) — 관측정보는 화면의 로그 패널로 간다.
    data_age_sec 는 캐시 나이가 아니라 원본 수집 시각 기준이다.
    캐시 나이로 쓰면 90초 묵은 데이터가 '0초 전'으로 표시된다.
    """
    cache_key = f"{kind}:{':'.join(f'{k}={v}' for k, v in sorted(params.items()))}"
    hit = store.live_cache.get(cache_key)
    if hit:
        value, cache_age = hit
        return value, {"endpoint": key, "latency_ms": 0, "cache": "hit",
                       "data_age_sec": data_age_seconds(value) if value else cache_age,
                       "cache_age_sec": cache_age, "status": 200}

    started = time.perf_counter()
    if USE_MOCK:
        data = mock.bus_positions(params["route_id"]) if kind == "bus_pos" else mock.arrivals(params["ars"])
        latency = int((time.perf_counter() - started) * 1000)
        obs = {"endpoint": key, "latency_ms": latency, "cache": "miss",
               "data_age_sec": 0, "status": 200, "mode": "mock"}
    else:
        try:
            if kind == "bus_pos":
                result = await client.call(key, busRouteId=params["route_id"])
            else:
                result = await client.call(key, arsId=params["ars"])
        except SeoulApiError as exc:
            log.warning("외부 API 실패: %s", exc)
            return [], {"endpoint": key, "latency_ms": int((time.perf_counter() - started) * 1000),
                        "cache": "miss", "status": 502, "error": exc.code, "message": exc.message}
        data = normalize(kind, result.items)
        obs = {"endpoint": key, "latency_ms": result.latency_ms, "cache": "miss",
               "data_age_sec": 0, "status": 200}

    store.live_cache.set(cache_key, data)
    obs["data_age_sec"] = data_age_seconds(data) if data else 0
    obs["cache_age_sec"] = 0
    return data, obs


# ---------------------------------------------------------------- 라우트

@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "mode": "mock" if USE_MOCK else "live",
        "master": await run_in_threadpool(store.counts),
        "live_cache": store.live_cache.stats(),
        "api_usage": await run_in_threadpool(store.api_usage_today),
        "collector": await run_in_threadpool(collector.stats),
    }


def turn_seq(stations: list[dict]) -> int | None:
    """상행이 끝나는 지점(회차지)의 seq. 없으면 None.

    API 는 상·하행을 따로 주지 않는다 — seq 1..N 한 줄에 회차지(transYn='Y')만 표시된다.
    즉 '상행/하행'은 우리가 여기서 잘라 만드는 것이다. 표시가 없으면(편도·순환)
    한 줄로 그린다 — 없는 방향을 지어내지 않는다.
    """
    turns = [s["seq"] for s in stations if s.get("is_turn")]
    if not turns:
        return None
    last = max(turns)
    # 회차지가 종점이면 사실상 편도 — 그걸로 자르면 하행이 빈 목록이 된다.
    return last if 0 < last < len(stations) else None


def _rank(items: list[dict], q: str) -> list[dict]:
    """정확히 일치 → 접두 일치 → 짧은 번호 순. 152 를 친 사람에게 1522 가 먼저 오면 안 된다."""
    return sorted(items, key=lambda r: (r["no"] != q, not r["no"].startswith(q), len(r["no"]), r["no"]))


@app.get("/api/routes/search")
async def routes_search(q: str = ""):
    """버스 번호로 찾는다(사용자가 아는 건 152 이지 노선 ID 100100152 가 아니다).

    캐시 우선 → 없으면 실시간 조회 후 캐시에 채운다. 한 번 찾은 버스는 다음부터 공짜.
    """
    q = q.strip()
    started = time.perf_counter()
    if not q:
        return {"query": q, "items": [], "result_count": 0,
                "observability": {"endpoint": "cache:routes", "latency_ms": 0, "cache": "hit", "status": 200}}

    cached = await run_in_threadpool(store.search_routes, q)
    # 정확히 일치하는 번호가 캐시에 있으면 끝. 부분 일치뿐이면 캐시가 불완전할 수
    # 있으므로(1522 가 아직 없을 수 있다) 실시간으로 확인해 합친다.
    if cached and any(r["no"] == q for r in cached):
        return {
            "query": q, "items": _rank(cached, q), "result_count": len(cached), "source": "cache",
            "observability": {"endpoint": "cache:routes", "latency_ms": int((time.perf_counter() - started) * 1000),
                              "cache": "hit", "status": 200},
        }

    # 캐시에 없거나 불완전 — 실시간 조회 후 캐시에 채운다
    if USE_MOCK:
        found = mock.search_routes(q)
        obs = {"endpoint": "노선번호목록조회", "latency_ms": int((time.perf_counter() - started) * 1000),
               "cache": "miss", "status": 200, "mode": "mock"}
    else:
        try:
            result = await client.call("route_search", strSrch=q)
            found = normalize("route", result.items)
            obs = {"endpoint": "노선번호목록조회", "latency_ms": result.latency_ms,
                   "cache": "miss", "status": 200}
        except SeoulApiError as exc:
            # 외부 조회가 실패해도 캐시의 부분 일치는 돌려준다 — 빈손보다 낫다.
            log.warning("노선 검색 실패: %s", exc)
            return {"query": q, "items": _rank(cached, q), "result_count": len(cached), "source": "cache_only",
                    "observability": {"endpoint": "노선번호목록조회", "cache": "miss",
                                      "status": 502, "error": exc.code, "message": exc.message}}

    found = [r for r in found if r.get("route_id") and r.get("no")]
    if found:
        await run_in_threadpool(store.upsert_routes, found)  # 다음 검색부터는 캐시에서 바로

    merged = {r["route_id"]: r for r in cached}
    merged.update({r["route_id"]: r for r in found})
    items = list(merged.values())
    return {"query": q, "items": _rank(items, q), "result_count": len(items), "source": "live",
            "observability": obs}


async def _ensure_stations(route_id: str) -> tuple[list[dict], str]:
    """정류장 순서 확보. 캐시에 없으면 한 번만 받아 채운다(on-demand hydration).

    사전 적재는 최적화일 뿐 전제 조건이 아니다 — 어떤 노선이든 노선도가 나와야 한다.
    """
    stations = await run_in_threadpool(store.get_route_stations, route_id)
    if stations:
        return stations, "hit"

    if USE_MOCK:
        rows = mock.route_stations(route_id)
    else:
        try:
            result = await client.call("route_stations", busRouteId=route_id)
            rows = [r for r in normalize("route_station", result.items) if r["name"]]
        except SeoulApiError as exc:
            log.warning("정류장 목록 조회 실패: %s", exc)
            return [], "error"
    if rows:
        await run_in_threadpool(store.upsert_route_stations, route_id, rows)
    return rows, "miss"


@app.get("/api/routes/{route_id}")
async def route_detail(route_id: str):
    """노선도 = 정적(정류장 순서) + 동적(버스 위치)."""
    route = await run_in_threadpool(store.get_route, route_id)
    if not route:
        return JSONResponse({"error": "route_not_found", "route_id": route_id}, status_code=404)
    stations, station_cache = await _ensure_stations(route_id)
    buses, obs = await _live("bus_pos", "bus_pos", route_id=route_id)
    obs = {**obs, "stations_cache": station_cache}
    # 정류장 수를 넘는 순번은 버린다(데이터 불일치)
    buses = [b for b in buses if 1 <= b["sect_ord"] <= max(1, len(stations))]
    return {"route": route, "stations": stations, "buses": buses,
            "turn_seq": turn_seq(stations), "running_count": len(buses),
            "observability": obs}


@app.get("/api/stations/search")
async def stations_search(q: str = ""):
    """정류장 이름 검색. 노선 검색과 같은 이유로 캐시 우선 · 실시간 폴백."""
    q = q.strip()
    started = time.perf_counter()
    items = await run_in_threadpool(store.search_stations, q) if q else []

    if q and not items:
        if USE_MOCK:
            found = mock.search_stations(q)
            obs = {"endpoint": "getStationByName", "latency_ms": int((time.perf_counter() - started) * 1000),
                   "cache": "miss", "status": 200, "mode": "mock"}
        else:
            try:
                result = await client.call("station_search", stSrch=q)
                found = normalize("station", result.items)
                obs = {"endpoint": "getStationByName", "latency_ms": result.latency_ms,
                       "cache": "miss", "status": 200}
            except SeoulApiError as exc:
                log.warning("정류장 검색 실패: %s", exc)
                return {"query": q, "items": [], "result_count": 0, "source": "error",
                        "observability": {"endpoint": "getStationByName", "cache": "miss",
                                          "status": 502, "error": exc.code, "message": exc.message}}
        found = [s for s in found if s.get("station_id") and s.get("name")]
        if found:
            await run_in_threadpool(store.upsert_stations, found)
        return {"query": q, "items": found, "result_count": len(found), "source": "live",
                "observability": obs}

    return {
        "query": q,
        "items": items,
        "result_count": len(items),
        "source": "cache",
        "observability": {"endpoint": "cache:stations", "latency_ms": int((time.perf_counter() - started) * 1000),
                          "cache": "hit", "status": 200},
    }


@app.get("/api/stations/{ars}")
async def station_detail(ars: str, soon_only: bool = False):
    """정류장 화면 = 정적(경유 노선) + 동적(도착 정보).

    soon_only 는 첫 화면의 '급해요' 의도. 화면에서만 걸러내므로 API 호출은 안 는다.
    """
    station = await run_in_threadpool(store.get_station, ars)
    if not station:
        # 검색 없이 직접 들어온 경우. 도착 정보는 ARS 만으로 조회되므로 404 로 끊지 않는다.
        if not ars.isdigit():
            return JSONResponse({"error": "station_not_found", "ars": ars}, status_code=404)
        station = {"station_id": ars, "ars": ars, "name": f"정류장 {ars}"}

    through = await run_in_threadpool(store.routes_through_station, station["ars"])
    arrivals, obs = await _live("arrival", "station_arrival", ars=station["ars"])

    known = {r["no"]: r for r in through}
    for a in arrivals:
        if a["no"] in known:
            a["seq_on_route"] = known[a["no"]]["seq"]

    if soon_only:
        arrivals = [a for a in arrivals if a.get("sec1", 99999) <= 300]

    return {
        "station": station,
        "routes": through,
        "arrivals": arrivals,
        "route_count": len(through),
        "arriving_count": sum(1 for a in arrivals if a.get("sec1", 99999) <= 300),
        "coverage_note": "경유 노선은 사전 적재한 노선 범위 안에서만 완전합니다.",
        "observability": obs,
    }


@app.post("/v1/collect")
async def collect(request: Request):
    """행동 로그 수집. 파일에 붙이는 것 외에는 아무것도 하지 않는다.

    파싱 전에 크기부터 본다 — 상한이 없으면 본문 하나로 서버 메모리를 밀어 올릴 수 있다.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_COLLECT_BYTES:
        return JSONResponse({"error": "payload_too_large", "limit": MAX_COLLECT_BYTES}, status_code=413)

    raw = await request.body()
    if len(raw) > MAX_COLLECT_BYTES:          # content-length 를 속였을 수 있다
        return JSONResponse({"error": "payload_too_large", "limit": MAX_COLLECT_BYTES}, status_code=413)

    try:
        batch = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    ip = request.client.host if request.client else None
    try:
        # 파일 쓰기는 블로킹 — 이벤트 루프를 막지 않게 스레드풀로.
        return await run_in_threadpool(collector.accept, batch, ip)
    except collector.Rejected as rej:
        log.warning("수집 거부: %s", rej.reason)
        return JSONResponse({"error": rej.reason}, status_code=rej.status)


# ---------------------------------------------------------------- 정적 파일

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
