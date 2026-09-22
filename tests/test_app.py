"""이 앱이 지키기로 한 약속을 테스트로 고정한다.

    python -m unittest discover -s tests -v

목업 모드라 인증키 없이 돈다 — 외부 API 가 죽었을 때 '우리 코드는 멀쩡하다'의 근거이기도 하다.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["USE_MOCK"] = "1"

# 테스트 출력에서 요청 로그 소음을 걷어낸다
import logging  # noqa: E402
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("app").setLevel(logging.ERROR)

import re  # noqa: E402

from app import collector, mock, store  # noqa: E402
from app.config import EVENT_SCHEMA_VERSION, STATIC_DIR  # noqa: E402
from app.seoul_api import data_age_seconds, parse_data_tm  # noqa: E402
from app.seoul_api import SeoulBusClient, as_int, normalize, pick  # noqa: E402


class SchemaParsingTest(unittest.TestCase):
    """응답 필드명이 가이드와 조금 달라도 죽지 않아야 한다."""

    def test_pick_falls_back_through_candidates(self):
        self.assertEqual(pick({"busRouteNm": "152"}, "busRouteNm", "busRouteAbrv"), "152")
        self.assertEqual(pick({"busRouteAbrv": "152"}, "busRouteNm", "busRouteAbrv"), "152")

    def test_pick_returns_default_when_missing(self):
        self.assertEqual(pick({}, "nope", default="-"), "-")

    def test_as_int_is_forgiving(self):
        self.assertEqual(as_int(" 12 "), 12)
        self.assertEqual(as_int(None), 0)
        self.assertEqual(as_int("없음", default=-1), -1)

    def test_xml_error_header_is_detected(self):
        """HTTP 200 이어도 본문 헤더에 오류가 담긴다. status_code 만 보면 놓친다."""
        xml = """<?xml version="1.0"?><ServiceResult>
          <msgHeader><headerCd>23</headerCd><headerMsg>초당 호출 제한 초과</headerMsg></msgHeader>
          <msgBody></msgBody></ServiceResult>"""
        items, code, message = SeoulBusClient._parse(xml)
        self.assertEqual(code, "23")
        self.assertIn("초당", message)
        self.assertEqual(items, [])

    def test_single_item_is_normalized_to_list(self):
        """항목이 1개면 XML 은 dict 로 온다. 항상 list 여야 한다."""
        xml = """<?xml version="1.0"?><ServiceResult>
          <msgHeader><headerCd>0</headerCd><headerMsg>정상</headerMsg></msgHeader>
          <msgBody><itemList><busRouteNm>152</busRouteNm><busRouteId>100100152</busRouteId></itemList></msgBody>
          </ServiceResult>"""
        items, code, _ = SeoulBusClient._parse(xml)
        self.assertEqual(code, "0")
        self.assertEqual(len(items), 1)
        self.assertEqual(normalize("route", items)[0]["no"], "152")

    def test_bus_position_normalization(self):
        row = {"vehId": "1", "plainNo": "서울70사1234", "sectOrd": "9", "stopFlag": "1", "congetion": "5"}
        out = normalize("bus_pos", [row])[0]
        self.assertEqual(out["sect_ord"], 9)
        self.assertTrue(out["stopped"])
        self.assertEqual(out["congestion"], "혼잡")
        self.assertEqual(out["plate"], "1234")


class MasterCacheTest(unittest.TestCase):
    """정적 마스터는 재적재해도 늘어나면 안 된다 (멱등)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import app.config as cfg
        cfg.CACHE_DB = Path(self.tmp.name) / "cache.db"
        store.CACHE_DB = cfg.CACHE_DB
        store.init_db()

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self):
        store.upsert_routes(mock.ROUTES)
        for r in mock.ROUTES:
            store.upsert_route_stations(r["route_id"], mock.route_stations(r["route_id"]))

    def test_preload_is_idempotent(self):
        self._load()
        first = store.counts()
        self._load()
        self.assertEqual(first, store.counts(), "재적재로 행이 늘어나면 안 된다")

    def test_route_search_prefers_prefix_match(self):
        self._load()
        results = store.search_routes("강남0")
        self.assertTrue(results)
        self.assertTrue(all(r["no"].startswith("강남0") for r in results[:2]))

    def test_routes_through_station_uses_no_api_call(self):
        """경유 노선은 정류장 목록을 뒤집어 만든다 — 외부 호출 0회.

        목업 데이터가 바뀌어도 깨지지 않게 이름·순번이 아니라 번호로 찾는다."""
        self._load()
        route = next(r for r in mock.SEEDED if r["no"] == "4312")
        stations = store.get_route_stations(route["route_id"])
        self.assertTrue(stations, "전제: 4312 의 정류장이 적재되어 있어야 한다")
        through = store.routes_through_station(stations[len(stations) // 3]["ars"])
        self.assertIn("4312", [r["no"] for r in through])


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        collector.EVENT_DIR = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, **over):
        base = {"event_id": "e-1", "event_name": "intent.select",
                "event_ts": "2026-09-16T05:00:00.000Z", "session_id": "s-1"}
        base.update(over)
        return base

    def _batch(self, events, **over):
        """등록된 write_key 를 포함한 배치. 서버는 이 키로 service 를 도출한다."""
        base = {"write_key": "wk_seoul_bus_demo", "events": events}
        base.update(over)
        return base

    def test_valid_event_is_accepted(self):
        out = collector.accept(self._batch([self._event()]), "203.0.113.10")
        self.assertEqual(out["accepted"], 1)
        self.assertEqual(out["rejected"], [])

    def test_partial_accept_keeps_good_events(self):
        """한 건이 불량이라고 배치 전체를 버리면 멀쩡한 이벤트가 같이 죽는다."""
        batch = self._batch([self._event(event_id="ok"),
                             self._event(event_id="bad", event_name="nope.event")])
        out = collector.accept(batch, "203.0.113.10")
        self.assertEqual(out["accepted"], 1)
        self.assertEqual(len(out["rejected"]), 1)
        self.assertIn("unknown event_name", out["rejected"][0]["errors"][0])

    def test_missing_required_field_is_rejected(self):
        bad = self._event()
        del bad["session_id"]
        out = collector.accept(self._batch([bad]), None)
        self.assertEqual(out["accepted"], 0)

    def test_ip_is_masked_before_it_is_written(self):
        # RFC 5737 문서 전용 대역을 쓴다 — 실제 대역을 박아두면 저장소에 남는다.
        collector.accept(self._batch([self._event()]), "203.0.113.207")
        line = next(iter(collector.EVENT_DIR.glob("events-*.jsonl"))).read_text(encoding="utf-8").strip()
        row = json.loads(line)
        self.assertEqual(row["ip_masked"], "203.0.113.0")
        self.assertNotIn("203.0.113.207", line)

    def test_rejected_events_go_to_dlq_file(self):
        collector.accept(self._batch([self._event(event_name="nope.event")]), None)
        dlq = list(collector.EVENT_DIR.glob("dlq-*.jsonl"))
        self.assertEqual(len(dlq), 1)
        self.assertIn("schema_violation", dlq[0].read_text(encoding="utf-8"))

    def test_read_count_equals_accepted_plus_rejected(self):
        """조용한 유실이 없다는 등식."""
        events = [self._event(event_id=f"e{i}") for i in range(5)]
        events.append(self._event(event_id="bad", event_name="nope.event"))
        out = collector.accept(self._batch(events), None)
        self.assertEqual(len(events), out["accepted"] + len(out["rejected"]))

    def test_service_key_never_appears_in_error_messages(self):
        """인증키는 .env 로 빼놨어도 예외 메시지를 타고 샌다.

        httpx 의 HTTPStatusError 가 담는 URL 의 쿼리에 serviceKey 가 있다.
        """
        from app.seoul_api import scrub
        leaked = ("Server error '500' for url "
                  "'http://ws.bus.go.kr/api/rest/buspos/getBusPosByRtid"
                  "?serviceKey=SUPERSECRET123&busRouteId=1'")
        self.assertNotIn("SUPERSECRET123", scrub(leaked))
        self.assertIn("serviceKey=***", scrub(leaked))

    def test_http_error_is_converted_without_the_url(self):
        """게이트웨이가 5xx 를 주면 상태 코드만 남기고 URL 은 버린다."""
        import httpx
        from app.seoul_api import SeoulApiError, SeoulBusClient

        client = SeoulBusClient(service_key="SUPERSECRET123", base="http://example.invalid")
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        client._client = httpx.AsyncClient(transport=transport)
        with self.assertRaises(SeoulApiError) as ctx:
            asyncio.run(client.call("bus_pos", busRouteId="1"))
        self.assertNotIn("SUPERSECRET123", str(ctx.exception))
        self.assertEqual(ctx.exception.code, "http_503")
        asyncio.run(client.aclose())

    def test_naive_timestamp_does_not_kill_the_batch(self):
        """타임존 없는 event_ts 하나가 배치 전체를 500 으로 날리던 회귀.

        aware - naive 뺄셈의 TypeError 가 accept() 밖으로 나가면 부분 수용이 깨진다.
        """
        batch = self._batch([self._event(event_id="naive", event_ts="2026-09-16T05:00:00"),
                             self._event(event_id="aware")],
                            sent_ts="2026-09-16T05:00:01.000Z")
        out = collector.accept(batch, None)
        self.assertEqual(out["accepted"], 2)
        row = json.loads(next(iter(collector.EVENT_DIR.glob("events-*.jsonl")))
                         .read_text(encoding="utf-8").strip().splitlines()[0])
        self.assertEqual(row["queue_delay_ms"], 1000)   # naive 는 UTC 로 해석한다

    def test_non_object_record_goes_to_dlq_instead_of_crashing(self):
        """events 배열에 숫자·문자열이 섞여 와도 배치가 죽지 않는다."""
        out = collector.accept(self._batch([123, "abc", None, self._event()]), None)
        self.assertEqual(out["accepted"], 1)
        self.assertEqual(len(out["rejected"]), 3)

    def test_wrong_field_types_are_rejected(self):
        """event_id 가 숫자면 후속 dedup 이 123 과 "123" 을 다른 키로 본다."""
        out = collector.accept(self._batch([self._event(event_id=123),
                                            self._event(event_id="ok", seq="열")]), None)
        self.assertEqual(out["accepted"], 0)
        self.assertEqual(len(out["rejected"]), 2)

    def test_batch_facts_are_written_to_every_row(self):
        """배치 단위 사실(버린 건수·전송 계기·시도 횟수)이 응답에만 있으면 그 순간 사라진다."""
        batch = self._batch([self._event()], sent_ts="2026-09-16T05:00:01.000Z",
                            dropped_count=5, trigger="pagehide", attempt=2)
        collector.accept(batch, None)
        row = json.loads(next(iter(collector.EVENT_DIR.glob("events-*.jsonl")))
                         .read_text(encoding="utf-8").strip())
        self.assertEqual(row["client_dropped"], 5)
        self.assertEqual(row["batch_trigger"], "pagehide")
        self.assertEqual(row["batch_attempt"], 2)

    def test_dlq_keeps_the_original_event(self):
        """거부 사유만 남기면 스키마를 고쳐도 다시 넣을 데이터가 없다."""
        collector.accept(self._batch([self._event(event_name="nope.event", query="4312")]), None)
        dlq = next(iter(collector.EVENT_DIR.glob("dlq-*.jsonl")))
        row = json.loads(dlq.read_text(encoding="utf-8").strip())
        self.assertEqual(row["raw"]["event_name"], "nope.event")
        self.assertEqual(row["raw"]["query"], "4312")

    def test_replay_puts_dlq_events_back(self):
        """스키마를 고친 뒤 재처리하면 수용되고, source 로 구분된다."""
        from scripts import replay_dlq
        collector.accept(self._batch([self._event(event_id="x1", event_name="nope.event")]), None)
        collector.ALLOWED_EVENTS.add("nope.event")          # 스키마를 고쳤다고 치고
        try:
            out = replay_dlq.replay()
        finally:
            collector.ALLOWED_EVENTS.discard("nope.event")
        self.assertEqual(out["accepted"], 1)
        rows = [json.loads(line) for p in collector.EVENT_DIR.glob("events-*.jsonl")
                for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([r["source"] for r in rows if r["event_id"] == "x1"], ["replay"])

    def test_long_free_text_is_truncated_not_dropped(self):
        """자유 텍스트 상한. 잘랐다는 사실을 함께 남긴다."""
        collector.accept(self._batch([self._event(event_name="search.query", query="가" * 500)]), None)
        row = json.loads(next(iter(collector.EVENT_DIR.glob("events-*.jsonl")))
                         .read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(len(row["query"]), collector.MAX_TEXT_LEN)
        self.assertTrue(row["query_truncated"])

    def test_client_clock_far_off_is_flagged_not_dropped(self):
        """시계가 어긋난 이벤트는 버리지 않고 표시한다."""
        out = collector.accept(self._batch([self._event(event_ts="2099-01-01T00:00:00.000Z")]), None)
        self.assertEqual(out["accepted"], 1)
        row = json.loads(next(iter(collector.EVENT_DIR.glob("events-*.jsonl")))
                         .read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertTrue(row["event_ts_suspect"])

    def test_schema_version_matches_between_client_and_server(self):
        """봉투를 바꾸면서 한쪽 버전만 올리는 실수를 막는다 — 실제로 한 번 겪었다."""
        js = (STATIC_DIR / "collector.js").read_text(encoding="utf-8")
        found = re.search(r'SCHEMA_VERSION\s*=\s*"([\d.]+)"', js)
        self.assertIsNotNone(found, "collector.js 에서 SCHEMA_VERSION 을 찾지 못했다")
        self.assertEqual(found.group(1), EVENT_SCHEMA_VERSION)

    def test_client_event_names_are_all_allowed(self):
        """app.js 가 실제로 쏘는 이름만 서버가 받는다. 오타는 조용히 DLQ 로 가므로 여기서 잡는다."""
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        names = set(re.findall(r'track\(\s*"([a-z._]+)"', js))
        self.assertTrue(names, "track() 호출을 하나도 찾지 못했다 — 정규식을 확인할 것")
        self.assertLessEqual(names, collector.ALLOWED_EVENTS,
                             f"서버가 모르는 이벤트: {names - collector.ALLOWED_EVENTS}")

    def test_event_type_is_derived_by_the_server(self):
        """product/system 구분은 클라이언트가 보낸 값이 아니라 event_name 에서 나온다."""
        collector.accept(self._batch([
            self._event(event_id="a", event_name="search.query", event_type="system"),
            self._event(event_id="b", event_name="api.call"),
        ]), None)
        rows = [json.loads(line) for line
                in next(iter(collector.EVENT_DIR.glob("events-*.jsonl")))
                .read_text(encoding="utf-8").strip().splitlines()]
        self.assertEqual([r["event_type"] for r in rows], ["product", "system"])

    def test_delays_are_measured_not_guessed(self):
        """큐 대기와 전송 지연은 따로 잰다. 못 재면 0 이 아니라 None 이다."""
        batch = self._batch([self._event(event_ts="2026-09-16T05:00:00.000Z")],
                            sent_ts="2026-09-16T05:00:03.000Z")
        collector.accept(batch, None)
        row = json.loads(next(iter(collector.EVENT_DIR.glob("events-*.jsonl")))
                         .read_text(encoding="utf-8").strip())
        self.assertEqual(row["queue_delay_ms"], 3000)
        self.assertIsNotNone(row["ingest_delay_ms"])
        self.assertNotIn("clock_skew_ms", row)

        collector.accept(self._batch([self._event(event_id="e-2")]), None)   # sent_ts 없음
        rows = next(iter(collector.EVENT_DIR.glob("events-*.jsonl"))).read_text(encoding="utf-8")
        self.assertIsNone(json.loads(rows.strip().splitlines()[-1])["queue_delay_ms"])

    def test_mask_ip_handles_garbage(self):
        self.assertIsNone(collector.mask_ip(None))
        self.assertIsNone(collector.mask_ip("not-an-ip"))


class BusNumberSearchTest(unittest.TestCase):
    """사용자가 아는 것은 버스에 적힌 번호다. 사전 적재 여부와 무관하게 찾을 수 있어야 한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import app.config as cfg
        cfg.CACHE_DB = Path(self.tmp.name) / "cache.db"
        store.CACHE_DB = cfg.CACHE_DB
        store.init_db()

        from fastapi.testclient import TestClient
        from app.main import app as fastapi_app
        self.client = TestClient(fastapi_app)
        self.client.__enter__()      # lifespan 실행 → 시드 적재

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_seeded_bus_number_comes_from_cache(self):
        res = self.client.get("/api/routes/search", params={"q": "4312"}).json()
        self.assertGreater(res["result_count"], 0)
        self.assertEqual(res["source"], "cache")
        self.assertEqual(res["items"][0]["no"], "4312")

    def test_unseeded_bus_number_is_found_live(self):
        """402 는 사전 적재하지 않았다. 그래도 사용자는 찾을 수 있어야 한다."""
        self.assertEqual(store.search_routes("402"), [], "전제: 캐시에 없어야 한다")
        res = self.client.get("/api/routes/search", params={"q": "402"}).json()
        self.assertEqual(res["source"], "live")
        self.assertIn("402", [r["no"] for r in res["items"]])

    def test_live_result_is_cached_for_next_time(self):
        self.client.get("/api/routes/search", params={"q": "402"})
        again = self.client.get("/api/routes/search", params={"q": "402"}).json()
        self.assertEqual(again["source"], "cache", "한 번 찾은 버스는 다음부터 캐시에서 나와야 한다")

    def test_exact_bus_number_ranks_first(self):
        """4312 를 친 사람에게 43120 을 먼저 보여주면 안 된다.

        목업 노선으로는 조건을 다 만들 수 없어 순위 규칙 자체를 직접 검증한다."""
        from app.main import _rank
        items = [{"no": "43120"}, {"no": "4312"}, {"no": "04312"}]
        self.assertEqual([r["no"] for r in _rank(items, "4312")],
                         ["4312", "43120", "04312"])

    def test_partial_cache_hit_still_checks_live(self):
        """캐시에 4312 만 있을 때 '4' 검색이 402 를 놓치면 안 된다.

        부분 일치만으로 끝내면 아직 적재되지 않은 노선은 영영 찾을 수 없다."""
        self.assertEqual(store.search_routes("402"), [], "전제: 402 는 캐시에 없어야 한다")
        res = self.client.get("/api/routes/search", params={"q": "4"}).json()
        numbers = [r["no"] for r in res["items"]]
        self.assertIn("4312", numbers)
        self.assertIn("402", numbers, "부분 일치만 있으면 실시간으로 더 확인해야 한다")

    def test_non_numeric_bus_numbers_are_searchable(self):
        """마을버스 강남06 처럼 버스 번호가 숫자만은 아니다."""
        for number in ("강남06",):
            res = self.client.get("/api/routes/search", params={"q": number}).json()
            self.assertIn(number, [r["no"] for r in res["items"]], number)

    def test_route_map_works_for_unseeded_bus(self):
        """사전 적재하지 않은 버스도 노선도가 나와야 한다 (on-demand hydration)."""
        found = self.client.get("/api/routes/search", params={"q": "402"}).json()
        route_id = found["items"][0]["route_id"]
        self.assertEqual(store.get_route_stations(route_id), [], "전제: 정류장 캐시가 비어 있어야 한다")

        detail = self.client.get("/api/routes/" + route_id).json()
        self.assertGreater(len(detail["stations"]), 0)
        self.assertEqual(detail["observability"]["stations_cache"], "miss")
        # 두 번째부터는 캐시에서 나온다
        again = self.client.get("/api/routes/" + route_id).json()
        self.assertEqual(again["observability"]["stations_cache"], "hit")


class ReviewFixTest(unittest.TestCase):
    """코드 리뷰에서 나온 문제들을 고친 뒤 다시 깨지지 않게 고정한다."""

    # ---- 1. TTL 캐시: 만료 경계와 무한 증가 ----
    def test_expired_entry_is_not_served(self):
        """int() 로 나이를 잘라 비교하면 ttl 을 막 넘긴 값이 그대로 나갔다."""
        cache = store.TTLCache(ttl=1)
        cache.set("k", ["v"])
        time.sleep(1.2)
        self.assertIsNone(cache.get("k"), "만료된 값을 돌려주면 안 된다")

    def test_expired_entry_is_removed_not_just_hidden(self):
        cache = store.TTLCache(ttl=1)
        cache.set("k", ["v"])
        time.sleep(1.2)
        cache.get("k")
        self.assertEqual(cache.stats()["entries"], 0, "만료 항목이 메모리에 남으면 안 된다")

    def test_cache_is_bounded(self):
        """상한이 없으면 조회된 모든 키가 영원히 메모리에 남는다."""
        cache = store.TTLCache(ttl=60, max_entries=50)
        for i in range(200):
            cache.set(f"k{i}", [i])
        self.assertEqual(cache.stats()["entries"], 50)
        self.assertIsNone(cache.get("k0"), "가장 오래된 키가 밀려나야 한다")
        self.assertIsNotNone(cache.get("k199"))

    # ---- 2. 신선도: 캐시 나이가 아니라 데이터 나이 ----
    def test_data_age_uses_collection_time_not_cache_time(self):
        from app.seoul_api import KST
        from datetime import datetime, timedelta
        old = (datetime.now(KST) - timedelta(seconds=90)).strftime("%Y%m%d%H%M%S")
        age = data_age_seconds([{"data_tm": old}])
        self.assertGreaterEqual(age, 88)
        self.assertLessEqual(age, 93)

    def test_data_tm_is_parsed_as_kst(self):
        """타임존을 안 붙이면 서버가 UTC 일 때 9시간이 신선도로 잡힌다."""
        parsed = parse_data_tm("20260916140054")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset().total_seconds(), 9 * 3600)

    def test_data_age_handles_missing_or_broken_stamp(self):
        self.assertIsNone(data_age_seconds([{"data_tm": ""}]))
        self.assertIsNone(parse_data_tm("20261399999999"))

    # ---- 3. 신뢰 경계: service 는 write_key 에서 도출 ----
    def test_service_comes_from_write_key_not_body(self):
        """본문의 service 를 믿으면 남의 서비스 이름으로 로그를 밀어넣을 수 있다."""
        batch = {"write_key": "wk_seoul_bus_demo", "service": "남의-서비스",
                 "events": [self._event()]}
        collector.accept(batch, None)
        row = json.loads(next(iter(collector.EVENT_DIR.glob("events-*.jsonl")))
                         .read_text(encoding="utf-8").strip())
        self.assertEqual(row["service"], "seoul-bus-web")

    def test_unknown_write_key_is_rejected(self):
        with self.assertRaises(collector.Rejected) as ctx:
            collector.accept({"write_key": "wk_아무거나", "events": [self._event()]}, None)
        self.assertEqual(ctx.exception.status, 401)

    def test_missing_write_key_is_rejected(self):
        with self.assertRaises(collector.Rejected):
            collector.accept({"events": [self._event()]}, None)

    # ---- 4. 배치 상한 ----
    def test_oversized_batch_is_rejected(self):
        batch = {"write_key": "wk_seoul_bus_demo",
                 "events": [self._event(event_id=f"e{i}") for i in range(500)]}
        with self.assertRaises(collector.Rejected) as ctx:
            collector.accept(batch, None)
        self.assertEqual(ctx.exception.status, 413)

    # ---- 5. 스키마 버전 ----
    def test_version_mismatch_warns_but_keeps_the_event(self):
        """버전이 다르다고 버리면 배포 순서 때문에 데이터가 사라진다."""
        batch = {"write_key": "wk_seoul_bus_demo", "schema_version": "9.9.9",
                 "events": [self._event()]}
        out = collector.accept(batch, None)
        self.assertEqual(out["accepted"], 1)

    def _event(self, **over):
        base = {"event_id": "e-1", "event_name": "intent.select",
                "event_ts": "2026-09-16T05:00:00.000Z", "session_id": "s-1"}
        base.update(over)
        return base

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        collector.EVENT_DIR = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class ConnectionHygieneTest(unittest.TestCase):
    """sqlite3 Connection 의 with 블록은 연결을 닫지 않는다 — 쿼리마다 새 연결이 새던 문제."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import app.config as cfg
        cfg.CACHE_DB = Path(self.tmp.name) / "cache.db"
        store.CACHE_DB = cfg.CACHE_DB
        store.init_db()
        store.upsert_routes(mock.ROUTES)

    def tearDown(self):
        store.close_thread_connection()
        self.tmp.cleanup()

    def test_repeated_queries_reuse_one_connection(self):
        import gc
        first = None
        for _ in range(50):
            with store.db() as conn:
                if first is None:
                    first = id(conn)
                store.search_routes("43")
        with store.db() as conn:
            self.assertEqual(id(conn), first, "스레드마다 연결을 하나만 써야 한다")

    def test_open_file_descriptors_do_not_grow(self):
        import os
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("/proc 없음")
        store.search_routes("43")
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(300):
            store.search_routes("43")
            store.get_route("104900034")
        after = len(os.listdir("/proc/self/fd"))
        self.assertLessEqual(after - before, 2, f"fd 가 늘어남: {before} → {after}")

    def test_api_usage_is_counted_per_kst_day(self):
        """한도 위에 설계를 올려놓고 정작 사용량을 세지 않고 있었다."""
        store.record_api_call("/busRouteInfo/getBusRouteList", ok=True)
        store.record_api_call("/busRouteInfo/getBusRouteList", ok=True)
        store.record_api_call("/buspos/getBusPosByRtid", ok=False)
        usage = store.api_usage_today(budget=1000)
        self.assertEqual(usage["total"], 3)
        self.assertEqual(usage["errors"], 1, "실패도 한도를 소모하므로 세야 한다")
        self.assertEqual(usage["remaining"], 997)
        self.assertEqual(len(usage["by_endpoint"]), 2)

    def test_usage_hook_does_not_break_the_call(self):
        """사용량 기록이 실패해도 조회는 계속돼야 한다."""
        from app.seoul_api import SeoulBusClient
        c = SeoulBusClient(service_key="x")
        c.on_call = lambda *a: (_ for _ in ()).throw(RuntimeError("DB 다운"))
        c._emit("/x", True)          # 예외가 밖으로 새면 안 된다

    def test_failed_write_rolls_back(self):
        with self.assertRaises(sqlite3.Error):
            with store.tx() as conn:
                conn.execute("INSERT INTO routes (route_id, no, updated_at) VALUES ('x','x','x')")
                conn.execute("INSERT INTO 없는테이블 VALUES (1)")
        self.assertIsNone(store.get_route("x"), "실패한 트랜잭션이 남으면 안 된다")


class MockDataTest(unittest.TestCase):
    def test_buses_stay_inside_route(self):
        for route in mock.ROUTES:
            stops = len(mock.route_stations(route["route_id"]))
            for bus in mock.bus_positions(route["route_id"]):
                self.assertGreaterEqual(bus["sect_ord"], 1)
                self.assertLessEqual(bus["sect_ord"], stops)

    def test_mock_timestamps_are_kst_not_utc(self):
        """목업이 UTC 로 찍으면 신선도가 9시간(32400초)으로 계산된다.

        버스위치만 본다 — 도착정보는 실 API 가 수집 시각을 주지 않아
        목업도 채우지 않는다(test_mock_arrival_has_no_collection_time).
        """
        age = data_age_seconds(mock.bus_positions("104900034"))
        self.assertIsNotNone(age, "data_tm 이 비어 있으면 신선도를 못 낸다")
        self.assertLess(age, 300, f"신선도가 {age}초 — 타임존이 어긋났을 가능성")

    def test_mock_arrival_has_no_collection_time(self):
        """목업이 실 API 에 없는 값을 지어내면, 목업으로 검증한 화면이 실제와 달라진다."""
        rows = mock.arrivals("23001")
        self.assertTrue(rows)
        self.assertTrue(all(r["data_tm"] == "" for r in rows))
        self.assertIsNone(data_age_seconds(rows))

    def test_arrivals_are_sorted_by_eta(self):
        arr = mock.arrivals("23001")
        self.assertEqual(arr, sorted(arr, key=lambda a: a["sec1"]))

    def test_station_search_tells_which_direction(self):
        """같은 이름의 정류장이 방면별로 나뉜다. 이름만 주면 사용자가 반대편을 고른다."""
        rows = mock.search_stations("종로3가") or mock.search_stations("대청")
        self.assertTrue(rows, "전제: 검색되는 정류장이 있어야 한다")
        self.assertTrue(all("directions" in r for r in rows), "방면 필드가 빠졌다")

        by_name: dict[str, set[str]] = {}
        for route in mock.ROUTES:
            for stop in mock.route_stations(route["route_id"]):
                by_name.setdefault(stop["name"], set()).add(stop["ars"])
        twins = [n for n, ars in by_name.items() if len(ars) > 1]
        if twins:                      # 합성 목업에는 없을 수 있다
            found = mock.search_stations(twins[0])
            same_name = [r for r in found if r["name"] == twins[0]]
            self.assertGreater(len(same_name), 1, "같은 이름이 ARS 별로 따로 나와야 한다")
            self.assertNotEqual(same_name[0]["directions"], same_name[1]["directions"],
                                "이름이 같으면 방면으로 구분되어야 한다")

    def test_browser_mock_caches_the_same_routes_as_the_server(self):
        """같은 검색이 서버 모드와 데모에서 다르게 동작하면(cache/live) 로그 설명이 틀려진다."""
        js = (STATIC_DIR / "api-mock.js").read_text(encoding="utf-8")
        cached = set(re.findall(r'r\.no === "([^"]+)"', js.split("var CACHED")[1].split(";")[1]))
        self.assertEqual(cached, {r["no"] for r in mock.SEEDED})

    def test_broken_fixture_does_not_stop_the_app(self):
        """픽스처가 깨져도 목업은 합성 데이터로 계속 돈다.

        픽스처 하나로 임포트가 실패하면 '키 없이도 뜬다'는 존재 이유가 사라진다."""
        original = mock._FIXTURE
        try:
            broken = Path(tempfile.mkdtemp()) / "mock_data.json"
            broken.write_text("{ 깨진 json", encoding="utf-8")
            mock._FIXTURE = broken
            self.assertEqual(mock._load_fixture(), {})
            mock._FIXTURE = broken.parent / "없는파일.json"
            self.assertEqual(mock._load_fixture(), {})
        finally:
            mock._FIXTURE = original

    def test_one_ars_is_one_stop_and_routes_meet_somewhere(self):
        """ARS 하나가 정류장 하나 — 이름이 아니라 ARS 가 식별자다.

        실데이터는 같은 '능인선원앞'이 방향에 따라 23365 / 23367 로 갈린다.
        이름으로 합치면 상행에서 탄 사람과 하행에서 탄 사람이 섞인다."""
        by_ars: dict[str, set[str]] = {}
        routes_by_ars: dict[str, set[str]] = {}
        for route in mock.ROUTES:
            for stop in mock.route_stations(route["route_id"]):
                by_ars.setdefault(stop["ars"], set()).add(stop["name"])
                routes_by_ars.setdefault(stop["ars"], set()).add(route["no"])
        for ars, names in by_ars.items():
            self.assertEqual(len(names), 1, f"ars {ars} 에 이름이 여러 개다: {names}")

        shared = [a for a, routes in routes_by_ars.items() if len(routes) > 1]
        self.assertTrue(shared, "여러 노선이 만나는 정류장이 없으면 정류장 화면이 한 줄짜리가 된다")

    def test_round_trip_route_is_counted_once_per_station(self):
        """한 노선이 상·하행으로 같은 정류장을 두 번 지나도 '경유 노선'은 한 번만 센다."""
        store.upsert_routes(mock.SEEDED)
        for route in mock.SEEDED:
            store.upsert_route_stations(route["route_id"], mock.route_stations(route["route_id"]))
        # 한 노선이 같은 ars 를 두 번 지나는 상황을 직접 만든다. 목업 데이터가
        # 합성이든 실데이터든 상관없이, 이 SQL 의 동작 자체를 고정해야 하기 때문이다.
        route = mock.SEEDED[0]
        store.upsert_route_stations(route["route_id"], [
            {"seq": 1, "station_id": "S1", "ars": "99001", "name": "왕복정류장", "direction": "가", "is_turn": False},
            {"seq": 2, "station_id": "S2", "ars": "99002", "name": "회차지", "direction": "가", "is_turn": True},
            {"seq": 3, "station_id": "S1", "ars": "99001", "name": "왕복정류장", "direction": "오", "is_turn": False},
        ])
        nos = [r["no"] for r in store.routes_through_station("99001")]
        self.assertEqual(len(nos), len(set(nos)), f"노선이 중복으로 세어졌다: {nos}")
        self.assertEqual(len(nos), 1)

    def test_arrivals_only_list_routes_that_stop_there(self):
        # 여러 노선이 만나는 정류장을 데이터에서 찾는다. 노선 번호나 정류장 이름을
        # 박아두면 목업 데이터를 바꿀 때마다 깨진다 — 실제로 두 번 깨졌다.
        by_ars: dict[str, set[str]] = {}
        for route in mock.ROUTES:
            for stop in mock.route_stations(route["route_id"]):
                by_ars.setdefault(stop["ars"], set()).add(route["no"])
        ars, serving = next(((a, r) for a, r in by_ars.items() if len(r) > 1), (None, set()))
        self.assertIsNotNone(ars, "여러 노선이 만나는 정류장이 있어야 한다")

        nos = {a["no"] for a in mock.arrivals(ars)}
        self.assertTrue(nos, "그 정류장에 오는 버스가 하나도 없다")
        self.assertLessEqual(nos, serving, f"그 정류장에 오지 않는 노선이 섞였다: {nos - serving}")


class LiveSchemaTest(unittest.TestCase):
    """실데이터에서만 드러나는 것들. 목업으로는 절대 안 잡히는 종류다."""

    def test_service_key_is_not_double_encoded(self):
        """Encoding 키를 자동 인코딩에 맡기면 % 가 %25 로 두 번 인코딩돼 키가 죽는다."""
        from app.seoul_api import build_query
        encoded = build_query("aB%2BcD%2FeF%3D%3D", {"strSrch": "4312"})
        self.assertIn("serviceKey=aB%2BcD%2FeF%3D%3D", encoded)
        self.assertNotIn("%25", encoded, "이중 인코딩")

    def test_decoding_key_plus_becomes_percent2b(self):
        """httpx 는 쿼리에서 +를 안전 문자로 봐서 그냥 둔다. 포털은 %2B 를 기대한다."""
        from app.seoul_api import build_query
        self.assertIn("serviceKey=aB%2BcD%2FeF%3D%3D", build_query("aB+cD/eF==", {}))

    def test_both_key_forms_produce_the_same_query(self):
        from app.seoul_api import build_query
        self.assertEqual(build_query("aB+cD/eF==", {"q": "1"}),
                         build_query("aB%2BcD%2FeF%3D%3D", {"q": "1"}))

    def test_congestion_field_name_has_the_api_typo(self):
        """API 명세의 철자가 congetion 이다. congestion 으로 읽으면 전부 '정보없음'이 된다."""
        from app.seoul_api import norm_bus_pos
        self.assertEqual(norm_bus_pos({"congetion": "5"})["congestion"], "혼잡")

    def test_arrival_riders_is_a_headcount_not_a_congestion_code(self):
        """rerideNum1 은 혼잡도 코드가 아니라 재차인원(명) — 코드로 읽으면 4명이 '보통'이 된다."""
        from app.seoul_api import norm_arrival
        self.assertEqual(norm_arrival({"rerideNum1": "24", "arrmsg1": "곧 도착"})["riders"], 24)
        self.assertIsNone(norm_arrival({"rerideNum1": "0", "arrmsg1": "곧 도착"})["riders"],
                          "0 은 없음이 아니라 대부분 미집계다")
        self.assertNotIn("congestion", norm_arrival({"arrmsg1": "곧 도착"}),
                         "없는 값을 지어내면 안 된다")

    def test_arrival_reports_unknown_collection_time(self):
        """도착정보 API 는 수집 시각을 주지 않는다 — 없는 값을 지어내면 안 된다.

        getStationByUid 응답에 dataTm 은 존재하지 않고, repTm1 은 옵션이라 대부분
        빠지며 들어와도 `2021-12-26 20:05:47.0` 같은 과거 값이다. 그럴듯한 값을
        채우면 화면이 '0초 전'이라고 단언하게 된다. 모르면 모른다고 둔다.
        """
        from app.seoul_api import norm_arrival
        self.assertEqual(norm_arrival({"arrmsg1": "곧 도착"})["data_tm"], "")
        # repTm1 이 들어와도 쓰지 않는다
        row = norm_arrival({"arrmsg1": "곧 도착", "repTm1": "2021-12-26 20:05:47.0"})
        self.assertEqual(row["data_tm"], "")
        self.assertIsNone(data_age_seconds([row]), "모르는 나이는 None 이어야 한다")

    def test_bus_position_still_carries_collection_time(self):
        """반대로 버스위치(getBusPosByRtid)에는 dataTm 이 실제로 있다 — 이쪽은 계산한다."""
        from app.seoul_api import norm_bus_pos
        row = norm_bus_pos({"vehId": "1", "plainNo": "서울74사4621",
                            "sectOrd": "7", "dataTm": "20260918140000"})
        self.assertEqual(row["data_tm"], "20260918140000")
        self.assertIsNotNone(data_age_seconds([row]))

    def test_boarding_blockers_are_surfaced(self):
        """시간만 보고 뛰어갔는데 만차·우회면 화면이 사람을 헛걸음시킨 것이다."""
        from app.seoul_api import norm_arrival
        row = norm_arrival({"arrmsg1": "곧 도착", "isFullFlag1": "1", "deTourAt": "11",
                            "isLast1": "1", "busType1": "1"})
        self.assertTrue(row["is_full"] and row["is_detour"] and row["is_last"] and row["low_floor"])
        normal = norm_arrival({"arrmsg1": "곧 도착", "deTourAt": "00"})
        self.assertFalse(normal["is_full"] or normal["is_detour"])

    def test_tratime_cannot_be_seconds(self):
        """traTime1 은 항목크기 3인데 arrmsg 샘플 '136분45초후'는 초로 담으면 8205 — 안 들어간다."""
        from app.seoul_api import norm_arrival
        row = norm_arrival({"arrmsg1": "136분45초후[29번째 전]", "traTime1": "136"})
        self.assertEqual(row["sec1"], 8205)
        self.assertGreater(row["sec1"], 999, "초로 담을 수 없는 값이라는 것이 근거다")

    def test_route_type_covers_other_regions(self):
        """7:인천 · 8:경기 · 0:공용 까지 명세에 있다. 모르는 코드로 떨어뜨리면 안 된다."""
        from app.seoul_api import bus_type
        self.assertEqual(bus_type("7"), "인천")
        self.assertEqual(bus_type("8"), "경기")
        self.assertEqual(bus_type("0"), "공용")

    def test_arrival_direction_prefers_the_bus_terminus(self):
        """방면은 그 버스의 최종 정류소가 가장 정확하다. adirection 은 정류소 기준이다."""
        from app.seoul_api import norm_arrival
        self.assertEqual(norm_arrival({"arrmsg1": "곧 도착", "stationNm1": "석촌역",
                                       "adirection": "솔샘역"})["to"], "석촌역")
        self.assertEqual(norm_arrival({"arrmsg1": "곧 도착", "adirection": "솔샘역"})["to"], "솔샘역")

    def test_eta_is_parsed_from_message_not_tratime(self):
        """traTime 은 공식 문서가 '분', 실사용 코드가 '초'로 쓴다 — 단위가 확정되지 않았다.
        확정되지 않은 값으로 '5분 내' 필터를 만들면 조용히 12배 틀린다."""
        from app.seoul_api import parse_arrmsg
        self.assertEqual(parse_arrmsg("3분12초후[2번째 전]"), (192, 2))
        self.assertEqual(parse_arrmsg("곧 도착"), (0, 0))
        self.assertEqual(parse_arrmsg("54초후[1번째 전]"), (54, 1))

    def test_unparseable_eta_is_not_treated_as_arriving_now(self):
        """'출발대기'를 0초로 두면 catch_now 가 오지 않는 버스를 추천한다."""
        from app.seoul_api import norm_arrival
        row = norm_arrival({"arrmsg1": "출발대기", "arrmsg2": "운행종료"})
        self.assertGreater(row["sec1"], 300)
        self.assertGreater(row["sec2"], 300)

    def test_unknown_route_type_is_not_called_간선(self):
        """모르는 코드를 간선으로 단정하면 화면에 거짓 분류가 그려진다."""
        from app.seoul_api import bus_type
        self.assertEqual(bus_type("2"), "마을")
        self.assertEqual(bus_type("6"), "광역")
        self.assertEqual(bus_type("99"), "기타")

    def test_village_bus_number_comes_from_busroutenm(self):
        """busRouteAbrv 는 '안내용 약칭(마을버스 제외)'이라 강남06 에서 비어 있다."""
        from app.seoul_api import norm_route
        self.assertEqual(norm_route({"busRouteNm": "강남06", "busRouteId": "1"})["no"], "강남06")

    def test_missing_key_fails_loudly_instead_of_faking_data(self):
        """키가 없을 때 조용히 목업으로 떨어지면, 운영에서 가짜 데이터가 뜨고 아무도 모른다."""
        import importlib, app.config as cfg
        saved = dict(os.environ)
        try:
            os.environ.pop("USE_MOCK", None)
            os.environ["SEOUL_BUS_SERVICE_KEY"] = ""
            reloaded = importlib.reload(cfg)
            self.assertFalse(reloaded.USE_MOCK, "키가 없다고 목업으로 떨어지면 안 된다")
            with self.assertRaises(reloaded.MissingServiceKey):
                reloaded.require_service_key()
        finally:
            os.environ.clear()
            os.environ.update(saved)
            importlib.reload(cfg)

    def test_turn_point_is_captured(self):
        from app.seoul_api import norm_station_of_route
        self.assertTrue(norm_station_of_route({"seq": "9", "transYn": "Y"})["is_turn"])
        self.assertFalse(norm_station_of_route({"seq": "9", "transYn": "N"})["is_turn"])


class DirectionSplitTest(unittest.TestCase):
    """상·하행 분리. API 는 방향을 따로 주지 않으므로 회차지에서 우리가 자른다."""

    def _stops(self, n, turn=None):
        return [{"seq": i + 1, "is_turn": (i + 1 == turn)} for i in range(n)]

    def test_turn_point_splits_the_route(self):
        from app.main import turn_seq
        self.assertEqual(turn_seq(self._stops(41, turn=21)), 21)

    def test_route_without_turn_point_is_not_split(self):
        """편도·순환 노선에 없는 방향을 지어내면 안 된다."""
        from app.main import turn_seq
        self.assertIsNone(turn_seq(self._stops(20)))

    def test_turn_at_the_last_stop_is_one_way(self):
        """회차지가 종점이면 자를 수 없다 — 하행이 빈 목록이 된다."""
        from app.main import turn_seq
        self.assertIsNone(turn_seq(self._stops(20, turn=20)))

    def test_mock_route_is_a_round_trip_like_the_real_api(self):
        """목업이 편도만 주면 방향 분리 코드가 목업 모드에서 한 번도 실행되지 않는다."""
        from app.main import turn_seq
        for route in mock.ROUTES:
            stops = mock.route_stations(route["route_id"])
            t = turn_seq(stops)
            self.assertIsNotNone(t, f"{route['no']}: 목업에도 회차지가 있어야 한다")
            self.assertLess(t, len(stops), f"{route['no']}: 회차지가 종점이면 편도다")
            # 되돌아오는지는 ARS 가 아니라 이름으로 본다. 실데이터에서 하행은
            # 길 건너편 정류장을 지나므로 같은 장소라도 ARS 가 다르다.
            up = {s["name"] for s in stops[:t]}
            down = {s["name"] for s in stops[t:]}
            self.assertTrue(up & down, f"{route['no']}: 하행이 상행 구간을 되짚지 않는다")

    def test_auto_refresh_stops_when_nobody_is_looking(self):
        """탭이 숨으면 자동 갱신을 멈춘다.

        안 멈추면 아무도 안 보는 화면이 API 예산(일 1,000회)을 태우고,
        refresh 이벤트가 실제 행동 이벤트를 수십 배로 덮어쓴다."""
        source = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("visibilitychange", source, "탭 숨김을 감지하지 않는다")
        self.assertIn("REFRESH_MAX", source, "연속 갱신 상한이 없다")

    def test_direction_is_a_render_concern_not_an_extra_call(self):
        """방향 전환은 같은 응답을 다시 그릴 뿐이다 — 외부 호출이 늘면 안 된다."""
        source = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")
        toggle = source[source.index('pane.querySelectorAll("[data-dir]")'):]
        toggle = toggle[:toggle.index("});\n    });")]
        self.assertIn("renderRoute", toggle)
        self.assertNotIn("BusAPI.", toggle, "토글이 API 를 부르면 안 된다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
