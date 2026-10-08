/* PlanUI table URL codec — URL state serialization (GU-TB1 / C12).
 * Pure query string encoder, decoder, and merger for table state.
 * ES5/ES2015 without modules: attaches to PlanUI and exports for node.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  function enc(v) {
    return encodeURIComponent(String(v == null ? "" : v))
      .replace(/%3A/gi, ":")
      .replace(/%7C/gi, "|");
  }

  function safeDec(str) {
    try {
      return decodeURIComponent(str);
    } catch (e) {
      return str;
    }
  }

  function tableUrlEncode(st) {
    st = PlanUI.tableNormalize(st);
    var def = PlanUI.tableDefaults();
    var parts = [];

    // Sort
    var isDefSort = st.sort.length === 1 && st.sort[0].col === "priority" && st.sort[0].dir === "desc";
    if (!isDefSort && st.sort.length) {
      parts.push("t_sort=" + st.sort.map(function (s) {
        return enc(s.col) + ":" + enc(s.dir);
      }).join(","));
    }

    // Cols
    var isDefCols = st.cols.length === def.cols.length && st.cols.every(function (c, i) {
      return c === def.cols[i];
    });
    if (!isDefCols && st.cols.length) {
      parts.push("t_cols=" + st.cols.map(enc).join(","));
    }

    // Group
    if (st.group !== "project>phase") {
      parts.push("t_group=" + enc(st.group));
    }

    // Collapsed
    if (st.collapsed.length) {
      parts.push("t_collapsed=" + st.collapsed.map(enc).join(","));
    }

    // Filter facets
    var facets = ["project", "phase", "state", "owner", "priority", "kind"];
    facets.forEach(function (f) {
      var arr = st.filters[f];
      if (arr && arr.length) {
        parts.push("t_" + f + "=" + arr.map(enc).join(","));
      }
    });

    if (st.filters.op) {
      parts.push("t_op=1");
    }

    if (st.search) {
      parts.push("t_q=" + enc(st.search));
    }

    return parts.join("&");
  }

  function tableUrlDecode(str) {
    var raw = (str == null ? "" : String(str)).trim();
    if (raw.charAt(0) === "?" || raw.charAt(0) === "#") raw = raw.slice(1);

    var st = PlanUI.tableDefaults();
    if (!raw) return PlanUI.tableNormalize(st);

    var pairs = raw.split("&");
    for (var i = 0; i < pairs.length; i++) {
      var pair = pairs[i];
      if (!pair) continue;
      var eq = pair.indexOf("=");
      var key = eq >= 0 ? pair.slice(0, eq) : pair;
      var val = eq >= 0 ? pair.slice(eq + 1) : "";

      if (key === "t_sort") {
        var sortItems = val.split(",").filter(Boolean);
        st.sort = sortItems.map(function (item) {
          var ci = item.indexOf(":");
          var col = ci >= 0 ? safeDec(item.slice(0, ci)) : safeDec(item);
          var dir = ci >= 0 ? safeDec(item.slice(ci + 1)) : "asc";
          return { col: col, dir: dir };
        });
      } else if (key === "t_cols") {
        st.cols = val.split(",").filter(Boolean).map(safeDec);
      } else if (key === "t_group") {
        st.group = safeDec(val);
      } else if (key === "t_collapsed") {
        st.collapsed = val.split(",").map(safeDec);
      } else if (key === "t_op") {
        st.filters.op = (val === "1" || val === "true");
      } else if (key === "t_q") {
        st.search = safeDec(val);
      } else if (key.indexOf("t_") === 0) {
        var facet = key.slice(2);
        if (facet === "priority") {
          st.filters.priority = val.split(",").filter(Boolean).map(function (v) {
            return Number(safeDec(v)) || 0;
          });
        } else if (st.filters[facet] !== undefined) {
          st.filters[facet] = val.split(",").map(safeDec);
        }
      }
    }

    return PlanUI.tableNormalize(st);
  }

  function tableUrlMerge(search, st) {
    var raw = (search == null ? "" : String(search)).trim();
    if (raw.charAt(0) === "?" || raw.charAt(0) === "#") raw = raw.slice(1);

    var kept = [];
    if (raw) {
      var pairs = raw.split("&");
      for (var i = 0; i < pairs.length; i++) {
        var p = pairs[i];
        if (!p) continue;
        if (p.indexOf("t_") !== 0) kept.push(p);
      }
    }

    var encSt = tableUrlEncode(st);
    if (encSt) kept.push(encSt);

    if (!kept.length) return "";
    return "?" + kept.join("&");
  }

  PlanUI.tableUrlEncode = tableUrlEncode;
  PlanUI.tableUrlDecode = tableUrlDecode;
  PlanUI.tableUrlMerge = tableUrlMerge;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = PlanUI;
  }
})(typeof window !== "undefined" ? window : globalThis);
