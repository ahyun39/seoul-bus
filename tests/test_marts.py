"""분석 파이프라인 회귀 테스트 — 멱등성과 귀속 규칙.

둘 다 틀려도 화면에는 아무 이상이 없고 숫자만 조용히 부풀어 오른다.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["USE_MOCK"] = "1"

from scripts import build_marts  # noqa: E402


def ev(seq, name, **over):
    base = {
        "event_id": f"e{seq}", "event_name": name, "session_id": "s-1", "seq": seq,
        "event_ts": f"2026-09-17T05:00:{seq:02d}.000Z",
        "sent_ts": f"2026-09-17T05:00:{seq + 2:02d}.000Z",
        "ingest_ts": f"2026-09-17T05:00:{seq + 2:02d}.100Z",
        "schema_version": "1.0.0", "source": "live",
    }
    base.update(over)
    return base


class MartsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = Path(self.tmp.name) / "events"
        self.events.mkdir()
        build_marts.EVENT_DIR = self.events
        self.db = Path(self.tmp.name) / "analytics.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows, name="events-2026-09-17-05.jsonl"):
        (self.events / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    def _rows(self, sql):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        out = [dict(r) for r in conn.execute(sql)]
        conn.close()
        return out

    def test_rebuild_is_idempotent_and_counts_duplicates(self):
        """몇 번을 돌려도 raw 행이 늘지 않는다. 강제 재적재(--rebuild)면 재도착으로 센다."""
        self._write([ev(1, "intent.select", intent="where_bus"), ev(2, "search.query", query="4312")])
        first = build_marts.build(self.db)
        again = build_marts.build(self.db, rebuild=True)
        self.assertEqual(first["new"], 2)
        self.assertEqual(again["new"], 0, "event_id 가 PK 라 행이 늘면 안 된다")
        self.assertEqual(again["duplicates"], 2, "다시 읽었으면 재도착으로 세야 한다")
        self.assertEqual(again["raw_total"], 2)

    def test_second_run_skips_files_it_already_read(self):
        """증분 적재. 같은 파일을 다시 읽으면 '재전송으로 두 번 도착'과
        '같은 파일 재적재'가 한 숫자로 섞여 중복률 지표를 못 믿게 된다."""
        self._write([ev(1, "search.query"), ev(2, "search.click")])
        first = build_marts.build(self.db)
        second = build_marts.build(self.db)
        self.assertEqual(first["read"], 2)
        self.assertEqual(second["read"], 0, "같은 파일을 다시 읽었다")
        self.assertEqual(second["skipped_files"], 1)
        self.assertEqual(second["duplicates"], 0, "재적재가 재도착으로 둔갑했다")

    def test_appended_file_is_read_again(self):
        """파일이 자라면 다시 읽는다 — 그 시간대 파일은 계속 쓰이는 중일 수 있다."""
        self._write([ev(1, "search.query")])
        build_marts.build(self.db)
        self._write([ev(1, "search.query"), ev(2, "search.click")])   # 같은 파일에 추가
        out = build_marts.build(self.db)
        self.assertEqual(out["read"], 2)
        self.assertEqual(out["new"], 1, "새 이벤트만 들어가야 한다")
        self.assertEqual(out["duplicates"], 1, "이미 있던 1건은 재도착으로 센다")

    def test_unparsable_line_is_counted_not_crashed(self):
        """깨진 줄 하나가 적재 전체를 멈추면 안 된다."""
        self._write([ev(1, "search.query")])
        with open(self.events / "events-2026-09-17-05.jsonl", "a", encoding="utf-8") as fp:
            fp.write("{ 깨진 줄\n")
        out = build_marts.build(self.db)
        self.assertEqual(out["unparsable"], 1)
        self.assertEqual(out["raw_total"], 1)

    def test_route_map_click_does_not_inflate_the_search_funnel(self):
        """노선도 클릭은 검색 결과 클릭이 아니다.

        구버전은 이것도 search.click 으로 남겨, 그대로 세면 클릭률이 200% 가 된다.
        """
        self._write([
            ev(1, "search.query", search_type="route", query="4312", result_count=1),
            ev(2, "search.click", target_type="route", target_id="100100500", position=1),
            ev(3, "route.view", route_id="100100500"),
            ev(4, "search.click", target_type="station", target_id="23149", **{"from": "route_map"}),
            ev(5, "station.view", station_id="122000049"),
        ])
        build_marts.build(self.db)
        funnel = self._rows("SELECT clicks, clicked, views FROM fact_search")
        self.assertEqual(len(funnel), 1)
        self.assertEqual(funnel[0]["clicks"], 1)          # 2 가 되면 퍼널이 거짓말을 한다
        self.assertEqual(funnel[0]["views"], 1)           # nav.click 뒤의 조회는 검색에 귀속되지 않는다
        names = {r["event_name"] for r in self._rows("SELECT event_name FROM clean_events")}
        self.assertIn("nav.click", names)

    def test_duplicate_search_id_does_not_inflate_the_funnel(self):
        """같은 search_id 로 두 번 기록돼도 fact_search 는 한 행이어야 한다.

        수집 로그에서 실제로 나온 상황(doSearch 재진입). 뷰가 grain 을 지키지 않으면
        검색 하나가 두 행이 되고, 그 두 행이 같은 클릭을 각각 세어 클릭 수가 부푼다.
        COUNT(*) 로 재는 이유: DISTINCT 로 재면 중복 행이 남아 있어도 통과한다.
        """
        self._write([
            ev(1, "search.query", search_id="sq-dup", query="일원동우체국",
               result_count=2, schema_version="1.2.0"),
            ev(2, "search.query", search_id="sq-dup", query="일원동우체국",
               result_count=2, schema_version="1.2.0"),
            ev(3, "search.click", search_id="sq-dup", ars="23400", position=1,
               schema_version="1.2.0"),
        ])
        build_marts.build(self.db)
        f = self._rows("""SELECT COUNT(*) rows, SUM(clicks) clicks, SUM(clicked) clicked
                          FROM fact_search""")[0]
        self.assertEqual(f["rows"], 1, "검색 한 번은 한 행이어야 한다")
        self.assertEqual(f["clicks"], 1, "클릭 이벤트는 1건인데 두 행이 각각 세면 2 가 된다")
        self.assertEqual(f["clicked"], 1, "SUM(clicked)/COUNT(*) 가 그대로 클릭률이어야 한다")

    def test_click_and_view_join_on_ars(self):
        """클릭은 ARS, 조회는 내부 ID 만 남기면 둘을 이을 수 없다(1.0.0 의 문제)."""
        self._write([
            ev(1, "search.click", search_id="s1", ars="23397", position=1, schema_version="1.2.0"),
            ev(2, "station.view", search_id="s1", ars="23397", station_id="122000293",
               schema_version="1.2.0"),
        ])
        build_marts.build(self.db)
        joined = self._rows("""SELECT k.ars FROM clean_events k JOIN clean_events v
                               ON v.event_name='station.view' AND v.ars = k.ars
                               WHERE k.event_name='search.click'""")
        self.assertEqual(len(joined), 1)

    def test_seq_gap_sees_loss_at_the_head_of_a_session(self):
        """seq 는 1부터 시작한다 — MIN(seq) 기준으로 재면 세션 앞부분 유실이 0 으로 보인다."""
        self._write([ev(3, "search.query"), ev(4, "search.click"), ev(5, "route.view")])
        build_marts.build(self.db)
        row = self._rows("SELECT seq_gap FROM fact_session")[0]
        self.assertEqual(row["seq_gap"], 2, "앞의 seq 1·2 가 사라진 것이 보이지 않는다")

    def test_client_drop_is_separable_from_server_loss(self):
        """gap 을 전부 서버 유실로 읽지 않으려면 클라이언트가 버린 건수가 함께 있어야 한다."""
        rows = [ev(3, "search.query"), ev(4, "search.click")]
        for r in rows:
            r["client_dropped"] = 2        # 수집 서버가 배치에서 옮겨 적는 값
        self._write(rows)
        build_marts.build(self.db)
        row = self._rows("SELECT seq_gap, client_dropped FROM fact_session")[0]
        self.assertEqual(row["seq_gap"], 2)
        self.assertEqual(row["client_dropped"], 2)

    def test_search_funnel_does_not_join_across_sessions(self):
        """search_id 는 짧은 난수라 겹친다 — 세션 경계 없이 조인하면 남의 클릭이 붙는다."""
        a = ev(1, "search.query", search_id="dup", query="4312", result_count=1)
        b = dict(ev(1, "search.click", search_id="dup", position=1), session_id="s-2", event_id="other")
        self._write([a, b])
        build_marts.build(self.db)
        rows = self._rows("SELECT session_id, clicks FROM fact_search")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["clicks"], 0, "다른 세션의 클릭이 붙었다")

    def test_batch_is_one_row_in_fact_batch(self):
        """'어떤 계기로, 몇 번째 시도에, 몇 건을 보냈나'는 이벤트가 아니라 전송 단위의 질문이다."""
        rows = [ev(1, "search.query"), ev(2, "search.click")]
        for r in rows:
            # 한 배치의 행들은 같은 sent_ts 를 갖는다(수집 서버가 배치 값을 복제한다).
            r.update({"batch_trigger": "pagehide", "batch_attempt": 3, "client_dropped": 0,
                      "sent_ts": "2026-09-17T05:00:30.000Z"})
        self._write(rows)
        build_marts.build(self.db)
        batches = self._rows("SELECT * FROM fact_batch")
        self.assertEqual(len(batches), 1, "같은 배치가 여러 행이 됐다")
        self.assertEqual(batches[0]["trigger"], "pagehide")
        self.assertEqual(batches[0]["attempt"], 3)
        self.assertEqual(batches[0]["events"], 2)

    def test_session_duration_ignores_broken_clocks(self):
        """시계가 어긋난 이벤트 하나가 섞이면 세션 길이가 며칠짜리가 된다."""
        rows = [ev(1, "search.query"), ev(2, "search.click"),
                dict(ev(3, "route.view"), event_ts="2099-01-01T00:00:00.000Z", event_id="future")]
        self._write(rows)
        build_marts.build(self.db)
        row = self._rows("SELECT duration_sec FROM fact_session")[0]
        self.assertLess(row["duration_sec"], 60, "미래 시각 이벤트가 세션 길이를 늘렸다")

    def test_rejected_events_are_queryable(self):
        """거부를 파일로만 두면 '늘었다'는 사실만 알고 '무엇이 왜'는 못 본다."""
        (self.events / "dlq-2026-09-17.jsonl").write_text(json.dumps({
            "received_at": "2026-09-17T05:00:00.000Z", "reason": "schema_violation",
            "index": 0, "event_id": "bad-1",
            "errors": ["unknown event_name: search.qeury"],
            "raw": {"event_id": "bad-1", "event_name": "search.qeury", "session_id": "s-1"},
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        self._write([ev(1, "search.query")])
        out = build_marts.build(self.db)
        self.assertEqual(out["dlq_new"], 1)
        row = self._rows("SELECT * FROM fact_rejection")[0]
        self.assertEqual(row["event_name"], "search.qeury")
        self.assertEqual(row["replayable"], 1, "원본이 없으면 재처리할 수 없다")

        again = build_marts.build(self.db, rebuild=True)
        self.assertEqual(again["dlq_new"], 0, "같은 거부가 두 번 적재됐다")

    def test_api_error_code_survives_to_the_mart(self):
        """오류 코드를 버리면 '실패했다'는 알지만 '왜'는 영영 모른다."""
        self._write([ev(1, "api.call", endpoint="bus_pos", status=502, error_code="23")])
        build_marts.build(self.db)
        self.assertEqual(self._rows("SELECT error_code FROM clean_events")[0]["error_code"], "23")

    def test_entry_intent_is_the_first_one_not_the_alphabetical_one(self):
        """MIN(current_intent) 으로 뽑으면 catch_now 가 첫 의도로 둔갑한다."""
        self._write([
            ev(1, "intent.select", intent="where_bus"),
            ev(2, "intent.select", intent="what_comes"),
            ev(3, "intent.select", intent="catch_now"),
        ])
        build_marts.build(self.db)
        self.assertEqual(self._rows("SELECT entry_intent FROM fact_session")[0]["entry_intent"],
                         "where_bus")

    def test_old_seq_is_marked_untrustworthy(self):
        """1.2.0 이전 seq 는 페이지 로드 단위라 gap 계산에 쓰면 안 된다."""
        self._write([ev(1, "search.query"), ev(2, "search.query", schema_version="1.2.0", event_id="new")])
        build_marts.build(self.db)
        rows = self._rows("SELECT schema_version, seq_trustworthy FROM clean_events ORDER BY schema_version")
        self.assertEqual([(r["schema_version"], r["seq_trustworthy"]) for r in rows],
                         [("1.0.0", 0), ("1.2.0", 1)])

    def test_legacy_delays_are_recomputed_from_timestamps(self):
        """구버전에는 queue/ingest delay 가 없다. 원본 시각에서 다시 계산한다."""
        self._write([ev(1, "search.query")])
        build_marts.build(self.db)
        row = self._rows("SELECT queue_delay_ms, ingest_delay_ms FROM clean_events")[0]
        self.assertEqual(row["queue_delay_ms"], 2000)
        self.assertEqual(row["ingest_delay_ms"], 100)


if __name__ == "__main__":
    unittest.main()
