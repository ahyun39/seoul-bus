"""설정 — 환경변수와 경로.

인증키는 서버 환경변수로만 읽고 브라우저로는 내려보내지 않는다.
브라우저가 공공 API 를 직접 못 부르는 이유이기도 하다(키 노출 + CORS + http 혼합 콘텐츠).
"""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from pathlib import Path

# 서비스 기준 시간대 — 단일 출처. 공공 API 의 시각도, 호출 한도의 자정도 KST 다.
KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "app" / "static"
DDL_PATH = ROOT / "sql" / "ddl.sql"
EVENT_DIR = DATA_DIR / "events"


def _load_dotenv() -> None:
    """.env 파싱. 의존성 하나를 줄이려고 직접 읽는다."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# 공공데이터포털 인증키. Encoding/Decoding 어느 쪽이든 seoul_api.build_query 가 맞춘다.
SERVICE_KEY: str = os.environ.get("SEOUL_BUS_SERVICE_KEY", "").strip()

# 서울시 버스 운행정보 API. http 전용이라 서버가 중계한다.
API_BASE = "http://ws.bus.go.kr/api/rest"

# 목업은 USE_MOCK 으로 명시적으로 켠다 — 기본값은 실데이터.
# 키가 없을 때 조용히 목업으로 떨어지면, 운영에서 가짜 데이터가 에러 없이 화면에 뜬다.
FORCE_MOCK = os.environ.get("USE_MOCK", "").lower() in ("1", "true", "yes")
USE_MOCK: bool = FORCE_MOCK

# 정적 마스터 캐시 — 모드별로 파일을 나눈다.
# 공유하면 목업이 적어둔 합성 노선이 실데이터 모드에서 '캐시 적중'으로 서빙된다.
CACHE_DB = DATA_DIR / ("bus_cache_mock.db" if USE_MOCK else "bus_cache.db")


class MissingServiceKey(RuntimeError):
    """키 없이 실데이터 모드로 기동할 때. 조용히 목업으로 떨어지지 않는다."""


def require_service_key() -> None:
    if not USE_MOCK and not SERVICE_KEY:
        raise MissingServiceKey(
            "SEOUL_BUS_SERVICE_KEY 가 비어 있습니다.\n"
            "  · 실데이터로 실행: .env 에 공공데이터포털 인증키를 넣으세요 "
            "(cp .env.example .env)\n"
            "  · 데이터 없이 화면만 보기: USE_MOCK=1 로 실행하세요\n"
            "  키가 통하는지 먼저 확인하려면: python -m scripts.smoke_live 4312"
        )

# 실시간 호출 캐시 TTL(초). 새로고침 연타가 외부 API 로 나가지 않게 막는다.
LIVE_TTL_SEC = int(os.environ.get("LIVE_TTL_SEC", "20"))

# 실시간 캐시 항목 상한. 초과 시 LRU 제거 — 없으면 조회된 키가 전부 메모리에 남는다.
LIVE_CACHE_MAX = int(os.environ.get("LIVE_CACHE_MAX", "500"))

# 외부 API 타임아웃. 없으면 워커가 묶여 연쇄 장애가 난다.
API_TIMEOUT_SEC = float(os.environ.get("API_TIMEOUT_SEC", "4.0"))

# 수집 배치 상한 — 본문 크기와 이벤트 수 양쪽을 막는다.
MAX_COLLECT_BYTES = int(os.environ.get("MAX_COLLECT_BYTES", "131072"))   # 128KiB
MAX_COLLECT_EVENTS = int(os.environ.get("MAX_COLLECT_EVENTS", "100"))

# write_key -> service 매핑. write_key 는 비밀이 아니라 식별자다(클라이언트에 노출).
# 그래도 service 는 서버가 이 표로 도출한다 — 본문 값을 믿으면 남의 서비스명을 사칭할 수 있다.
WRITE_KEYS: dict[str, str] = {}
for pair in os.environ.get("WRITE_KEYS", "wk_seoul_bus_demo:seoul-bus-web").split(","):
    if ":" in pair:
        _k, _v = pair.split(":", 1)
        WRITE_KEYS[_k.strip()] = _v.strip()

# 이벤트 스키마 버전. 스키마 단일 출처는 docs/event-schema.md.
# 봉투가 바뀌면 collector.js 의 SCHEMA_VERSION 과 함께 올린다(일치는 테스트가 지킨다).
EVENT_SCHEMA_VERSION = "1.3.0"

# scripts/preload.py 가 미리 적재할 노선
PRELOAD_ROUTES = [
    r.strip() for r in os.environ.get("PRELOAD_ROUTES", "4312,402,강남06,160").split(",") if r.strip()
]

DATA_DIR.mkdir(parents=True, exist_ok=True)
EVENT_DIR.mkdir(parents=True, exist_ok=True)
