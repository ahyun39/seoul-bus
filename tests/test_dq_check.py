"""수집 상태 점검 회귀 테스트 — 점검기가 실제로 고장을 잡는가.

통과만 시키는 점검기는 없는 것과 같고, 없는 줄도 모르게 된다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["USE_MOCK"] = "1"

from app.config import KST  # noqa: E402
from scripts import dq_check  # noqa: E402


def iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DqCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        dq_check.EVENT_DIR = self.dir
        dq_check.ANALYTICS_DB = self.dir / "none.db"      # landing 단계만 본다
        self.now = datetime.now(KST)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows, name="events-2026-09-18-05-w1.jsonl"):
        (self.dir / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    def _ev(self, ingest, **over):
        base = {"event_id": "e", "event_name": "search.query", "session_id": "s", "seq": 1,
                "ingest_ts": iso(ingest), "schema_version": "1.3.0"}
        base.update(over)
        return base

    def _failed(self):
        return [r[0] for r in dq_check.check_landing(self.now) if r[3] == "❌"]

    def test_no_files_at_all_is_caught(self):
        """한 번도 수집되지 않은 상태. 조용하다고 정상이 아니다."""
        self.assertIn("수집 파일", self._failed())

    def test_collection_stopped_is_caught(self):
        """가장 놓치기 쉬운 고장 — 아무 에러도 안 나고 그냥 조용해진다."""
        self._write([self._ev(self.now - timedelta(days=2))])
        self.assertIn("마지막 수집", self._failed())

    def test_fresh_collection_passes(self):
        self._write([self._ev(self.now - timedelta(days=1), event_id=f"y{i}") for i in range(20)]
                    + [self._ev(self.now, event_id=f"t{i}") for i in range(18)])
        self.assertEqual(self._failed(), [])

    def test_volume_drop_is_caught(self):
        """전일의 절반 미만이면 사람이 봐야 한다."""
        self._write([self._ev(self.now - timedelta(days=1), event_id=f"y{i}") for i in range(30)]
                    + [self._ev(self.now, event_id="t1")])
        self.assertIn("전일 대비 물량", self._failed())

    def test_newer_contract_version_is_caught(self):
        """서버보다 높은 버전이 들어오면 배포 순서가 어긋난 것이다."""
        self._write([self._ev(self.now, event_id="a"),
                     self._ev(self.now, event_id="b", schema_version="9.9.9")])
        self.assertIn("스키마 버전", self._failed())

    def test_rejection_rate_is_caught(self):
        """거부율이 오르는 것은 클라이언트와 서버의 스키마가 어긋났다는 첫 신호다."""
        self._write([self._ev(self.now, event_id=f"e{i}") for i in range(100)])
        (self.dir / "dlq-2026-09-18.jsonl").write_text(
            "\n".join(json.dumps({"received_at": iso(self.now), "reason": "schema_violation",
                                  "errors": ["unknown event_name"]}) for _ in range(5)) + "\n",
            encoding="utf-8")
        self.assertIn("거부율", self._failed())

    def test_mtime_alone_does_not_count_as_fresh(self):
        """파일을 건드리기만 해도 mtime 은 올라간다. 신선도는 안의 시각으로 판단해야 한다."""
        self._write([self._ev(self.now - timedelta(days=3))])
        (self.dir / "events-2026-09-18-05-w1.jsonl").touch()      # mtime 만 지금으로
        self.assertIn("마지막 수집", self._failed())

    def test_landing_check_works_without_the_mart(self):
        """적재 자체가 멈춘 것도 탐지 대상이라, 마트가 있어야만 점검되면 그 고장을 놓친다."""
        self._write([self._ev(self.now)])
        self.assertFalse(dq_check.ANALYTICS_DB.exists())
        rows = dq_check.check_landing(self.now)
        self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main()
