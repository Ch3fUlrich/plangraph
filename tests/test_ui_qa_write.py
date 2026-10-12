"""PG-ANSWER-UI W3 — node-run tests for the Q&A write mode (PlanUI.QAWrite).

Write mode is OFF unless the page carries a keeper base URL
(`<meta name="pg-answer-api">` or window.PLANGRAPH_ANSWER_API) and that URL is
https (or http://127.0.0.1 / http://localhost for dev).  When ON, an open
question on an operator-owned item offers one button per option plus a
free-text box; clicking does GET {base}/v1/operator_csrf then
POST {base}/v1/operator_answer with credentials include + X-PG-CSRF, retries
exactly once with a fresh token after a 403, and never touches localStorage.

Hermetic: the DOM is the same node-run stub as tests/test_ui_qa_page.py and the
keeper transport (XMLHttpRequest — the page keeps its documented "no fetch("
invariant) is stubbed inside the harness, so nothing touches the network.
Skips when node is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
WEB = SRC / "plangraph" / "web"

sys.path.insert(0, str(SRC))

need_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

BASE = "https://keeper.example"
DEV_BASE = "http://127.0.0.1:8731"
TOKEN = "csrf-token-1"
KEY_ANSWERS = "plangraph.qa.answers.v1"

CHOICES = {
    "options": [
        {"id": "opt-a", "label": "Use option A", "consequence": "Unblocks P1"},
        {"id": "opt-b", "label": "<img src=x onerror=alert(1)>",
         "consequence": "Injects <b>markup</b> here"},
        {"id": "opt-c", "label": "Option C", "consequence": "Keeps P2 blocked"},
    ],
    "default": "opt-c", "deadline": "2026-11-01", "blocks": "P1 rollout",
}


def _item(qid: str, **kw) -> dict:
    it = {"project": "atlas", "id": qid, "key": "atlas:" + qid, "title": "Q: " + qid + " stage?",
          "kind": "question", "track": "P0", "phase": "P0", "owner": "operator",
          "state": "open", "operator": True, "priority": 3, "depends_on": [],
          "estimate_h": 1, "evidence_kinds": ["decision"], "evidence": [],
          "review_tier": "standard", "choices": None}
    it.update(kw)
    return it


def snapshot() -> dict:
    return {"generated": "2026-10-12T00:00:00Z", "head": {"sha": "abc"},
            "items": [
                _item("Q-1", choices=CHOICES),
                _item("Q-2", choices=None),
                _item("Q-3", operator=False, choices=CHOICES),
                _item("Q-4", state="done", answer={"text": "already known"}, choices=CHOICES),
            ],
            "questions": None, "decisions": []}


HARNESS = r"""
"use strict";
var fs = require("fs");
var WEB = process.argv[2];
var SNAPSHOT = process.argv[3];
var SCENARIO = process.argv[4];
var CFG = process.argv[5];          // "none" | a URL | "meta:<url>"
function emit(o) { process.stdout.write(JSON.stringify(o)); }
function after(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

var ids = {};
function El(tag) {
  this.nodeType = 1;
  this.tagName = String(tag).toUpperCase();
  this.childNodes = [];
  this.parentNode = null;
  this.attributes = {};
  this.listeners = {};
  this.style = { setProperty: function () {}, getPropertyValue: function () { return ""; } };
  this.className = "";
  this.hidden = false;
  this.disabled = false;
  this.value = "";
}
Object.defineProperty(El.prototype, "firstChild", {
  get: function () { return this.childNodes.length ? this.childNodes[0] : null; }
});
Object.defineProperty(El.prototype, "textContent", {
  get: function () {
    var t = this._text || "";
    for (var i = 0; i < this.childNodes.length; i++) t += this.childNodes[i].textContent || "";
    return t;
  },
  set: function (v) { this._text = String(v); this.childNodes = []; }
});
El.prototype.appendChild = function (c) { c.parentNode = this; this.childNodes.push(c); return c; };
El.prototype.removeChild = function (c) {
  var i = this.childNodes.indexOf(c);
  if (i >= 0) { this.childNodes.splice(i, 1); c.parentNode = null; }
  walk(c, []).concat([c]).forEach(function (n) {
    if (n.attributes && n.attributes.id && ids[n.attributes.id] === n) delete ids[n.attributes.id];
  });
  return c;
};
El.prototype.setAttribute = function (n, v) {
  this.attributes[n] = String(v);
  if (n === "id") ids[String(v)] = this;
  if (n === "class") this.className = String(v);
};
El.prototype.getAttribute = function (n) { return (n in this.attributes) ? this.attributes[n] : null; };
El.prototype.removeAttribute = function (n) { delete this.attributes[n]; };
El.prototype.addEventListener = function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.click = function () {
  var f = this.listeners.click || [];
  for (var i = 0; i < f.length; i++) f[i]({ type: "click", preventDefault: function () {} });
};
El.prototype.closest = function () { return null; };
El.prototype.querySelectorAll = function (sel) { return qsa(this, sel); };
El.prototype.querySelector = function (sel) { return qsa(this, sel)[0] || null; };
El.prototype.getBoundingClientRect = function () {
  return { left: 0, top: 0, bottom: 0, right: 0, width: 0, height: 0 };
};
El.prototype.classList = { add: function () {}, remove: function () {},
  toggle: function () {}, contains: function () { return false; } };
Object.defineProperty(El.prototype, "id", {
  get: function () { return this.attributes.id || ""; },
  set: function (v) { this.setAttribute("id", String(v)); }
});

function tokenize(sel) {
  var out = [], i = 0, buf = "";
  while (i < sel.length) {
    var c = sel.charAt(i);
    if (c === "[") { var j = sel.indexOf("]", i); buf += sel.slice(i, j + 1); i = j + 1; }
    else if (c === " ") { if (buf) { out.push(buf); buf = ""; } i++; }
    else { buf += c; i++; }
  }
  if (buf) out.push(buf);
  return out;
}
function matches(node, part) {
  if (!node || node.nodeType !== 1) return false;
  var re = /\[([\w-]+)=['"]([^'"]*)['"]\]/g, m, rest = part;
  while ((m = re.exec(part))) {
    if (node.getAttribute(m[1]) !== m[2]) return false;
    rest = rest.replace(m[0], "");
  }
  var cls = rest.match(/\.[\w-]+/g) || [];
  var tag = rest.replace(/\.[\w-]+/g, "").replace(/#[\w-]+/g, "");
  var idm = part.match(/#([\w-]+)/);
  if (idm && node.getAttribute("id") !== idm[1]) return false;
  if (tag && tag !== "*") {
    if (node.tagName !== tag.toUpperCase()
        && node.tagName.toLowerCase() !== String(tag).toLowerCase()) return false;
  }
  var own = (node.className || "").split(/\s+/);
  for (var i = 0; i < cls.length; i++) if (own.indexOf(cls[i].slice(1)) < 0) return false;
  return true;
}
function walk(node, out) {
  if (!node || !node.childNodes) return out;
  node.childNodes.forEach(function (c) { out.push(c); walk(c, out); });
  return out;
}
function ancestorOk(node, parts, k) {
  for (var a = node.parentNode; k >= 0; k--, a = (a ? a.parentNode : null)) {
    while (a && !matches(a, parts[k])) a = a.parentNode;
    if (!a) return false;
  }
  return true;
}
function qsa(root, sel) {
  var parts = tokenize(sel), last = parts[parts.length - 1], hits = [];
  walk(root, []).forEach(function (n) {
    if (last && matches(n, last) && ancestorOk(n, parts, parts.length - 2)) hits.push(n);
  });
  return hits;
}

var htmlEl = new El("html");
var head = add(htmlEl, "head", null, [], null);
var body = new El("body");
htmlEl.appendChild(body);
var doc = {
  nodeType: 9, readyState: "complete", body: body, documentElement: htmlEl, head: head,
  createElement: function (t) { return new El(t); },
  createTextNode: function (t) { var n = new El("#text"); n.nodeType = 3; n.textContent = String(t); return n; },
  getElementById: function (id) { return ids[id] || null; },
  querySelectorAll: function (sel) { return qsa(htmlEl, sel); },
  querySelector: function (sel) { return qsa(htmlEl, sel)[0] || null; },
  addEventListener: function () {}, removeEventListener: function () {}
};
var win = { innerWidth: 1024, innerHeight: 768, addEventListener: function () {} };
win.document = doc;
global.window = win;
global.document = doc;
var store = {};
global.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); },
  removeItem: function (k) { delete store[k]; }
};
win.navigator = { clipboard: { writeText: function () {} } };

function add(parent, tag, cls, attrs, text) {
  var n = new El(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  (attrs || []).forEach(function (p) { n.setAttribute(p[0], p[1]); });
  parent.appendChild(n);
  return n;
}

var sidebar = add(body, "aside", "sb", [["id", "sidebar"]]);
add(sidebar, "button", "sb-close", [["id", "sb-close"]]);
var tablist = add(sidebar, "div", "sb-tabs");
["needs", "qa", "answers", "settings"].forEach(function (t) {
  var b = add(tablist, "button", "sb-tab", [["data-tab", t], ["role", "tab"]]);
  var c = add(b, "span", "sb-count", [["id", "sb-count-" + t], ["hidden", ""]]);
  c.hidden = true;
});
var sbBody = add(sidebar, "div", "sb-body");
["needs", "qa", "answers", "settings"].forEach(function (t) {
  add(sbBody, "section", "sb-panel", [["id", "sb-panel-" + t], ["data-panel", t]]);
});
var tabbar = add(body, "nav", "tabs", [["id", "tabbar"]]);
add(tabbar, "span", "tab-badge", [["id", "inbox-badge"]]);

var SNAP = JSON.parse(fs.readFileSync(SNAPSHOT, "utf8"));
global.PlanPage = {
  state: { selected: null, projectsOff: {}, tracksOff: {}, snapshot: SNAP },
  $: function (id) { return doc.getElementById(id); },
  el: function (tag, cls, text) {
    var n = new El(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  },
  clear: function (n) { while (n.firstChild) n.removeChild(n.firstChild); },
  selectNode: function (k) { this.state.selected = k; },
  focusNode: function (k) { this.state.selected = k; },
  visibleItems: function () { return []; },
  setTab: function () {}, renderGraph: function () {}
};

/* ---- keeper transport stub: XMLHttpRequest, the page's "no fetch(" invariant --- */
var MODE = "ok";                 // "ok" | "403-once" | "bad-request" | "csrf-down"
var calls = [], refused403 = false;
function respond(url, body) {
  function out(status, obj) { return { status: status, text: JSON.stringify(obj) }; }
  if (url.indexOf("/v1/operator_csrf") >= 0) {
    if (MODE === "csrf-down") return out(500, { error: "config", msg: "no csrf key configured" });
    return out(200, { csrf: "__TOKEN__" });
  }
  if (url.indexOf("/v1/operator_answer") >= 0) {
    if (MODE === "403-once" && !refused403) {
      refused403 = true;
      return out(403, { error: "csrf", msg: "csrf token expired" });
    }
    if (MODE === "bad-request") return out(400, { error: "validation", msg: "no such option for this item" });
    var sent = JSON.parse(body || "{}");
    return out(200, { key: sent.key, state: "done", choice_id: sent.choice_id || null, source: "ui" });
  }
  return out(404, { error: "not_found", msg: "no such door" });
}
function FakeXHR() { this.headers = {}; this.status = 0; this.responseText = ""; }
FakeXHR.prototype.open = function (m, u) { this.method = String(m); this.url = String(u); };
FakeXHR.prototype.setRequestHeader = function (k, v) { this.headers[k] = String(v); };
FakeXHR.prototype.send = function (body) {
  var self = this;
  calls.push({ url: self.url, method: self.method,
               credentials: self.withCredentials === true ? "include" : "omit",
               headers: JSON.parse(JSON.stringify(self.headers)),
               body: body === undefined || body === null ? null : String(body) });
  var r = respond(self.url, body);
  self.status = r.status; self.responseText = r.text;
  setTimeout(function () { if (self.onload) self.onload(); }, 0);
};
win.XMLHttpRequest = global.XMLHttpRequest = FakeXHR;

/* ---- page ------------------------------------------------------------------ */
function rowOf(qid) { return doc.getElementById("qa-row-" + qid); }
function allButtons(qid) { var r = rowOf(qid); return r ? qsa(r, "button") : []; }
function options(qid) {
  var r = rowOf(qid);
  if (!r) return [];
  return qsa(r, ".qa-opt").map(function (b) {
    return { text: b.textContent, id: b.getAttribute("data-choice"),
             title: b.title || b.getAttribute("title") || "", cls: b.className,
             disabled: !!b.disabled,
             elementChildren: b.childNodes.filter(function (n) { return n.nodeType === 1; }).length };
  });
}
function buttonTexts(qid) { return allButtons(qid).map(function (b) { return b.textContent; }); }
function badgeOf(qid) {
  var r = rowOf(qid);
  if (!r) return null;
  var found = null;
  walk(r, []).forEach(function (n) {
    if (!found && n.className && n.className.split(/\s+/).indexOf("tc-badge") >= 0) found = n;
  });
  return found ? found.textContent : null;
}
function texts(qid) {
  var r = rowOf(qid);
  return r ? walk(r, []).map(function (n) { return n.textContent; }) : [];
}
function joined(qid) { return texts(qid).join("\n"); }
function errorText(qid) {
  var r = rowOf(qid);
  if (!r) return null;
  var hits = qsa(r, ".qa-err");
  return hits.length ? hits[0].textContent : null;
}
function openEditor(qid) {
  var btns = allButtons(qid);
  for (var i = 0; i < btns.length; i++) {
    var t = btns[i].textContent;
    if (t === "Answer" || t === "Edit" || t === "Re-answer") { btns[i].click(); return true; }
  }
  return false;
}
function textArea(qid) {
  var r = rowOf(qid);
  if (!r) return null;
  var ta = qsa(r, "textarea");
  return ta.length ? ta[0] : null;
}
function sendButton(qid, cls) {
  var r = rowOf(qid);
  if (!r) return null;
  var b = qsa(r, "." + cls);
  return b.length ? b[0] : null;
}

function editorButtons(qid) { var r = rowOf(qid); return r ? qsa(r, ".qa-edit-write button") : []; }
function editorShape(qid) {
  var shape = { row: !!rowOf(qid), opened: openEditor(qid), options: options(qid),
                buttons: buttonTexts(qid),
                hasSendText: !!sendButton(qid, "qa-send-text") };
  var ta = textArea(qid);
  shape.maxlength = ta ? ta.getAttribute("maxlength") : null;
  shape.rowText = joined(qid);
  shape.stored = global.localStorage.getItem("plangraph.qa.answers.v1");
  shape.calls = JSON.parse(JSON.stringify(calls));
  return shape;
}

var SCENARIOS = {
  editor: function (qid) { return editorShape(qid); },

  click_option: function (qid) {
    openEditor(qid);
    var opt = options(qid);
    if (!opt.length) return { error: "no option buttons" };
    var btn = sendButton(qid, "qa-opt");
    btn.click();
    var during = editorButtons(qid).map(function (b) { return !!b.disabled; });
    return after(30).then(function () {
      return { duringAllDisabled: during.indexOf(false) < 0 && during.length === 6,
               duringDisabled: during,
               afterDisabled: options(qid).map(function (o) { return o.disabled; }),
               calls: JSON.parse(JSON.stringify(calls)), badge: badgeOf(qid),
               rowText: joined(qid), stored: global.localStorage.getItem("plangraph.qa.answers.v1"),
               errText: errorText(qid) };
    });
  },

  click_text: function (qid, arg) {
    openEditor(qid);
    var ta = textArea(qid);
    if (!ta) return { error: "no textarea" };
    ta.value = arg;
    var b = sendButton(qid, "qa-send-text");
    if (!b) return { error: "no Answer with text button" };
    b.click();
    return after(30).then(function () {
      return { calls: JSON.parse(JSON.stringify(calls)), badge: badgeOf(qid),
               rowText: joined(qid), stored: global.localStorage.getItem("plangraph.qa.answers.v1"),
               errText: errorText(qid),
               disabled: options(qid).map(function (o) { return o.disabled; }) };
    });
  },

  local_answer: function (qid) {
    openEditor(qid);
    var b = sendButton(qid, "qa-local");
    if (!b) return { error: "no secondary local button" };
    var ta = textArea(qid);
    ta.value = "typed locally";
    b.click();
    return after(30).then(function () {
      return { calls: JSON.parse(JSON.stringify(calls)), badge: badgeOf(qid),
               rowText: joined(qid),
               stored: global.localStorage.getItem("plangraph.qa.answers.v1") };
    });
  },

  click_403: function (qid) {
    MODE = "403-once";
    openEditor(qid);
    var b = sendButton(qid, "qa-opt");
    b.click();
    return after(30).then(function () {
      return { calls: JSON.parse(JSON.stringify(calls)), badge: badgeOf(qid),
               rowText: joined(qid), stored: global.localStorage.getItem("plangraph.qa.answers.v1"),
               errText: errorText(qid) };
    });
  },

  click_error: function (qid) {
    MODE = "bad-request";
    openEditor(qid);
    var b = sendButton(qid, "qa-opt");
    b.click();
    var duringDisabled = options(qid).map(function (o) { return o.disabled; });
    return after(30).then(function () {
      return { duringDisabled: duringDisabled,
               calls: JSON.parse(JSON.stringify(calls)), badge: badgeOf(qid),
               errText: errorText(qid),
               afterDisabled: options(qid).map(function (o) { return o.disabled; }),
               stored: global.localStorage.getItem("plangraph.qa.answers.v1") };
    });
  },

  csrf_failure: function (qid) {
    MODE = "csrf-down";
    openEditor(qid);
    var b = sendButton(qid, "qa-opt");
    b.click();
    return after(30).then(function () {
      return { calls: JSON.parse(JSON.stringify(calls)), errText: errorText(qid),
               badge: badgeOf(qid),
               afterDisabled: options(qid).map(function (o) { return o.disabled; }) };
    });
  }
};

function applyConfig() {
  if (CFG === "none") return;
  if (CFG.indexOf("meta:") === 0) {
    add(head, "meta", null, [["name", "pg-answer-api"], ["content", CFG.slice(5)]]);
    return;
  }
  win.PLANGRAPH_ANSWER_API = CFG;
}

function main() {
  require(WEB + "/page.js");
  var PlanUI = require(WEB + "/page_model.js");
  require(WEB + "/page_store.js");
  require(WEB + "/page_search.js");
  require(WEB + "/page_side.js");
  require(WEB + "/page_side_tables.js");
  PlanUI.Preview = { attach: function () {} };
  require(WEB + "/page_qa_write.js");
  require(WEB + "/page_qa.js");
  win.PlanPage = global.PlanPage;
  win.localStorage = global.localStorage;
  applyConfig();

  var vm = PlanUI.buildViewModel(JSON.parse(fs.readFileSync(SNAPSHOT, "utf8")));
  var ui = new PlanUI.Store({});
  PlanUI.app = { ui: ui, store: ui, vm: vm, jump: function () {} };
  PlanUI.Sidebar.mount(ui, vm);
  PlanUI.QAPage.render(vm);

  var qid = process.argv[6];
  var arg = process.argv[7];
  var fn = SCENARIOS[SCENARIO];
  if (!fn) return emit({ error: "unknown scenario " + SCENARIO });
  return Promise.resolve(fn(qid, arg)).then(function (o) {
    o.base = (PlanUI.QAWrite && PlanUI.QAWrite.base) ? PlanUI.QAWrite.base() : null;
    o.enabled = !!(PlanUI.QAWrite && PlanUI.QAWrite.enabled && PlanUI.QAWrite.enabled());
    emit(o);
  }, function (e) { emit({ error: String((e && e.stack) || e) }); });
}

try { main(); } catch (e) { emit({ error: String((e && e.stack) || e) }); }
"""


def run(tmp_path: Path, scenario: str, cfg: str = "none",
        qid: str = "Q-1", arg: str = "") -> dict:
    snap = tmp_path / "snapshot.json"
    snap.write_text(json.dumps(snapshot()), encoding="utf-8")
    harness = tmp_path / "qa_write_harness.js"
    harness.write_text(HARNESS.replace("__TOKEN__", TOKEN), encoding="utf-8")
    cmd = ["node", str(harness), str(WEB), str(snap), scenario, cfg, qid, arg]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), "harness produced no JSON"
    out = json.loads(proc.stdout)
    assert "error" not in out, out.get("error")
    return out


def titles(out: dict) -> list:
    return [o["text"] for o in out["options"]]


def header(out: dict, index: int, name: str):
    headers = {k.lower(): v for k, v in out["calls"][index]["headers"].items()}
    return headers.get(name.lower())


def body_of(out: dict, index: int) -> dict:
    return json.loads(out["calls"][index]["body"])


# ---------------------------------------------------------------- write off --

@need_node
def test_write_mode_off_without_any_config(tmp_path):
    out = run(tmp_path, "editor", cfg="none")
    assert out["enabled"] is False and out["base"] is None
    assert out["options"] == []
    assert out["hasSendText"] is False
    assert "Save answer" in out["buttons"]          # page identical to today
    assert "Answer with text" not in out["buttons"]


@need_node
@pytest.mark.parametrize("bad", ["javascript:alert(1)", "http://evil.example",
                                 "https://keeper.example good", "//keeper.example",
                                 "http:/keeper.example", ""])
def test_write_mode_off_for_a_non_https_non_local_url(tmp_path, bad):
    out = run(tmp_path, "editor", cfg=bad)
    assert out["enabled"] is False, bad
    assert out["base"] is None, bad
    assert out["options"] == []
    assert "Save answer" in out["buttons"]


@need_node
def test_write_mode_on_for_http_localhost_dev_base(tmp_path):
    out = run(tmp_path, "editor", cfg="http://localhost:8731")
    assert out["enabled"] is True
    assert out["base"] == "http://localhost:8731"
    assert titles(out) == ["Option C", "Use option A", "<img src=x onerror=alert(1)>"]


@need_node
def test_write_mode_on_from_the_meta_tag(tmp_path):
    out = run(tmp_path, "editor", cfg="meta:" + BASE)
    assert out["enabled"] is True and out["base"] == BASE
    assert len(out["options"]) == 3


# --------------------------------------------------------------- write on ---

@need_node
def test_one_button_per_option_with_label_text(tmp_path):
    out = run(tmp_path, "editor", cfg=BASE)
    assert out["enabled"] is True
    assert titles(out) == ["Option C", "Use option A", "<img src=x onerror=alert(1)>"]
    assert [o["id"] for o in out["options"]] == ["opt-c", "opt-a", "opt-b"]
    assert out["options"][0]["cls"].split().count("qa-opt-default") == 1   # default marked, first
    assert "qa-opt-default" not in out["options"][1]["cls"]
    assert out["options"][0]["title"] == "Keeps P2 blocked"                # consequence as hint
    assert out["maxlength"] == "1000"
    assert "Answer with text" in out["buttons"]
    assert "Save as local answer" in " ".join(out["buttons"])              # secondary local path


@need_node
def test_item_without_choices_gets_the_text_box_only(tmp_path):
    out = run(tmp_path, "editor", cfg=BASE, qid="Q-2")
    assert out["options"] == [] and out["hasSendText"] is True
    assert out["maxlength"] == "1000"


@need_node
def test_non_operator_item_stays_local(tmp_path):
    out = run(tmp_path, "editor", cfg=BASE, qid="Q-3")
    assert out["options"] == [] and out["hasSendText"] is False
    assert "Save answer" in out["buttons"]


@need_node
def test_answered_question_shows_no_option_buttons(tmp_path):
    out = run(tmp_path, "editor", cfg=BASE, qid="Q-4")
    assert out["options"] == [] and out["hasSendText"] is False
    assert "already known" in out["rowText"]           # snapshot answer still shown
    assert "Save answer" in out["buttons"]             # re-answer stays local-only


@need_node
def test_clicking_an_option_gets_csrf_then_posts_the_choice(tmp_path):
    out = run(tmp_path, "click_option", cfg=BASE)
    assert out["duringAllDisabled"] is True, out["duringDisabled"]   # every editor button
    calls = out["calls"]
    assert [c["method"] for c in calls] == ["GET", "POST"]
    assert calls[0]["url"] == BASE + "/v1/operator_csrf"
    assert calls[0]["credentials"] == "include" and calls[0]["headers"] == {}
    assert calls[1]["url"] == BASE + "/v1/operator_answer"
    assert calls[1]["credentials"] == "include"
    assert header(out, 1, "X-PG-CSRF") == TOKEN
    assert header(out, 1, "Content-Type") == "application/json"
    assert not [k for k in calls[1]["headers"] if k.lower() == "authorization"]
    assert body_of(out, 1) == {"key": "atlas:Q-1", "choice_id": "opt-c"}
    assert "csrf-token-1" not in out["rowText"]        # the token is never displayed
    assert out["badge"] == "answered (UI)", out["badge"]
    assert "answered via ui" in out["rowText"].lower()
    assert "next snapshot" in out["rowText"].lower()
    assert out["stored"] is None                       # nothing written to localStorage


@need_node
def test_free_text_posts_key_and_text(tmp_path):
    out = run(tmp_path, "click_text", cfg=BASE, arg="go with the third option")
    assert [c["method"] for c in out["calls"]] == ["GET", "POST"]
    assert body_of(out, 1) == {"key": "atlas:Q-1", "text": "go with the third option"}
    assert header(out, 1, "X-PG-CSRF") == TOKEN
    assert out["stored"] is None and out["badge"] == "answered (UI)"


@need_node
def test_over_1000_characters_is_blocked_client_side(tmp_path):
    out = run(tmp_path, "click_text", cfg=BASE, arg="x" * 1001)
    assert out["calls"] == []                          # no request left the page
    assert "1000" in out["errText"]
    assert out["badge"] == "open"
    assert out["disabled"] == [False, False, False]    # buttons usable again


@need_node
def test_403_refetches_csrf_and_retries_exactly_once(tmp_path):
    out = run(tmp_path, "click_403", cfg=BASE)
    calls = out["calls"]
    assert [c["url"].rsplit("/", 1)[1] for c in calls] == [
        "operator_csrf", "operator_answer", "operator_csrf", "operator_answer"]
    assert [c["method"] for c in calls] == ["GET", "POST", "GET", "POST"]
    assert all(body_of(out, i) == {"key": "atlas:Q-1", "choice_id": "opt-c"}
               for i in (1, 3))
    assert out["badge"] == "answered (UI)" and out["stored"] is None
    assert "csrf token expired" not in out["rowText"]  # the swallowed 403 is not shown


@need_node
def test_error_shows_msg_text_and_re_enables_buttons(tmp_path):
    out = run(tmp_path, "click_error", cfg=BASE)
    assert out["duringDisabled"] == [True, True, True]
    assert out["errText"] == "no such option for this item"
    assert "<" not in out["errText"]                     # textContent only
    assert out["afterDisabled"] == [False, False, False]
    assert out["badge"] == "open" and out["stored"] is None
    assert len(out["calls"]) == 2                      # 400 is not retried


@need_node
def test_csrf_failure_is_shown_and_buttons_re_enabled(tmp_path):
    out = run(tmp_path, "csrf_failure", cfg=BASE)
    assert len(out["calls"]) == 1 and out["calls"][0]["method"] == "GET"
    assert out["errText"] and "token" in out["errText"].lower()
    assert out["badge"] == "open"
    assert out["afterDisabled"] == [False, False, False]


@need_node
def test_option_label_is_rendered_as_literal_text(tmp_path):
    out = run(tmp_path, "editor", cfg=BASE)
    evil = "<img src=x onerror=alert(1)>"
    assert evil in titles(out)
    target = [o for o in out["options"] if o["text"] == evil][0]
    assert target["elementChildren"] == 0             # no element created from the label
    assert target["cls"].split().count("qa-opt") == 1


@need_node
def test_local_answer_path_still_writes_local_storage(tmp_path):
    """The secondary action keeps today's behaviour (and write mode does not)."""
    out = run(tmp_path, "local_answer", cfg=BASE)
    assert out["stored"] and json.loads(out["stored"])["Q-1"]["text"] == "typed locally"
    assert out["calls"] == []                          # nothing sent to the keeper
    assert out["badge"].startswith("answered")
