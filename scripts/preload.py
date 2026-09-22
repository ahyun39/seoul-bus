"""정적 마스터 사전 적재 배치.

거의 바뀌지 않는 노선·정류장을 하루 한 번 로컬 DB 에 넣어두면 실시간 호출이
'버스 위치'와 '도착 정보' 둘로 줄어든다. 노선 1개당 2회(검색+정류장목록)이므로
20개를 적재해도 하루 한도 1,000회의 4%.

    python -m scripts.preload                 # .env 의 PRELOAD_ROUTES 사용
    python -m scripts.preload 152 153 273     # 노선번호 직접 지정
"""

from __future__ import annotations

import asyncio
import sys

from app import store
from app.config import PRELOAD_ROUTES, USE_MOCK
from app.seoul_api import SeoulApiError, SeoulBusClient, normalize


async def preload(route_numbers: list[str]) -> dict:
    store.init_db()

    if USE_MOCK:
        print("목업 모드입니다. 실데이터를 받으려면 .env 에 SEOUL_BUS_SERVICE_KEY 를 넣으세요.")
        from app import mock
        store.upsert_routes(mock.ROUTES)
        for route in mock.ROUTES:
            store.upsert_route_stations(route["route_id"], mock.route_stations(route["route_id"]))
        print("목업 마스터 적재 완료:", store.counts())
        return store.counts()

    client = SeoulBusClient()
    calls = routes_saved = stations_saved = errors = 0

    try:
        for number in route_numbers:
            # 1) 노선번호로 후보를 찾는다
            try:
                found = await client.call("route_search", strSrch=number)
                calls += 1
            except SeoulApiError as exc:
                print(f"  [실패] {number} 노선 검색: {exc}")
                errors += 1
                continue

            routes = normalize("route", found.items)
            # 정확히 일치하는 번호만 — '15' 는 150, 152 를 전부 물고 온다
            exact = [r for r in routes if r["no"] == number] or routes[:1]
            if not exact:
                print(f"  [없음] {number}")
                continue
            store.upsert_routes(exact)
            routes_saved += len(exact)

            # 2) 경유 정류소 순서 — 노선도의 뼈대
            for route in exact:
                try:
                    stops = await client.call("route_stations", busRouteId=route["route_id"])
                    calls += 1
                except SeoulApiError as exc:
                    print(f"  [실패] {route['no']} 정류장 목록: {exc}")
                    errors += 1
                    continue
                rows = normalize("route_station", stops.items)
                rows = [r for r in rows if r["name"]]
                store.upsert_route_stations(route["route_id"], rows)
                stations_saved += len(rows)
                print(f"  [완료] {route['no']:>5}  정류장 {len(rows):>3}개  ({route['from']} ↔ {route['to']})")

            # 초당 호출 제한(코드 23) 회피
            await asyncio.sleep(0.35)
    finally:
        await client.aclose()

    print(f"\nAPI 호출 {calls}회 · 노선 {routes_saved}개 · 정류장 {stations_saved}건 · 실패 {errors}건")
    print("적재 현황:", store.counts())
    return store.counts()


if __name__ == "__main__":
    targets = sys.argv[1:] or PRELOAD_ROUTES
    print("사전 적재 대상:", ", ".join(targets))
    asyncio.run(preload(targets))
