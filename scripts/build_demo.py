"""단일 파일 데모 빌드 — 서버도 키도 네트워크도 없이 더블클릭으로 열린다.

app/static 의 index.html·styles.css·collector.js·api-mock.js·app.js 와
app/mock_data.json 을 하나의 .html 로 합친다. api-http.js 자리에 api-mock.js 를
끼우는 것이 전부이고, app.js 는 BusAPI 만 알기 때문에 화면 코드는 그대로다.

    python -m scripts.build_demo [출력경로]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DEFAULT_OUT = ROOT / "docs" / "demo.html"

BANNER = """
<div class="demoNote">
  <b>단일 파일 데모</b>
  서버 없이 이 파일 하나로 동작합니다. 담긴 노선은 <code>4312</code> · <code>402</code> ·
  <code>강남06</code> · <code>160</code> 네 개이고,
  <b>정류장 순서·차량 번호·도착 시각·재차인원은 모두 합성 데이터</b>입니다. 실제 운행 정보가 아닙니다.
  노선 종류와 기·종점만 실제 노선을 참고했습니다.
  <span>오른쪽 패널은 이 화면에서 만들어지는 이벤트입니다. 데모에서는 전송하지 않고 표시만 합니다.</span>
</div>
""".strip()

BANNER_CSS = """
.demoNote{border:1px solid var(--line-2);background:var(--accent-soft);color:var(--ink-2);
  border-radius:10px;padding:11px 14px;margin-bottom:16px;font-size:12.5px;line-height:1.65}
.demoNote b{color:var(--accent-ink)}
.demoNote code{font-family:"JetBrains Mono",monospace;font-size:11.5px;background:var(--surface);
  border:1px solid var(--line);border-radius:4px;padding:1px 5px}
.demoNote span{display:block;margin-top:5px;color:var(--muted)}
""".strip()


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def build() -> str:
    page = read("index.html")

    # <link>/<script> 로 걸린 로컬 파일을 본문에 인라인한다.
    css = read("styles.css") + "\n\n" + BANNER_CSS
    page = page.replace(
        '<link rel="stylesheet" href="/static/styles.css">',
        "<style>\n" + css + "\n</style>",
    )

    # 정적 마스터를 먼저 심는다 — api-mock.js 가 이걸 보고 진짜 노선으로 그린다.
    # 없으면 심지 않고, api-mock.js 는 합성 노선으로 떨어진다.
    fixture = ROOT / "app" / "mock_data.json"
    blocks = []
    if fixture.exists():
        blocks.append("<script>\nwindow.MOCK_DATA = " +
                      fixture.read_text(encoding="utf-8").strip() + ";\n</script>")
    blocks += ["<script>\n" + read(name) + "\n</script>"
               for name in ("collector.js", "api-mock.js", "app.js")]
    scripts = "\n".join(blocks)
    # 치환문은 람다로. 문자열로 넘기면 re 가 백슬래시를 이스케이프로 해석해
    # JS 정규식(\s, \d)이 들어오는 순간 'bad escape' 로 터진다.
    page = re.sub(
        r'<script src="/static/collector\.js"></script>\s*'
        r'<script src="/static/api-http\.js"></script>\s*'
        r'<script src="/static/app\.js"></script>',
        lambda _: scripts,
        page,
    )
    if "/static/" in page:
        raise SystemExit("[build_demo] 남은 외부 참조가 있습니다: " +
                         ", ".join(sorted(set(re.findall(r'"[^"]*?/static/[^"]*"', page)))))

    page = page.replace('<div class="app">', BANNER + '\n    <div class="app">', 1)
    page = page.replace("<title>서울버스 노선 뷰어</title>",
                        "<title>서울버스 노선 뷰어 — 단일 파일 데모</title>")
    return page


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    html = build()
    out.write_text(html, encoding="utf-8")
    print(f"[build_demo] {out} · {len(html.encode('utf-8')):,} bytes")


if __name__ == "__main__":
    main()
