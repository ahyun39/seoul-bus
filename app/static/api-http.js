/* api-http.js — 백엔드 호출 데이터 계층.
 * app.js 는 이 인터페이스만 안다 → 출처를 목업 ↔ 실서버로 바꿔도 화면 코드는 그대로.
 */
(function (global) {
  "use strict";

  async function get(path) {
    const res = await fetch(path, { headers: { Accept: "application/json" } });
    if (!res.ok) {
      const detail = await res.json().catch(function () { return {}; });
      const err = new Error(detail.error || ("HTTP " + res.status));
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  global.BusAPI = {
    mode: "http",
    health: function () { return get("/api/health"); },
    searchRoutes: function (q) { return get("/api/routes/search?q=" + encodeURIComponent(q)); },
    routeDetail: function (routeId) { return get("/api/routes/" + encodeURIComponent(routeId)); },
    searchStations: function (q) { return get("/api/stations/search?q=" + encodeURIComponent(q)); },
    stationDetail: function (ars, soonOnly) {
      return get("/api/stations/" + encodeURIComponent(ars) + (soonOnly ? "?soon_only=true" : ""));
    }
  };
})(window);
