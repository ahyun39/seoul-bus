"""실데이터 연동 점검 — 목업으로는 증명할 수 없는 '우리 가정이 실제 응답과 맞는가'.

엔드포인트마다 1회씩(총 5~6회, 하루 한도의 0.6%) 부르고 정규화 전 원본 필드명과
정규화 후 값을 나란히 찍는다 → pick() 후보가 어긋나면 그 자리에서 보인다.

    python -m scripts.smoke_live              # 기본 노선(4312)
    python -m scripts.smoke_live 강남06        # 다른 번호로

볼 것: 인증키 통과 여부 · 비숫자 노선번호(강남06) · "필드를 찾지 못했습니다" 경고 ·
traTime1 의 단위(명세와 실사용 구현이 엇갈린다).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from app import store
from app.config import SERVICE_KEY, USE_MOCK
from app.seoul_api import EP, SeoulBusClient, data_age_seconds, normalize

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_ROUTE = "4312"


def show(title: str, raw: list[dict], kind: str, limit: int = 2) -> list[dict]:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    if not raw:
        print("  (빈 응답)")
        return []

    print(f"  건수: {len(raw)}")
    print(f"  원본 필드명: {sorted(raw[0].keys())}")
    norm = normalize(kind, raw)
    for row in norm[:limit]:
        print("  정규화: " + json.dumps(row, ensure_ascii=False))

    # 빈 값은 필드명이 어긋났다는 신호. 단 bool 은 제외한다 —
    # False 는 '없음'이 아니고, False == 0 이라 멀쩡한 필드가 경고로 잡힌다.
    empties = [k for k, v in norm[0].items()
               if not isinstance(v, bool) and (v == "" or v is None)]
    if empties:
        print(f"  ⚠ 비어 있는 필드: {empties}  ← pick() 후보 이름을 확인하세요")
    return norm


async def main() -> int:
    if USE_MOCK or not SERVICE_KEY:
        print("인증키가 없어 목업 모드입니다. .env 에 SEOUL_BUS_SERVICE_KEY 를 넣고 다시 실행하세요.")
        return 2

    number = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ROUTE
    client = SeoulBusClient()
    # 하루 1,000회 한도는 앱과 스크립트가 함께 쓴다. 훅을 안 걸면 여기서 태운 몫이
    # api_usage 에 안 잡혀 /api/health 의 잔여 한도가 실제보다 낙관적으로 나온다.
    client.on_call = store.record_api_call
    calls = 0
    print(f"점검 대상 버스 번호: {number}   (엔드포인트당 1회씩 호출합니다)")

    try:
        # 1) 버스 번호로 노선 찾기
        res = await client.call("route_search", strSrch=number)
        calls += 1
        routes = show(f"1. 노선번호목록조회  {EP['route_search']}", res.items, "route")
        if not routes:
            print(f"\n✗ '{number}' 이(가) 조회되지 않습니다. 서울시 면허 노선이 맞는지 확인하세요.")
            return 1
        exact = next((r for r in routes if r["no"] == number), routes[0])
        route_id = exact["route_id"]
        print(f"\n  → 선택: {exact['no']} ({exact['type']})  route_id={route_id}")

        # 2) 그 노선의 정류장 순서
        res = await client.call("route_stations", busRouteId=route_id)
        calls += 1
        stops = show(f"2. 노선별경유정류소목록조회  {EP['route_stations']}", res.items, "route_station")
        if stops:
            print(f"  → 기점 {stops[0]['name']} … 종점 {stops[-1]['name']}  (총 {len(stops)}개)")

        # 3) 실시간 버스 위치 — sectOrd 가 정류장 수 안에 들어오는지
        res = await client.call("bus_pos", busRouteId=route_id)
        calls += 1
        buses = show(f"3. 노선별버스위치목록조회  {EP['bus_pos']}", res.items, "bus_pos")
        if buses:
            age = data_age_seconds(buses)
            print(f"  → 데이터 나이: {age}초")
            if age is not None and abs(age) > 600:
                print("  ⚠ 나이가 비정상입니다 — dataTm 타임존 해석을 확인하세요 (KST 여야 합니다)")
            worst = max(b["sect_ord"] for b in buses)
            print(f"  → 최대 sect_ord={worst}, 정류장 수={len(stops)}")
            if stops and worst > len(stops):
                print("  ⚠ sect_ord 가 정류장 수를 넘습니다 — 상·하행이 한 목록에 섞여 있을 수 있습니다")
        else:
            print("  (운행 중인 버스가 없습니다 — 운행 시간대에 다시 확인하세요)")

        # 4) 정류장 이름 검색
        name = stops[len(stops) // 2]["name"] if stops else "서울역"
        res = await client.call("station_search", stSrch=name)
        calls += 1
        stations = show(f"4. 정류소명검색  {EP['station_search']}  (검색어: {name})", res.items, "station")

        # 5) 그 정류장의 도착 정보
        if stations and stations[0]["ars"]:
            ars = stations[0]["ars"]
            res = await client.call("station_arrival", arsId=ars)
            calls += 1
            arr = show(f"5. 정류소도착정보조회  {EP['station_arrival']}  (ARS: {ars})", res.items, "arrival")
            if arr:
                print(f"  → 데이터 나이: {data_age_seconds(arr)}초")
                # traTime 단위 실측 — arrmsg 에서 뽑은 초와 비교하면 분/초가 드러난다.
                raw = res.items[0] if res.items else {}
                tra = raw.get("traTime1")
                if tra not in (None, ""):
                    sec = arr[0]["sec1"]
                    print(f"  → traTime1={tra} vs arrmsg 파싱 {sec}초 "
                          f"({'분 단위로 보임' if sec and abs(int(tra) * 60 - sec) < 90 else '초 단위로 보임' if sec and abs(int(tra) - sec) < 90 else '판단 불가'})")
                # 재차인원·만차·우회 — '탈 수 있는 버스인가'를 가르는 신호
                print(f"  → 재차인원 rerideNum1={raw.get('rerideNum1')!r} "
                      f"만차 isFullFlag1={raw.get('isFullFlag1')!r} "
                      f"우회 deTourAt={raw.get('deTourAt')!r}")

    except Exception as exc:  # noqa: BLE001 — 점검 스크립트라 원인을 그대로 보여준다
        print(f"\n✗ 실패: {type(exc).__name__}: {exc}")
        print("\n  자주 보는 코드 (공공데이터포털 게이트웨이 기준):")
        print("    20 인증키 미포함 / 이용 권한 미확인 · 30 등록되지 않은 인증키 · 31 사용기한 만료")
        print("    22 일일 호출량 초과 · 23 초당 호출량 초과(잠시 뒤 재시도)")
        print("    10 요청 파라미터 오류 · 05 기관 API 응답 지연")
        return 1
    finally:
        await client.aclose()

    print(f"\n{'=' * 72}")
    print(f"✓ 점검 완료 — 외부 호출 {calls}회 사용")
    print("  위에 ⚠ 가 없고 정규화 값이 비어 있지 않다면 실데이터 연동이 끝난 것입니다.")
    print(f"  다음: python -m scripts.preload {number} …  로 사전 적재하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
