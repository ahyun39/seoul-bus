"""서울시 버스 운행정보 공유서비스 클라이언트.

이 API 의 함정 셋:
1. 실패해도 HTTP 200 — 오류 코드는 본문 헤더(headerCd)에 있다.
2. 필드명이 '활용가이드' 문서에만 있다 → pick() 으로 후보 이름을 감싸고 경고를 남긴다.
3. 외부 의존성은 반드시 느려지거나 죽는다 → 타임아웃·재시도는 처음부터 넣는다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote

import httpx
import xmltodict

from .config import KST, API_BASE, API_TIMEOUT_SEC, SERVICE_KEY

log = logging.getLogger("seoul_api")

# ---------------------------------------------------------------- 엔드포인트
# 필드명이 바뀌면 이 표만 고치면 된다.
EP = {
    "route_search":  "/busRouteInfo/getBusRouteList",    # 노선번호목록조회
    "route_info":    "/busRouteInfo/getRouteInfo",       # 노선기본정보항목조회
    "route_stations": "/busRouteInfo/getStaionByRoute",  # 노선별경유정류소목록조회 (원문 철자 그대로)
    "bus_pos":       "/buspos/getBusPosByRtid",          # 노선별 버스위치 목록조회
    "station_search": "/stationinfo/getStationByName",   # 정류소명 검색
    "station_routes": "/stationinfo/getRouteByStation",  # 정류소 경유노선 목록
    "station_arrival": "/stationinfo/getStationByUid",   # 정류소 도착정보
}

# 정상 응답 코드. 그 외는 본문 메시지를 그대로 올린다.
OK_CODES = {"0"}




def parse_data_tm(value: str) -> datetime | None:
    """수집 시각(dataTm, 'YYYYMMDDHHMMSS' KST) → datetime.

    타임존을 붙이지 않으면 UTC 서버에서 9시간 차이가 그대로 '신선도'가 된다.
    """
    value = (value or "").strip()
    if len(value) < 14 or not value[:14].isdigit():
        return None
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None


def data_age_seconds(rows: Iterable[dict], now: datetime | None = None) -> int | None:
    """원본 데이터가 몇 초 전 것인지.

    캐시 나이와 다른 값이다 — 캐시가 방금 채워져도 원본이 90초 전이면 90초 전 데이터다.
    """
    now = now or datetime.now(timezone.utc)
    stamps = [t for t in (parse_data_tm(r.get("data_tm", "")) for r in rows) if t]
    if not stamps:
        return None
    newest = max(stamps)
    return max(0, int((now - newest).total_seconds()))


_KEY_IN_URL = re.compile(r"(serviceKey=)[^&\s'\"]+", re.IGNORECASE)


def scrub(text: Any) -> str:
    """메시지에서 인증키를 지운다.

    httpx 의 HTTPStatusError 는 요청 URL 을 통째로 담고, 그 쿼리에 serviceKey 가 있다.
    그대로 두면 .env 로 뺀 키가 로그·모니터링으로 퍼진다.
    """
    return _KEY_IN_URL.sub(r"\1***", str(text))


class SeoulApiError(RuntimeError):
    def __init__(self, code: str, message: str, endpoint: str):
        super().__init__(f"[{code}] {message} ({endpoint})")
        self.code = code
        self.message = message
        self.endpoint = endpoint


@dataclass
class ApiResult:
    """호출 결과 + 관측 정보(latency·캐시 여부)."""
    items: list[dict]
    latency_ms: int
    endpoint: str
    from_cache: bool = False


def pick(row: dict, *names: str, default: Any = None) -> Any:
    """후보 필드명 중 먼저 존재하는 값. 철자가 달라도 죽지 않게 하는 완충 장치.

    끝까지 못 찾으면 경고를 남긴다 — 조용히 None 이 채워지지 않도록.
    """
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    if default is None:
        log.warning("필드를 찾지 못했습니다: %s (사용 가능한 키: %s)", names, list(row)[:12])
    return default


# ---------------------------------------------------------------- 인증키
# 포털은 같은 키를 Encoding / Decoding 두 형태로 준다. 자동 인코딩에 맡기면 둘 다 터진다.
#   · Encoding 키(`%2B` 포함): `%` 가 `%25` 로 이중 인코딩 → 오류 30
#   · Decoding 키: httpx 가 `+` 를 안전 문자로 보고 안 바꾸는데 포털은 `%2B` 를 기대
#     → 키에 `+` 가 있을 때만 간헐 실패
# 그래서 쿼리스트링을 직접 만든다. 어느 쪽 키를 넣어도 동작한다.

_PCT = re.compile(r"%[0-9A-Fa-f]{2}")


def looks_encoded(key: str) -> bool:
    """`%2B` 같은 이스케이프가 있으면 Encoding 키다."""
    return bool(_PCT.search(key))


def build_query(service_key: str, params: dict[str, Any]) -> str:
    """serviceKey 는 이중 인코딩을 피해 그대로, 나머지 파라미터는 정상 인코딩."""
    key = service_key if looks_encoded(service_key) else quote(service_key, safe="")
    rest = "".join(
        f"&{quote(str(k), safe='')}={quote(str(v), safe='')}"
        for k, v in params.items() if v is not None
    )
    return f"serviceKey={key}{rest}"


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_list(node: Any) -> list[dict]:
    """XML 특성상 항목이 1개면 dict, 여러 개면 list 로 온다. 항상 list 로 맞춘다."""
    if node is None:
        return []
    if isinstance(node, list):
        return [n for n in node if isinstance(n, dict)]
    if isinstance(node, dict):
        return [node]
    return []


class SeoulBusClient:
    def __init__(self, service_key: str = SERVICE_KEY, base: str = API_BASE):
        self.service_key = service_key
        self.base = base.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        # 호출 통지 훅. 이 모듈은 DB 를 모른다 — main.py 가 store.record_api_call 을 연결한다.
        self.on_call: Any = None

    def _emit(self, endpoint: str, ok: bool) -> None:
        if self.on_call is None:
            return
        try:
            self.on_call(endpoint, ok)
        except Exception:          # 사용량 기록 실패가 조회를 막으면 안 된다
            log.warning("API 사용량 기록 실패", exc_info=True)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(API_TIMEOUT_SEC),
                headers={"Accept": "application/xml"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call(self, key: str, **params: Any) -> ApiResult:
        """오퍼레이션 하나를 호출한다. 재시도는 지수 백오프로 최대 3회."""
        endpoint = EP[key]
        url = self.base + endpoint + "?" + build_query(self.service_key, params)

        client = await self._http()
        started = time.perf_counter()
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                resp = await client.get(url)
                self._emit(endpoint, resp.is_success)   # 실패해도 한도는 소모된다
                resp.raise_for_status()
                items, code, msg = self._parse(resp.text)
                if code not in OK_CODES:
                    # 초당 제한 초과(코드 23)는 쉬면 풀린다 — 재시도 대상.
                    if code == "23" and attempt < 2:
                        await asyncio.sleep(0.6 * (attempt + 1))
                        continue
                    raise SeoulApiError(code, msg, endpoint)
                latency = int((time.perf_counter() - started) * 1000)
                return ApiResult(items=items, latency_ms=latency, endpoint=endpoint)
            except httpx.HTTPStatusError as exc:
                # 이 예외를 그대로 올리면 메시지의 URL 로 인증키가 로그에 남는다.
                # 상태 코드만 꺼내 우리 예외로 바꾼다.
                status = exc.response.status_code
                raise SeoulApiError(f"http_{status}", f"HTTP {status}", endpoint) from None
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._emit(endpoint, False)
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))  # 0.5s → 1.0s
                    continue

        raise SeoulApiError("network", f"호출 실패: {scrub(last_error)}", endpoint)

    @staticmethod
    def _parse(text: str) -> tuple[list[dict], str, str]:
        doc = xmltodict.parse(text)
        root = next(iter(doc.values())) if doc else {}
        header = root.get("msgHeader") or root.get("comMsgHeader") or {}
        code = str(header.get("headerCd", header.get("resultCode", "0")))
        message = str(header.get("headerMsg", header.get("resultMsg", "")))
        body = root.get("msgBody") or {}
        items = _as_list(body.get("itemList"))
        return items, code, message


# ---------------------------------------------------------------- 정규화
# API 응답 → 프론트엔드 모양. 필드명이 바뀌어도 프론트엔드는 안 건드린다.

# routeType 코드(getStationByUid 명세). 7:인천 · 8:경기 — 타 시도 노선도 섞여 온다.
BUS_TYPE = {"0": "공용", "1": "공항", "2": "마을", "3": "간선", "4": "지선",
            "5": "순환", "6": "광역", "7": "인천", "8": "경기", "9": "폐지"}


def bus_type(code: Any) -> str:
    """모르는 코드는 단정하지 않는다 — 화면에 거짓 분류가 그려진다."""
    return BUS_TYPE.get(str(code or "").strip(), "기타")


# 버스위치(getBusPosByRtid)의 차량내부혼잡도. 필드명 congetion 은 원문 오타 그대로다.
CONGESTION = {"0": "정보없음", "3": "여유", "4": "보통", "5": "혼잡", "6": "매우혼잡"}

# getStationByUid 에는 이 코드가 없고 rerideNum1/2 로 '재차인원'(명)이 온다.
# 혼잡도 코드로 읽으면 승객 1명이 '정보없음', 4명이 '보통'이 된다 — 척도가 다르다.

# arrmsg1 은 '3분12초후[2번째 전]', '곧 도착', '출발대기', '운행종료' 같은 사람이 읽는 문자열이다.
_ETA_MIN_SEC = re.compile(r"(?:(\d+)분)?\s*(?:(\d+)초)?후")
_ETA_STOPS = re.compile(r"\[(\d+)번째")


def parse_arrmsg(msg: str) -> tuple[int | None, int | None]:
    """도착 메시지 → (초, 몇 번째 전).

    traTime1 은 단위가 불확실하다(항목표는 '분', 초로 다루는 구현도 많다).
    arrmsg 는 단위가 글자로 적혀 있어 해석 여지가 없다 — 그래서 이쪽이 기준.
    """
    msg = (msg or "").strip()
    if not msg:
        return None, None
    if "곧 도착" in msg:
        return 0, 0
    m = _ETA_MIN_SEC.search(msg)
    sec = None
    if m and (m.group(1) or m.group(2)):
        sec = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    st = _ETA_STOPS.search(msg)
    return sec, (int(st.group(1)) if st else None)


def norm_route(row: dict) -> dict:
    # 노선번호는 busRouteNm. busRouteAbrv 는 마을버스 제외 약칭이라 강남06 이 빈칸이 된다.
    return {
        "route_id": str(pick(row, "busRouteId", "busRouteID", default="")),
        "no": str(pick(row, "busRouteNm", "busRouteAbrv", default="")),
        "type": bus_type(pick(row, "routeType", default="")),
        "from": str(pick(row, "stStationNm", "stStaNm", default="")),
        "to": str(pick(row, "edStationNm", "edStaNm", default="")),
        "first": _hhmm(pick(row, "firstBusTm", "beginTm", default="")),
        "last": _hhmm(pick(row, "lastBusTm", "lastTm", default="")),
        "interval": str(pick(row, "term", default="") or ""),
        "company": str(pick(row, "corpNm", default="") or ""),
    }


def _hhmm(value: Any) -> str:
    """'20260917053000' → '0530'. 길이가 다르면 건드리지 않는다."""
    text = str(value or "").strip()
    return text[8:12] if len(text) >= 12 and text[:12].isdigit() else text


def norm_station_of_route(row: dict) -> dict:
    # transYn('Y')=회차지. 상·하행이 한 seq 로 내려와 이 값만이 방향을 가른다.
    return {
        "seq": as_int(pick(row, "seq", "station_seq", default=0)),
        "station_id": str(pick(row, "station", "stationId", default="")),
        "ars": str(pick(row, "arsId", "stationNo", default="")),
        "name": str(pick(row, "stationNm", "stNm", default="")),
        "direction": str(pick(row, "direction", default="") or ""),
        "is_turn": str(pick(row, "transYn", default="N")).upper() == "Y",
    }


def norm_bus_pos(row: dict) -> dict:
    # stopFlag: 1=정류소 도착, 0=운행중.
    # congetion 은 API 원문 철자다 — 'congestion' 으로 읽으면 혼잡도가 전부 비어 나온다.
    return {
        "veh_id": str(pick(row, "vehId", default="")),
        "plain_no": str(pick(row, "plainNo", default="")),
        "plate": str(pick(row, "plainNo", default=""))[-4:],
        "sect_ord": as_int(pick(row, "sectOrd", default=0)),
        "stopped": str(pick(row, "stopFlag", default="0")) == "1",
        "congestion": CONGESTION.get(str(pick(row, "congetion", "congestion", default="0")), "정보없음"),
        "low_floor": str(pick(row, "busType", default="0")) == "1",
        "is_last": str(pick(row, "islastyn", "isLast", default="0")) == "1",
        "data_tm": str(pick(row, "dataTm", default="") or ""),
    }


def norm_station(row: dict) -> dict:
    # 정류소명 검색은 stId/stNm — 경유정류소 목록(station/stationNm)과 규칙이 다르다.
    return {
        "station_id": str(pick(row, "stId", "stationId", default="")),
        "ars": str(pick(row, "arsId", default="")),
        "name": str(pick(row, "stNm", "stationNm", default="")),
    }


def _riders(value: Any) -> int | None:
    """재차인원(명). 0 은 '없음'이 아니라 대개 '미집계'라 None 으로 구분한다."""
    n = as_int(value, default=-1)
    return n if n > 0 else None


def norm_arrival(row: dict) -> dict:
    """정류소 도착정보(getStationByUid).

    도착 시각은 traTime 이 아니라 arrmsg 에서 뽑는다 — traTime1 은 항목크기 3 인데
    같은 명세의 arrmsg 샘플 '136분45초후'는 초로 담으면 8,205 로 세 자리를 넘는다.
    즉 단위가 분이고, 초로 읽으면 '5분 내 도착' 필터가 60배 틀린다(parse_arrmsg 참고).
    """
    msg1 = str(pick(row, "arrmsg1", "arrmsgSec1", default="") or "")
    msg2 = str(pick(row, "arrmsg2", "arrmsgSec2", default="") or "")
    sec1, stops1 = parse_arrmsg(msg1)
    sec2, _ = parse_arrmsg(msg2)
    return {
        "route_id": str(pick(row, "busRouteId", default="")),
        "no": str(pick(row, "rtNm", "busRouteNm", "busRouteAbrv", default="")),
        "type": bus_type(pick(row, "routeType", "busRouteType", default="")),
        "to": str(pick(row, "stationNm1", "adirection", "nxtStn", default="") or ""),
        "eta1": msg1 or "정보 없음",
        "eta2": msg2 or "정보 없음",
        "sec1": sec1 if sec1 is not None else 10 ** 6,
        "sec2": sec2 if sec2 is not None else 10 ** 6,
        "stops1": stops1 if stops1 is not None else 0,
        "riders": _riders(pick(row, "rerideNum1", default="0")),
        "is_full": str(pick(row, "isFullFlag1", default="0")) == "1",
        "is_detour": str(pick(row, "deTourAt", default="00")).strip() == "11",
        "is_last": str(pick(row, "isLast1", default="0")) == "1",
        "low_floor": str(pick(row, "busType1", default="0")) == "1",
        "data_tm": "",
    }


def normalize(kind: str, rows: Iterable[dict]) -> list[dict]:
    fn = {
        "route": norm_route,
        "route_station": norm_station_of_route,
        "bus_pos": norm_bus_pos,
        "station": norm_station,
        "arrival": norm_arrival,
    }[kind]
    return [fn(r) for r in rows]
