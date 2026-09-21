# 서울버스 노선 뷰어 _ 사용자 행동 로그 수집 파이프라인

서울시 공공 버스 데이터를 조회하는 웹앱을 만들고, 그 과정에서 발생하는 **사용자 행동 로그를 raw landing부터 분석용 데이터셋까지 연결한 Data Engineering 프로젝트**입니다.

화면보다 다음 질문에 집중했습니다.

> **외부 API가 불완전하고, 브라우저에서 이벤트가 유실될 수 있으며, 수집 서버가 잘못된 데이터를 받더라도 데이터가 조용히 망가지는 것을 어떻게 막을까?**

`외부 API 방어 → 정적/동적 분리 → 캐시 → 이벤트 수집 → 검증·부분 수용 → DLQ → raw → clean → mart → 품질 점검`을 하나의 흐름으로 구현했습니다.

---

## 한눈에 보기

| 항목 | 내용 |
|---|---|
| 프로젝트 성격 | 사용자 행동 로그 수집 파이프라인 + 서울버스 조회 웹앱 |
| 핵심 관심사 | 수집 신뢰성 · 데이터 품질 · 외부 API 방어 · 분석 가능성 |
| Backend | FastAPI |
| Storage | SQLite + JSONL |
| Frontend | Vanilla JS |
| 외부 데이터 | 서울시 공공데이터 API (XML · 일 1,000회 한도) |
| 분석 흐름 | `raw → clean → mart` (증분 · 멱등) |
| 테스트 | Python `unittest` 109개 + Node SDK self-check |
| 이벤트 스키마 | `1.3.0` ([단일 출처](docs/event-schema.md)) |
| 실행 | `docker compose up -d` (기본 목업 모드, 인증키 불필요) |
| 현재 범위 | 로컬 단일 호스트를 기준으로 한 운영형 설계 연습 |

이 프로젝트에서 보여주려는 것:

1. 외부 API가 실패하거나 응답 구조가 달라도 안전하게 처리합니다.
2. 변경 주기가 다른 데이터를 분리하고, 캐시가 검색 정확성을 떨어뜨리지 않게 합니다.
3. 브라우저 이벤트를 buffering + batch 전송하고, 실패 시 큐를 보존합니다.
4. 잘못된 이벤트만 격리하고 정상 이벤트는 계속 수용합니다(부분 수용 + DLQ).
5. raw를 보존한 뒤 멱등·증분 배치로 분석 데이터셋을 재생성합니다.
6. 중복·유실 징후·지연·거부·API 오류를 데이터로 남기고, 수집이 멈추면 탐지합니다.

---

## 빠르게 확인하기

### Demo

서버와 인증키 없이 브라우저에서 바로 열 수 있습니다.

**[docs/demo.html](docs/demo.html)**

데모에는 다음 노선이 포함됩니다.

- `4312` (지선)
- `402` (간선)
- `강남06` (마을)
- `160` (간선)

노선과 정류장 순서는 실제 서울시 API에서 받아 굳혀 둔 값(`app/mock_data.json`)입니다. 반대로 버스 위치와 도착 시각은 고정하지 않고 매번 합성한 데이터입니다. 정적 마스터는 재사용하되, 동적 운행정보를 과거 값처럼 보여주지 않기 위함입니다.

### 포트폴리오 한 장

**[docs/portfolio.html](docs/portfolio.html)**

로그 스키마와 데이터베이스 모델을 어떤 근거로 설계했는지, 실행 데모에서 이벤트 한 건이 브라우저에서 분석 마트까지 어떻게 이동하는지를 한 페이지에서 볼 수 있습니다.

### 설계 해설

**[docs/index.html](docs/index.html)**

실제 소스 코드 조각과 함께 설계 이유와 현재 구현 범위를 설명합니다.

| 문서 | 내용 |
|---|---|
| [이벤트 스키마](docs/event-schema.md) | 필드 · 열거값 · 검증 규칙 · 버전 이력 (단일 출처) |
| [데이터 모델](docs/data-model.html) | 테이블별 grain · 컬럼 · 키 · 관계 |
| [설계 방어](docs/design-defense.html) | 반론 → 그렇게 하지 않은 이유 → 이 선택이 틀리는 경우 |
| [개선·결함 기록](docs/change-log.md) | 무엇이 잘못됐고 어떻게 알아냈는지, 날짜별 |
| [운영 설정](ops/README.md) | cron · systemd timer 주기 점검 |

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
                                   ├─ fact_api_call         날짜 × 엔드포인트
                                   ├─ fact_rejection        날짜 × 거부 사유
                                   └─ fact_pipeline_health  날짜 하루
                                   │
                                   ▼
                           dq_check   임계치 초과 → exit 1
```

수집 API는 분석까지 처리하는 계층이 아니라 검증하고 raw landing하는 얇은 계층입니다. 정제와 집계는 언제든 다시 돌릴 수 있는 별도 배치로 분리했습니다.

---

## 핵심 설계

### 1. 외부 API를 신뢰하지 않는 구조

공공 API는 HTTP 상태 코드만으로 성공 여부를 판단할 수 없습니다. 방어하는 상황은 HTTP `200` 속 업무 오류 코드, 항목 수에 따라 `dict`/`list`로 바뀌는 XML 구조, 오퍼레이션별로 다른 필드명, 초당·일일 호출 제한, timeout·transport error, 외부 데이터 시각과 캐시 시각의 혼동입니다.

```python
response = await client.get(url)
response.raise_for_status()

items, code, message = self._parse(response.text)

if code not in OK_CODES:
    if code == "23" and attempt < 2:          # 초당 제한은 잠깐 쉬면 풀린다
        await asyncio.sleep(0.6 * (attempt + 1))
        continue
    raise SeoulApiError(code, message, endpoint)
```

> HTTP 성공 ≠ 데이터 성공

`httpx` 예외는 요청 URL을 통째로 메시지에 담고 그 쿼리스트링에는 `serviceKey`가 있습니다. 자체 예외로 변환하고 민감한 쿼리값을 마스킹합니다. `.env`로 뺀 키가 로그로 새면 같은 사고입니다.

---

### 2. 정적 데이터와 동적 데이터를 변경 주기로 분리

| 구분 | 데이터 | 전략 | 외부 API |
|---|---|---|---|
| 정적 | 노선 목록, 노선별 정류장 순서, 정류장 마스터 | SQLite + 사전 적재 | 반복 조회 시 호출 없음 |
| 동적 | 버스 위치, 도착 예정 | 메모리 TTL cache | 요청 시 live + 20초 cache |

정적 마스터는 자연키를 PK로 두어 재적재해도 행이 늘지 않습니다.

```sql
PRIMARY KEY (route_id, seq)
ON CONFLICT(route_id, seq) DO UPDATE SET ...
```

실시간 데이터는 스케줄로 미리 받지 않습니다. 받는 순간 낡고, 아무도 보지 않는 데이터로 일 1,000회 한도를 태우게 됩니다. 같은 이유로 탭이 숨으면 자동 갱신을 멈추고, 연속 20회(10분)에서 스스로 중단합니다.

---

### 3. 캐시가 검색 정확성을 깨뜨리지 않게

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

`152`만 캐시된 상태에서 `152` 검색을 cache-only로 끝내면 아직 적재되지 않은 `1522`를 찾지 못합니다. 그래서 **정확히 일치할 때만 cache-only로 종료**합니다.

정렬은 deterministic rule(`정확히 일치 → 접두 일치 → 번호 길이 → 사전순`)이며, 문자열 비교라 `강남06` 같은 번호도 같은 규칙으로 처리됩니다.

---

### 4. 화면과 데이터 출처를 분리

```text
app.js
  ↓
BusAPI
  ├── api-http.js  → FastAPI
  └── api-mock.js  → Browser Mock
```

`BusAPI` 인터페이스 뒤의 구현체만 교체할 수 있도록 분리했습니다. 목적은 편리한 데모가 아니라 외부 의존성의 경계를 만들고 동일한 화면 흐름을 목업과 실데이터에서 반복 검증하는 것입니다.

---

### 5. 사용자 흐름과 노선도

첫 화면은 검색창이 아니라 **"무엇을 알고 싶으세요?"** 라는 의도 선택으로 시작합니다.

| 의도 | 결과 | 로그 |
|---|---|---|
| 버스가 지금 어디쯤인지 | 노선도 + 운행 중 버스 위치 | `current_intent=where_bus` |
| 이 정류장에 어떤 버스가 오는지 | 경유 노선 + 도착 예정 | `current_intent=what_comes` |
| 지금 바로 탈 수 있는 버스 | 5분 내 도착만 필터링 | `current_intent=catch_now` |

`catch_now`는 API를 추가로 부르지 않고 동일한 도착정보 응답을 화면에서 필터링합니다.

노선도는 지도 SDK 없이 **정류장 순번(`sectOrd`)**으로 그립니다. API가 좌표를 주지 않는 상황에서 GPS 위치처럼 보이게 만들지 않습니다.

상·하행은 API가 직접 주는 방향 값이 아니라 회차지(`transYn`)를 기준으로 계산합니다. 회차지가 없는 편도·순환 노선은 없는 방향을 임의로 만들지 않고 “편도 · 회차 구간 없음”으로 표시합니다.

정류장 검색에서는 이름뿐 아니라 **방면과 ARS**를 함께 보여줍니다. 현재 적재한 정류장 마스터에서 이름 319개 중 113개는 서로 다른 ARS를 가진 같은 이름으로 확인되었습니다.

---

### 6. 이벤트 스키마

단일 출처는 [docs/event-schema.md](docs/event-schema.md)입니다.

| 이벤트 | 유형 | 의미 |
|---|---|---|
| `intent.select` | product | 첫 화면에서 목적 선택 |
| `search.query` / `search.click` | product | 검색 실행 · 결과 선택 |
| `nav.click` | product | 검색을 거치지 않은 이동 |
| `route.view` / `station.view` | product | 노선도 · 정류장 조회 |
| `ui.toggle` / `sample.pick` | product | 화면 내 조작 |
| `page.leave` | product | 탭 숨김 · 이탈 |
| `api.call` / `refresh` | system | 외부 API 호출 · 자동 갱신 관측 |

`api.call`과 `refresh`는 사용자 행동이 아니라 **system telemetry**입니다. 

`event_type`은 서버가 `event_name`에서 도출하고 클라이언트가 보낸 값을 신뢰하지 않습니다. 제품 행동만 분석할 때는 `event_type = 'product'`로 필터링합니다.

주요 필드:

#### `event_id`

클라이언트가 생성합니다. 재시도해도 같은 행동은 같은 ID를 유지합니다.

raw JSONL 단계에서는 dedup하지 않고, 분석 적재에서 `event_id`를 PK로 사용해 중복을 흡수하고 재도착 건수를 보고합니다.

#### `session_id + seq`

세션은 마지막 이벤트로부터 30분 무활동이면 새로 시작합니다. `session_id`와 `seq`는 새로고침 이후에도 이어집니다.

이 구조는 세션 내 순서와 누락 번호를 분석하는 데 사용됩니다. 단, **`seq_gap`만으로 유실 원인을 확정하지 않습니다.** `client_dropped`와 함께 알려진 client-side drop을 구분하고, 나머지는 별도의 원인 분류 없이 “유실 징후”로 취급합니다.

장기 추적용 `user_id`나 `anonymous_id`는 두지 않습니다.

#### `search_id`

`session_id`가 세션 전체를 묶는다면 `search_id`는 **검색 한 번의 시도**를 묶습니다.

```text
search.query → search.click → route.view
                     ↑
               같은 search_id
```

한 세션에서 검색을 여러 번 해도 어떤 클릭이 어느 검색에서 발생했는지 추정할 필요가 없습니다.

#### `entry_intent / current_intent`

처음 선택한 목적과 현재 목적을 분리합니다. 이를 통해 한 세션 안에서 의도가 바뀌는 경로를 보존할 수 있습니다.

실제 수집한 두 세션에서도 여러 `current_intent` 전환이 관찰되었습니다.

#### `result_status`

`result_count = 0`만으로는 “실제로 결과가 없음”과 “API 장애로 빈손”을 구분할 수 없습니다.

따라서 결과 상태를 별도로 기록해 사용자 행동과 시스템 장애를 섞지 않습니다.

#### 배치 메타

`batch_trigger`, `batch_attempt`, `client_dropped` 등 전송 단위의 사실을 함께 기록합니다.

이 값이 있어야 `seq_gap`을 전부 서버 유실로 잘못 해석하지 않고, 알려진 client-side drop과 구분할 수 있습니다.

#### 정류장 식별자

클릭 시점에는 ARS가 핵심 식별자이고 조회 단계에서는 `station_id`와 `ars`를 함께 보존합니다. 정류장 이름만 식별자로 사용하지 않습니다.


---

## 브라우저 SDK

### 7. buffering + batch 전송

`track()`은 호출 즉시 HTTP 요청을 만들지 않습니다.

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

중요한 점은 `sendBeacon`의 반환값이 **서버 저장 성공을 의미하지 않는다는 것**입니다. 브라우저 전송 큐에 넣을 수 있었는지만 확인할 수 있으므로, 이탈 시점 전송은 best-effort입니다.

#### 전송 실패 규칙

| 상황 | 규칙 |
|---|---|
| 응답 대기 중 | 다시 보내지 않음 |
| 영구 4xx | 큐에서 버리고 건수 기록 |
| 408 · 429 | 일시적 오류로 보고 재시도 |
| 5xx · 네트워크 | 큐 유지 + backoff (5초 → 60초) |
| 이탈 시점 | backoff를 건너뜀 |
| 큐 상한 초과 | 오래된 이벤트부터 버리고 `dropped` 기록 |

서버 응답으로 성공한 이벤트를 큐에서 제거할 때는 **`event_id`를 기준**으로 판단합니다. 위치 기반으로 “앞의 N개가 성공했다”고 가정하지 않습니다.

또한 `track()`의 payload가 다음 envelope 필드를 덮어쓰지 못하도록 보호합니다.

- `event_id`
- `event_ts`
- `session_id`
- `seq`
- 기타 예약 필드

이 규칙은 브라우저 self-check로 회귀 검증합니다.

---
### 8. 지연 시간을 한 숫자로 합치지 않기

```text
event_ts ── queue ──> sent_ts ── network ──> ingest_ts

queue_delay_ms  = sent_ts  - event_ts
ingest_delay_ms = ingest_ts - sent_ts
```

예전에는 뒤쪽 값을 `clock_skew_ms`라고 불렀지만, 실제로는 시계 차이와 네트워크 지연을 분리해 측정한 값이 아니었습니다.

현재 구현에서는:

- `queue_delay_ms`: 이벤트 생성부터 배치 전송까지
- `ingest_delay_ms`: 배치 전송부터 서버 수신까지

로 분리합니다.

실제 시계 오차를 측정하지 않는 이상 `clock skew`라고 부르지 않습니다. 알 수 없는 값은 `0`이 아니라 `null`로 둡니다.

---

## 수집 서버의 데이터 품질 방어

### 9. 부분 수용 + DLQ

```text
50건 수신

├── 49건 accepted → raw JSONL
└──  1건 rejected → DLQ
```

한 건이 잘못됐다고 전체 배치를 거부하지 않습니다.

`읽은 건수 = accepted + rejected` 관계도 테스트로 고정했습니다.

#### 서버측 보호

- 요청 본문 크기 · 이벤트 개수 상한
- JSON 형식 · 필수 필드 · 허용된 이벤트명 · 타입 검증
- 자유 텍스트 길이 상한
- 원본 IP 즉시 마스킹
- 크게 어긋난 클라이언트 시계는 폐기하지 않고 `event_ts_suspect`로 표시
- 시간 · 워커 단위 JSONL 분리
- 동기 파일 I/O를 thread pool로 분리

거부된 이벤트는 **원본과 검증 오류를 함께 DLQ에 남겨 replay**할 수 있습니다.

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

이미 읽은 파일은 건너뛰고, 파일이 커진 경우에만 새 구간을 읽습니다.

이벤트 중복은 `event_id` PK로 흡수하고 재도착 건수는 별도로 보고합니다.

따라서:

- 같은 이벤트가 재전송된 경우
- 같은 파일을 적재 배치가 다시 읽은 경우

를 서로 다른 현상으로 관찰할 수 있습니다.

#### 구버전 정규화

구버전 스키마는 `_normalize()`에서 현재 계약으로 변환합니다.

```text
intent
→ current_intent

clock_skew_ms
→ queue_delay_ms / ingest_delay_ms

target_type / target_id
→ route_id / ars

search.click(from=route_map)
→ nav.click
```

복원할 수밖에 없는 값은 `estimated` 또는 `trustworthy` 플래그를 함께 기록합니다.

#### fact의 grain

`api.call`은 요청 단위 raw event이고, `fact_api_call`은 **날짜 × endpoint 단위의 분석 fact**입니다.

다른 fact도 “한 행이 무엇인가”를 먼저 정의해 집계 기준이 섞이지 않도록 했습니다.

---

## 수집이 멈춘 것을 탐지

### 11. Data Quality Check

로그 파이프라인에서 위험한 고장은 에러가 아니라 **아무 것도 나오지 않는 것**입니다.

이를 두 단계로 나눕니다.

| 단계 | 보는 것 |
|---|---|
| landing (`--landing`) | 마지막 수집 시각 · 전일 대비 물량 · 계약 버전 혼재 · 거부율 |
| mart | 중복 도착 · seq gap · 시계 이상 · `search_id` 중복 |

신선도는 파일 mtime이 아니라 **파일 안의 `ingest_ts`**를 기준으로 봅니다.

실제 테스트에서는 다음과 같은 장애 상황을 의도적으로 만들어 탐지합니다.

- 한 번도 수집되지 않음
- 이틀째 수집 중단
- 평소 대비 물량 급감
- 서버보다 높은 계약 버전
- 거부율 5% 초과
- 정상 상태

`dq_check`가 임계치를 넘으면 non-zero로 종료하고, cron/systemd timer에서 이를 사용할 수 있습니다.

---

## 실제 수집 데이터 + 성능 측정

### 12. 실제 관측 결과

표본은 작지만 **모든 숫자는 실제 서울시 API에 연결해 수집·적재한 결과**입니다.

```bash
python -m scripts.build_marts --report
```

예시:

```text
수집 57건 · 세션 2 · 중복 0 · 파싱불가 0 · DLQ 0

검색 퍼널 (검색 단위)          외부 호출                수집 지연
  search.query   7              캐시 적중  8회  0~1ms    큐 대기  평균 2,557~2,780ms
  search.click   7 (100.0%)     외부 호출 10회 24~324ms  전송     평균 3~4ms
  1위 결과 클릭   4 ( 57.1%)     오류       0회           시계 이상  0건
  조회 도달       7 (100.0%)                                seq gap   0건
```

이 수치는 일반 사용자의 행동을 대표하는 통계가 아니라 **해당 실제 수집 표본에서 관찰한 사례**입니다.

#### 관찰된 내용

- 총 수집 지연의 대부분이 전송 이후 서버가 아니라 **브라우저 큐 대기 구간**에서 발생했습니다.
- 캐시 응답과 외부 API 응답의 latency 차이를 관측할 수 있었습니다.
- 두 세션에서 여러 `current_intent` 전환을 관찰할 수 있었습니다.
- `station_arrival`의 `data_age_sec`가 7회 모두 `null`인 문제를 발견했고, 원인은 응답 정규화 단계에서 원본 수집 시각을 담지 않은 것이었습니다.

즉 **로그가 화면에 보이지 않는 데이터 품질 문제까지 드러내는 관측 계층**으로 동작했습니다.


### 13. 합성 부하로 처리 성능 측정

실제 사용자 표본은 57건이므로, 파이프라인 처리 성능은 별도의 합성 부하로 측정했습니다.

```bash
python -m scripts.loadgen 100000
python -m scripts.loadgen 1000000
```

> 아래 수치는 사용자 행동 통계가 아니라 파이프라인 처리 성능이며, 실행 환경에 따라 달라질 수 있습니다.

| | 10만 건 | 100만 건 |
|---|---:|---:|
| 수집 (검증 + landing) | 0.8초 · 119,928 건/초 | 8.3초 · 120,715 건/초 |
| 적재 (`raw → clean → mart`) | 2.2초 · 45,683 건/초 | 27.8초 · 35,958 건/초 |
| landing 파일 | 76MB | 759MB |
| `analytics.db` | 145MB | 1,453MB |
| `fact_search` 조회 | 83ms | 1,305ms |
| `fact_session` 조회 | 46ms | 497ms |

현재 단일 호스트 환경에서는 수집·적재 처리량이 데이터 증가에 대해 준선형으로 증가했습니다.

반면 `fact_search` 조회는 10배 데이터에서 약 16배 느려졌습니다. 실행계획을 확인해 복합 인덱스를 추가해 일부 개선했지만, `fact_session`·`fact_pipeline_health`처럼 전체 테이블 집계가 필요한 뷰는 인덱스만으로 해결되지 않습니다.

대규모 환경에서는 이 지점부터 **파티션 단위 집계·실체화**가 필요합니다.

또한 `raw_events`와 `clean_events`가 원본 payload를 각각 보존하기 때문에 analytics DB가 landing 파일보다 약 1.9배 큰다는 점도 확인했습니다.

---
## 실제 API를 붙이며 발견한 문제

### 14. 목업만으로는 잡히지 않았던 문제

#### 형제 API도 필드 의미가 다름

같은 기관의 API라도 오퍼레이션마다 필드명과 의미가 다릅니다.

예를 들어 정류소 도착정보의 `rerideNum1`은 혼잡도 코드가 아니라 **재차인원(명)**으로 해석해야 합니다.

#### 도착시간은 단위가 명시된 값을 우선

`traTime1`처럼 단위가 모호한 숫자 대신 `arrmsg1`의 `"3분12초후"`처럼 단위가 포함된 문자열을 파싱합니다.

`출발대기`, `운행종료` 같은 상태를 `0초`로 바꾸면 잘못된 “곧 도착” 상태가 됩니다.

#### 정류장 이름만으로 식별하면 안 됨

예를 들어 `능인선원앞`이 방향에 따라 `23365` / `23367`로 나뉘었습니다.

따라서 정류장 식별자는 이름이 아니라 **ARS**를 중심으로 관리하고, 조회 단계에서는 `station_id`와 `ars`를 함께 보존합니다.

---

## 테스트

### 15. 회귀 테스트

```bash
python -m unittest discover -s tests -v
node tests/collector_selfcheck.js
```

회귀 테스트 **109개**입니다. 함수 하나의 정답보다 **설계 보장**을 검증합니다.

| 테스트 | 검증하는 것 |
|---|---|
| `test_xml_error_header_is_detected` | HTTP 200 응답 본문의 오류 코드·메시지 파싱 |
| `test_single_item_is_normalized_to_list` | XML 항목이 1개여도 list 로 정규화 |
| `test_service_key_never_appears_in_error_messages` | 인증키가 예외 메시지로 새지 않음 |
| `test_partial_accept_keeps_good_events` | 불량 이벤트가 정상 이벤트를 죽이지 않음 |
| `test_read_count_equals_accepted_plus_rejected` | 수신 건수의 추적 가능성 |
| `test_ip_is_masked_before_it_is_written` | 원본 IP가 raw에 기록되지 않음 |
| `test_event_type_is_derived_by_the_server` | product/system 구분을 클라이언트가 못 바꿈 |
| `test_delays_are_measured_not_guessed` | 지연 구간 분리, 모르면 `null` |
| `test_schema_version_matches_between_client_and_server` | 클라이언트·서버 스키마 버전 일치 |
| `test_dlq_keeps_the_original_event` · `test_replay_puts_dlq_events_back` | DLQ 원본 보존과 재처리 가능성 |
| `test_second_run_skips_files_it_already_read` | 증분 적재 — 재적재가 재도착으로 둔갑하지 않음 |
| `test_duplicate_search_id_does_not_inflate_the_funnel` | 검색 단위 집계로 클릭률 부풀림 방지 |
| `test_seq_gap_sees_loss_at_the_head_of_a_session` | 세션 앞부분 유실이 0으로 보이지 않음 |
| `test_collection_stopped_is_caught` | 수집이 멈춘 것을 탐지 |
| `test_mtime_alone_does_not_count_as_fresh` | 파일을 건드린 것과 쌓인 것을 구분 |
| `test_unseeded_bus_number_is_found_live` · `test_route_map_works_for_unseeded_bus` | 적재하지 않은 노선의 live fallback · on-demand hydration |

브라우저 SDK는 `collector_selfcheck.js`에서 envelope 보호, 중복 전송 방지, 4xx 폐기, 5xx backoff, 이탈 시 전송 등을 검사합니다.

목업 모드로 실행하기 때문에 인증키 없이 CI에서 테스트할 수 있으며, CI는 테스트뿐 아니라 생성된 문서·데모가 소스와 일치하는지도 검사합니다.

---

## API 사용량과 관측성

### 16. 외부 API 사용량도 데이터로 취급

개발계정의 일일 호출 제한을 설계에 반영했기 때문에 **API 사용량 자체가 중요한 데이터**입니다.

`api_usage`에 다음을 기록합니다.

- KST 기준 날짜
- endpoint별 호출 수
- 오류 수
- 총 사용량

동적 데이터 관측에는 다음 값을 함께 기록합니다.

```text
endpoint
latency_ms
cache
status
error_code
data_age_sec
cache_age_sec
```

> “캐시는 빨랐지만 원천 데이터는 오래됐다”

`data_age_sec`는 캐시에 들어온 지가 아니라 원본이 수집된 지 몇 초가 지났는지입니다.

---

## 실행하기

### 17. 목업 모드 — 인증키 불필요

```bash
docker compose up -d
```

접속:

```text
http://127.0.0.1:8000
```

또는 직접 실행:

```bash
pip install -r requirements.txt
USE_MOCK=1 uvicorn app.main:app --reload
```

서버 없이 데모만 열 수도 있습니다.

```bash
open docs/demo.html
```

Docker는 기본이 목업 모드이고 `./data`를 바인드 마운트해 수집한 로그가 컨테이너 외부에 남도록 구성합니다.

### 실데이터 모드

```bash
cp .env.example .env
# SEOUL_BUS_SERVICE_KEY=... 입력

python -m scripts.smoke_live 4312
python -m scripts.preload 4312 402 강남06 160
uvicorn app.main:app --reload
```

목업은 반드시 명시적으로 켭니다. 인증키 누락을 자동으로 목업으로 숨기지 않습니다.

### 수집 로그를 분석 테이블로

```bash
python -m scripts.build_marts --report
python -m scripts.dq_check --landing
python -m scripts.dq_check
python -m scripts.replay_dlq --dry-run
```

---

## 공공 API 서비스

| 서비스 | 데이터 번호 | 용도 |
|---|---:|---|
| 서울특별시_노선정보조회 | `15000193` | 노선 검색, 정류장 순서, 노선 기본정보 |
| 서울특별시_정류소정보조회 | `15000303` | 정류장 검색, 경유 노선, 도착 정보 |
| 서울특별시_버스위치정보조회 | `15000332` | 현재 버스 위치 |

인증키는 `.env`에만 두고 브라우저로 전달하지 않습니다.

---
## 현재 범위와 한계

이 프로젝트는 **production 규모의 중앙 로그 플랫폼이 아니라, 운영을 고려한 수집·분석 계층을 로컬 환경에서 검증한 프로젝트**입니다.

| 항목 | 현재 구현 | production 확장 방향 |
|---|---|---|
| 수집 | FastAPI + raw JSONL | 중앙 큐 + object storage |
| 브라우저 buffering | `localStorage` | IndexedDB 등 |
| 중복 제거 | 분석 적재 시 `event_id` PK | 중앙 dedup 저장소 |
| 분석 적재 | 증분 `raw → clean → mart` | 파티셔닝 · 증분 재생성 |
| 유실 탐지 | `seq_gap` + `client_dropped`로 유실 징후와 알려진 client-side drop 관찰 | transport/server 원인 세분화 |
| 품질 점검 | `dq_check` + exit code | 알림 채널 연계 |
| 스케줄 실행 | cron · systemd | 오케스트레이터 |
| 수집 속도 제한 | 없음 (`write_key`는 공개 식별자) | token bucket + `429` |
| 보존 기간 | 정의만 있고 삭제 코드 없음 | 회전 · 압축 · 만료 배치 |
| 파일 닫힘 신호 | 시간대 롤오버 | 종료 훅 + idle 기준 |
| 정류장 검색 | 적재된 노선 범위 | 전체 마스터 또는 FTS |

> **이 프로젝트가 증명하는 것**
>
> 브라우저 이벤트가 발생한 순간부터 검증·landing되고, 멱등·증분 배치를 통해 분석 가능한 데이터셋까지 이어지는 경로를 설계했으며, 그 핵심 보장을 회귀 테스트로 고정했습니다.

---

## 프로젝트 구조

```text
.
├── app/
│   ├── config.py        환경변수 · 설정 · 명시적 목업 모드 · KST
│   ├── seoul_api.py     서울시 API 클라이언트 (XML · 오류코드 · 재시도 · 정규화)
│   ├── store.py         SQLite 정적 마스터 · TTL cache · API 사용량
│   ├── mock.py          인증키 없이 실행하는 목업 제공자
│   ├── mock_data.json   실데이터에서 굳힌 정적 마스터
│   ├── collector.py     이벤트 검증 · 부분 수용 · DLQ · JSONL
│   ├── main.py          FastAPI 라우트
│   └── static/          index.html · app.js · collector.js · api-http.js · api-mock.js · styles.css
│
├── scripts/             preload · smoke_live · build_marts · dq_check · replay_dlq
│                        export_mock · build_docs · build_demo · loadgen
│
├── docs/                portfolio.html · index.html · data-model.html · design-defense.html
│                        event-schema.md · change-log.md · demo.html · demo-guide.html
│
├── sql/
│   ├── ddl.sql          수집용 (정적 마스터 · API 사용량)
│   └── marts.sql        분석용 (raw → clean → fact)
│
├── tests/
│   ├── test_app.py             수집 · API · 저장 계층
│   ├── test_marts.py           분석 파이프라인 (증분 · 멱등 · 귀속 규칙)
│   ├── test_dq_check.py        수집 상태 점검
│   └── collector_selfcheck.js  브라우저 SDK 규약 · 전달 보장 (node)
│
├── Dockerfile · docker-compose.yml   목업 모드로 바로 뜨는 컨테이너
├── ops/                 주기 실행 설정 (cron · systemd timer)
├── data/samples/        적재 결과 샘플 (이벤트 · DLQ · 마트 리포트)
└── .github/workflows/   CI — 테스트 · 자체 점검 · 생성물 일치 검사
```

---

> **서울버스 데이터를 이용한 사용자 행동 로그 수집 파이프라인 — 신뢰성 중심의 Data Engineering 설계 프로젝트**
