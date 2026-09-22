# data/samples

저장소에는 실제 수집 로그를 올리지 않습니다(`data/` 는 무시됩니다). 대신 **무엇이 어떤 모양으로 적재되는지**를 보여주는 샘플만 둡니다.

| 파일 | 내용 |
|---|---|
| `events-sample.jsonl` | 수용된 이벤트 10건. 한 세션의 `intent.select → search.query → search.click → route.view → nav.click → station.view` 흐름이 **전송 3회에 나뉘어** 담겨 있습니다(SDK 가 5초 간격으로 보냅니다) |
| `dlq-sample.jsonl` | 거부된 이벤트 2건. **거부 사유와 함께 원본(`raw`)이 남아 있어** `scripts/replay_dlq.py` 로 다시 넣을 수 있습니다 |
| `marts-report.txt` | `python -m scripts.build_marts --report` 출력. 퍼널·외부 호출·수집 지연·데이터 품질 |

샘플은 데모 흐름을 재현해 만든 것이라 개인 검색 이력이 들어 있지 않습니다. 실제 수집 결과 숫자는 README 상단에 있습니다.

**이 표본의 스키마 버전은 `1.2.0` 입니다**(실제 수집 시점). `batch_trigger`·`client_dropped` 는 `1.3.0` 에 생긴 필드라 여기에는 없고, 스키마 문서의 "되채울 수 없음" 규칙대로 비어 있습니다.

필드별 정의와 누가 무엇을 채우는지는 [docs/event-schema.md](../../docs/event-schema.md) 가 단일 출처입니다.
