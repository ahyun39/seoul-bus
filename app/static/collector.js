/* collector.js — 행동 로그 수집 SDK
 *
 *  - track() 은 큐에만 넣는다. 즉시 전송하면 요청이 폭발한다.
 *  - event_id 는 클라이언트가 만든다 → 재시도해도 서버가 멱등하게 흡수.
 *  - 큐는 localStorage. 탭을 닫거나 오프라인이어도 남아 '지연 도착'이 실제로 생긴다.
 *  - 이탈 전송은 sendBeacon(beforeunload 의 fetch 는 폐기된다). 본문 64KiB 상한.
 *  - 버린 건수(dropped)를 다음 전송에 실어 보낸다 — 안 보내면 서버가 영원히 모른다.
 */
(function (global) {
  "use strict";

  var QUEUE_KEY = "bus_clq_v3";      // {queue, dropped} 를 함께 저장한다. 포맷이 바뀌어 키를 올렸다.
  var SESSION_KEY = "bus_sess_v2";   // v1 은 seq 를 갖지 않는다. 이어받으면 seq 가 겹친다.
  var SDK_VERSION = "0.1.0";
  var SCHEMA_VERSION = "1.3.0";   // 서버와 맞춰야 하는 이벤트 스키마 버전 (docs/event-schema.md)

  var MAX_BATCH = 50;          // 한 요청에 담을 이벤트 수
  var MAX_BODY_BYTES = 60000;  // sendBeacon 64KiB 한계보다 낮게
  var MAX_QUEUE = 200;         // 큐 상한. 넘으면 오래된 것부터 버린다
  var FLUSH_EVERY_MS = 5000;
  var FLUSH_AT_COUNT = 10;
  // 세션: 마지막 이벤트로부터 30분 무활동이면 새 세션.
  // 탭/브라우저 종료로는 안 끊기고(localStorage), 총 길이 상한도 없다.
  var SESSION_IDLE_MS = 30 * 60 * 1000;

  function safeGet(key) { try { return global.localStorage.getItem(key); } catch (e) { return null; } }
  function safeSet(key, value) { try { global.localStorage.setItem(key, value); } catch (e) { /* 무시 */ } }

  function uuid() {
    try {
      if (global.crypto && global.crypto.randomUUID) return global.crypto.randomUUID();
    } catch (e) { /* 폴백으로 내려간다 */ }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      return (c === "x" ? r : ((r & 0x3) | 0x8)).toString(16);
    });
  }

  function iso(d) { return new Date(d).toISOString(); }

  // 봉투 필드 — payload 로 덮을 수 없다.
  // 덮이면 dedup·유실률·세션 분석이 전부 조용히 틀어진다.
  var RESERVED = ["event_id", "event_name", "event_ts", "session_id", "seq",
                  "event_type", "device", "screen_w"];

  var RETRY_BASE_MS = 5000;          // 실패 후 재시도 간격의 시작값
  var RETRY_MAX_MS = 60000;

  function Collector() {
    this.opts = null;
    this.context = {};
    this.queue = [];
    this.inFlight = false;           // 응답을 기다리는 중인가
    this.retryAt = 0;                // 이 시각 전에는 다시 보내지 않는다 (백오프)
    this.fails = 0;
    this.seq = 0;
    this.dropped = 0;
    this.sent = 0;
    this.failed = 0;
    this.rejected = 0;             // 서버가 영구 거부해 버린 건수
    this.timer = null;
    this.listeners = [];
  }

  Collector.prototype.init = function (opts) {
    this.opts = Object.assign({
      endpoint: "/v1/collect",
      service: "seoul-bus-web",
      writeKey: "",
      respectDnt: true
    }, opts || {});

    // 추적 거부 신호(DNT/GPC) 존중.
    if (this.opts.respectDnt && (global.navigator.doNotTrack === "1" || global.navigator.globalPrivacyControl)) {
      this.disabled = true;
      return this;
    }

    var stored = safeGet(QUEUE_KEY);
    if (stored) {
      try {
        var saved = JSON.parse(stored) || {};
        this.queue = saved.queue || [];
        // dropped 도 복구한다. 메모리에만 두면 탭을 닫는 순간 유실 카운터 자체가 유실된다.
        this.dropped = saved.dropped || 0;
      } catch (e) { this.queue = []; }
    }

    var self = this;
    this.timer = global.setInterval(function () { self.flush("interval"); }, FLUSH_EVERY_MS);

    // 탭 숨김 = 모바일에서 가장 신뢰할 수 있는 이탈 신호.
    // 떠나기 직전 page.leave 를 남겨야 '마지막 클릭 이후 들여다본 시간'이 살아남는다.
    this.openedAt = Date.now();
    global.document.addEventListener("visibilitychange", function () {
      if (global.document.visibilityState === "hidden") {
        self._leave("hidden");
        self.flush("hidden");
      } else {
        // 복귀하면 다음 이탈도 기록한다. 첫 이탈만 남기면 체류 시간이 짧게 잡힌다.
        self.left = false;
      }
    });
    global.addEventListener("pagehide", function () { self._leave("pagehide"); self.flush("pagehide"); });

    // 온라인 복귀 시 밀린 이벤트부터 밀어낸다 → 실제 지연 도착 발생
    if (this.queue.length) this.flush("startup");
    return this;
  };

  // 세션과 seq 는 수명이 같아 함께 저장한다.
  // seq 를 인스턴스에 두면 새로고침마다 1 로 되감겨 (session_id, seq) 가 유일하지 않게 된다.
  Collector.prototype._session = function () {
    var now = Date.now();
    var raw = safeGet(SESSION_KEY);
    var s = null;
    if (raw) { try { s = JSON.parse(raw); } catch (e) { s = null; } }
    if (!s || !s.id || (now - s.at) > SESSION_IDLE_MS) {
      s = { id: "s-" + uuid().slice(0, 12), at: now, seq: 0 };
    }
    return s;
  };

  Collector.prototype.sessionId = function () { return this._session().id; };

  // 이벤트마다 세션 시각과 seq 를 함께 전진시킨다.
  // ponytail: 다중 탭에서 seq 가 겹칠 수 있다(localStorage 에 원자적 증가가 없다).
  // 중복 판정은 event_id 가 하고 seq 는 gap 탐지용 근사치로 쓴다.
  Collector.prototype._bump = function () {
    var s = this._session();
    s.at = Date.now();
    s.seq = (s.seq || 0) + 1;
    safeSet(SESSION_KEY, JSON.stringify(s));
    return s;
  };

  // 이탈 이벤트는 한 번만. visibilitychange 와 pagehide 가 연달아 와 두 건이 되기 쉽다.
  Collector.prototype._leave = function (reason) {
    if (this.left) return;
    this.left = true;
    // 이 구간의 자동 flush 차단. 없으면 page.leave 가 큐 임계치를 건드려 track() 안에서
    // flush("count") 가 먼저 돌고, 이탈 전송이 인플라이트 가드에 막혀 beacon 이 아닌
    // fetch 로 나간다 — 페이지가 죽으면서 취소된다.
    this.leaving = true;
    try {
      this.track("page.leave", { reason: reason, visible_ms: Date.now() - (this.openedAt || Date.now()) });
    } finally {
      this.leaving = false;
    }
  };

  Collector.prototype._save = function () {
    safeSet(QUEUE_KEY, JSON.stringify({ queue: this.queue, dropped: this.dropped }));
  };

  Collector.prototype.onEvent = function (fn) { this.listeners.push(fn); return this; };

  // 모든 이벤트에 따라붙는 공통 문맥(entry_intent/current_intent 등).
  Collector.prototype.setContext = function (ctx) {
    Object.assign(this.context, ctx || {});
    return this;
  };

  Collector.prototype.track = function (name, payload) {
    if (this.disabled || !this.opts) return null;
    var session = this._bump();
    this.seq = session.seq;              // stats() 표시용 사본
    var nav = global.navigator || {};
    // 예약 필드는 병합 전에 떨어뜨린다. 봉투를 나중에 덮는 것만으로는
    // event_type 처럼 서버가 채우는 필드를 막지 못한다.
    var props = {};
    Object.keys(payload || {}).forEach(function (k) {
      if (RESERVED.indexOf(k) < 0) props[k] = payload[k];
      else if (global.console) global.console.warn("collector: payload." + k + " 는 예약 필드라 무시됩니다");
    });
    var event = Object.assign(props, this.context, {
      event_id: uuid(),
      event_name: name,
      event_ts: iso(Date.now()),
      session_id: session.id,
      seq: session.seq,
      device: /Mobi|Android|iPhone/i.test(nav.userAgent || "") ? "mobile" : "pc",
      screen_w: (global.screen && global.screen.width) || null
    });

    this.queue.push(event);
    if (this.queue.length > MAX_QUEUE) {
      // 큐 상한 초과 — 최신이 더 가치 있으므로 오래된 것부터 버리고 센다.
      this.dropped += this.queue.length - MAX_QUEUE;
      this.queue = this.queue.slice(-MAX_QUEUE);
    }
    this._save();

    for (var i = 0; i < this.listeners.length; i++) {
      try { this.listeners[i](event); } catch (e) { /* 화면 표시 실패가 수집을 막지 않는다 */ }
    }
    if (!this.leaving && this.queue.length >= FLUSH_AT_COUNT) this.flush("count");
    return event;
  };

  function bodySize(batch) { return new Blob([JSON.stringify(batch)]).size; }

  Collector.prototype._take = function () {
    var batch = this.queue.slice(0, MAX_BATCH);
    // sendBeacon 64KiB 제한에 맞춰 앞에서부터 줄인다
    while (batch.length > 1 && bodySize(batch) > MAX_BODY_BYTES) {
      batch = batch.slice(0, Math.floor(batch.length / 2));
    }
    // 단건이 상한을 넘으면 영원히 못 보낸다. 큐 앞에 남아 뒤를 전부 막으므로(HOL) 버린다.
    if (batch.length === 1 && bodySize(batch) > MAX_BODY_BYTES) {
      this.queue.shift();
      this.dropped += 1;
      this._save();
      return [];
    }
    return batch;
  };

  Collector.prototype.flush = function (trigger) {
    if (this.disabled || !this.opts || !this.queue.length) return;
    // 보낼 곳이 없으면(목업·데모) 네트워크를 건드리지 않는다. 큐는 그대로 둔다 —
    // 여기서 _take() 까지 갔다가 돌아오면 이벤트가 어디에도 없이 사라진다.
    if (!this.opts.endpoint) return;
    // 인플라이트 가드 — flush 는 인터벌·큐 임계치·탭 숨김·이탈 네 곳에서 불린다.
    // 중복 전송도 문제지만, 두 응답이 각각 큐를 잘라내면 미전송 이벤트까지 사라진다.
    if (this.inFlight) return;
    // 실패 후 백오프 — 서버 장애 때 모든 브라우저가 5초마다 때리지 않게.
    // 이탈 시점만 예외: 여기서 못 보내면 재방문까지 밀리고, 안 돌아오면 큐 상한에 밀려 버려진다.
    var leaving = (trigger === "hidden" || trigger === "pagehide");
    if (!leaving && this.retryAt && Date.now() < this.retryAt) return;

    var batch = this._take();
    if (!batch.length) return;                 // 거대 이벤트를 버린 경우

    var body = JSON.stringify({
      write_key: this.opts.writeKey,
      service: this.opts.service,
      sdk_version: SDK_VERSION,
      schema_version: SCHEMA_VERSION,
      sent_ts: iso(Date.now()),
      dropped_count: this.dropped,
      trigger: trigger,
      // 몇 번째 시도인지. 없으면 재시도 끝의 성공과 한 번에 된 성공이 서버에서 구분되지 않는다.
      attempt: this.fails + 1,
      events: batch
    });

    var self = this;
    var taken = batch.length;
    var sentIds = {};
    for (var i = 0; i < batch.length; i++) sentIds[batch[i].event_id] = 1;

    this.inFlight = true;

    function done() { self.inFlight = false; }

    function ok() {
      // 위치가 아니라 event_id 로 지운다. 전송 중 큐 앞이 잘리면 '앞에서 taken 개'가 어긋난다.
      self.queue = self.queue.filter(function (e) { return !sentIds[e.event_id]; });
      self.sent += taken;
      self.dropped = 0;
      self.fails = 0;
      self.retryAt = 0;
      self._save();
    }

    function backoff() {
      self.failed += 1;
      self.fails += 1;
      self.retryAt = Date.now() + Math.min(RETRY_MAX_MS, RETRY_BASE_MS * Math.pow(2, self.fails - 1));
    }

    // 이탈 시점은 sendBeacon 만 신뢰할 수 있다. 반환값은 '서버 수신'이 아니라
    // '브라우저 전송 큐 등재' 여부 — false 를 성공으로 치면 이벤트가 조용히 사라진다.
    if (leaving && global.navigator.sendBeacon) {
      try {
        if (global.navigator.sendBeacon(self.opts.endpoint, new Blob([body], { type: "application/json" }))) {
          ok();
        }
        done();
        return;
      } catch (e) { done(); /* 아래 fetch 로 폴백 */ }
    }

    global.fetch(self.opts.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body,
      keepalive: true
    }).then(function (res) {
      if (res.ok) { ok(); done(); return; }
      // 재시도해도 같은 4xx 는 버린다(408·429 제외).
      // 구분이 없으면 413 이나 401 한 건이 큐를 영원히 막는다.
      if (res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429) {
        ok();
        self.dropped += taken;      // 조용히 사라지지 않게 센다 (ok() 가 0 으로 되돌린 뒤)
        self.rejected += taken;
        self._save();
        done();
        return;
      }
      backoff();
      done();
    }).catch(function () {
      backoff();                    // 네트워크 실패 — 큐에 남겨 다음 기회에 다시
      done();
    });
  };

  Collector.prototype.stats = function () {
    return { queued: this.queue.length, sent: this.sent, dropped: this.dropped,
             rejected: this.rejected, failed: this.failed, seq: this.seq,
             retryInMs: this.retryAt ? Math.max(0, this.retryAt - Date.now()) : 0 };
  };

  global.collector = new Collector();
})(window);
