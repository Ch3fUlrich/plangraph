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
  this.checked = false;
  this.clientWidth = 0;
  this.clientHeight = 0;
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
El.prototype.appendChild = function (c) {
  c.parentNode = this; this.childNodes.push(c); return c;
};
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
El.prototype.getAttribute = function (n) {
  return (n in this.attributes) ? this.attributes[n] : null;
};
El.prototype.removeAttribute = function (n) { delete this.attributes[n]; };
El.prototype.addEventListener = function (t, fn) {
  (this.listeners[t] = this.listeners[t] || []).push(fn);
};
El.prototype.click = function () {
  (this.listeners.click || []).slice().forEach(function (f) { f({ type: "click", preventDefault: function () {} }); });
};
El.prototype.focus = function () {};
El.prototype.blur = function () {};
El.prototype.select = function () {};
El.prototype.closest = function () { return null; };
El.prototype.getBoundingClientRect = function () {
  return { left: 0, top: 0, bottom: 0, right: 0, width: 0, height: 0 };
};
El.prototype.getClientRects = function () { return []; };
El.prototype.querySelectorAll = function (sel) { return qsa(this, sel); };
El.prototype.querySelector = function (sel) { return qsa(this, sel)[0] || null; };
El.prototype.classList = {
  add: function () {}, remove: function () {},
  toggle: function () {}, contains: function () { return false; }
};
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
  for (var i = 0; i < cls.length; i++) {
    if (own.indexOf(cls[i].slice(1)) < 0) return false;
  }
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
var body = new El("body");
htmlEl.appendChild(body);
var doc = {
  nodeType: 9,
  readyState: "complete",
  body: body,
  documentElement: htmlEl,
  createElement: function (t) { return new El(t); },
  createTextNode: function (t) {
    var n = new El("#text");
    n.nodeType = 3;
    n.textContent = String(t);
    return n;
  },
  getElementById: function (id) { return ids[id] || null; },
  querySelectorAll: function (sel) { return qsa(body, sel); },
  querySelector: function (sel) { return qsa(body, sel)[0] || null; },
  addEventListener: function () {},
  removeEventListener: function () {},
  execCommand: function () { return false; }
};
global.document = doc;
global.window = global;
function setG(k, v) { try { Object.defineProperty(global, k, { value: v, configurable: true, writable: true }); } catch (e) { try { global[k] = v; } catch (x) {} } }
setG("navigator", { clipboard: { writeText: function () {} } });
setG("location", { hash: "", pathname: "/", search: "" });
setG("history", { replaceState: function () {} });
setG("performance", { now: function () { return 0; }, mark: function () {} });
setG("innerWidth", 1280);
setG("innerHeight", 800);
setG("getComputedStyle", function () { return { getPropertyValue: function () { return ""; }, display: "block" }; });
setG("matchMedia", function () { return { matches: false, addEventListener: function () {}, addListener: function () {} }; });
setG("requestAnimationFrame", function (fn) { fn(); return 0; });
setG("addEventListener", function () {});
setG("removeEventListener", function () {});
global.URL = { createObjectURL: function () { return ""; }, revokeObjectURL: function () {} };
global.Blob = function () {};
var store = {};
global.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
  setItem: function (k, v) { store[k] = String(v); },
  removeItem: function (k) { delete store[k]; }
};

function add(parent, tag, cls, attrs, text) {
  var n = new El(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  (attrs || []).forEach(function (p) { n.setAttribute(p[0], p[1]); });
  parent.appendChild(n);
  return n;
}

