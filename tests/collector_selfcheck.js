/* node tests/collector_selfcheck.js
 *
 * collector.js 의 규약: payload 는 봉투 필드를 덮을 수 없고, 유실은 조용하지 않다.
 * 깨져도 화면에는 아무 이상이 없고 dedup·유실률·세션 분석만 조용히 틀어진다.
 */
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const mem = {};
const win = {
  localStorage: { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = v; } },
  navigator: { userAgent: "node", doNotTrack: "0" },
  document: { visibilityState: "visible", addEventListener() {} },
  screen: { width: 1280 },
  console: { warn() {} },
  addEventListener() {},
  setInterval: () => 0,
  fetch: () => Promise.resolve({ ok: true, status: 200 }),
};
// vm 컨텍스트는 node 전역을 물려받지 않는다 — SDK 가 쓰는 것만 넣는다.
const SRC = fs.readFileSync(path.join(__dirname, "../app/static/collector.js"), "utf8");
const load = (w) => vm.runInNewContext(SRC, { window: w, Blob: require("node:buffer").Blob });
load(win);

const c = win.collector.init({ endpoint: "/v1/collect", writeKey: "wk_test" });
c.setContext({ entry_intent: "where_bus", current_intent: "what_comes" });

const honest = c.track("search.query", { query: "402" });
const hostile = c.track("search.query", {
  query: "402",
  event_id: "fake", seq: 999999, session_id: "another-session",
  event_ts: "wrong-time", event_type: "system",
});

assert.notStrictEqual(hostile.event_id, "fake", "event_id 가 payload 로 덮였다");
assert.strictEqual(hostile.seq, honest.seq + 1, "seq 가 payload 로 덮였다");
assert.strictEqual(hostile.session_id, honest.session_id, "session_id 가 payload 로 덮였다");
assert.ok(!isNaN(Date.parse(hostile.event_ts)), "event_ts 가 payload 로 덮였다");
assert.strictEqual(hostile.event_type, undefined, "event_type 은 서버가 도출한다");
assert.strictEqual(hostile.query, "402", "일반 payload 필드는 살아 있어야 한다");
assert.strictEqual(hostile.entry_intent, "where_bus", "문맥 필드가 빠졌다");
assert.strictEqual(hostile.current_intent, "what_comes", "문맥 필드가 빠졌다");

// 새로고침 재현 — seq 가 인스턴스에 있으면 여기서 1 로 되감긴다.
const win2 = Object.assign({}, win, { collector: undefined });
load(win2);
const reloaded = win2.collector.init({ endpoint: "/v1/collect", writeKey: "wk_test" })
                     .track("refresh", {});

assert.strictEqual(reloaded.session_id, hostile.session_id, "새로고침에서 세션이 끊겼다");
assert.ok(reloaded.seq > hostile.seq,
          `새로고침 후 seq 가 되감겼다 (이전 ${hostile.seq} → 지금 ${reloaded.seq})`);

// ── 전달 보장 ────────────────────────────────────────────────────────
// '조용한 유실이 없다'를 성립시키는 조건들.

function freshCollector(fetchImpl, beaconImpl) {
  const mem2 = {};
  const w = Object.assign({}, win, {
    collector: undefined,
    localStorage: { getItem: (k) => (k in mem2 ? mem2[k] : null), setItem: (k, v) => { mem2[k] = v; } },
    navigator: { userAgent: "node", doNotTrack: "0", sendBeacon: beaconImpl },
    fetch: fetchImpl,
  });
  load(w);
  return w.collector.init({ endpoint: "/v1/collect", writeKey: "wk_test" });
}

// 1) 응답을 기다리는 동안 다시 보내지 않는다 (중복 전송 + 미전송 유실)
let calls = 0;
const hanging = freshCollector(() => { calls += 1; return new Promise(() => {}); });
for (let i = 0; i < 12; i++) hanging.track("refresh", { i });   // 10건에서 자동 flush
hanging.flush("interval");
hanging.flush("interval");
assert.strictEqual(calls, 1, `인플라이트 중에 ${calls}번 보냈다 — 중복 전송`);

// 2) 영구 4xx 는 큐에서 버리고 건수를 센다 (poison pill 방지)
const rejecting = freshCollector(() => Promise.resolve({ ok: false, status: 413 }));
for (let i = 0; i < 10; i++) rejecting.track("refresh", { i });
setTimeout(() => {
  assert.strictEqual(rejecting.stats().queued, 0, "413 을 받고도 큐에 남아 뒤를 막는다");
  assert.ok(rejecting.stats().rejected > 0, "버린 건수를 세지 않았다");

  // 3) 5xx 는 큐에 남기고 백오프한다
  const failing = freshCollector(() => Promise.resolve({ ok: false, status: 500 }));
  for (let i = 0; i < 10; i++) failing.track("refresh", { i });
  setTimeout(() => {
    assert.ok(failing.stats().queued > 0, "5xx 인데 큐를 비웠다 — 유실");
    assert.ok(failing.stats().retryInMs > 0, "백오프 없이 즉시 재시도한다");

    // 4) sendBeacon 이 false 면 전송된 것이 아니다
    const beaconFails = freshCollector(() => Promise.resolve({ ok: true }), () => false);
    beaconFails.track("refresh", {});
    beaconFails.flush("pagehide");
    assert.ok(beaconFails.stats().queued > 0, "beacon 이 거부했는데 보냈다고 쳤다 — 이탈 시점 유실");

    // 5) 백오프 중이라도 이탈 시점에는 보낸다
    let beaconed = 0;
    const leaving = freshCollector(() => Promise.resolve({ ok: false, status: 500 }),
                                   () => { beaconed += 1; return true; });
    for (let i = 0; i < 10; i++) leaving.track("refresh", { i });
    setTimeout(() => {
      assert.ok(leaving.stats().retryInMs > 0, "전제: 백오프 중이어야 한다");
      leaving.flush("pagehide");
      assert.strictEqual(beaconed, 1, "백오프가 이탈 시점 전송까지 막았다");

      // 6) 이탈 이벤트가 이탈 전송을 가로채지 않는다
      //    page.leave 가 큐를 임계치로 밀면 이탈 전송이 인플라이트 가드에 막혀 fetch 로 나간다.
      let sent = { fetch: 0, beacon: 0 };
      const mem6 = {};
      const handlers = {};
      const w6 = Object.assign({}, win, {
        collector: undefined,
        localStorage: { getItem: (k) => (k in mem6 ? mem6[k] : null), setItem: (k, v) => { mem6[k] = v; } },
        navigator: { userAgent: "node", doNotTrack: "0", sendBeacon: () => { sent.beacon += 1; return true; } },
        document: { visibilityState: "visible", addEventListener: (e, f) => { handlers[e] = f; } },
        addEventListener: (e, f) => { handlers[e] = f; },
        fetch: () => { sent.fetch += 1; return Promise.resolve({ ok: true, status: 200 }); },
      });
      load(w6);
      const leaver = w6.collector.init({ endpoint: "/v1/collect", writeKey: "wk_test" });
      for (let i = 0; i < 9; i++) leaver.track("refresh", { i });   // 임계치 10 직전
      handlers["pagehide"]();
      assert.strictEqual(sent.beacon, 1, "이탈 전송이 beacon 으로 나가지 않았다");
      assert.strictEqual(sent.fetch, 0, "이탈 시점인데 fetch 로 나갔다 — 페이지가 죽으면 취소된다");
      assert.strictEqual(leaver.stats().queued, 0, "이탈 배치가 큐를 비우지 않았다");

      // 7) endpoint 가 없으면(목업·데모) 네트워크를 건드리지 않는다.
      //    복원된 큐가 있으면 init 의 startup flush 가 곧바로 'null' 로 POST 를 쐈다.
      let nowhere = { fetch: 0, beacon: 0 };
      const mem7 = {};
      const w7 = Object.assign({}, win, {
        collector: undefined,
        localStorage: { getItem: (k) => (k in mem7 ? mem7[k] : null), setItem: (k, v) => { mem7[k] = v; } },
        navigator: { userAgent: "node", doNotTrack: "0", sendBeacon: () => { nowhere.beacon += 1; return true; } },
        fetch: () => { nowhere.fetch += 1; return Promise.resolve({ ok: true, status: 200 }); },
      });
      load(w7);
      const mock = w7.collector.init({ endpoint: null, writeKey: "wk_test" });
      for (let i = 0; i < 12; i++) mock.track("refresh", { i });   // 임계치를 넘겨 자동 flush
      mock.flush("pagehide");
      assert.strictEqual(nowhere.fetch, 0, "endpoint 가 없는데 fetch 로 나갔다");
      assert.strictEqual(nowhere.beacon, 0, "endpoint 가 없는데 beacon 으로 나갔다");
      assert.strictEqual(mock.stats().queued, 12, "보내지도 않고 큐에서 사라졌다");

      console.log("collector.js 자체 점검 통과");
    }, 10);
  }, 10);
}, 10);
