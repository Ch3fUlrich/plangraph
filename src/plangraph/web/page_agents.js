(function (global) {
  "use strict";
  var UI = (global.PlanUI = global.PlanUI || {});
  var fields = global.PlanAgentsSchema.fields;
  var panel = document.getElementById("panel-agents"), hosts = document.getElementById("panel-hosts");
  if (panel && hosts && hosts.parentNode) hosts.parentNode.appendChild(panel);
  function el(tag, value) {
    var node = document.createElement(tag);
    if (value !== undefined) node.textContent = value;
    return node;
  }
  function render() {
    var box = document.getElementById("sb-panel-agents");
    if (!box) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    var PP = global.PlanPage, data = PP && PP.state && PP.state.snapshot && PP.state.snapshot.agents_view;
    var stamp = data && data.generated_at, age = Date.now() - Date.parse(stamp);
    box.appendChild(el("p", "Generated: " + (stamp || "unknown") +
      (!Number.isFinite(age) || age < 0 || age > 600000 ? " (stale/unknown)" : "")));
    var table = el("table"), head = el("tr"), body = el("tbody");
    fields.forEach(function (key) { head.appendChild(el("th", key === "age_s" ? "age" : key)); });
    table.appendChild(head);
    function row(agent, reason) {
      var tr = el("tr");
      if (reason) tr.title = reason;
      fields.forEach(function (key) {
        var value = agent[key];
        tr.appendChild(el("td", value === null || value === undefined || value === "" ? "unknown" : String(value)));
      });
      body.appendChild(tr);
    }
    if (!data || !Array.isArray(data.hosts) || !data.hosts.length) row({});
    else data.hosts.forEach(function (host) {
      if (!host || host.readable !== true || !Array.isArray(host.agents)) row({host: host && host.host}, host && host.reason);
      else host.agents.forEach(function (agent) { row(agent || {}); });
    });
    table.appendChild(body); box.appendChild(table);
  }
  UI.AgentsPage = {render: render};
})(typeof window !== "undefined" ? window : globalThis);
