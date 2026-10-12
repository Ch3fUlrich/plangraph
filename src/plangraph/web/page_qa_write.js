/* PlanUI.QAWrite — the Q&A page's write mode (PG-ANSWER-UI).
 *
 * OFF unless the page names a keeper base URL — <meta name="pg-answer-api"
 * content="https://…"> or window.PLANGRAPH_ANSWER_API — and that URL is
 * https, or http for 127.0.0.1/localhost (dev). When OFF the page answers
 * locally exactly as before.
 *
 * When ON, an open question on an operator-owned item answers through the
 * keeper: GET {base}/v1/operator_csrf (lazily, and again after a 403) then
 * POST {base}/v1/operator_answer with the SSO session cookie (withCredentials,
 * never an Authorization header). A keeper answer is kept in memory for this
 * page session only; localStorage is not touched. DOM via
 * createElement/textContent: labels, consequences and error messages are text.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});
  if (typeof document === "undefined") return;

  var TEXT_CAP = 1000;
  var CSRF_HEADER = "X-PG-CSRF";
  var NOTE = "Answered via UI; the plan page updates at the next snapshot";

  var csrf = null;          // cached token: fetched before the first POST, dropped on a 403
  var uiAnswers = {};       // question id -> {text, at, ui: true}

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  function str(v) { return v === null || v === undefined ? "" : String(v); }
  function errText(e) { return str((e && (e.message || e.msg)) || e) || "request failed"; }

  /* ---- configuration ------------------------------------------------------- */
  /* The base URL is read on every call: the page can be re-rendered after the
   * host sets it, and a rejected value must leave the page in local mode. */
  function declaredBase() {
    var m = document.querySelector('meta[name="pg-answer-api"]');
    var u = m && m.getAttribute ? m.getAttribute("content") : null;
    return (u && String(u).trim()) || str(global.PLANGRAPH_ANSWER_API).trim();
  }

  /* An absolute https URL, or http only on the loopback dev hosts. */
  function validBase(u) {
    if (typeof u !== "string") return null;
    var s = u.trim().replace(/\/+$/, "");
    var m = /^(https?):\/\/([^/?]+)(\/[^?]*)?$/.exec(s);
    if (!m || /\s/.test(s) || s.indexOf("\\") >= 0) return null;
    var authority = m[2], path = m[3] || "";
    if (authority.indexOf("@") >= 0) return null;                 // no credentials in the URL
    var parts = authority.split(":"), host = parts.shift().toLowerCase(), port = parts.join(":");
    if (!/^[a-z0-9.\-_]+$/.test(host)) return null;
    if (port && !/^\d{1,5}$/.test(port)) return null;
    if (path && !/^\/[A-Za-z0-9._~:/#@!$&'()*+,;=%-]*$/.test(path)) return null;
    if (m[1].toLowerCase() === "https") return s;
    if (host === "127.0.0.1" || host === "localhost") return s;
    return null;
  }

  function base() { return validBase(declaredBase()); }
  function enabled() { return !!base(); }

  /* ---- the item's options --------------------------------------------------- */
  /* vm.items records carry no choices, so the snapshot item is the source
   * (views._choices / store_read.snapshot expose {options, default, deadline,
   * blocks}, as a dict or as the stored JSON text). */
  function rawItem(key) {
    var snap = global.PlanPage && global.PlanPage.state && global.PlanPage.state.snapshot;
    var list = snap && Array.isArray(snap.items) ? snap.items : [];
    for (var i = 0; i < list.length; i++) if (list[i] && list[i].key === key) return list[i];
    return null;
  }
  function parseChoices(v) {
    if (!v) return null;
    if (typeof v === "string") { try { v = JSON.parse(v); } catch (e) { return null; } }
    if (!v || typeof v !== "object" || !Array.isArray(v.options)) return null;
    var options = v.options.filter(function (o) { return o && typeof o.id === "string" && o.label !== undefined; });
    return { options: options, default: typeof v.default === "string" ? v.default : null,
             deadline: v.deadline === undefined ? null : str(v.deadline),
             blocks: v.blocks === undefined ? null : str(v.blocks) };
  }
  function choicesFor(vm, key) {
    var it = vm && vm.items && vm.items.get ? vm.items.get(key) : null;
    return parseChoices(it && it.choices) || parseChoices((rawItem(key) || {}).choices);
  }
  function operatorOwned(vm, key) {
    var it = vm && vm.items && vm.items.get ? vm.items.get(key) : null;
    if (it && it.operator) return true;
    var raw = rawItem(key);
    return !!raw && (raw.operator === true || raw.waits_on_operator === true);
  }
  /* The default option first (it is the one to focus), the rest in item order. */
  function ordered(ch) {
    var list = ch.options.slice(), at = -1;
    for (var i = 0; i < list.length; i++) if (list[i].id === ch.default) { at = i; break; }
    if (at > 0) list.unshift(list.splice(at, 1)[0]);
    return list;
  }

  /* ---- the two keeper calls --------------------------------------------------- */
  /* XMLHttpRequest, not the fetch API — the rendered plan page keeps its
   * no-fetch-call invariant (docs/KNOWLEDGE-UI.md, tests/test_render.py).
   * Session cookie only: withCredentials carries the Authelia SSO cookie and
   * no Authorization header is ever set. */
  function outcome(status, text) {
    var body = {};
    try { body = JSON.parse(text) || {}; } catch (e) { body = {}; }
    if (!body || typeof body !== "object") body = {};
    var ok = status >= 200 && status < 300;
    return { status: status, ok: ok, body: body,
             msg: typeof body.msg === "string" && body.msg ? body.msg
               : status === 0 ? "Could not reach the keeper."
                 : "keeper error " + (body.error ? str(body.error) : "") + " (HTTP " + status + ")" };
  }
  function call(method, url, headers, body) {
    var X = global.XMLHttpRequest;
    if (typeof X !== "function") return Promise.resolve(outcome(0, ""));
    return new Promise(function (resolve) {
      try {
        var x = new X();
        x.open(method, url);
        x.withCredentials = true;
        Object.keys(headers || {}).forEach(function (k) { x.setRequestHeader(k, headers[k]); });
        x.timeout = 15000;
        x.onload = x.onerror = x.ontimeout = x.onabort = function () { resolve(outcome(x.status, x.responseText)); };
        x.send(body === undefined ? null : body);
      } catch (e) { resolve(outcome(0, "")); }
    });
  }
  function getToken() {
    if (csrf) return Promise.resolve(csrf);
    return call("GET", base() + "/v1/operator_csrf", null).then(function (res) {
      var t = res.ok && typeof res.body.csrf === "string" ? res.body.csrf : "";
      csrf = t || null;
      return csrf;
    });
  }
  function post(body, token) {
    var headers = {};
    headers["Content-Type"] = "application/json";
    headers[CSRF_HEADER] = token;
    return call("POST", base() + "/v1/operator_answer", headers, JSON.stringify(body));
  }
  function request(body) {
    return getToken().then(function (t) {
      if (!t) return { ok: false, msg: "Could not get a CSRF token from the keeper." };
      return post(body, t).then(function (res) {
        if (res.status !== 403) return res;
        csrf = null;                                    // exactly one refetch + retry
        return getToken().then(function (t2) {
          if (!t2) return { ok: false, msg: "Could not get a fresh CSRF token from the keeper." };
          return post(body, t2);
        });
      });
    });
  }

  /* ---- the write-mode editor ---------------------------------------------------- */
  function editor(vm, q, hooks) {
    hooks = hooks || {};
    if (!enabled() || !q || q.status !== "open" || !q.item || !operatorOwned(vm, q.item)) return null;
    var ch = choicesFor(vm, q.item);
    var box = el("div", "qa-edit qa-edit-write");
    var note = el("div", "qa-err");
    var btns = [];
    var ta = el("textarea", "qa-ta");
    ta.rows = 3; ta.placeholder = "Your answer…";
    ta.setAttribute("aria-label", "Answer to " + q.id);
    ta.setAttribute("maxlength", String(TEXT_CAP));

    function disableAll() { btns.forEach(function (b) { b.disabled = true; }); }
    function enableAll() { btns.forEach(function (b) { b.disabled = false; }); }
    function stop(msg) { note.textContent = msg; enableAll(); }

    function send(body, label) {
      note.textContent = "";
      disableAll();
      request(body).then(function (res) {
        if (!res.ok) { stop(res.msg); return; }
        uiAnswers[q.id] = { text: label, at: new Date().toISOString(), ui: true };
        if (hooks.answered) hooks.answered();
      }, function (e) { stop(errText(e)); });
    }
    function sendChoice(o) {
      send({ key: q.item, choice_id: o.id }, str(o.label));
    }
    function sendText() {
      var text = str(ta.value).trim();
      if (!text) { note.textContent = "Type an answer first."; return; }
      if (text.length > TEXT_CAP) {
        note.textContent = "The keeper takes at most " + TEXT_CAP + " characters (this is " + text.length + ").";
        return;
      }
      send({ key: q.item, text: text }, text);
    }

    if (ch && ch.options.length) {
      ordered(ch).forEach(function (o) {
        var b = el("button", "qa-opt" + (o.id === ch.default ? " qa-opt-default" : ""), str(o.label));
        b.type = "button";
        b.setAttribute("data-choice", o.id);
        if (o.consequence) b.title = str(o.consequence);
        b.addEventListener("click", function () { sendChoice(o); });
        box.appendChild(b); btns.push(b);
      });
      var hint = [];
      if (ch.deadline) hint.push("Deadline " + ch.deadline);
      if (ch.blocks) hint.push("Blocks " + ch.blocks);
      if (hint.length) box.appendChild(el("div", "qa-opt-hint", hint.join(" — ")));
    }
    box.appendChild(ta);
    var textBtn = el("button", "tt-btn qa-send-text", "Answer with text");
    textBtn.type = "button";
    textBtn.addEventListener("click", sendText);
    box.appendChild(textBtn); btns.push(textBtn);

    var localBtn = el("button", "tt-btn qa-local", "Save as local answer (export only)");
    localBtn.type = "button";
    localBtn.addEventListener("click", function () {
      if (hooks.local) hooks.local(str(ta.value).trim());
    });
    box.appendChild(localBtn); btns.push(localBtn);

    var cancel = el("button", "tt-btn", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", function () { if (hooks.cancel) hooks.cancel(); });
    box.appendChild(cancel); btns.push(cancel);
    box.appendChild(note);
    return box;
  }

  PlanUI.QAWrite = {
    NOTE: NOTE, TEXT_CAP: TEXT_CAP,
    base: base, enabled: enabled, choicesFor: choicesFor, operatorOwned: operatorOwned,
    editor: editor,
    isAnswered: function (id) { return !!uiAnswers[id]; },
    answeredMap: function () { return uiAnswers; }
  };
  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
