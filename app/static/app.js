/* app.js — 화면 흐름.
 * 첫 화면은 검색창이 아니라 '무엇을 알고 싶은지'를 묻는다.
 */
(function (global) {
  "use strict";

  var INTENTS = {
    where_bus: {
      icon: "🚌",
      title: "버스가 지금 어디쯤인지 보고 싶어요",
      desc: "버스 노선도와 버스 위치를 보여줍니다.",
      next: "버스 번호로 찾기",
      mode: "route",
      placeholder: "버스 번호를 입력하세요 (예: 4312, 402, 강남06)",
      ask: "몇번 버스를 타시나요?",
      samples: ["4312", "402", "강남06", "160"]
    },
    what_comes: {
      icon: "📍",
      title: "이 정류장에 어떤 버스가 오는지 알고 싶어요",
      desc: "해당 정류장에 오는 버스를 알려줍니다.",
      next: "정류장 이름으로 찾기",
      mode: "station",
      placeholder: "정류장 이름을 입력하세요 (예: 대청역)",
      ask: "어느 정류장에 계신가요?",
      samples: ["대청역", "삼성역", "여의도", "종로3가"]
    },
    catch_now: {
      icon: "⚡",
      title: "지금 바로 탈 수 있는 버스를 알고 싶어요",
      desc: "5분 안에 도착하는 버스만 보여줍니다. 없는 경우도 있습니다.",
      next: "정류장 이름으로 찾기",
      mode: "station",
      soonOnly: true,
      placeholder: "정류장 이름을 입력하세요 (예: 대청역)",
      ask: "어느 정류장에서 타시나요?",
      samples: ["대청역", "삼성역", "여의도", "종로3가"]
    }
  };

  var state = {
    screen: "intro",
    intentKey: null,
    entryIntent: null,  // 세션에서 처음 고른 의도. 이후 의도가 바뀌어도 유지된다
    searchId: null,     // 검색 한 번의 식별자. query → click → view 를 잇는다
    query: "",
    route: null,
    dir: "up",          // "up" = 기점→회차지, "down" = 회차지→종점
    routeData: null,
    dataAge: 0,
    refreshTimer: null,
    refreshFn: null,
    refreshLeft: 0
  };

  var pane, input, searchRow, freshTxt, freshDot, logBody, logN, logFoot;

  /* ------------------------------------------------------------ 유틸 */
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function el(html) { var d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstElementChild; }
  function intent() { return INTENTS[state.intentKey] || null; }

  /* ------------------------------------------------------------ 로그 패널 */
  var logCount = 0;

  function logKind(name) {
    if (name.indexOf("intent") === 0) return "intent";
    if (name.indexOf("search.click") === 0 || name.indexOf("nav.click") === 0) return "click";
    if (name.indexOf("search") === 0) return "search";
    if (name.indexOf("api") === 0) return "api";
    return "view";
  }

  function paintLog(event) {
    logCount += 1;
    logN.textContent = logCount + "건";
    var skip = { event_id: 1, event_name: 1, event_ts: 1, session_id: 1, seq: 1, device: 1, screen_w: 1,
                 entry_intent: 1 };
    var kv = Object.keys(event).filter(function (k) { return !skip[k]; })
      .map(function (k) {
        return '<span class="kv">' + esc(k) + "=<i>" + esc(event[k]) + "</i></span>";
      }).join(" ");
    var zero = event.result_count === 0;
    var cls = zero ? "zero" : logKind(event.event_name);
    var node = el(
      '<div class="lg ev-' + cls + '">' +
        '<span class="ts">' + new Date(event.event_ts).toTimeString().slice(0, 8) + "</span> " +
        '<span class="ev-' + cls + '">' + esc(event.event_name) + "</span> " + kv +
      "</div>"
    );
    logBody.insertBefore(node, logBody.firstChild);
    while (logBody.children.length > 60) logBody.removeChild(logBody.lastChild);
    refreshLogFoot();
  }

  function refreshLogFoot() {
    var s = global.collector.stats();
    logFoot.innerHTML =
      "<span>대기 <b>" + s.queued + "</b> · 전송 <b>" + s.sent + "</b> · 실패 <b>" + s.failed + "</b></span>" +
      (global.BusAPI.mode === "http"
        ? "<span>batch → <b>/v1/collect</b></span>"
        : "<span>데모 — <b>전송하지 않음</b></span>");
  }

  /* ------------------------------------------------------------ 신선도 */
  // 서버의 data_age_sec 에서 시작한다. 0 부터 세면 90초 묵은 데이터가 '0초 전'이 된다.
  // null 은 '방금'이 아니라 '모름'이다 — 도착정보 API 는 수집 시각을 주지 않는다.
  // || 0 으로 뭉개면 모르는 값이 가장 신선한 값으로 둔갑한다.
  function resetAge(obs) {
    var age = obs ? obs.data_age_sec : undefined;
    state.dataAge = (age === null || age === undefined) ? null : age;
  }
  function ageText() { return state.dataAge === null ? "수집 시각 미제공" : state.dataAge + "초 전"; }
  function tickAge() {
    if (state.dataAge !== null) state.dataAge += 1;
    freshTxt.textContent = ageText();
    freshDot.classList.toggle("stale", state.dataAge !== null && state.dataAge > 60);
  }

  /* ------------------------------------------------------------ 검색바 */
  function showSearchBar(show) {
    searchRow.hidden = !show;
    if (show) {
      var i = intent();
      input.placeholder = i ? i.placeholder : "";
      input.value = state.query || "";
    }
  }

  /* ------------------------------------------------------------ 화면: 의도 선택 */
  function renderIntro() {
    state.screen = "intro";
    state.intentKey = null;
    state.route = null;
    stopRefresh();
    showSearchBar(false);

    var cards = Object.keys(INTENTS).map(function (key) {
      var it = INTENTS[key];
      return (
        '<button type="button" class="intent" data-intent="' + key + '">' +
          '<span class="ic">' + it.icon + "</span>" +
          '<span class="t">' + esc(it.title) + "</span>" +
          '<span class="d">' + esc(it.desc) + "</span>" +
          '<span class="n">' + esc(it.next) + " →</span>" +
        "</button>"
      );
    }).join("");

    pane.innerHTML =
      '<div class="intro">' +
        '<div class="head"><h2>Where Is My Bus?</h2>' +
        '<div class="intents">' + cards + "</div>" +
        '<p class="introfoot">user action은 <code>intent.select</code> 이벤트로 기록됩니다. '
      "</div>";

    pane.querySelectorAll(".intent").forEach(function (btn) {
      btn.addEventListener("click", function () { chooseIntent(btn.dataset.intent); });
    });
  }

  function chooseIntent(key) {
    state.intentKey = key;
    state.entryIntent = state.entryIntent || key;
    state.query = "";
    var it = INTENTS[key];
    // 진입 의도와 현재 의도를 나눠 싣는다. 한 값이면 화면 간 이동이 안 보인다.
    global.collector.setContext({ entry_intent: state.entryIntent, current_intent: key });
    global.collector.track("intent.select", { next_mode: it.mode });
    renderSearchPrompt();
  }

  /* ------------------------------------------------------------ 화면: 검색 */
  function crumbs(currentLabel) {
    var it = intent();
    return (
      '<div class="stepbar">' +
        '<button type="button" class="crumb" data-go="intro">처음</button>' +
        '<span class="sep">›</span>' +
        (it ? '<button type="button" class="crumb" data-go="search">' + esc(it.next) + "</button>" +
              '<span class="sep">›</span>' : "") +
        '<span class="now">' + esc(currentLabel) + "</span>" +
      "</div>"
    );
  }

  function wireCrumbs() {
    pane.querySelectorAll("[data-go]").forEach(function (b) {
      b.addEventListener("click", function () {
        if (b.dataset.go === "intro") renderIntro();
        else renderSearchPrompt();
      });
    });
  }

  function renderSearchPrompt() {
    state.screen = "search";
    stopRefresh();
    showSearchBar(true);
    var it = intent();
    // 데모 노선이 한정적이라 예시를 보여준다. 없으면 첫 화면이 '결과 없음'이 된다.
    var chips = (it.samples || []).map(function (v) {
      return '<button type="button" class="chip" data-fill="' + esc(v) + '">' + esc(v) + "</button>";
    }).join("");
    pane.innerHTML = crumbs("검색") +
      '<p class="askline"><span>' + esc(it.ask) + "</span></p>" +
      (chips ? '<div class="chiprow"><span class="chiplabel">이 데모에 담긴 ' +
               (it.mode === "route" ? "버스" : "정류장") + "</span>" + chips + "</div>" : "");
    wireCrumbs();
    pane.querySelectorAll("[data-fill]").forEach(function (b) {
      b.addEventListener("click", function () {
        input.value = b.dataset.fill;
        global.collector.track("sample.pick", { value: b.dataset.fill });
        doSearch();
      });
    });
    input.focus();
  }

  // result_count=0 만으로는 '결과 없음'과 'API 장애'가 구분되지 않아 장애를 사용자 행동으로 읽는다.
  function resultStatus(res, obs) {
    if (res.source === "cache_only") return "cache_only";
    if (res.source === "error" || (obs.status || 200) >= 400) return "api_error";
    return res.result_count ? "success" : "zero_result";
  }

  async function doSearch() {
    var it = intent();
    if (!it) { renderIntro(); return; }
    var q = input.value.trim();
    if (!q) return;
    if (state.searching) return;     // Enter 연타 · 버튼+Enter 동시 입력으로 두 번 들어오는 것을 막는다
    state.searching = true;
    state.query = q;
    // 검색 단위 식별자 — session_id 만으로는 어느 검색의 클릭인지 모른다.
    // 지역 변수로 잡는다. await 뒤에 state 를 읽으면 그 사이 두 번째 검색이 덮어써
    // 서로 다른 검색이 같은 search_id 로 기록된다(수집 로그에서 실제로 발생).
    var searchId = "sq-" + Math.random().toString(36).slice(2, 10);
    state.searchId = searchId;

    var res;
    try {
      res = it.mode === "route" ? await global.BusAPI.searchRoutes(q) : await global.BusAPI.searchStations(q);
    } catch (e) {
      pane.innerHTML = crumbs("오류") +
        '<div class="empty"><h4>검색에 실패했습니다</h4><p>' + esc(e.message) + "</p></div>";
      wireCrumbs();
      return;
    } finally {
      state.searching = false;
    }

    var obs = res.observability || {};
    global.collector.track("api.call", {
      endpoint: obs.endpoint, latency_ms: obs.latency_ms, status: obs.status,
      cache: obs.cache, error_code: obs.error
    });
    global.collector.track("search.query", {
      search_id: searchId, search_type: it.mode, query: q,
      result_count: res.result_count, result_status: resultStatus(res, obs),
      data_source: obs.mode === "mock" ? "mock" : (res.source || "cache"),
      latency_ms: obs.latency_ms
    });

    if (!res.result_count) { renderEmpty(q); return; }
    renderResults(q, res.items, it.mode, searchId);
  }

  function renderEmpty(q) {
    pane.innerHTML = crumbs("결과 없음") +
      '<div class="empty"><h4>‘' + esc(q) + "’ 검색 결과가 없습니다</h4>" +
      "<p>버스 번호나 정류장 이름을 다시 확인해 주세요. 이 데모에는 4312 · 402 · 강남06 · 160 " +
      "네 노선과 그 경유 정류장만 들어 있습니다.</p>" +
      '<p class="hint">방금 <code>result_count=0</code> 이벤트가 기록됐습니다 — 무결과율 지표의 재료입니다.</p></div>';
    wireCrumbs();
  }

  function renderResults(q, items, mode, searchId) {
    var rows = items.map(function (r, i) {
      if (mode === "route") {
        return '<button type="button" class="rescard" data-i="' + i + '">' +
          '<span class="no">' + esc(r.no) + "</span>" +
          '<span class="info"><span class="t">' + esc(r.from) + " ↔ " + esc(r.to) + "</span>" +
          '<span class="s">' + " "+ esc(r.type) + "버스</span></span></button>";
      }
      // 같은 이름이 방면별로 나뉜다(길 건너편은 다른 ARS). 방면을 안 보여주면
      // 똑같은 두 줄이 떠 반대편을 고르게 된다. 모르면 모른다고 적는다.
      var dirs = (r.directions || []);
      var others = dirs.filter(function (d) { return d && d !== r.name; });
      // 방면이 자기 자신이면 거기가 종점 — 도착한 사람에게 더 가라고 적을 수는 없다.
      var sub = others.length ? others.join(" · ") + " 방면"
                             : (dirs.length ? "종점" : "방면 정보 없음");
      return '<button type="button" class="rescard" data-i="' + i + '">' +
        '<span class="no" style="font-size:13px;min-width:56px">' + esc(r.ars) + "</span>" +
        '<span class="info"><span class="t">' + esc(r.name) + "</span>" + " " +
        '<span class="s">' + esc(sub) + "</span></span></button>";
    }).join("");

    pane.innerHTML = crumbs("검색 결과") +
      '<p style="font-size:13.5px;color:var(--muted);margin-bottom:10px">‘<b style="color:var(--ink)">' +
      esc(q) + '</b>’ 검색 결과 <b style="color:var(--ink)">' + items.length + "건</b></p>" +
      '<div class="results">' + rows + "</div>";
    wireCrumbs();

    pane.querySelectorAll(".rescard").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var i = parseInt(btn.dataset.i, 10);
        var item = items[i];
        // target_type/target_id 대신 이름 있는 ID — downstream 에 해석 규칙이 필요 없다.
        // 정류장은 ARS 로 식별한다. 이 시점에 화면이 아는 값이 ARS 뿐이라,
        // 이름만 station_id 로 붙이면 다른 체계가 같은 이름을 써 조인이 조용히 깨진다.
        var click = { search_id: searchId, search_type: mode, position: i + 1 };
        if (mode === "route") click.route_id = item.route_id;
        else click.ars = item.ars;
        global.collector.track("search.click", click);
        if (mode === "route") openRoute(item.route_id);
        else openStation(item.ars);
      });
    });
  }

  /* ------------------------------------------------------------ 화면: 노선도 */
  function busMark(b) {
    return '<span class="busMark' + (b.stopped ? "" : " moving") + '" title="' + esc(b.plain_no) + '">' +
      (b.stopped ? "▮" : "▸") + esc(b.plate || b.veh_id) + "</span>" +
      '<span class="cong c-' + esc(b.congestion) + '">' + esc(b.congestion) + "</span>" +
      (b.is_last ? '<span class="lastbadge">막차</span>' : "");
  }

  function stripSvg(stations, buses, route) {
    var n = stations.length;
    if (!n) return "";
    var pad = 46, gap = 26, y = 30;
    // sect_ord 는 노선 전체 기준, stations 는 방향별로 잘린 구간 — 빼지 않으면 하행 버스가 캔버스 밖에 그려진다.
    var base = stations[0].seq;
    var w = pad * 2 + (n - 1) * gap;
    var s = '<svg viewBox="0 0 ' + w + ' 56" width="' + w + '" height="56" role="img" aria-label="' +
      esc(route.no) + "번 노선 전체에서 운행 중인 버스 " + buses.length + '대의 위치">';
    s += '<line x1="' + pad + '" y1="' + y + '" x2="' + (w - pad) + '" y2="' + y +
         '" stroke="var(--bus-blue)" stroke-width="3"/>';
    for (var i = 0; i < n; i++) {
      var x = pad + i * gap, term = (i === 0 || i === n - 1);
      s += '<circle cx="' + x + '" cy="' + y + '" r="' + (term ? 5.5 : 3.5) +
           '" fill="var(--surface)" stroke="var(--bus-blue)" stroke-width="' + (term ? 3 : 2.5) + '"/>';
    }
    buses.forEach(function (b) {
      var x = pad + (b.sect_ord - base) * gap + (b.stopped ? 0 : gap / 2);
      s += '<polygon points="' + (x - 5) + "," + (y - 11) + " " + (x + 5) + "," + (y - 11) + " " +
           x + "," + (y - 3) + '" fill="var(--bus-blue)"/>';
      s += '<text x="' + x + '" y="' + (y - 14) + '" text-anchor="middle" font-family="JetBrains Mono, monospace" ' +
           'font-size="9" fill="var(--bus-blue)">' + esc(b.plate || "") + "</text>";
    });
    s += '<text x="' + pad + '" y="' + (y + 18) + '" text-anchor="middle" font-size="9.5" fill="var(--muted)">' +
         esc(stations[0].name) + "</text>";
    s += '<text x="' + (w - pad) + '" y="' + (y + 18) + '" text-anchor="middle" font-size="9.5" fill="var(--muted)">' +
         esc(stations[n - 1].name) + "</text>";
    return s + "</svg>";
  }

  /* --------------------------------------------- 방향(상·하행) 분리
   * API 는 seq 1..N 을 한 줄로 주고 회차지(turn_seq)만 표시한다 — 방향은 여기서 잘라 만든다.
   * 회차지가 없는 노선(편도·순환)은 토글을 감춘다. 없는 방향을 지어내지 않는다.
   */
  function splitByDirection(data, dir) {
    var t = data.turn_seq;
    if (!t) return { stations: data.stations, buses: data.buses, split: false };
    var lo = dir === "down" ? t + 1 : 1;
    var hi = dir === "down" ? data.stations.length : t;
    return {
      split: true,
      stations: data.stations.filter(function (s) { return s.seq >= lo && s.seq <= hi; }),
      buses: data.buses.filter(function (b) { return b.sect_ord >= lo && b.sect_ord <= hi; })
    };
  }

  async function openRoute(routeId, isRefresh) {
    var data;
    try {
      data = await global.BusAPI.routeDetail(routeId);
    } catch (e) {
      pane.innerHTML = crumbs("오류") + '<div class="empty"><h4>노선을 불러오지 못했습니다</h4><p>' +
        esc(e.message) + "</p></div>";
      wireCrumbs();
      return;
    }

    state.screen = "route";
    state.route = data.route;
    state.routeData = data;
    resetAge(data.observability);
    showSearchBar(false);

    var obs = data.observability || {};
    global.collector.track("api.call", {
      endpoint: obs.endpoint, latency_ms: obs.latency_ms, status: obs.status,
      cache: obs.cache, data_age_sec: obs.data_age_sec, error_code: obs.error
    });
    global.collector.track(isRefresh ? "refresh" : "route.view", {
      route_id: data.route.route_id, stop_count: data.stations.length,
      running_bus_count: data.running_count, search_id: state.searchId || undefined,
      trigger: isRefresh ? "auto" : "click"
    });

    renderRoute(routeId, data);
    startRefresh(function () { openRoute(routeId, true); });
  }

  function renderRoute(routeId, data) {
    var r = data.route;
    var view = splitByDirection(data, state.dir);
    var byOrd = {};
    view.buses.forEach(function (b) {
      (byOrd[b.sect_ord] = byOrd[b.sect_ord] || []).push(b);
    });

    var rows = view.stations.map(function (s, i) {
      var here = (byOrd[s.seq] || []).map(busMark).join("");
      var term = (i === 0 || i === view.stations.length - 1);
      return '<button type="button" class="stop' + (term ? " terminal" : "") + '" data-ars="' + esc(s.ars) + '">' +
        '<span class="ord">' + String(s.seq).padStart(2, "0") + "</span>" +
        '<span class="rail"><span class="node"></span></span>' +
        '<span class="sname">' + esc(s.name) + '<span class="sars">' + esc(s.ars) + "</span></span>" +
        '<span class="right">' + here + "</span></button>";
    }).join("");

    pane.innerHTML = crumbs(r.no + "번 버스") +
      '<div class="rtHead"><div>' +
        '<div style="display:flex;gap:9px;align-items:center;flex-wrap:wrap">' +
          '<span class="rtNo">' + esc(r.no) + "</span>" +
          '<span class="btype bt-' + esc(r.type) + '">' + esc(r.type) + "</span></div>" +
        '<div class="rtMeta"><span><b>' + esc(r.from) + "</b> ↔ <b>" + esc(r.to) + "</b></span>" +
          (r.first ? "<span>첫차 " + esc(r.first) + " · 막차 " + esc(r.last) + "</span>" : "") +
          (r.interval ? "<span>배차 " + esc(r.interval) + "분</span>" : "") +
          "<span>정류장 " + view.stations.length + "개</span>" +
          "<span>운행 중 <b>" + view.buses.length + "대</b></span></div></div>" +
        (view.split
          ? '<div class="dirtog">' +
              '<button type="button" data-dir="up" aria-pressed="' + (state.dir === "up") + '">' +
                esc(r.to) + " 방면</button>" +
              '<button type="button" data-dir="down" aria-pressed="' + (state.dir === "down") + '">' +
                esc(r.from) + " 방면</button></div>"
          : '<div class="dirtog"><span class="oneway">편도 · 회차 구간 없음</span></div>') +
        "</div>" +
      '<div class="stripWrap"><div class="stripTitle"><h4>' +
        (view.split ? (state.dir === "up" ? esc(r.to) : esc(r.from)) + " 방면" : "전체 노선") + "</h4><span>" +
        view.stations.length + "개 정류장 · 버스 " + view.buses.length + "대</span></div>" +
        '<div class="strip">' + stripSvg(view.stations, view.buses, r) + "</div></div>" +
      '<div class="listWrap">' + rows + "</div>";

    wireCrumbs();
    pane.querySelectorAll("[data-dir]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (state.dir === btn.dataset.dir) return;
        state.dir = btn.dataset.dir;
        // 방향 전환은 화면에서만 — 같은 응답을 다시 그릴 뿐 외부 호출이 없다.
        global.collector.track("ui.toggle", {
          control: "direction", value: state.dir, route_id: routeId
        });
        renderRoute(routeId, state.routeData);
      });
    });
    pane.querySelectorAll(".stop").forEach(function (btn) {
      btn.addEventListener("click", function () {
        // 검색 결과 클릭이 아니다 — search.click 으로 세면 퍼널이 부푼다.
        global.collector.track("nav.click", {
          from: "route_map", ars: btn.dataset.ars, route_id: routeId
        });
        // 검색의 결과가 아니므로 비운다. 안 그러면 뒤따르는 station.view 가 직전 search_id 를 달고 나간다.
        state.searchId = null;
        openStation(btn.dataset.ars);
      });
    });
  }

  /* ------------------------------------------------------------ 화면: 정류장 */
  async function openStation(ars, isRefresh) {
    var it = intent();
    var soonOnly = !!(it && it.soonOnly);
    var data;
    try {
      data = await global.BusAPI.stationDetail(ars, soonOnly);
    } catch (e) {
      pane.innerHTML = crumbs("오류") + '<div class="empty"><h4>정류장을 불러오지 못했습니다</h4><p>' +
        esc(e.message) + "</p></div>";
      wireCrumbs();
      return;
    }

    state.screen = "station";
    resetAge(data.observability);
    showSearchBar(false);

    var obs = data.observability || {};
    global.collector.track("api.call", {
      endpoint: obs.endpoint, latency_ms: obs.latency_ms, status: obs.status,
      cache: obs.cache, data_age_sec: obs.data_age_sec, error_code: obs.error
    });
    global.collector.track(isRefresh ? "refresh" : "station.view", {
      // 둘 다 남긴다 — ars 는 클릭 이벤트와, station_id 는 마스터와 잇는 키.
      station_id: data.station.station_id, ars: data.station.ars, route_count: data.route_count,
      arriving_count: data.arriving_count, soon_only: soonOnly,
      search_id: state.searchId || undefined, trigger: isRefresh ? "auto" : "click"
    });

    // 도착 메시지는 '3분12초후[2번째 전]' 형태. 남은 정류장은 아래 줄에 쓰므로 괄호를 뗀다.
    function etaMain(msg) { return String(msg || "").replace(/\s*\[[^\]]*\]\s*$/, ""); }

    // 상태 배지 — 시간이 맞아도 만차·우회면 헛걸음이다.
    // 도착정보 API 는 혼잡도 코드 대신 재차인원(명)을 준다. 사람 수라서 '여유/보통'으로 바꾸지 않는다.
    function flags(a) {
      var out = [];
      if (a.is_full) out.push('<span class="flag f-warn">만차</span>');
      if (a.is_detour) out.push('<span class="flag f-warn">우회</span>');
      if (a.is_last) out.push('<span class="flag f-last">막차</span>');
      if (a.low_floor) out.push('<span class="flag f-low">저상</span>');
      if (a.riders) out.push('<span class="flag f-num">재차 ' + a.riders + "명</span>");
      return out.join("") || '<span class="flag f-none">—</span>';
    }

    var rows = data.arrivals.map(function (a) {
      var soon = a.sec1 >= 0 && a.sec1 <= 300;
      return '<tr class="' + (soon ? "soon" : "") + '">' +
        '<td class="rno"><span class="btype bt-' + esc(a.type) + '" style="font-size:10px;margin-right:6px">' +
          esc(a.type) + "</span>" + esc(a.no) + "</td>" +
        "<td>" + esc(a.to) + (a.to ? " 방면" : "") + "</td>" +
        '<td class="eta"><span class="big">' + esc(etaMain(a.eta1)) + "</span>" +
          '<span class="sub">' + (a.stops1 ? a.stops1 + "번째 전" : "전 정류장 출발") + "</span></td>" +
        '<td class="eta"><span>' + esc(etaMain(a.eta2)) + "</span></td>" +
        "<td>" + flags(a) + "</td></tr>";
    }).join("");

    var body = rows || '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:26px">' +
      (soonOnly ? "5분 안에 도착하는 버스가 없습니다." : "도착 예정 정보가 없습니다. 운행 시간을 확인해 주세요.") + "</td></tr>";

    pane.innerHTML = crumbs(data.station.name) +
      '<div class="stHead"><div class="nm">' + esc(data.station.name) + "</div>" +
        '<div class="mt"><span> ARS <span class="mono">' + esc(data.station.ars) + "</span></span>" +
        "<span>경유 노선 <b>" + data.route_count + "개</b></span>" +
        (soonOnly ? '<span style="color:var(--accent-ink)">5분 내 도착만 표시</span>' : "") +
        "</div></div>" +
      '<div class="tw"><table><thead><tr><th>노선</th><th>방면</th><th>첫 번째 도착</th>' +
        "<th>두 번째</th><th>상태</th></tr></thead><tbody>" + body + "</tbody></table></div>" +
      '<p class="note">도착 예정은 <span class="mono">' + esc(ageText()) + "</span> 기준입니다. " +
      esc(data.coverage_note || "") + "</p>";

    wireCrumbs();
    startRefresh(function () { openStation(ars, true); });
  }

  /* ------------------------------------------------------------ 자동 갱신 */
  // 자동 갱신은 '보고 있는 동안'만 돈다.
  //  · 탭이 숨으면 정지 — 안 보는 화면이 일 1,000회 예산을 태운다.
  //  · 연속 20회(10분)면 정지 — 방치된 탭의 refresh 가 실제 행동 이벤트를 덮어쓴다.
  var REFRESH_MS = 30000, REFRESH_MAX = 20;

  function startRefresh(fn) {
    stopRefresh();
    state.refreshFn = fn;
    state.refreshLeft = REFRESH_MAX;
    state.refreshTimer = global.setInterval(function () {
      if (state.refreshLeft-- <= 0) { stopRefresh(); return; }
      fn();
    }, REFRESH_MS);
  }
  function stopRefresh() {
    if (state.refreshTimer) { global.clearInterval(state.refreshTimer); state.refreshTimer = null; }
  }

  function wireRefreshVisibility() {
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") {
        stopRefresh();
      } else if (state.refreshFn && (state.screen === "route" || state.screen === "station")) {
        startRefresh(state.refreshFn);   // 돌아오면 다시 센다
      }
    });
  }

  /* ------------------------------------------------------------ 부팅 */
  function boot() {
    pane = document.getElementById("pane");
    input = document.getElementById("q");
    searchRow = document.getElementById("searchRow");
    freshTxt = document.getElementById("freshTxt");
    freshDot = document.getElementById("freshDot");
    logBody = document.getElementById("logBody");
    logN = document.getElementById("logN");
    logFoot = document.getElementById("logFoot");

    global.collector.init({
      endpoint: (global.BusAPI.mode === "http") ? "/v1/collect" : null,
      service: "seoul-bus-web",
      writeKey: "wk_seoul_bus_demo"
    }).onEvent(paintLog);

    // 목업 모드에는 보낼 서버가 없다. endpoint 가 null 이면 collector 가 알아서
    // 네트워크를 건드리지 않는다 — 여기서 flush 를 덮으면 init 의 startup flush 를 놓친다.

    document.getElementById("goBtn").addEventListener("click", doSearch);
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") doSearch(); });
    document.getElementById("homeBtn").addEventListener("click", renderIntro);
    document.getElementById("clrBtn").addEventListener("click", function () {
      logBody.innerHTML = ""; logCount = 0; logN.textContent = "0건";
    });

    var badge = document.getElementById("modeBadge");
    if (global.BusAPI.health) {
      global.BusAPI.health().then(function (h) {
        badge.textContent = h.mode === "live" ? "실데이터" : "목업 모드";
        badge.classList.toggle("live", h.mode === "live");
      }).catch(function () { badge.textContent = "서버 없음"; });
    } else {
      badge.textContent = "목업 모드";
    }

    wireRefreshVisibility();
    global.setInterval(tickAge, 1000);
    renderIntro();
    refreshLogFoot();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})(window);
