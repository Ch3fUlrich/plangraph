/* plangraph page — UI-VERSION-STAMP header line (task B / V-SHELL-4).
 *
 * Pure helpers that turn (build sha, snapshot.generated, head_seq, keeper
 * head) into the #meta-line text, tooltip and stale state. No DOM in format;
 * buildSha reads the <meta name="plangraph-build"> tag render.py emits.
 * ES5, one namespace `PlanUI`, no modules (same idiom as page_model.js). */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  var DOT = "\u00b7"; // ·

  /* sha: build hash from the meta tag, "" / null -> "unknown".
   * generated: snapshot ISO timestamp; missing/unparseable -> never stale.
   * headSeq: printed as-is, "-" when absent. headHash: keeper head for the
   * tooltip, first 10 chars. nowMs: view-time clock (Date.now() default). */
  function format(sha, generated, headSeq, headHash, nowMs) {
    var code = sha ? String(sha) : "unknown";
    var data = generated ? String(generated) : "-";
    var seq = (headSeq === undefined || headSeq === null || headSeq === "")
      ? "-" : String(headSeq);
    var text = "code " + code + " " + DOT + " data " + data + " " + DOT + " seq " + seq;
    var ageMin = null;
    var stale = false;
    if (generated) {
      var t = Date.parse(String(generated));
      if (!isNaN(t)) {
        var now = typeof nowMs === "number" ? nowMs : Date.now();
        ageMin = Math.floor((now - t) / 60000);
        if (ageMin < 0) ageMin = 0;
        if (ageMin >= 15) {
          stale = true;
          text += " " + DOT + " age " + (ageMin >= 60
            ? Math.floor(ageMin / 60) + "h " + (ageMin % 60) + "m"
            : ageMin + " min");
        }
      } else {
        ageMin = null;
      }
    }
    var title = headHash ? "keeper head " + String(headHash).slice(0, 10) : "";
    return { text: text, title: title, stale: stale, ageMin: ageMin };
  }

  /* Content of the render.py build tag; "unknown" when absent or headless. */
  function buildSha(doc) {
    var d = doc || global.document;
    if (!d || typeof d.querySelector !== "function") return "unknown";
    var m = null;
    try { m = d.querySelector('meta[name="plangraph-build"]'); } catch (e) { m = null; }
    return (m && m.content) || "unknown";
  }

  /* Re-render every 60 s so a page left open turns amber on its own.
   * Guarded for non-browser environments; unref'd so node harnesses exit. */
  function startClockRefresh(render) {
    if (typeof document === "undefined" || typeof setInterval === "undefined") return null;
    var iv = setInterval(function () {
      var fn = render || (global.PlanPage && global.PlanPage.renderHeader);
      if (fn) fn();
    }, 60000);
    if (iv && typeof iv.unref === "function") iv.unref();
    return iv;
  }

  startClockRefresh();

  PlanUI.Stamp = { format: format, buildSha: buildSha, startClockRefresh: startClockRefresh };

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
