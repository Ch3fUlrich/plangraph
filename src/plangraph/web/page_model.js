/* PlanUI view model — pure derivation the UI draws from a snapshot
 * (UI-PLAN §3, T1). Reuses `PlanGraph` from page.js for everything SPEC-owned
 * (display states, unblocks counts, critical path, terminal/estimate), so
 * "ready/blocked NEVER stored" stays true. No DOM, no I/O, ES5-safe, no
 * modules: one global namespace `PlanUI`, also exported via `module.exports`.
 */
(function (global) {
  "use strict";

  var PlanUI = (global.PlanUI = global.PlanUI || {});

  /* Stable project colours (R10), assigned by sorted project position. */
  var COLORS = ["#2f6f4f", "#3b5bdb", "#b45309", "#7c3aed",
                "#0e7490", "#be123c", "#4d7c0f", "#6d28d9"];

  function graph() {
    var g = global.PlanGraph;
    if (!g) throw new Error("PlanGraph missing — load page.js before page_model.js");
    return g;
  }

  function str(v) { return v == null ? "" : String(v); }
  function num(v) { var n = Number(v); return isFinite(n) ? n : 0; }

  /* Natural order mirroring Python nat(): digit runs compare as numbers so
   * P0 < P1 < … < P10 (P1 == P01 numerically), otherwise token-wise, then
   * shorter-first (a prefix sorts before its extension). */
  function naturalCmp(a, b) {
    var as = str(a).split(/(\d+)/), bs = str(b).split(/(\d+)/);
    var n = Math.min(as.length, bs.length);
    for (var i = 0; i < n; i++) {
      if (as[i] === bs[i]) continue;
      var ad = /^\d+$/.test(as[i]), bd = /^\d+$/.test(bs[i]);
      if (ad !== bd) return ad ? -1 : 1;               // (0,num) < (1,text)
      if (ad) {
        var d = num(as[i]) - num(bs[i]);
        if (d) return d > 0 ? 1 : -1;
        continue;                                      // "1" == "01"
      }
      return as[i] < bs[i] ? -1 : 1;
    }
    return as.length - bs.length;
  }

  /* Topological wave: longest dependency distance from a source. Rounds are
   * bounded by |items| so dependency cycles terminate (loops cap at n). Used
   * only as the phase-order tie-break (§3), after the natural sort. */
  function waveMap(items) {
    var w = {}, known = {};
    items.forEach(function (it) {
      if (it.key) { w[it.key] = 0; known[it.key] = true; }
    });
    for (var round = 0; round < items.length; round++) {
      var changed = false;
      items.forEach(function (it) {
        if (!it.key) return;
        (Array.isArray(it.depends_on) ? it.depends_on : []).forEach(function (d) {
          if (known[d] && w[it.key] < w[d] + 1) { w[it.key] = w[d] + 1; changed = true; }
        });
      });
      if (!changed) break;
    }
    return w;
  }

  /* Slack hours = critical hours − the longest path THROUGH the item
   * (forward + backward − own estimate), clamped at 0; terminal items get 0.
   * Mirrors docs/SPEC.md `views.slack` (probe-verified, 0 mismatches on the
   * 254-item fixture). Forward/backward relax only over non-terminal items —
   * terminal items satisfy no dependency (SPEC). */
  function slackMap(items, hours) {
    var G = graph();
    var idx = {};
    items.forEach(function (it) {
      if (it.key && !G.isTerminal(it)) idx[it.key] = it;
    });
    var keys = Object.keys(idx), f = {}, b = {}, out = {};
    keys.forEach(function (k) { f[k] = b[k] = G.estimate(idx[k]); });
    for (var round = 0; round < keys.length; round++) {
      var changed = false;
      keys.forEach(function (k) {
        var own = G.estimate(idx[k]);
        (Array.isArray(idx[k].depends_on) ? idx[k].depends_on : []).forEach(function (d) {
          if (!idx[d]) return;
          if (f[d] + own > f[k]) { f[k] = f[d] + own; changed = true; }
          if (b[k] + G.estimate(idx[d]) > b[d]) { b[d] = b[k] + G.estimate(idx[d]); changed = true; }
        });
      });
      if (!changed) break;
    }
    keys.forEach(function (k) {
      out[k] = Math.max(0, hours - (f[k] + b[k] - G.estimate(idx[k])));
    });
    return out;
  }

  /* a question is an item titled "Q: …" or a lane whose id starts Q-
   * (any project); snapshots carrying an explicit `questions` list pass
   * through untouched. */
  function questionOf(item) {
    return str(item.title).slice(0, 2) === "Q:" || /^Q-/.test(str(item.id));
  }

  function questionStatus(state) {
    return state === "done" ? "answered" : state === "cancelled" ? "cancelled" : "open";
  }

  function deriveQuestions(items) {
    return items.filter(questionOf).map(function (i) {
      return { id: i.id, project: i.project, text: i.title, asked_by: i.owner,
               status: questionStatus(i.state), item: i.key,
               answer: i.state === "done" && i.done_criteria != null ? i.done_criteria : null };
    });
  }

  function deriveDecisions(items) {
    var order = [], byId = {};
    items.forEach(function (i) {
      (Array.isArray(i.evidence) ? i.evidence : []).forEach(function (ev) {
        if (!ev || ev.kind !== "decision" || !ev.ref) return;
        var d = byId[ev.ref];
        if (!d) {                                    // first-seen order, first-seen project
          d = byId[ev.ref] = { id: ev.ref, project: i.project, title: "Decision " + ev.ref,
                               text: "", date: str(ev.at).slice(0, 10), items: [] };
          order.push(d);
        }
        if (d.items.indexOf(i.key) < 0) d.items.push(i.key);
      });
    });
    return order;
  }

  /* EFFECTIVE question status: a local answer (PlanUI.QAPage, localStorage
   * key plangraph.qa.answers.v1) overrides the snapshot status to "answered";
   * removing the local answer restores the snapshot status. `local` is the
   * answers map (id -> {text, at}); pass {} when there are no local answers. */
  function qaEffectiveStatus(q, local) {
    if (local && Object.prototype.hasOwnProperty.call(local, q.id) && local[q.id]) return "answered";
    return q.status;
  }

  /* Recompute the question-derived slices after a local answer changes, so
   * Needs You / Answers / badges follow without a reload. Operator items are
   * kept as-is (only open questions leave needsYou); same (-priority,
   * -unblocks) order as buildViewModel. Mutates and returns vm. */
  function refreshQA(vm, local) {
    local = local || {};
    var ops = (vm.needsYou || []).filter(function (e) { return e.text === undefined; });
    var ny = ops.concat((vm.questions || []).filter(function (q) { return qaEffectiveStatus(q, local) === "open"; }));
    ny.sort(function (a, b) {
      return num(b.priority) - num(a.priority) ||
        (b.unblocks ? b.unblocks.length : 0) - (a.unblocks ? a.unblocks.length : 0);
    });
    vm.needsYou = ny;
    vm.answers = (vm.questions || []).filter(function (q) { return qaEffectiveStatus(q, local) === "answered"; });
    return vm;
  }

  function capitalise(name) {
    return str(name).charAt(0).toUpperCase() + str(name).slice(1).toLowerCase();
  }

  function depsOf(it) {
    return Array.isArray(it.depends_on) ? it.depends_on : [];
  }

  function earliestEvidence(it) {
    var ev = Array.isArray(it.evidence) ? it.evidence : [], best = "";
    ev.forEach(function (e) { if (e && e.at && (!best || e.at < best)) best = e.at; });
    return best;
  }

  /* planning | implementation | review. An explicit item `role` wins; otherwise
   * gates are reviews, goals are planning, then the title decides, else implementation. */
  function roleOf(it) {
    var r = String(it.role || "").toLowerCase();
    if (r === "planning" || r === "implementation" || r === "review") return r;
    if (it.kind === "gate") return "review";
    if (it.kind === "goal") return "planning";
    var t = String(it.title || "");
    if (/\b(review|audit|verify|verification|qa|check)\b/i.test(t)) return "review";
    if (/\b(plan|planning|research|design|spec|scope|investigate)\b/i.test(t)) return "planning";
    return "implementation";
  }

  function buildViewModel(snap) {
    var G = graph();
    snap = snap || {};
    var raw = Array.isArray(snap.items) ? snap.items.filter(Boolean) : [];
    var display = G.computeDisplayState(raw);        // SPEC: derived, never stored
    var ub = G.unblocksMap(raw);
    var cp = G.criticalPath(raw);
    var slack = slackMap(raw, cp.hours);
    var wave = waveMap(raw);
    var idx = G.indexByKey(raw);
    var critSet = {};
    cp.keys.forEach(function (k) { critSet[k] = true; });

    /* Waiters per key: non-terminal items depending on it (= unblocksMap counts). */
    var waiters = {};
    raw.forEach(function (it) {
      if (!it.key || G.isTerminal(it)) return;
      depsOf(it).forEach(function (d) {
        if (idx[d]) (waiters[d] = waiters[d] || []).push(it.key);
      });
    });

    var questions = Array.isArray(snap.questions) ? snap.questions : deriveQuestions(raw);
    var decisions = Array.isArray(snap.decisions) ? snap.decisions : deriveDecisions(raw);
    var decisionById = {};
    decisions.forEach(function (d) { decisionById[d.id] = d; });

    /* Group by project (natural order) then phase label; phases ordered by
     * natural sort of the label, ties broken by min topological wave (§3). */
    var groups = {};
    raw.forEach(function (it) {
      if (!it.key) return;
      var pname = it.project == null ? "" : it.project;
      groups[pname] = groups[pname] || {};
      var ph = it.phase || it.track || "";
      groups[pname][ph] = (groups[pname][ph] || []).concat([it]);
    });

    var phaseIdxOf = {};
    var projects = Object.keys(groups).sort(naturalCmp).map(function (pname, pi, arr) {
      var byPhase = groups[pname];
      var names = Object.keys(byPhase).sort(function (a, b) {
        var wa = minWave(wave, byPhase[a]), wb = minWave(wave, byPhase[b]);
        return naturalCmp(a, b) || (wa < wb ? -1 : wa > wb ? 1 : 0) ||
          (a < b ? -1 : a > b ? 1 : 0);
      });
      names.forEach(function (ph, fi) {
        byPhase[ph].forEach(function (it) { phaseIdxOf[it.key] = fi; });
      });
      return { name: pname, label: capitalise(pname), color: PlanUI.projectColor ? PlanUI.projectColor(pname, arr) : COLORS[pi % COLORS.length],
               goalKey: pname + ":GOAL",
               phases: names.map(function (ph, fi) {
                 var list = byPhase[ph];
                 return { name: ph, idx: fi, keys: list.map(function (i) { return i.key; }),
                          total: list.length,
                          done: list.filter(function (i) { return i.state === "done"; }).length,
                          ready: list.filter(function (i) { return display[i.key] === "ready"; }).length,
                          blocked: list.filter(function (i) { return display[i.key] === "blocked"; }).length };
               }) };
    });

    var items = new Map();
    var needsItems = [];
    raw.forEach(function (it) {
      if (!it.key) return;
      var record = {
        key: it.key, id: it.id, project: it.project, title: it.title,
        role: roleOf(it),
        claimedBy: it.claimed_by || null, workerRun: it.worker_run || null,
        createdAt: it.created_at || earliestEvidence(it) || "",
        phase: it.phase || it.track || "", phaseIdx: phaseIdxOf[it.key] || 0,
        kind: it.kind, state: it.state, display: display[it.key],
        owner: it.owner, operator: !!it.operator, priority: num(it.priority),
        estimate_h: G.estimate(it), deps: depsOf(it), unblocks: waiters[it.key] || [],
        critical: !!critSet[it.key],
        slack: G.isTerminal(it) ? 0 : (slack[it.key] || 0),
        loop: it.loop == null ? null : it.loop,
        doneCriteria: it.done_criteria == null ? null : it.done_criteria,
        evidence: Array.isArray(it.evidence) ? it.evidence : [],
        reviewTier: it.review_tier || "standard",
        updatedAt: it.updated_at || "",
        crossDeps: depsOf(it).filter(function (d) {
          return !!idx[d] && idx[d].project !== it.project;   // deps in other projects
        })
      };
      items.set(it.key, record);
      if (record.operator && !G.isTerminal(it)) needsItems.push(record);
    });

    /* needsYou = operator items (non-terminal) first, then open questions,
     * stably sorted by (-priority, -unblocks); question rows rank (0,0). */
    var needsYou = needsItems.concat(questions.filter(function (q) {
      return q.status === "open";
    }));
    needsYou.sort(function (a, b) {
      return num(b.priority) - num(a.priority) ||
        (b.unblocks ? b.unblocks.length : 0) - (a.unblocks ? a.unblocks.length : 0);
    });

    function humanize(key) {
      var it = items.get(key);
      return it ? str(it.title) + " (" + it.project + ")" : str(key);
    }

    return { items: items, projects: projects, questions: questions,
             decisions: decisions, decisionById: decisionById, needsYou: needsYou,
             answers: questions.filter(function (q) { return q.status === "answered"; }),
             critical: cp.keys, humanize: humanize };
  }

  function minWave(wave, list) {
    var m = Infinity;
    list.forEach(function (it) {
      var w = wave[it.key];
      if (w !== undefined && w < m) m = w;
    });
    return m;
  }

  PlanUI.buildViewModel = buildViewModel;
  PlanUI.qaEffectiveStatus = qaEffectiveStatus;
  PlanUI.refreshQA = refreshQA;
  PlanUI.naturalCmp = naturalCmp;

  if (typeof module !== "undefined" && module.exports) module.exports = PlanUI;
})(typeof window !== "undefined" ? window : globalThis);
