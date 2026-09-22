-- 정적 마스터 캐시 — 노선 목록/정류장 순서/정류장 마스터.
-- 거의 바뀌지 않는 이 셋을 미리 받아두고, 실시간 호출은 '버스 위치'와 '도착 정보'
-- 둘로 줄인다. 개발계정 일 1,000회 한도를 지키는 핵심 설계 판단.

CREATE TABLE IF NOT EXISTS routes (
    route_id    TEXT PRIMARY KEY,
    no          TEXT NOT NULL,
    type        TEXT,
    from_stop   TEXT,
    to_stop     TEXT,
    first_bus   TEXT,
    last_bus    TEXT,
    interval    TEXT,
    company     TEXT,
    updated_at  TEXT NOT NULL
);
-- 노선번호 접두 검색용
CREATE INDEX IF NOT EXISTS idx_routes_no ON routes (no);

CREATE TABLE IF NOT EXISTS route_stations (
    route_id    TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    station_id  TEXT,
    ars         TEXT,
    name        TEXT NOT NULL,
    direction   TEXT,
    -- 회차지(API 의 transYn). 상·하행이 하나의 연속된 seq 로 내려오므로
    -- 이 값만이 방향을 가른다. 없으면 노선도가 한 방향으로만 그려진다.
    is_turn     INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (route_id, seq)          -- 재적재는 덮어쓰기 (멱등)
);
CREATE INDEX IF NOT EXISTS idx_rs_ars ON route_stations (ars);

CREATE TABLE IF NOT EXISTS stations (
    station_id  TEXT PRIMARY KEY,
    ars         TEXT,
    name        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- 이름 검색은 LIKE '%q%' 라 인덱스를 못 탄다(EXPLAIN QUERY PLAN 확인). 쓰기 비용만
-- 늘어서 제거. 서울 전체가 만 단위라 스캔으로 충분하고, FTS5 는 이 규모엔 과하다.
DROP INDEX IF EXISTS idx_stations_name;
CREATE INDEX IF NOT EXISTS idx_stations_ars ON stations (ars);

-- 외부 API 호출량 — 일 1,000회 한도 소진 추적. 설계 전체가 이 한도 위에 서 있다.
-- 한도가 KST 자정에 초기화되므로 집계 날짜도 KST 다(UTC 로 세면 오전 9시에 끊긴다).
CREATE TABLE IF NOT EXISTS api_usage (
    day         TEXT NOT NULL,          -- KST YYYY-MM-DD
    endpoint    TEXT NOT NULL,
    calls       INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (day, endpoint)
);
