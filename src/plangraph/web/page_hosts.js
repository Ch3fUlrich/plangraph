/* Snapshot-only FleetSummary view. Collection and authoritative counts belong upstream. */
(function (global) {
  "use strict";
  var PlanUI = (global.PlanUI = global.PlanUI || {});
  var METRICS = ["cpu_percent", "ram_available_bytes", "gpu_percent",
    "network_rx_bytes_per_s", "network_tx_bytes_per_s", "disk_free_bytes"];
  var CAPS = ["max_runs", "max_writers", "max_seats", "max_sessions", "mem_floor_mb"];
  function object(v) { return v !== null && typeof v === "object" && !Array.isArray(v); }
  function number(v) { return typeof v === "number" && Number.isFinite(v) && v >= 0 ? String(v) : "unknown"; }
  function text(v) { return typeof v === "string" && v.trim() ? v : "unknown"; }
  function el(tag, value) {
    var n = document.createElement(tag);
    if (value !== undefined) n.textContent = value;
    return n;
  }
  function render() {
    if (typeof document === "undefined") return;
    var box = document.getElementById("sb-panel-hosts");
    if (!box) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    var PP = global.PlanPage, fleet = PP && PP.state && PP.state.snapshot && PP.state.snapshot.fleet;
    if (!object(fleet) || !Array.isArray(fleet.hosts)) {
      box.appendChild(el("p", "Host fleet unavailable in this snapshot.")); return;
    }
    box.appendChild(el("p", "Fleet running agents: " + number(fleet.running_agents)));
    var table = el("table"), head = el("thead"), titles = el("tr"), body = el("tbody");
    table.appendChild(el("caption", "Fleet hosts"));
    ["Host id", "Class", "Caps", "CPU %", "Available RAM bytes", "GPU %",
      "Network RX bytes/s", "Network TX bytes/s", "Free disk bytes", "Running agents"].forEach(function (h) {
      var th = el("th", h); th.setAttribute("scope", "col"); titles.appendChild(th);
    });
    head.appendChild(titles); table.appendChild(head);
    fleet.hosts.slice().sort(function (a, b) {
      var x = text(object(a) ? a.host_id : null), y = text(object(b) ? b.host_id : null);
      return x < y ? -1 : x > y ? 1 : 0;
    }).forEach(function (input) {
      var r = object(input) ? input : {}, sample = object(r.sample) ? r.sample : {};
      var caps = object(r.caps) ? CAPS.map(function (k) { return k + "=" + number(r.caps[k]); }).join(", ") : "unknown";
      var values = [text(r.host_id), text(r.class), caps];
      METRICS.forEach(function (k) { values.push(number(sample[k])); });
      values.push(number(r.running_agents));
      var row = el("tr");
      values.forEach(function (v) { row.appendChild(el("td", v)); });
      body.appendChild(row);
    });
    table.appendChild(body); box.appendChild(table);
  }
  PlanUI.HostsPage = { render: render };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
