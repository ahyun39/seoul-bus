"""DLQ 재처리 — 스키마를 고친 뒤 거부됐던 이벤트를 다시 넣는다.

다시 넣을 수 없으면 DLQ 가 아니라 에러 로그다. 그래서 수집기가 사유와 함께 남긴
원본(raw)을 이 스크립트가 다시 태운다. 재처리분은 source='replay' 로 구분되고,
event_id 를 그대로 써서 중복은 후속 적재의 PK 가 흡수한다.

    python -m scripts.replay_dlq --dry-run   # 무엇이 들어갈지만
    python -m scripts.replay_dlq             # 실제 재주입
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app import collector
from app.config import MAX_COLLECT_EVENTS, WRITE_KEYS


def _rows(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                      # 깨진 줄 하나가 재처리 전체를 멈추지 않게
    return out


def replay(dry_run: bool = False) -> dict:
    if not WRITE_KEYS:
        raise SystemExit("[replay_dlq] 등록된 write_key 가 없습니다 (.env 의 WRITE_KEYS)")
    key = next(iter(WRITE_KEYS))

    # 수집기가 실제로 쓰는 디렉터리를 따라간다 — config 를 따로 읽으면 테스트에서 어긋난다.
    event_dir = collector.EVENT_DIR

    total = replayable = accepted = still_bad = 0
    for path in sorted(event_dir.glob("dlq-*.jsonl")):
        rows = _rows(path)
        total += len(rows)
        raws = [r["raw"] for r in rows if isinstance(r.get("raw"), dict)]
        replayable += len(raws)
        if dry_run or not raws:
            continue

        for i in range(0, len(raws), MAX_COLLECT_EVENTS):
            out = collector.accept(
                {"write_key": key, "events": raws[i:i + MAX_COLLECT_EVENTS]},
                None, source="replay")
            accepted += out["accepted"]
            still_bad += len(out["rejected"])
        # 처리 표시만 하고 지우지 않는다 — 무엇이 왜 거부됐는지는 사고 조사의 근거다.
        path.rename(path.with_suffix(".jsonl.replayed"))

    return {"dlq_rows": total, "replayable": replayable,
            "accepted": accepted, "still_rejected": still_bad}


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    out = replay(dry_run=dry)
    print(f"[replay_dlq]{' (dry-run)' if dry else ''} "
          f"DLQ {out['dlq_rows']}행 · 재처리 가능 {out['replayable']}건 · "
          f"수용 {out['accepted']} · 여전히 거부 {out['still_rejected']}")
    if out["dlq_rows"] and not out["replayable"]:
        print("  ⚠ raw 가 없는 예전 DLQ 입니다. 원본을 보존하기 전에 기록된 건은 재처리할 수 없습니다.")
