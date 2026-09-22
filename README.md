# 서울버스 노선 뷰어 — 사용자 행동 로그 수집 파이프라인

서울시 공공 버스 데이터를 조회하는 웹앱을 만들고, 그 과정에서 발생하는 사용자 행동 로그를 raw landing부터 분석용 데이터셋까지 연결한 프로젝트입니다.


> 외부 API가 불완전하고, 브라우저에서 이벤트가 유실될 수 있으며, 수집 서버가 잘못된 데이터를 받더라도, 그 사실이 아무 에러 없이 묻히지 않게 하려면 어떻게 설계해야 할까?

flow:

`외부 API 방어 → 정적/동적 분리 → 캐시 → 이벤트 수집 → 검증·부분 수용 → DLQ → raw → clean → mart → 품질 점검`

## 한눈에 보기

| 항목 | 내용 |
|---|---|
| 프로젝트 성격 | 사용자 행동 로그 수집 파이프라인 + 서울버스 조회 웹앱 |
| 핵심 관심사 | 수집 신뢰성 · 데이터 품질 · 외부 API 방어 · 분석 가능성 |
| Backend | FastAPI |
| Frontend | Vanilla JS |
| Storage | SQLite + JSONL |
| 외부 데이터 | 서울시 공공데이터 API (XML · 일 1,000회 한도) |
| 분석 흐름 | `raw → clean → mart` |
| 이벤트 스키마 | `1.3.0` |
| 테스트 | Python `unittest` 109개 + 브라우저 SDK self-check |
| 실행 | `docker compose -f docker/docker-compose.yml up -d` (목업 모드, 인증키 불필요) |
| 현재 범위 | 로컬 단일 호스트 기준의 운영형 설계 검증 |

### Task

1. 외부 API의 HTTP 성공과 데이터 성공을 분리한다.
2. 변경 주기가 다른 데이터를 분리하고, 캐시가 검색 정확성을 깨뜨리지 않게 한다.
3. 브라우저 이벤트를 buffering + batch 전송하고, 실패 시 큐를 보존한다.
4. 잘못된 이벤트만 DLQ로 격리하고 정상 이벤트는 계속 수용한다.
5. raw를 보존한 뒤 증분·멱등 배치로 분석 데이터셋을 재생성한다.
6. 지연·거부·중복·유실 징후와 수집 중단을 별도로 관측한다.

---

## 실행하기

### Demo

**<https://ahyun39.github.io/seoul-bus/>**


노선·정류장 순서는 실제 서울시 API에서 받아 고정한 값이고, 버스 위치와 도착 시각은 현재 정보처럼 보이지 않도록 매번 합성합니다. 서버가 없으므로 이벤트는 표시만 하고 전송하지 않습니다.

### 목업 모드

```bash
docker compose -f docker/docker-compose.yml up -d
# http://127.0.0.1:8000
```

직접 실행:

```bash
pip install -r requirements.txt
USE_MOCK=1 uvicorn app.main:app --reload
```

Docker는 기본이 목업 모드이고 `data/`를 바인드 마운트해 수집한 로그가 컨테이너 밖에 남습니다.

### 실데이터 모드

```bash
cp .env.example .env
# SEOUL_BUS_SERVICE_KEY=[API KEY]

python -m scripts.smoke_live 4312      # 실제 응답의 필드명·의미부터 확인
python -m scripts.preload 4312 402 강남06 160
uvicorn app.main:app --reload
```

### 공공 API 서비스

| 서비스 | 데이터 번호 | 용도 |
|---|---:|---|
| 서울특별시_노선정보조회 | `15000193` | 노선 검색, 정류장 순서, 노선 기본정보 |
| 서울특별시_정류소정보조회 | `15000303` | 정류장 검색, 경유 노선, 도착 정보 |
| 서울특별시_버스위치정보조회 | `15000332` | 현재 버스 위치 |

인증키는 `.env`에만 두고 브라우저로 전달하지 않습니다. 브라우저가 이 API를 직접 부르지 못하는 이유이기도 해서(키 노출 + CORS + http 혼합 콘텐츠) 서버가 중계합니다.

### 분석 · 품질 점검

```bash
python -m scripts.build_marts --report   # raw → clean → mart + 지표 출력
python -m scripts.dq_check --landing     # 수집이 멈췄는지 (마트 없이)
python -m scripts.dq_check               # 적재 후 품질까지
python -m scripts.replay_dlq --dry-run   # DLQ 재처리 대상 확인
```

주기 실행은 cron 또는 systemd timer에 겁니다. 로그는 앱이 도는 host에 쌓이므로 CI 스케줄로는 점검할 수 없습니다.

```cron
0  *  * * *  cd $BUS && .venv/bin/python -m scripts.dq_check --landing
30 4  * * *  cd $BUS && .venv/bin/python -m scripts.build_marts && .venv/bin/python -m scripts.dq_check
0  5  * * 1  cd $BUS && .venv/bin/python -m scripts.preload
```


---

## 아키텍처

```text
┌────────────────────────── Browser ──────────────────────────┐
│  app.js ──→ BusAPI ──→ api-http.js / api-mock.js            │
│     │                                                       │
│     └──→ collector.js                                       │
│            ├─ event_id · session_id · seq                   │
│            ├─ event_ts · sent_ts                            │
│            ├─ localStorage queue (+ dropped)                │
│            └─ batch · sendBeacon                            │
└─────────────────────────┬───────────────────────────────────┘
                          │ POST /v1/collect
                          ▼
┌────────────────────────── FastAPI ──────────────────────────┐
│  schema validation                                          │
│       ├─ accepted ──→ raw JSONL                             │
│       └─ rejected ──→ DLQ (원본 보존 → replay 가능)            │
│                                                             │
│  IP 마스킹 · 텍스트 상한 · 시계 이상 표시 · 배치 메타 보존             │
└─────────────────────────┬───────────────────────────────────┘
                          │ 증분 적재 (읽은 파일·크기 추적)
                          ▼
                 raw_events ──→ clean_events
                                   │
                                   ├─ fact_search           검색 한 번
                                   ├─ fact_session          세션 하나
                                   ├─ fact_batch            전송 한 번
                                   ├─ fact_api_call         날짜 × 엔드포인트 × 캐시
                                   ├─ fact_rejection        날짜 × 이벤트명 × 사유
                                   └─ fact_pipeline_health  날짜 하루
                                   │
                                   ▼
                           dq_check   임계치 초과 → exit 1
```

수집 API는 분석까지 처리하는 두꺼운 계층이 아니라 검증하고 raw landing하는 얇은 계층입니다. 정제와 집계는 별도 배치로 분리해 언제든 다시 실행할 수 있게 했습니다.

---

## 핵심 설계

### 1. 외부 API를 신뢰하지 않는다

공공 API는 HTTP 상태 코드만으로 성공 여부를 판단할 수 없습니다.

- HTTP `200`이지만 본문의 업무 오류 코드가 실패를 나타내는 경우
- XML 항목이 1개일 때 `dict`, 여러 개일 때 `list`로 달라지는 경우
- 오퍼레이션마다 필드명과 의미가 다른 경우
- 초당 호출 제한 · timeout · 전송 오류

핵심은 HTTP 성공 ≠ 데이터 성공입니다. 응답 본문의 `headerCd`를 확인하고, 쉬면 풀리는 오류만 재시도합니다. 초당 제한(코드 23)은 0.6초·1.2초 간격으로, 타임아웃과 전송 오류는 0.5초·1.0초로 간격을 늘려가며 최대 3회까지 시도합니다. 그 밖의 업무 오류 코드는 다시 불러도 같은 답이 오므로 재시도하지 않습니다.

인증키도 여기서 막습니다. 요청 URL에 `serviceKey`가 들어가는데, HTTP 예외를 그대로 올리면 예외 메시지에 URL 전체가 담겨 키가 로그로 새어 나갑니다. 그래서 상태 코드만 꺼내 자체 예외로 변환하고, 남는 경로는 마스킹합니다.

### 2. 정적 데이터와 동적 데이터를 분리한다

| 구분 | 데이터 | 전략 |
|---|---|---|
| 정적 | 노선·정류장 마스터 | SQLite + 사전 적재 |
| 동적 | 버스 위치·도착 예정 | 메모리 TTL 캐시 + live fallback |

정적 마스터는 자연키를 PK로 두어 재적재해도 행이 늘지 않습니다.

```sql
PRIMARY KEY (route_id, seq)
ON CONFLICT(route_id, seq) DO UPDATE SET ...
```

실시간 데이터는 스케줄로 미리 받지 않습니다. 받는 순간 낡고, 아무도 보지 않는 데이터로 일 1,000회 한도를 태우게 됩니다. 같은 이유로 탭이 숨으면 자동 갱신을 멈추고, 연속 20회(10분)에서 스스로 중단합니다.

### 3. 캐시가 검색 정확성을 깨뜨리지 않게 한다

```text
정확히 일치하는 cache hit
        ↓
   cache 반환

그 외
  ↓
실시간 조회
  ↓
기존 cache와 merge
  ↓
cache 저장 + 반환
```

캐시에 일부가 있다고 검색 결과가 완성됐다고 판단하지 않습니다. `152`를 검색했을 때 캐시에 `1522`만 있으면 부분 일치일 뿐이므로, 정확히 일치할 때만 cache-only로 종료하고 그 외에는 live 조회 후 기존 캐시와 합칩니다.

정렬은 deterministic rule(`정확히 일치 → 접두 일치 → 번호 길이 → 사전순`)이며, 문자열 비교라 `강남06` 같은 번호도 같은 규칙으로 처리됩니다.

### 4. 목업과 실데이터의 경계를 분리한다

```text
app.js
  ↓
BusAPI
  ├── api-http.js  → FastAPI
  └── api-mock.js  → Browser Mock
```

목업은 편의 기능이 아니라 외부 의존성 경계입니다. 인증키 없이 동일한 화면 흐름과 이벤트 계측을 반복 검증할 수 있고, 외부 API가 죽었을 때 우리 문제와 저쪽 문제를 가를 수 있습니다.

목업은 `USE_MOCK`으로 명시적으로만 켜집니다. 키가 없을 때 자동으로 목업으로 떨어지면 운영에서 합성 데이터가 아무 에러 없이 화면에 뜨기 때문에, 키가 없으면 기동 시점에 멈춥니다.

---

## 로그 설계

### 5. 이벤트의 grain을 먼저 정한다

이 프로젝트에서 가장 중요한 식별자는 하나가 아니라 목적별로 나뉩니다.

#### `event_id`

클라이언트에서 생성하며 재시도해도 같은 ID를 유지합니다. raw landing에서는 dedup하지 않고, 분석 적재에서 PK로 사용해 중복을 흡수합니다. 수집은 무조건 받고, 중복 판정은 나중에 합니다.

#### `session_id + seq`

마지막 이벤트로부터 30분 무활동이면 새 세션이며, `seq`는 세션 단위로 증가합니다.

`seq_gap`은 유실 징후를 찾는 데 쓰지만 gap 자체가 서버 유실을 뜻하지는 않습니다. 클라이언트가 큐 상한을 넘겨 버린 건수(`client_dropped`)와 함께 해석하며, transport/server 원인을 자동으로 확정하지 않습니다.

#### `search_id`

```text
search.query → search.click → route.view

        같은 search_id
```

`session_id`만으로는 한 세션에서 검색이 여러 번 일어났을 때 어떤 클릭이 어떤 검색에서 나왔는지 추정해야 합니다. `search_id`는 그 추정을 없애고 `fact_search`의 grain을 정의합니다.

#### `entry_intent / current_intent`

처음 선택한 목적과 현재 목적을 분리합니다. 하나로 합치면 한 세션 안에서 목적이 바뀐 경로가 사라집니다.

#### `result_status`

`result_count = 0`만으로는 실제 무결과와 API 장애를 구분할 수 없습니다. 둘을 섞으면 무결과율 지표가 장애 때마다 오염됩니다. 그래서 결과 상태와 `api.call`의 오류 코드를 따로 남깁니다.

#### 배치 메타

`batch_trigger` · `batch_attempt` · `client_dropped`를 각 레코드에 복제해 전송 단위의 사실을 보존합니다. 응답으로만 돌려주면 나중에 "몇 건을 버렸나"를 되짚을 수 없습니다.

### 6. 이벤트 스키마

| 이벤트 | 유형 | 의미 |
|---|---|---|
| `intent.select` | product | 목적 선택 |
| `search.query` | product | 검색 |
| `search.click` | product | 검색 결과 선택 |
| `nav.click` | product | 검색을 거치지 않은 이동 |
| `route.view` / `station.view` | product | 조회 도달 |
| `ui.toggle` / `sample.pick` | product | 화면 조작 |
| `page.leave` | product | 이탈 |
| `api.call` | system | 외부 API 관측 |
| `refresh` | system | 자동 갱신 관측 |

`event_type`은 서버가 `event_name`에서 도출합니다. 클라이언트가 임의로 product/system을 바꿀 수 없게 해, 사용자 행동 지표에 시스템 이벤트가 섞이는 것을 막습니다.

같은 이유로 `service`도 본문 값이 아니라 `write_key`에서 서버가 도출합니다. 본문을 믿으면 누구나 남의 서비스명으로 로그를 밀어 넣을 수 있습니다.

### 7. 지연 시간을 한 숫자로 합치지 않기

```text
event_ts ── queue ──> sent_ts ── network ──> ingest_ts

queue_delay_ms  = sent_ts   - event_ts     (브라우저 큐 체류)
ingest_delay_ms = ingest_ts - sent_ts      (전송 + 서버 수용)
```

초기에는 `clock_skew_ms`라고 불렀지만, 실제로는 시계 차이와 네트워크 지연을 분리해 측정한 값이 아니었습니다.

현재 구현에서는:

- queue_delay_ms: 이벤트 생성부터 배치 전송까지
- ingest_delay_ms: 배치 전송부터 서버 수신까지

로 분리합니다.

실제 시계 오차를 측정하지 않는 이상 `clock skew`라고 부르지 않습니다. 알 수 없는 값은 `0`이 아니라 `null`로 둡니다.

---

## 브라우저 SDK

### 8. buffering + batch 전송

track()은 호출 즉시 HTTP 요청을 만들지 않습니다.

```text
track()
  ↓
localStorage queue
  ↓
이벤트 수 / 시간 / 이탈 조건
  ↓
batch 전송
```

이탈 시점에는 `sendBeacon`을 사용합니다.

중요한 점은 `sendBeacon`의 반환값이 서버 저장 성공을 의미하지 않는다는 것입니다. 브라우저 전송 큐에 넣을 수 있었는지만 확인할 수 있으므로, 이탈 시점 전송은 best-effort입니다.

#### 전송 실패 규칙

| 상황 | 규칙 |
|---|---|
| 4xx (408 · 429 제외) | 폐기 + 건수 기록 |
| 408 · 429 | 일시적 오류로 보고 재시도 |
| 5xx · 네트워크 | 큐 유지 + backoff (5초 → 60초) |
| 이탈 | backoff를 건너뜀 |
| 큐 상한 초과 | 오래된 이벤트부터 폐기 + `dropped` 기록 |

서버 응답으로 성공한 이벤트를 큐에서 제거할 때는 `event_id`를 기준으로 판단합니다. 위치 기반으로 “앞의 N개가 성공했다”고 가정하지 않습니다.

또한 `track()`의 payload가 다음 envelope 필드를 덮어쓰지 못하도록 보호합니다.

- event_id
- event_ts
- session_id
- seq
- 기타 예약 필드

이 규칙은 브라우저 self-check로 회귀 검증합니다.

---

## 수집 서버의 데이터 품질

### 9. 부분 수용 + DLQ

```text
50건 수신

├── 49건 accepted → raw JSONL
└──  1건 rejected → DLQ (원본 + 검증 오류)
```

한 건이 잘못됐다고 전체 배치를 거부하지 않습니다.

`읽은 건수 = accepted + rejected` 관계도 테스트로 고정했습니다.

#### 서버측 보호

- 요청 본문 크기 · 이벤트 개수 상한
- JSON 형식 · 필수 필드 · 허용된 이벤트명 · 타입 검증
- 검색어(`query`) 길이 상한 200자
- 원본 IP 즉시 마스킹
- 크게 어긋난 클라이언트 시계는 폐기하지 않고 event_ts_suspect로 표시
- 시간 · 워커 단위 JSONL 분리
- 동기 파일 I/O를 thread pool로 분리
- 거부된 이벤트는 원본과 검증 오류를 함께 DLQ에 남겨 replay할 수 있습니다.

#### `write_key`
브라우저에 노출되므로 비밀키가 아닙니다. 등록된 클라이언트 설정을 식별하고 `service` 값을 서버가 결정하는 데 사용합니다.

공개 서비스 수준에서는 rate limiting · abuse blocking · request signing 등이 별도로 필요합니다.

---
## 분석 파이프라인

### 10. raw → clean → mart

```text
data/events/*.jsonl
       ↓  읽은 파일 · 크기 추적 (ingested_files)
       ↓  event_id PK 기반 적재 → 재도착 건수 보고 (ingest_runs)
raw_events        원본 JSON 한 줄 그대로
       ↓  스키마 버전 정규화 (_normalize)
clean_events      분석 컬럼 + 신뢰도 플래그
       ↓
fact_search · fact_session · fact_batch · fact_api_call · fact_rejection · fact_pipeline_health
```

#### 왜 나누었나
수집은 가능한 한 실패하지 않아야 하고, 정제는 언제든 다시 실행할 수 있어야 합니다.

수집 시점에 분석 로직을 넣지 않기 때문에 정제 규칙을 변경해도 raw에서 과거 데이터를 다시 만들 수 있습니다.

#### 증분 + 멱등
크기가 그대로인 파일은 건너뜁니다. 크기가 달라진 파일은 처음부터 다시 읽고, 이미 들어온 줄은 `event_id` PK가 흡수합니다. 읽은 구간의 offset은 추적하지 않습니다.

재도착 건수(`읽은 줄 - 새로 들어간 줄 - 파싱 실패`)는 `ingest_runs`에 실행마다 남깁니다. 적재가 끝나면 테이블에는 흔적이 남지 않는 값이라 그 순간에 기록해 둡니다.

한계도 여기서 나옵니다. 건너뛴 파일 덕분에 이미 다 읽은 파일이 재도착률을 부풀리지는 않지만, 크기가 커진 파일에서 나온 재도착 건수에는 실제 재전송과 같은 줄의 재읽기가 함께 들어갑니다. 현재 시간대 파일은 계속 커지므로 대개 이 경우에 해당합니다. 둘을 가르려면 파일별 읽기 offset이 필요합니다.

---

## 관측과 실제 검증

### 11. 수집이 멈춘 것을 별도로 감시한다

로그 수집은 에러가 아니라 아무것도 기록되지 않는 상태로 고장납니다. 기록이 없다는 사실만으로는 "오늘 사용자가 없었다"와 구분되지 않습니다.

그래서 점검을 두 단계로 나눕니다. 마트가 있어야만 점검이 되면 적재 정지 자체를 놓치기 때문에 1단계는 적재 배치 없이 돕니다.

| 단계 | 주요 점검 |
|---|---|
| landing | 마지막 `ingest_ts` · 물량 변화 · 스키마 버전 혼재 · 거부율 |
| mart | 중복 도착 · `seq_gap` · 시계 이상 · 검색 중복 |

신선도는 파일 mtime이 아니라 로그 안의 `ingest_ts` 기준입니다. `dq_check`는 임계치를 넘으면 non-zero로 종료해 cron·systemd에 그대로 걸 수 있습니다.

임계치는 "이 시간 동안 아무도 안 쓰는 게 정상인가"로 정합니다. 너무 빡빡하면 매일 울리고, 매일 울리는 알람은 아무도 보지 않습니다.

### 12. 실제 수집 데이터

실제 서울시 API에 연결해 수집·적재한 표본은 57건, 2세션입니다. 사용자 행동의 통계가 아니라 파이프라인이 무엇을 드러내는지 보여주는 사례입니다.

```text
수집 57건 · 세션 2 · 중복 0 · 파싱불가 0 · DLQ 0

검색 퍼널 (검색 단위)          외부 호출                수집 지연
  search.query   7              캐시 적중  8회  0~1ms    큐 대기  평균 2,557~2,780ms
  search.click   7 (100.0%)     외부 호출 10회 24~324ms  전송     평균 3~4ms
  1위 결과 클릭   4 ( 57.1%)     오류       0회           시계 이상  0건
  조회 도달       7 (100.0%)                              seq gap   0건
```

관찰된 것:

- 총 수집 지연의 99% 이상이 브라우저 큐 대기였습니다(큐 대기 2,557~2,780ms 대 전송 3~4ms). 병목은 서버가 아니라 SDK의 전송 주기입니다.
- 캐시 적중 0~1ms, 외부 호출 24~324ms. §2의 정적/동적 분리가 아낀 것이 이 차이입니다.
- 한 세션에서 목적이 3번 바뀌는 경로가 남았습니다. `entry_intent`와 `current_intent`를 분리했기 때문에 보이는 기록입니다.
- `station_arrival`의 `data_age_sec`가 7회 모두 `null` 이었습니다. 화면에도 로그에도 에러는 없었고, 원인은 응답 정규화에서 원본 수집 시각을 빠뜨린 것이었습니다.

### 13. 실제 API를 붙여야만 나온 문제

목업만으로는 잡히지 않았던 것들입니다.

#### 형제 API도 필드 의미가 다르다

정류소 도착정보의 `rerideNum1`은 혼잡도 코드가 아니라 재차인원(명)입니다. 혼잡도 코드로 읽으면 승객 1명이 '정보없음', 4명이 '보통'이 됩니다. 척도 자체가 다릅니다.

#### 단위가 글자로 적힌 값을 쓴다

`traTime1`은 명세의 항목 크기와 샘플 값이 어긋나 분인지 초인지 갈립니다. 그래서 `arrmsg1`의 `"3분12초후"`를 파싱합니다. `출발대기` · `운행종료` 같은 상태를 `0초`로 바꾸면 오지 않는 버스가 "곧 도착"으로 표시됩니다.

#### 정류장은 이름이 아니라 ARS로 식별한다

`능인선원앞`이 방향에 따라 `23365` / `23367`로 나뉩니다. 화면에는 똑같은 두 줄이지만 반대편 정류장입니다.

실제로 클릭 이벤트의 ARS와 조회 응답의 내부 ID가 같은 `station_id` 이름을 쓰는 바람에, 조인 결과가 0건인데 에러는 나지 않는 문제가 수집 로그에서 나왔습니다. 스키마 `1.2.0`에서 클릭 계열은 `ars`, 조회는 `station_id`와 `ars`를 함께 남기도록 정리했습니다.

#### 원문의 오타도 그대로 따른다

버스 위치 API의 혼잡도 필드는 `congetion`입니다. `congestion`으로 읽으면 값이 전부 비어서 나옵니다.

### 14. 합성 부하로 처리 성능 측정

수집 경로와 적재 경로를 실제 코드 그대로 통과시켜 측정했습니다. 다만 `collector.accept()` 를 직접 부르는 단일 프로세스 측정이라 HTTP·동시성은 빠져 있습니다. 검증과 JSONL 기록의 처리량이지 엔드포인트 처리량이 아닙니다.

```bash
python -m scripts.loadgen 100000     # 임시 디렉터리에서 돌고 제거
python -m scripts.loadgen 1000000
```

| | 10만 건 | 100만 건 |
|---|---:|---:|
| 수집 (검증 + landing) | 0.8초 · 119,928건/s | 8.3초 · 120,715건/s |
| 적재 (`raw → clean → mart`) | 2.2초 · 45,683건/s | 27.8초 · 35,958건/s |
| `fact_search` 조회 | 83ms | 1,305ms |
| `fact_session` 조회 | 46ms | 497ms |

> 이 수치는 파이프라인 처리 성능이며 사용자 행동 통계가 아닙니다. 실행 환경에 따라 달라집니다.

데이터가 10배가 될 때 `fact_search` 조회는 16배 느려졌습니다. 전체 테이블 집계가 필요한 뷰는 인덱스만으로 해결되지 않는다는 한계를 그대로 드러냅니다.

---

## 테스트

### 15. 테스트는 설계 보장을 검증한다

Python 회귀 테스트 109개와 브라우저 SDK self-check가 있습니다. 커버리지 숫자가 아니라 앞에서 설명한 보장 하나하나를 고정하는 것이 목적입니다.

| 검증 | 무엇을 지키는가 |
|---|---|
| 외부 API 오류 코드 · XML 구조 정규화 | §1 HTTP 성공 ≠ 데이터 성공 |
| 인증키가 예외 메시지에 노출되지 않는지 | §1 키 유출 차단 |
| live fallback · on-demand hydration | §3 캐시가 정확성을 깨지 않음 |
| 서버의 `event_type` 판정 | §6 클라이언트가 못 바꿈 |
| queue/ingest 지연 분리 측정 | §7 지연을 합치지 않음 |
| 부분 수용 + DLQ replay | §9 불량 이웃 격리 |
| 원본 IP 마스킹 | §9 개인정보 |
| 증분 · 멱등 적재 | §10 다시 실행 가능 |
| `search_id` 기반 퍼널 grain | §5 검색 단위 |
| `seq_gap` 탐지 · 수집 중단 탐지 | §11 에러 없는 고장 |
| 스키마 버전 일치 (클라이언트 ↔ 서버) | 봉투 불일치 방지 |

브라우저 SDK는 `collector_selfcheck.js`에서 envelope 보호 · 재시도 · 백오프 · 이탈 시 전송을 별도로 점검합니다(Node, 의존성 없음).

전부 목업 모드에서 돌아 인증키 없이 CI에서 실행됩니다. CI가 초록불이면 clone한 사람도 그대로 돌릴 수 있습니다.

---

## 현재 범위와 한계

이 프로젝트는 production 규모의 중앙 로그 플랫폼이 아니라, 운영을 고려한 수집·분석 계층을 로컬 환경에서 검증한 것입니다.

| 항목 | 현재 구현 | 확장 방향 |
|---|---|---|
| 수집 | FastAPI + raw JSONL | 중앙 큐 + object storage |
| 브라우저 buffering | `localStorage` | IndexedDB 등 |
| 중복 제거 | 분석 적재 시 `event_id` PK | 중앙 dedup 저장소 |
| 분석 적재 | 증분 `raw → clean → mart` | 파티셔닝 · 증분 재생성 |
| 재도착 집계 | 파일 크기로 변경만 감지 — 재전송과 재읽기가 한 숫자에 섞임 | 파일별 읽기 offset 추적 |
| 유실 분석 | `seq_gap` + `client_dropped` | transport/server 원인 세분화 |
| 품질 점검 | `dq_check` + exit code | 알림 채널 연계 |
| 스케줄 실행 | cron · systemd | 오케스트레이터 |
| 수집 속도 제한 | 없음 (`write_key`는 공개 식별자) | token bucket + `429` |
| 보존 기간 | 정의만 있고 삭제 코드 없음 | 회전 · 압축 · 만료 배치 |
| 정류장 검색 | 적재된 노선 범위 | 전체 마스터 또는 FTS |

### 프로젝트 구조

```text
app/            외부 API 클라이언트 · 캐시 · FastAPI · 수집기 · 프런트엔드
scripts/        사전적재 · 실API 점검 · 마트 빌드 · 품질 점검 · DLQ 재처리 · 목업 데이터 생성 · 부하 측정
sql/            ddl.sql (수집) · marts.sql (분석)
tests/          회귀 테스트 109개 + 브라우저 SDK self-check
data/samples/   적재 결과 샘플 (이벤트 · DLQ · 마트 리포트)
docker/         Dockerfile · docker-compose.yml
.github/        CI — 테스트 · SDK 점검 (인증키 없이 목업 모드로 실행)
```

---

> 서울버스 데이터를 이용한 사용자 행동 로그 수집 파이프라인
