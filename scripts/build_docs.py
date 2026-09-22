"""프로젝트 해설 문서 빌드.

docs/template.html 의 {{SNIP:이름}} 자리에 소스를 그때그때 뽑아 끼운다 —
복사해 두면 코드를 고쳤을 때 문서만 옛날 것으로 남는다.

    python -m scripts.build_docs
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "docs" / "template.html"
# 출력은 index.html 하나 — docs/ 를 그대로 정적 사이트로 띄울 수 있게.
OUTPUT = ROOT / "docs" / "index.html"


def grab(path: str, start: str, *, end: str | None = None, lines: int | None = None,
         strip_doc: bool = False) -> str:
    """파일에서 한 조각을 뽑는다(start 정규식이 처음 걸리는 줄부터)."""
    text = (ROOT / path).read_text(encoding="utf-8").splitlines()
    start_re = re.compile(start)
    begin = next((i for i, line in enumerate(text) if start_re.search(line)), None)
    if begin is None:
        raise SystemExit(f"[build_docs] 시작 패턴을 찾지 못했습니다: {path} :: {start}")

    if lines is not None:
        chunk = text[begin:begin + lines]
    elif end is not None:
        end_re = re.compile(end)
        stop = next((i for i in range(begin + 1, len(text)) if end_re.search(text[i])), len(text))
        chunk = text[begin:stop]
    else:
        raise SystemExit("[build_docs] end 또는 lines 중 하나는 있어야 합니다")

    while chunk and not chunk[-1].strip():
        chunk.pop()

    if strip_doc:  # 문서화용으로 docstring 을 걷어낼 때
        out, in_doc = [], False
        for line in chunk:
            if line.strip().startswith('"""'):
                in_doc = not in_doc or line.strip().count('"""') == 2 and False
                continue
            if not in_doc:
                out.append(line)
        chunk = out

    return dedent("\n".join(chunk))


SNIPS: dict[str, str] = {
    # ---- 04 외부 API ----
    "api_ep": grab("app/seoul_api.py", r"^EP = \{", end=r"^OK_CODES"),
    "api_call": grab("app/seoul_api.py", r"    async def call", end=r"^    @staticmethod"),
    "api_parse": grab("app/seoul_api.py", r"    def _parse", end=r"^# -{5,} 정규화"),
    "api_pick": grab("app/seoul_api.py", r"^def pick", end=r"^def as_int"),
    "api_norm": grab("app/seoul_api.py", r"^def norm_bus_pos", end=r"^def norm_station\("),
    "api_aslist": grab("app/seoul_api.py", r"^def _as_list", end=r"^class SeoulBusClient"),

    # ---- 05 저장 계층 ----
    "ddl_rs": grab("sql/ddl.sql", r"^CREATE TABLE IF NOT EXISTS route_stations", end=r"^CREATE TABLE IF NOT EXISTS stations"),
    "store_upsert": grab("app/store.py", r"^def upsert_route_stations", end=r"^def upsert_stations"),
    "store_through": grab("app/store.py", r"^def routes_through_station", end=r"^def counts"),
    "store_ttl": grab("app/store.py", r"^class TTLCache", end=r"^live_cache = TTLCache"),

    # ---- 06 검색 ----
    "main_rank": grab("app/main.py", r"^def _rank", lines=3),
    "main_search": grab("app/main.py", r"    cached = await run_in_threadpool\(store\.search_routes", end=r"^async def _ensure_stations"),
    "main_ensure": grab("app/main.py", r"^async def _ensure_stations", end=r"^@app\.get\(\"/api/routes/\{route_id\}\"\)"),

    # ---- 07 SDK ----
    "col_track": grab("app/static/collector.js", r"Collector\.prototype\.track = ", end=r"Collector\.prototype\._take"),
    "col_take": grab("app/static/collector.js", r"Collector\.prototype\._take = ", end=r"Collector\.prototype\.flush"),
    # 앵커는 코드에 건다 — 주석 문구에 걸면 주석을 고칠 때마다 빌드가 깨진다.
    "col_flush": grab("app/static/collector.js", r"if \(leaving && global\.navigator\.sendBeacon\)", lines=8),

    # ---- 08 수집 엔드포인트 ----
    "srv_path": grab("app/collector.py", r"^def _path_for", end=r"^def accept"),
    "srv_accept": grab("app/collector.py", r"^def accept", end=r"^def stats"),

    # ---- 09 화면 ----
    "app_intents": grab("app/static/app.js", r"  var INTENTS = \{", end=r"^  var state = \{"),
    "app_strip": grab("app/static/app.js", r"  function stripSvg", end=r"^  async function openRoute"),
    "app_live": grab("app/main.py", r"^async def _live", end=r"^# -{5,} 라우트"),

    # ---- 14 코드 리뷰 ----
    "fix_db": grab("app/store.py", r"^def db\(\) -> Iterator", end=r"^@contextmanager"),
    "fix_ttl_get": grab("app/store.py", r"    def get\(self, key: str\)", end=r"    def set\(self, key"),
    "fix_service": grab("app/collector.py", r"^def resolve_service", end=r"^def _now"),
    "fix_age": grab("app/seoul_api.py", r"^def data_age_seconds", end=r"^class SeoulApiError"),
    "fix_usage": grab("app/store.py", r"^def record_api_call", end=r"^def api_usage_today"),
    "fix_hook": grab("app/seoul_api.py", r"        self\.on_call", end=r"    async def _http"),
    "fix_limits": grab("app/main.py", r"^async def collect", end=r"^@app\.get\(.api/collector/stats"),

    # ---- 14 실데이터 연동 ----
    "fix_arrmsg": grab("app/seoul_api.py", r"^def parse_arrmsg", end=r"^def norm_route"),
    "fix_key": grab("app/seoul_api.py", r"^def build_query", end=r"^def as_int"),
    "fix_turn": grab("app/main.py", r"^def turn_seq", end=r"^def _rank"),

    # ---- 10 목업 ----
    "cfg_mock": grab("app/config.py", r"^# 공공데이터포털 인증키", end=r"^# 실시간 호출 캐시"),
}


def main() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    used: set[str] = set()

    def sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in SNIPS:
            raise SystemExit(f"[build_docs] 없는 조각입니다: {name}")
        used.add(name)
        return html.escape(SNIPS[name], quote=False)

    out = re.sub(r"\{\{SNIP:([a-z_]+)\}\}", sub, template)

    unused = set(SNIPS) - used
    if unused:
        print(f"[build_docs] 템플릿에서 쓰이지 않은 조각: {sorted(unused)}", file=sys.stderr)

    # 템플릿에 <head> 가 없다. charset 선언 없이 저장하면 서버 설정에 따라 한글이 깨진다.
    head, _, body = out.partition("\n\n<header")
    page = (
        '<!DOCTYPE html>\n<html lang="ko">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="color-scheme" content="light dark">\n'
        '<meta name="description" content="서울버스 로그 수집 프로젝트 — 신뢰성 중심의 수집 설계와 현재 구현이 보장하는 범위를 코드와 함께 설명한 문서.">\n'
        + head + "\n</head>\n<body>\n\n<header" + body + "\n\n</body>\n</html>\n"
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(page, encoding="utf-8")
    print(f"[build_docs] {OUTPUT.relative_to(ROOT)} · {len(page):,} bytes · 코드 조각 {len(used)}개")


if __name__ == "__main__":
    main()
