"""목업 데이터 제공자 — 인증키 없이도 앱 전체가 돌아가게 한다.

키 발급을 기다리지 않고 화면을 볼 수 있고, 외부 API 가 죽었을 때 우리 문제와
저쪽 문제를 가를 수 있다. 정규화된 모양이 실제 API 와 같아서 서비스 계층은
어느 쪽이 들어와도 동일하게 동작한다.

데이터 출처
  · 정적 마스터(노선·정류장 순서): app/mock_data.json 이 있으면 실데이터를 쓴다
    (scripts/export_mock.py 가 생성). 없으면 아래 합성 노선으로 떨어진다.
  · 버스 위치·도착 시각: 매번 합성한다. 굳히는 순간 어제 위치를 지금처럼 보여주게 된다.

노선 종류와 기·종점만 실제를 참고했고 정류장 순서·차량번호·도착 시각은 전부 합성이다.

목업을 바꿨는데 옛 정류장이 남으면 data/bus_cache_mock.db 를 지운다.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import KST

# dataTm 은 KST 다. UTC 로 찍으면 신선도가 9시간(32400초)으로 계산된다.

_RNG = random.Random(20260916)

_FIXTURE = Path(__file__).resolve().parent / "mock_data.json"


def _load_fixture() -> dict[str, dict]:
    """실데이터 픽스처. 없으면 빈 dict — 합성 데이터로 떨어진다."""
    if not _FIXTURE.exists():
        return {}
    try:
        data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}                       # 깨진 픽스처가 앱을 멈추게 하지 않는다
    return {r["no"]: r for r in data.get("routes", []) if r.get("stations")}


_REAL = _load_fixture()

# ----- 노선 -------------------------------------------------------------
# stops 는 기점 → 종점 순. 이름이 같으면 같은 정류장이다(_STOP_IDS).

ROUTES = [
    {
        "route_id": "104900034", "no": "4312", "type": "지선",
        "from": "개포동(구룡마을)", "to": "삼성역",
        "first": "0400", "last": "2300", "interval": "9", "company": "예시여객",
        "stops": [
            "구룡마을", "개포자이", "구룡역", "도곡역", "한티역", "대치역",
            "대청역", "학여울역", "삼성중앙역", "삼성역",
        ],
    },
    {
        "route_id": "100100032", "no": "402", "type": "간선",
        "from": "장지공영차고지", "to": "서울역",
        "first": "0400", "last": "2250", "interval": "10", "company": "예시운수",
        "stops": [
            "장지공영차고지", "가락시장역", "삼성역", "강남구청역", "신사역",
            "한남대교북단", "순천향대병원", "남산1호터널", "을지로입구",
            "종로3가역", "서울역",
        ],
    },
    {
        "route_id": "121000016", "no": "강남06", "type": "마을",
        "from": "세곡동(세곡푸르지오)", "to": "대청역",
        "first": "0550", "last": "2330", "interval": "11", "company": "예시교통",
        "stops": [
            "세곡푸르지오", "세곡사거리", "자곡동", "수서역", "일원역",
            "대모산입구역", "개포동역", "대청역",
        ],
    },
    {
        "route_id": "100100016", "no": "160", "type": "간선",
        "from": "도봉산역광역환승센터", "to": "온수역",
        "first": "0410", "last": "2240", "interval": "8", "company": "예시교통운수",
        "stops": [
            "도봉산역광역환승센터", "쌍문역", "미아사거리역", "신설동역",
            "동대문", "종로3가역", "종각역", "서울시청", "충정로역",
            "마포역", "여의도역", "영등포시장", "온수역",
        ],
    },
]

# 픽스처가 있는 노선은 그쪽 메타데이터로 교체. stops 는 폴백용으로 남긴다.
for _r in ROUTES:
    _real = _REAL.get(_r["no"])
    if _real:
        _r.update({k: _real[k] for k in
                   ("route_id", "type", "from", "to", "first", "last", "interval", "company")})
        _r["real"] = True


# 시작 시 캐시에 미리 넣을 노선
SEEDED = [r for r in ROUTES if r["no"] in ("4312", "강남06", "160")]

_BY_ID = {r["route_id"]: r for r in ROUTES}


# ----- 정류장 -----------------------------------------------------------
# 이름이 같으면 같은 정류장이어야 여러 노선이 한 정류장에서 만난다.
# 노선별로 ars 를 따로 만들면 같은 '대청역'이 노선 수만큼 생긴다.

def _build_stop_ids() -> dict[str, tuple[str, str]]:
    """이름 → (station_id, ars). 픽스처가 있으면 진짜 ARS 를 그대로 쓴다.

    합성 번호를 덧씌우면 화면의 ARS 와 실제 정류장이 어긋난다.
    """
    out: dict[str, tuple[str, str]] = {}
    for real in _REAL.values():
        for stop in real["stations"]:
            out.setdefault(stop["name"], (stop["station_id"], stop["ars"]))

    synthetic = [name for route in ROUTES if not route.get("real")
                 for name in route["stops"] if name not in out]
    seen: list[str] = []
    for name in synthetic:
        if name not in seen:
            seen.append(name)
    for i, name in enumerate(seen):
        out[name] = (f"1180{23001 + i}", str(23001 + i))
    return out


_STOP_IDS = _build_stop_ids()


def route_stations(route_id: str) -> list[dict]:
    """노선별 경유 정류소. 실제 API 와 같은 모양으로 왕복을 만든다.

    실제 API 는 기점 → 회차지 → 종점을 하나의 seq 로 주고 회차지에만 transYn='Y' 를 붙인다.
    목업이 편도만 주면 방향 분리 코드가 데모에서 한 번도 실행되지 않는다.
    """
    route = _BY_ID.get(route_id)
    if not route:
        return []

    real = _REAL.get(route["no"])
    if real:
        # 실데이터는 이미 하나의 seq 로 들어 있다 — 합성할 것이 없다.
        return [dict(s) for s in real["stations"]]

    out_names = route["stops"]
    back_names = list(reversed(out_names))[1:]          # 회차지는 한 번만
    names = out_names + back_names
    turn = len(out_names)
    rows = []
    for i, name in enumerate(names):
        station_id, ars = _STOP_IDS[name]
        rows.append({
            "seq": i + 1, "station_id": station_id, "ars": ars, "name": name,
            "direction": route["to"] if i < turn else route["from"],
            "is_turn": i + 1 == turn,
        })
    return rows


def _public(route: dict) -> dict:
    """stops 는 내부용 — 검색 응답에 정류장 전체를 실어 보내지 않는다."""
    return {k: v for k, v in route.items() if k != "stops"}


def search_routes(q: str) -> list[dict]:
    exact = [r for r in ROUTES if r["no"].startswith(q)]
    partial = [r for r in ROUTES if q in r["no"] and r not in exact]
    return [_public(r) for r in exact + partial]


def _directions_at(ars: str) -> list[str]:
    """그 정류장을 지나는 노선들이 알려주는 방면(중복 제거)."""
    out: list[str] = []
    for route in ROUTES:
        for stop in route_stations(route["route_id"]):
            if stop["ars"] == ars and stop.get("direction") and stop["direction"] not in out:
                out.append(stop["direction"])
    return out


def search_stations(q: str) -> list[dict]:
    """ARS 하나가 정류장 하나다. 이름이 같아도 방면이 다르면 다른 정류장이다."""
    seen: set[str] = set()
    out = []
    for route in ROUTES:
        for stop in route_stations(route["route_id"]):
            if q not in stop["name"] or stop["ars"] in seen:
                continue
            seen.add(stop["ars"])
            out.append({"station_id": stop["station_id"], "ars": stop["ars"],
                        "name": stop["name"], "directions": _directions_at(stop["ars"])})
    return out[:15]


def routes_at(ars: str) -> list[dict]:
    """그 정류장을 지나는 노선 — 도착 정보 생성에 쓴다."""
    hits = []
    for route in ROUTES:
        for stop in route_stations(route["route_id"]):
            if stop["ars"] == ars:
                hits.append(route)
                break
    return hits


# ----- 실시간 -----------------------------------------------------------
_BUS_SEED = {
    "104900034": [(3, True), (8, False), (14, False)],     # 4312
    "100100032": [(4, True), (9, False), (15, True)],      # 402
    "121000016": [(2, False), (6, True)],                  # 강남06
    "100100016": [(5, True), (11, False), (17, True)],     # 160
}


def _spread(stops: int) -> list[tuple[int, bool]]:
    """정류장 수에 맞춰 버스를 고르게 배치한다.

    실데이터는 왕복 170정류장이 넘어, 고정 시드를 쓰면 버스가 전부 기점에 몰린다.
    """
    count = max(2, min(12, stops // 14))
    gap = max(1, stops // count)
    return [(1 + i * gap, i % 3 == 0) for i in range(count)]


def bus_positions(route_id: str) -> list[dict]:
    """25초에 한 칸씩 전진."""
    stops = len(route_stations(route_id)) or 1
    seed = _BUS_SEED.get(route_id) or _spread(stops)
    step = int(time.time() // 25)
    # 실제 API 처럼 '조금 전' 시각을 준다 — 늘 0초면 신선도 표시가 무의미하다.
    now = (datetime.now(KST) - timedelta(seconds=step % 40)).strftime("%Y%m%d%H%M%S")
    out = []
    for idx, (base_ord, base_stopped) in enumerate(seed):
        half = step + idx * 3
        ordinal = (base_ord + half // 2 - 1) % stops + 1
        stopped = base_stopped if half % 2 == 0 else not base_stopped
        out.append({
            "veh_id": f"mock-{route_id[-3:]}-{idx}",
            "plain_no": f"서울70사{1234 + idx * 607}",
            "plate": str(1234 + idx * 607),
            "sect_ord": ordinal,
            "stopped": stopped,
            "congestion": ["여유", "보통", "혼잡"][(step + idx) % 3],
            "is_last": idx == len(seed) - 1 and step % 7 == 0,
            "data_tm": now,
        })
    return out


def arrivals(ars: str) -> list[dict]:
    """정류장 도착 정보 — 실제로 지나는 노선만."""
    base = int(ars) if ars.isdigit() else 23001
    rng = random.Random(base + int(time.time() // 30))
    picks = routes_at(ars) or ROUTES[:2]
    out = []
    for r in picks:
        s1 = rng.randint(0, 900)
        s2 = s1 + rng.randint(240, 900)
        stops1 = max(0, s1 // 150)
        out.append({
            "route_id": r["route_id"], "no": r["no"], "type": r["type"], "to": r["to"],
            # 화면이 원문 메시지를 그대로 보여주므로 실제 API 와 문장 모양을 맞춘다.
            "eta1": "곧 도착" if s1 < 60 else f"{s1 // 60}분{s1 % 60}초후[{stops1}번째 전]",
            "eta2": f"{s2 // 60}분{s2 % 60}초후[{max(0, s2 // 150)}번째 전]",
            "sec1": s1, "sec2": s2,
            "stops1": stops1,
            "riders": rng.choice([None, None, 3, 7, 12, 24]),
            "is_full": rng.random() < 0.12,
            "is_detour": rng.random() < 0.06,
            "is_last": rng.random() < 0.05,
            "low_floor": rng.random() < 0.4,
            # 실 API(getStationByUid)는 수집 시각을 주지 않는다.
            "data_tm": "",
        })
    out.sort(key=lambda x: x["sec1"])
    return out
