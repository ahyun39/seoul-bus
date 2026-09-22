-- 분석 스키마 — raw → clean → mart
--
-- 수집 DB(ddl.sql)와 분리한 이유는 수명이 달라서다. 여기 있는 모든 테이블은
-- JSONL 에서 재생성 가능하다 — 지우고 다시 돌려도 결과가 같다.

-- ─────────────────────────────────────────────── raw: 원본 보존, dedup 만
--
-- event_id 가 PK — 같은 파일을 다시 돌려도, 재전송으로 같은 이벤트가 와도 행이 안 는다.
CREATE TABLE IF NOT EXISTS raw_events (
    event_id   TEXT PRIMARY KEY,
    ingest_ts  TEXT NOT NULL,
    src_file   TEXT NOT NULL,          -- 어느 landing 파일에서 왔는지 (재처리 추적용)
    payload    TEXT NOT NULL           -- 원본 JSON 한 줄 그대로
);

-- 거부된 이벤트. 파일로만 두면 '늘었다'는 사실만 알고 '무엇이 왜'는 못 본다.
CREATE TABLE IF NOT EXISTS dlq_events (
    dlq_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at  TEXT NOT NULL,
    event_date   TEXT NOT NULL,        -- received_at(KST) 기준
    event_id     TEXT,                 -- 없을 수 있다 (필수 필드가 빠진 거부)
    event_name   TEXT,
    session_id   TEXT,
    reason       TEXT NOT NULL,        -- 첫 번째 오류 메시지
    errors       TEXT NOT NULL,        -- 전체 오류 목록 (JSON)
    raw          TEXT,                 -- 재처리의 근거
    src_file     TEXT NOT NULL,
    UNIQUE (src_file, received_at, event_id, reason)   -- 다시 읽어도 늘지 않는다
);
CREATE INDEX IF NOT EXISTS idx_dlq_date ON dlq_events (event_date, reason);

-- 거부 사유별 집계 — 어떤 이벤트가 어떤 이유로 몇 건.
DROP VIEW IF EXISTS fact_rejection;
CREATE VIEW fact_rejection AS
SELECT event_date, COALESCE(event_name, '(이름 없음)') AS event_name, reason,
       COUNT(*) AS rejected,
       SUM(CASE WHEN raw IS NOT NULL THEN 1 ELSE 0 END) AS replayable
FROM dlq_events GROUP BY event_date, event_name, reason;

-- 증분 적재 커서 — 어느 파일을 어디까지 읽었는지.
-- 없으면 매번 전량을 다시 읽어 '중복 도착'과 '재적재'가 한 숫자로 섞인다.
CREATE TABLE IF NOT EXISTS ingested_files (
    name         TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,      -- 바이트. 파일이 자라면 다시 읽는다
    rows         INTEGER NOT NULL,
    ingested_at  TEXT NOT NULL
);

-- 적재 실행 이력. '읽은 것 중 몇 건이 이미 있었나(중복 도착)'는 적재 순간에만
-- 알 수 있고 테이블에 흔적이 남지 않아서 따로 적어 둔다.
CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT NOT NULL,
    read_rows   INTEGER NOT NULL,
    new_rows    INTEGER NOT NULL,
    duplicates  INTEGER NOT NULL,
    unparsable  INTEGER NOT NULL
);

-- ─────────────────────────────────────────────── clean: 버전 차이를 흡수한 정규화
--
-- schema_version 이 섞여 들어온다(1.0.0 은 intent·clock_skew_ms·target_id,
-- 1.2.0 은 entry/current_intent·queue/ingest_delay_ms·이름 있는 ID).
-- 차이를 여기서 한 번만 흡수해 마트와 쿼리는 버전을 모르게 한다.
DROP TABLE IF EXISTS clean_events;
CREATE TABLE clean_events (
    event_id       TEXT PRIMARY KEY,
    event_date     TEXT NOT NULL,      -- ingest_ts(KST) 기준. event_ts 는 클라이언트 시계라 파티션 키로 쓰지 않는다
    event_ts       TEXT,
    sent_ts        TEXT,               -- 배치 키의 일부다 (session_id + sent_ts = 전송 한 번)
    ingest_ts      TEXT,
    schema_version TEXT,
    sdk_version    TEXT,               -- 특정 SDK 버전에서만 유실률이 높은지 보려면 필요하다
    service        TEXT,               -- 지금은 한 서비스뿐이지만, 늘어나면 모든 지표의 분모가 된다
    ingest_source  TEXT,               -- live / replay. 원본의 source 를 이름만 바꿔 담는다
                                       -- (클라이언트의 data_source 와 값이 겹쳐 헷갈리기 때문)
    batch_trigger  TEXT,               -- interval / count / hidden / pagehide / startup
    batch_attempt  INTEGER,            -- 몇 번째 전송 시도였나
    client_dropped INTEGER,            -- 그 배치 시점까지 클라이언트가 버린 누적 건수
    event_name     TEXT NOT NULL,
    event_type     TEXT NOT NULL,      -- product / system
    session_id     TEXT NOT NULL,
    seq            INTEGER,
    seq_trustworthy INTEGER NOT NULL,  -- 1.2.0 미만의 seq 는 페이지 로드 단위라 gap 계산에 쓸 수 없다
    entry_intent   TEXT,
    current_intent TEXT,
    search_id      TEXT,
    search_id_estimated INTEGER NOT NULL DEFAULT 0,  -- 1.2.0 이전 데이터는 세션·순서로 추정해 채운 값
    search_type    TEXT,
    query          TEXT,
    result_count   INTEGER,
    result_status  TEXT,
    data_source    TEXT,
    position       INTEGER,
    route_id       TEXT,
    ars            TEXT,
    station_id     TEXT,
    endpoint       TEXT,
    status         INTEGER,
    error_code     TEXT,               -- 외부 API 가 준 오류 코드. 없으면 '왜 실패했는지'를 알 수 없다
    cache          TEXT,
    latency_ms     INTEGER,
    data_age_sec   INTEGER,
    trigger        TEXT,
    visible_ms     INTEGER,            -- page.leave 의 체류 시간
    from_screen    TEXT,               -- nav.click 이 어느 화면에서 났는지 (원본 필드명은 from)
    next_mode      TEXT,               -- intent.select 가 어느 모드로 이어졌는지
    control        TEXT,               -- ui.toggle 이 무엇을 바꿨는지
    value          TEXT,               -- ui.toggle · sample.pick 의 값
    queue_delay_ms INTEGER,
    ingest_delay_ms INTEGER,
    device         TEXT,
    ts_suspect     INTEGER NOT NULL DEFAULT 0   -- |event_ts - ingest_ts| > 24h
    -- ip_masked 는 일부러 뺀다. 마스킹했어도 분석 계층까지 내릴 이유가 없고,
    -- 필요해지면 raw_events 에서 꺼내면 된다.
);
CREATE INDEX IF NOT EXISTS idx_clean_date_name ON clean_events (event_date, event_name);
CREATE INDEX IF NOT EXISTS idx_clean_search ON clean_events (search_id);
-- fact_search 의 선집계 경로. 30만 건 기준 307ms → 258ms (측정값).
-- 전체 집계인 fact_session·fact_pipeline_health 는 인덱스로 줄지 않는다.
CREATE INDEX IF NOT EXISTS idx_clean_name_session_search
    ON clean_events (event_name, session_id, search_id);
CREATE INDEX IF NOT EXISTS idx_clean_session ON clean_events (session_id, seq);

-- ─────────────────────────────────────────────── mart: 질문 하나당 뷰 하나

-- 검색 한 번 = 한 행. 클릭을 먼저 묶고 조인한다 — 바로 조인하면 검색 행이 클릭 수만큼 분다.
--
-- search.query 가 같은 search_id 로 두 번 기록될 수 있다(doSearch 재진입 — 실제 로그에서 나왔다).
-- 이벤트를 그대로 쓰면 검색 하나가 두 행이 되고 SUM(clicks) 가 같은 클릭을 두 번 센다.
-- grain 은 뷰가 지킨다 — 쿼리마다 DISTINCT 를 기억하게 만들면 그건 해석 규칙이지 모델이 아니다.
DROP VIEW IF EXISTS fact_search;
CREATE VIEW fact_search AS
WITH q AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY session_id, search_id
                                 ORDER BY COALESCE(seq, 0), event_ts) AS rn
    FROM clean_events WHERE event_name = 'search.query'
)
SELECT q.search_id, q.event_date, q.session_id, q.current_intent,
       q.search_type, q.query, q.result_count, q.result_status, q.data_source, q.latency_ms,
       COALESCE(c.clicks, 0)                       AS clicks,
       c.first_position,
       CASE WHEN c.search_id IS NOT NULL THEN 1 ELSE 0 END AS clicked,
       CASE WHEN c.first_position = 1 THEN 1 ELSE 0 END    AS clicked_first,
       COALESCE(v.views, 0)                        AS views,
       q.search_id_estimated
FROM q
-- 조인 키에 session_id 를 함께 둔다. search_id 는 클라이언트가 만든 짧은 난수라
-- 세션이 많아지면 충돌하고, 그러면 남의 세션 클릭이 내 검색에 붙는다(est- 도 동일).
LEFT JOIN (SELECT session_id, search_id, COUNT(*) AS clicks, MIN(position) AS first_position
           FROM clean_events WHERE event_name = 'search.click' AND search_id IS NOT NULL
           GROUP BY session_id, search_id) c
       ON c.search_id = q.search_id AND c.session_id = q.session_id
LEFT JOIN (SELECT session_id, search_id, COUNT(*) AS views
           FROM clean_events WHERE event_name IN ('route.view','station.view') AND search_id IS NOT NULL
           GROUP BY session_id, search_id) v
       ON v.search_id = q.search_id AND v.session_id = q.session_id
WHERE q.rn = 1;

-- 세션 한 개 = 한 행. 자동 갱신(system)은 뺀다 — 안 그러면 세션 지표를 타이머가 결정한다.
DROP VIEW IF EXISTS fact_session;
-- entry_intent 는 시간 순서로 뽑는다. MIN(current_intent) 은 알파벳순 최솟값이다
-- (catch_now < what_comes < where_bus).
CREATE VIEW fact_session AS
WITH first_intent AS (
    SELECT session_id, current_intent,
           ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY COALESCE(seq, 0), event_ts) AS rn
    FROM clean_events WHERE event_name = 'intent.select'
)
SELECT session_id,
       MIN(event_date)                                                   AS event_date,
       COALESCE(MAX(entry_intent),
                (SELECT fi.current_intent FROM first_intent fi
                  WHERE fi.session_id = clean_events.session_id AND fi.rn = 1)) AS entry_intent,
       COUNT(*)                                                          AS events_all,
       SUM(CASE WHEN event_type = 'product' THEN 1 ELSE 0 END)           AS events_product,
       SUM(CASE WHEN event_name = 'search.query' THEN 1 ELSE 0 END)      AS searches,
       SUM(CASE WHEN event_name IN ('route.view','station.view') THEN 1 ELSE 0 END) AS views,
       COUNT(DISTINCT CASE WHEN event_name = 'intent.select' THEN current_intent END) AS intents_used,
       -- 시계가 어긋난 이벤트 제외. 하나만 섞여도 세션 길이가 음수나 며칠이 된다.
       MIN(CASE WHEN ts_suspect = 0 THEN event_ts END)                   AS started_at,
       MAX(CASE WHEN ts_suspect = 0 THEN event_ts END)                   AS ended_at,
       (julianday(MAX(CASE WHEN ts_suspect = 0 THEN event_ts END))
        - julianday(MIN(CASE WHEN ts_suspect = 0 THEN event_ts END))) * 86400 AS duration_sec,
       -- 이탈 이벤트가 있으면 '마지막 클릭'이 아니라 '떠난 시각'까지가 체류 시간.
       MAX(CASE WHEN event_name = 'page.leave' THEN visible_ms END)      AS visible_ms,
       MAX(CASE WHEN event_name = 'page.leave' THEN 1 ELSE 0 END)        AS has_leave,
       MIN(seq_trustworthy)                                              AS seq_trustworthy,
       -- seq 는 세션에서 1부터 시작한다. MIN(seq) 기준으로 재면 세션 앞부분이
       -- 통째로 유실됐을 때(큐가 오래된 것부터 버린다) 그 손실이 안 보인다.
       MAX(seq) - COUNT(DISTINCT seq)                                    AS seq_gap,
       MAX(client_dropped)                                               AS client_dropped
FROM clean_events GROUP BY session_id;

-- 외부 호출 관측 — 캐시 적중이 실제로 호출을 줄였는지.
DROP VIEW IF EXISTS fact_api_call;
CREATE VIEW fact_api_call AS
SELECT event_date, endpoint, cache,
       COUNT(*)                                          AS calls,
       SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END)    AS errors,
       MIN(latency_ms)                                   AS min_latency_ms,
       CAST(AVG(latency_ms) AS INT)                      AS avg_latency_ms,
       MAX(latency_ms)                                   AS max_latency_ms,
       SUM(CASE WHEN data_age_sec IS NULL THEN 1 ELSE 0 END) AS unknown_freshness
FROM clean_events WHERE event_name = 'api.call'
GROUP BY event_date, endpoint, cache;

-- 전송 한 번 = 한 행, 키는 (session_id, sent_ts).
-- '어떤 계기로 몇 번째 시도에 몇 건'은 이벤트 단위로는 못 푸는 질문이다.
DROP VIEW IF EXISTS fact_batch;
CREATE VIEW fact_batch AS
SELECT session_id, sent_ts,
       MIN(event_date)        AS event_date,
       MIN(batch_trigger)     AS trigger,
       MAX(batch_attempt)     AS attempt,
       MAX(client_dropped)    AS client_dropped,
       COUNT(*)               AS events,
       MAX(ingest_delay_ms)   AS ingest_delay_ms,
       MAX(queue_delay_ms)    AS max_queue_delay_ms
FROM clean_events
WHERE sent_ts IS NOT NULL
GROUP BY session_id, sent_ts;

-- 수집 파이프라인 자체의 건강 상태 — 로그를 모으는 시스템도 관측 대상이다.
DROP VIEW IF EXISTS fact_pipeline_health;
CREATE VIEW fact_pipeline_health AS
SELECT event_date,
       COUNT(*)                                             AS events,
       COUNT(DISTINCT session_id)                           AS sessions,
       SUM(ts_suspect)                                      AS ts_suspect,
       SUM(CASE WHEN queue_delay_ms IS NULL THEN 1 ELSE 0 END) AS delay_unknown,
       CAST(AVG(queue_delay_ms) AS INT)                     AS avg_queue_delay_ms,
       MAX(queue_delay_ms)                                  AS max_queue_delay_ms,
       CAST(AVG(ingest_delay_ms) AS INT)                    AS avg_ingest_delay_ms,
       -- 클라이언트가 큐 상한으로 버린 건수. gap 을 전부 서버 유실로 읽지 않으려면 함께 본다.
       (SELECT COALESCE(SUM(client_dropped), 0) FROM
          (SELECT MAX(client_dropped) AS client_dropped FROM clean_events c2
            WHERE c2.event_date = clean_events.event_date
            GROUP BY c2.session_id, c2.sent_ts)) AS client_dropped
FROM clean_events GROUP BY event_date;
