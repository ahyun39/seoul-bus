/* api-mock.js — 서버 없이 화면만 돌려보기 위한 데이터 계층.
 *
 * api-http.js 자리에 끼우면 백엔드 없이 전체 흐름이 돈다(단일 HTML 데모 · GitHub Pages).
 * 서버의 app/mock.py 와 같은 모양, 같은 데이터를 만든다.
 *
 * 정적 마스터는 window.MOCK_DATA(실데이터에서 추출)가 있으면 그쪽을 우선 쓰고,
 * 버스 위치·도착 시각은 매번 합성한다 — 굳히면 어제 위치를 지금처럼 보여주게 된다.
 * 노선 4312/402/강남06/160 의 종류와 기·종점만 실제를 참고했고 나머지는 합성이다.
 */
(function (global) {
  "use strict";

  var ROUTES = [
    { route_id:"104900034", no:"4312", type:"지선",
      from:"개포동(구룡마을)", to:"삼성역",
      first:"0400", last:"2300", interval:"9", company:"예시여객",
      stops:["구룡마을","개포자이","구룡역","도곡역","한티역","대치역",
             "대청역","학여울역","삼성중앙역","삼성역"] },
    { route_id:"100100032", no:"402", type:"간선",
      from:"장지공영차고지", to:"서울역",
      first:"0400", last:"2250", interval:"10", company:"예시운수",
      stops:["장지공영차고지","가락시장역","삼성역","강남구청역","신사역","한남대교북단",
             "순천향대병원","남산1호터널","을지로입구","종로3가역","서울역"] },
    { route_id:"121000016", no:"강남06", type:"마을",
      from:"세곡동(세곡푸르지오)", to:"대청역",
      first:"0550", last:"2330", interval:"11", company:"예시교통",
      stops:["세곡푸르지오","세곡사거리","자곡동","수서역","일원역",
             "대모산입구역","개포동역","대청역"] },
    { route_id:"100100016", no:"160", type:"간선",
      from:"도봉산역광역환승센터", to:"온수역",
      first:"0410", last:"2240", interval:"8", company:"예시교통운수",
      stops:["도봉산역광역환승센터","쌍문역","미아사거리역","신설동역","동대문",
             "종로3가역","종각역","서울시청","충정로역","마포역","여의도역",
             "영등포시장","온수역"] }
  ];

  // 실데이터 픽스처. export_mock.py 가 만들고 build_demo.py 가 데모에 심는다.
  var REAL = {};
  ((global.MOCK_DATA && global.MOCK_DATA.routes) || []).forEach(function (r) {
    if (r.stations && r.stations.length) REAL[r.no] = r;
  });
  ROUTES.forEach(function (r) {
    var real = REAL[r.no];
    if (!real) return;
    ["route_id","type","from","to","first","last","interval","company"].forEach(function (k) { r[k] = real[k]; });
    r.real = true;
  });

  // 합성 노선에만 번호를 지어낸다 — 실데이터는 진짜 ARS 를 쓴다.
  var STOP_IDS = {};
  (function () {
    var i = 0;
    ROUTES.forEach(function (r) {
      if (r.real) return;
      r.stops.forEach(function (n) {
        if (!STOP_IDS[n]) { STOP_IDS[n] = { station_id: "1180" + (23001 + i), ars: String(23001 + i) }; i++; }
      });
    });
  })();

  var BY_ID = {};
  ROUTES.forEach(function (r) { BY_ID[r.route_id] = r; });

  // 시작 시 캐시에 들어 있는 노선. 402 만 빼서 '캐시에 없는 번호' 경로를 보여준다.
  // 서버의 mock.SEEDED 와 같아야 한다 — 어긋나면 (cache/live) 표시가 데모와 서버에서 달라진다.
  var CACHED = {};
  ROUTES.filter(function (r) { return r.no === "4312" || r.no === "강남06" || r.no === "160"; })
        .forEach(function (r) { CACHED[r.route_id] = true; });

  function pub(r) {
    return { route_id:r.route_id, no:r.no, type:r.type, from:r.from, to:r.to,
             first:r.first, last:r.last, interval:r.interval, company:r.company };
  }

  function rank(items, q) {
    return items.slice().sort(function (a, b) {
      if ((a.no === q) !== (b.no === q)) return a.no === q ? -1 : 1;
      var ap = a.no.indexOf(q) === 0, bp = b.no.indexOf(q) === 0;
      if (ap !== bp) return ap ? -1 : 1;
      return a.no.length - b.no.length || a.no.localeCompare(b.no);
    });
  }

  var SEED = {
    "104900034": [[3,true],[8,false],[14,false]],   // 4312
    "100100032": [[4,true],[9,false],[15,true]],    // 402
    "121000016": [[2,false],[6,true]],              // 강남06
    "100100016": [[5,true],[11,false],[17,true]]    // 160
  };

  // 실제 API 는 기점→회차지→종점을 한 seq 로 준다. 편도만 주면 방향 분리 코드가 데모에서 안 돈다.
  function stationsOf(routeId) {
    var route = BY_ID[routeId];
    if (!route) return [];

    var real = REAL[route.no];
    if (real) {
      // 실데이터는 이미 한 seq 로 들어 있다 — 합성할 것이 없다.
      return real.stations.map(function (s) {
        return { seq:s.seq, station_id:s.station_id, ars:s.ars, name:s.name,
                 direction:s.direction, is_turn:s.is_turn };
      });
    }

    var names = route.stops.concat(route.stops.slice().reverse().slice(1));
    var turn = route.stops.length;
    return names.map(function (n, i) {
      return { seq:i+1, station_id: STOP_IDS[n].station_id, ars: STOP_IDS[n].ars,
               name:n, direction: i < turn ? route.to : route.from,
               is_turn: i + 1 === turn };
    });
  }

  function turnSeqOf(routeId) {
    var st = stationsOf(routeId);
    for (var i = 0; i < st.length; i++) if (st[i].is_turn) return st[i].seq;
    return null;
  }

  // ARS 하나가 정류장 하나. 이름으로 묶으면 길 건너편이 같은 정류장이 된다.
  var STATIONS = [];
  (function () {
    var seen = {};
    ROUTES.forEach(function (r) {
      stationsOf(r.route_id).forEach(function (s) {
        if (seen[s.ars]) return;
        seen[s.ars] = true;
        STATIONS.push({ station_id:s.station_id, ars:s.ars, name:s.name });
      });
    });
  })();

  // 그 정류장을 지나는 노선들이 알려주는 방면.
  function directionsAt(ars) {
    var out = [];
    ROUTES.forEach(function (r) {
      stationsOf(r.route_id).forEach(function (s) {
        if (s.ars === ars && s.direction && out.indexOf(s.direction) < 0) out.push(s.direction);
      });
    });
    return out;
  }

  function routesAt(ars) {
    return ROUTES.filter(function (r) {
      return stationsOf(r.route_id).some(function (s) { return s.ars === ars; });
    });
  }

  function kstStamp(secondsAgo) {
    var d = new Date(Date.now() + 9 * 3600e3 - secondsAgo * 1000);
    var p = function (v, n) { return String(v).padStart(n || 2, "0"); };
    return d.getUTCFullYear() + p(d.getUTCMonth()+1) + p(d.getUTCDate()) +
           p(d.getUTCHours()) + p(d.getUTCMinutes()) + p(d.getUTCSeconds());
  }

  function busesOf(routeId) {
    var stops = stationsOf(routeId).length || 1;
    // 실데이터는 왕복 80정류장이 넘어, 고정 시드를 쓰면 버스가 전부 기점에 몰린다.
    var seed = SEED[routeId];
    if (!seed) {
      var n = Math.max(2, Math.min(12, Math.floor(stops / 14))), gap = Math.max(1, Math.floor(stops / n));
      seed = [];
      for (var k = 0; k < n; k++) seed.push([1 + k * gap, k % 3 === 0]);
    }
    var step = Math.floor(Date.now() / 25000);
    var stamp = kstStamp(step % 40);
    return seed.map(function (s, idx) {
      var half = step + idx * 3;
      return {
        veh_id: "mock-" + routeId.slice(-3) + "-" + idx,
        plain_no: "서울70사" + (1234 + idx * 607),
        plate: String(1234 + idx * 607),
        sect_ord: ((s[0] + Math.floor(half / 2) - 1) % stops) + 1,
        stopped: half % 2 === 0 ? s[1] : !s[1],
        congestion: ["여유","보통","혼잡"][(step + idx) % 3],
        is_last: false,
        data_tm: stamp
      };
    });
  }

  function seededRandom(seed) {
    var x = Math.sin(seed) * 10000;
    return x - Math.floor(x);
  }

  function arrivalsOf(ars) {
    var base = parseInt(ars, 10) || 23001;
    var bucket = Math.floor(Date.now() / 30000);
    var picks = routesAt(ars);
    if (!picks.length) picks = ROUTES.slice(0, 2);
    return picks.map(function (r, i) {
      var s1 = Math.floor(seededRandom(base + bucket + i) * 900);
      var s2 = s1 + 240 + Math.floor(seededRandom(base + bucket + i + 99) * 660);
      var st1 = Math.max(0, Math.floor(s1/150));
      var rnd = function (k) { return seededRandom(base + bucket + i + k); };
      return {
        route_id:r.route_id, no:r.no, type:r.type, to:r.to,
        // 화면이 원문 메시지를 그대로 보여주므로 실제 API 와 문장 모양을 맞춘다.
        eta1: s1 < 60 ? "곧 도착" : Math.floor(s1/60) + "분" + (s1%60) + "초후[" + st1 + "번째 전]",
        eta2: Math.floor(s2/60) + "분" + (s2%60) + "초후[" + Math.max(0, Math.floor(s2/150)) + "번째 전]",
        sec1:s1, sec2:s2, stops1: st1,
        riders: rnd(11) < 0.4 ? null : Math.floor(rnd(12) * 30) + 1,
        is_full: rnd(13) < 0.12,
        is_detour: rnd(14) < 0.06,
        is_last: rnd(15) < 0.05,
        low_floor: rnd(16) < 0.4,
        // 실 API(getStationByUid)는 수집 시각을 주지 않는다. 목업이 채우면
        // 데모만 '몇 초 전'을 보여주고 실데이터는 '미제공'이라 화면이 갈린다.
        data_tm: ""
      };
    }).sort(function (a, b) { return a.sec1 - b.sec1; });
  }

  function obs(endpoint, t0, ageSec) {
    // ageSec 을 생략하면 0(가장 신선함)이 아니라 null(모름)이다.
    return { endpoint:endpoint, latency_ms: Math.round(performance.now() - t0),
             cache:"mock", status:200,
             data_age_sec: (ageSec === undefined || ageSec === null) ? null : ageSec };
  }

  function delay(v) { return new Promise(function (r) { setTimeout(function () { r(v); }, 60 + Math.random() * 120); }); }

  global.BusAPI = {
    mode: "mock",
    searchRoutes: function (q) {
      // 캐시 우선 → 없으면 실시간 후 캐시에 채운다(서버와 같은 규칙).
      var t0 = performance.now();
      var cached = ROUTES.filter(function (r) { return CACHED[r.route_id] && r.no.indexOf(q) > -1; });
      var exact = cached.some(function (r) { return r.no === q; });
      if (cached.length && exact) {
        return delay({ query:q, items:rank(cached.map(pub), q), result_count:cached.length, source:"cache",
                       observability:obs("cache:routes", t0) });
      }
      var live = ROUTES.filter(function (r) { return r.no.indexOf(q) > -1; });
      live.forEach(function (r) { CACHED[r.route_id] = true; });
      var merged = {}, all = cached.concat(live);
      all.forEach(function (r) { merged[r.route_id] = pub(r); });
      var items = Object.keys(merged).map(function (k) { return merged[k]; });
      return delay({ query:q, items:rank(items, q), result_count:items.length, source:"live",
                     observability:obs("노선번호목록조회", t0) });
    },
    routeDetail: function (routeId) {
      var t0 = performance.now();
      var route = BY_ID[routeId];
      if (!route) return Promise.reject(new Error("route_not_found"));
      var stations = stationsOf(routeId), buses = busesOf(routeId);
      return delay({ route:pub(route), stations:stations, buses:buses, running_count:buses.length,
                     turn_seq: turnSeqOf(routeId),
                     observability:obs("getBusPosByRtid", t0, Math.floor(Date.now()/25000) % 40) });
    },
    searchStations: function (q) {
      var t0 = performance.now(), items = [];
      STATIONS.forEach(function (s) {
        if (s.name.indexOf(q) > -1) {
          items.push({ station_id:s.station_id, ars:s.ars, name:s.name, directions: directionsAt(s.ars) });
        }
      });
      items = items.slice(0, 15);
      return delay({ query:q, items:items, result_count:items.length, observability:obs("cache:stations", t0) });
    },
    stationDetail: function (ars, soonOnly) {
      var t0 = performance.now();
      var name = null, through = [];
      ROUTES.forEach(function (r) {
        var first = stationsOf(r.route_id).filter(function (s) { return s.ars === ars; })[0];
        if (first) { name = first.name; through.push({ route_id:r.route_id, seq:first.seq, no:r.no, type:r.type, to:r.to }); }
      });
      if (!name) return Promise.reject(new Error("station_not_found"));
      var arr = arrivalsOf(ars);
      var shown = soonOnly ? arr.filter(function (a) { return a.sec1 <= 300; }) : arr;
      return delay({
        station:{ station_id:(STATIONS.filter(function (s) { return s.ars === ars; })[0] || {}).station_id || ars,
                ars:ars, name:name },
        routes:through, arrivals:shown, route_count:through.length,
        arriving_count: arr.filter(function (a) { return a.sec1 <= 300; }).length,
        coverage_note:"합성 데이터입니다. 실제 운행 정보가 아닙니다.",
        observability:obs("getStationByUid", t0, null)   // 수집 시각 미제공
      });
    }
  };
})(window);
