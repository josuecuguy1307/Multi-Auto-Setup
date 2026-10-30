/* Owner's screen. No framework, no remote resource. Text from the world (machine names) is only ever put into the page with textContent. All copy lives in strings.es.json. */
"use strict";

const POLL_MS = 1500;
const state = { runId: null, runTimer: null, strings: {}, data: null, skew: 0, csrf: "", dismissed: new Set(), deciding: false, confirmShownFor: null, stopped: false };
const $ = (id) => document.getElementById(id);

function t(key, vars) {
  const raw = Object.prototype.hasOwnProperty.call(state.strings, key) ? state.strings[key] : key;
  return raw.replace(/\{(\w+)\}/g, (_, name) => (vars && name in vars ? String(vars[name]) : ""));
}

function applyStrings() {
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.title = t("app.title");
}

function notice(text) {
  const el = $("notice");
  el.textContent = text || "";
  el.hidden = !text;
}

async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin", cache: "no-store" };
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = state.csrf;
    init.body = JSON.stringify(body || {});
  }
  const res = await fetch(path, init);
  let payload = {};
  try { payload = await res.json(); } catch (_) { payload = {}; }
  if (!res.ok) {
    const err = new Error(payload.error || "http_" + res.status);
    err.status = res.status;
    err.code = payload.error || "";
    throw err;
  }
  return payload;
}

function fail(err) {
  if (err.status === 401) {
    state.stopped = true;
    notice(t("error.session"));
    return;
  }
  const key = "error." + err.code;
  notice(Object.prototype.hasOwnProperty.call(state.strings, key) ? t(key) : t("error.generic"));
}

/* ---- time ---- */

function nowServer() { return Date.now() / 1000 + state.skew; }

function ago(seconds) {
  if (seconds === null) return t("machines.heartbeat.never");
  const s = Math.max(0, Math.floor(seconds));
  if (s < 120) return t("machines.heartbeat.ago_s", { n: s });
  if (s < 7200) return t("machines.heartbeat.ago_min", { n: Math.floor(s / 60) });
  return t("machines.heartbeat.ago_h", { n: Math.floor(s / 3600) });
}

/* ---- machines ---- */

function fact(dl, labelKey, value, mono) {
  const dt = document.createElement("dt");
  dt.textContent = t(labelKey);
  const dd = document.createElement("dd");
  dd.textContent = value;
  if (mono) dd.className = "mono";
  dl.append(dt, dd);
}

const openAnalysis = new Set();
const analysisCache = {};

function section(titleKey, rows) {
  const box = document.createElement("div");
  box.className = "ana-group";
  const h = document.createElement("h4");
  h.textContent = t(titleKey);
  box.append(h);
  const ul = document.createElement("ul");
  ul.className = "ana-list";
  (rows.length ? rows : [t("node.analysis.none")]).forEach((r) => { const li = document.createElement("li"); li.textContent = r; ul.append(li); });
  box.append(ul);
  return box;
}

function kv(obj) { return Object.entries(obj || {}).map(([k, v]) => k + ": " + (v === undefined || v === null ? t("node.analysis.not_checked") : typeof v === "object" ? JSON.stringify(v) : String(v))); }

function renderAnalysis(panel, a) {
  const ai = a.ai || {};
  const prog = a.programs || {};
  const aiRows = [];
  (ai.agents || []).forEach((x) => aiRows.push([x.name, x.state, x.category].filter(Boolean).join(" · ")));
  (ai.model_servers || []).forEach((x) => aiRows.push([x.kind || x.name, x.version, x.port].filter(Boolean).join(" · ")));
  const fit = ai.largest_model_that_fits;
  if (fit) aiRows.push(t("node.analysis.fit", { p: fit.params_b, q: fit.path || fit.quant || "", l: t("node.analysis.limit." + fit.limited_by) }));
  (ai.runtimes || []).forEach((x) => aiRows.push([x.name, x.running ? "running" : "installed", (x.models || []).length + " models"].join(" · ")));
  (ai.clis || []).forEach((x) => aiRows.push([x.name, x.set_up ? "set up" : "installed"].join(" · ")));
  const names = (ai.api_key_variable_names || []).map((x) => x.name);
  if (names.length) aiRows.push(t("node.analysis.keys", { names: names.join(", ") }));
  const progRows = (prog.tools_found === undefined || prog.tools_found === null) ? [t("node.analysis.tools_not_checked")] : kv({ tools_found: prog.tools_found, ...(prog.by_category || {}) });
  (prog.installed_programs || []).slice(0, 10).forEach((x) => progRows.push([x.name, x.version].filter(Boolean).join(" ")));
  (prog.installed_apps || []).slice(0, 8).forEach((x) => progRows.push([x.name, x.version].filter(Boolean).join(" ")));
  panel.replaceChildren(section("node.analysis.system", kv(a.system)), section("node.analysis.hardware", kv(a.hardware)), section("node.analysis.programs", progRows.filter(Boolean)),
    section("node.analysis.ai", aiRows), ...((a.limits || []).length ? [section("node.analysis.limits", a.limits)] : []));
}

async function showAnalysis(id, panel) {
  try {
    analysisCache[id] = await api("GET", "/api/node/analysis?id=" + encodeURIComponent(id));
    renderAnalysis(panel, analysisCache[id]);
  } catch (err) { fail(err); }
}

async function startOptions(id) {
  notice("");
  try {
    const res = await api("POST", "/api/options/start", { node: id });
    accept(res);
    followRun(res.run.id);
  } catch (err) { fail(err); }
}

async function runTask(id, type) {
  notice("");
  try {
    const res = await api("POST", "/api/analysis/start", { node: id });
    accept(res);
    followRun(res.run.id);
  } catch (err) { fail(err); }
}

/* ---- the agent run, live ---- */

function chip(tag) {
  const s = document.createElement("span");
  s.className = "tag tag-" + tag;
  s.textContent = t("run.step." + tag);
  return s;
}

function row(text, cls) {
  const li = document.createElement("li");
  if (cls) li.className = cls;
  li.textContent = text;
  return li;
}

function resultView(run) {
  const box = document.createElement("div");
  const res = run.result || {};
  if (!res.agent) {
    const p = document.createElement("p");
    p.className = "banner";
    p.textContent = t("run.code_only");
    box.append(p);
  }
  Object.entries(res.groups || {}).forEach(([g, kv]) => {
    if (!Object.keys(kv).length) return;
    const h = document.createElement("h4");
    h.textContent = t("run.group." + g);
    const ul = document.createElement("ul");
    ul.className = "ana-list";
    Object.entries(kv).forEach(([k, v]) => {
      const state = (res.evidence_state || {})[k];
      ul.append(row(k + ": " + (typeof v === "object" ? JSON.stringify(v).slice(0, 160) : String(v)) + (state ? " [" + t("run.evidence", { s: state }) + "]" : "")));
    });
    box.append(h, ul);
  });
  if ((res.findings || []).length) {
    const h = document.createElement("h4");
    h.textContent = t("run.findings");
    const ul = document.createElement("ul");
    ul.className = "ana-list";
    res.findings.forEach((f) => ul.append(row(f.text + " ← " + f.rests_on.join(", ") + " [" + f.status + "]")));
    box.append(h, ul);
  }
  if (res.finished) {
    const h = document.createElement("h4");
    h.textContent = t("run.unknowns");
    const ul = document.createElement("ul");
    ul.className = "ana-list";
    ["known", "unknown", "why"].forEach((k) => res.finished[k] && ul.append(row(t("run.finished." + k) + ": " + res.finished[k])));
    box.append(h, ul);
  }
  return box;
}

function optionsView(run) {
  const res = run.result || {};
  const wrap = document.createElement("div");
  if (!res.agent || !res.option_set) return wrap;
  const h = document.createElement("h4");
  h.textContent = t("run.options");
  const hint = document.createElement("p");
  hint.className = "muted";
  hint.textContent = t("run.options.hint");
  wrap.append(h, hint);
  if (!res.options.length) { const p = document.createElement("p"); p.textContent = t("run.option.none"); wrap.append(p); return wrap; }
  const list = document.createElement("ul");
  list.className = "cards";
  res.options.forEach((o) => {
    const li = document.createElement("li");
    li.className = "card";
    const title = document.createElement("h3");
    title.textContent = o.title;
    const facts = document.createElement("ul");
    facts.className = "ana-list";
    facts.append(row(o.cloud ? t("run.option.cloud") : t("run.option.local"), o.cloud ? "check bad" : "check ok"), row(t("run.option.cost", { c: t("run.cost." + o.cost_class) })), row(t("run.option.status") + " · " + o.label),
                 row((o.license ? String(o.license) : "") + (o.source ? " · " + o.source : "")));
    (o.limits || []).forEach((l) => facts.append(row(l)));
    li.append(title, facts);
    if (o.explanation) {
      const p = document.createElement("p");
      p.textContent = t("run.option.explanation") + ": " + o.explanation;
      li.append(p);
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn primary";
    const picked = run.picked_option === o.id;
    btn.textContent = picked ? t("run.option.picked") : t("run.option.pick");
    btn.disabled = !!run.picked_option;
    btn.addEventListener("click", async () => {
      try { await api("POST", "/api/run/pick", { run: run.id, option: o.id }); pollRun(); } catch (err) { fail(err); }
    });
    li.append(btn);
    list.append(li);
  });
  wrap.append(list);
  return wrap;
}

function renderRun(run) {
  const panel = $("run-panel");
  const key = JSON.stringify([run.id, run.status, (run.events || []).length, run.pending && run.pending.question, run.finished, run.picked_option]);
  if (panel.dataset.key === key) return;  // rebuilt only when something changed: a button must not move under the owner's pointer
  panel.dataset.key = key;
  panel.hidden = false;
  const head = document.createElement("div");
  head.className = "bar";
  const h = document.createElement("h2");
  h.id = "run-title";
  h.textContent = run.kind === "lan" ? t("run.title.lan") : run.kind === "options" ? t("run.title.options", { node: run.node }) : t("run.title", { node: run.node });
  const status = document.createElement("span");
  status.className = "badge " + (["done", "running", "starting"].includes(run.status) ? "online" : "offline");
  status.textContent = t("run.status." + run.status);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "btn";
  close.textContent = t("run.close");
  close.addEventListener("click", () => { state.runId = null; panel.hidden = true; panel.dataset.key = ""; });
  head.append(h, status, close);
  const info = document.createElement("p");
  info.className = "muted";
  info.textContent = (run.model && run.model.available ? t("run.model.ok", { p: run.model.profile }) : t("run.model.none", { r: (run.model && run.model.reason) || run.reason })) + " — "
    + t("run.actors", { m: run.actors.MODEL || 0, g: run.actors.GUARD || 0, s: run.actors.SCRIPT || 0, o: run.actors.OWNER || 0, c: run.actors["CLAUDE-CODE"] || 0 });
  const parts = [head, info];
  if (run.reason && run.status !== "no_model") { const p = document.createElement("p"); p.className = "notice"; p.textContent = run.reason; parts.push(p); }
  if (run.pending) {
    const q = document.createElement("div");
    q.className = "panel confirm";
    const qt = document.createElement("h3");
    qt.textContent = t("run.question") + ": " + run.pending.text;
    const acts = document.createElement("div");
    acts.className = "actions";
    run.pending.options.forEach((o) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn primary";
      b.textContent = o.label;
      b.title = o.detail || "";
      b.addEventListener("click", async () => {
        try { await api("POST", "/api/run/answer", { run: run.id, question: run.pending.question, option: o.id }); pollRun(); } catch (err) { fail(err); }
      });
      acts.append(b);
      if (o.detail) { const d = document.createElement("span"); d.className = "muted opt-detail"; d.textContent = o.detail; acts.append(d); }
    });
    q.append(qt, acts);
    parts.push(q);
  }
  if (run.result) { parts.push(resultView(run), optionsView(run)); }
  const sh = document.createElement("h4");
  sh.textContent = t("run.steps");
  const steps = document.createElement("ol");
  steps.className = "steps";
  (run.events || []).forEach((e) => { const li = document.createElement("li"); li.append(chip(e.tag), document.createTextNode(" " + e.text)); steps.append(li); });
  parts.push(sh, steps);
  panel.replaceChildren(...parts);
}

async function pollRun() {
  if (!state.runId) return;
  try {
    const run = await api("GET", "/api/run?id=" + encodeURIComponent(state.runId));
    renderRun(run);
    if (["starting", "running"].includes(run.status) || run.pending) state.runTimer = setTimeout(pollRun, 1200);
    else if (run.status === "no_model" && !run.finished) state.runTimer = setTimeout(pollRun, 1200);
  } catch (err) { fail(err); }
}

function followRun(id) {
  state.runId = id;
  const panel = $("run-panel");
  panel.dataset.key = "";
  clearTimeout(state.runTimer);
  pollRun();
}

async function askLanAgent() {
  notice("");
  try {
    const res = await api("POST", "/api/lan/start", {});
    accept(res);
    followRun(res.run.id);
  } catch (err) { fail(err); }
}

function reportBlock(m) {
  const box = document.createElement("div");
  box.className = "report";
  if (!m.report) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = t("node.report.none");
    box.append(p);
    return box;
  }
  const ports = m.report.sockets || [];
  const keyOk = !(m.report.key && m.report.key.exists && m.report.key.owner_only === false);
  const list = document.createElement("ul");
  list.className = "checks";
  [[ports.length === 0, ports.length === 0 ? t("node.report.ok_ports") : t("node.report.bad_ports", { ports: ports.map((s) => s.proto + ":" + s.port).join(", ") })],
   [keyOk, keyOk ? t("node.report.ok_key") : t("node.report.bad_key")]].forEach(([ok, text]) => {
    const li = document.createElement("li");
    li.className = ok ? "check ok" : "check bad";
    li.textContent = (ok ? "✓ " : "⚠ ") + text;
    list.append(li);
  });
  (m.alerts || []).filter((a) => a.kind === "clock_skew").forEach((a) => {
    const li = document.createElement("li");
    li.className = "check warn";
    li.textContent = "⚠ " + t("node.alert.clock", { n: Math.round(Math.abs(a.skew_s) / 60) });
    list.append(li);
  });
  box.append(list);
  return box;
}

function renderMachines(data) {
  const list = $("machines");
  const online = data.machines.filter((m) => m.status === "online").length;
  $("machines-count").textContent = data.machines.length ? t("machines.count", { n: data.machines.length, online }) : "";
  $("machines-empty").hidden = data.machines.length > 0;
  const running = new Set((data.tasks || []).filter((x) => x.type === "machine_analysis" && ["created", "sent", "accepted", "running"].includes(x.status)).map((x) => x.node));
  const key = JSON.stringify(data.machines.map((m) => [m.id, m.name, m.os, m.arch, m.status, m.fingerprint, m.report_at, m.analysis_at, m.agent_analysis_at, m.alerts, running.has(m.id)]));
  if (list.dataset.key !== key) {  // rebuilt only when something changed: a button must not be replaced under the owner's pointer
    list.dataset.key = key;
    list.replaceChildren(...data.machines.map((m) => {
      const li = document.createElement("li");
      li.className = "card";
      const head = document.createElement("div");
      head.className = "card-head";
      const h = document.createElement("h3");
      h.textContent = m.name;
      const badge = document.createElement("span");
      badge.className = "badge " + (m.status === "online" ? "online" : "offline");
      badge.textContent = t(m.status === "online" ? "machines.status.online" : "machines.status.offline");
      head.append(h, badge);
      const dl = document.createElement("dl");
      dl.className = "facts";
      fact(dl, "machines.field.os", m.os + " / " + m.arch, false);
      fact(dl, "machines.field.heartbeat", "", false);
      dl.lastChild.dataset.beat = m.last_heartbeat === null ? "" : String(m.last_heartbeat);
      fact(dl, "machines.field.fingerprint", m.fingerprint, true);
      const actions = document.createElement("div");
      actions.className = "actions";
      const an = document.createElement("button");
      an.type = "button";
      an.className = "btn primary";
      an.textContent = t(running.has(m.id) ? "node.analyzing" : "node.analyze");
      an.disabled = m.status !== "online" || running.has(m.id);
      an.addEventListener("click", () => runTask(m.id, "machine_analysis"));
      actions.append(an);
      if (m.agent_analysis_at) {  // the options protocol reads the stored analysis: the button exists only when there is one
        const op = document.createElement("button");
        op.type = "button";
        op.className = "btn";
        op.textContent = t("node.options");
        op.addEventListener("click", () => startOptions(m.id));
        actions.append(op);
      }
      const panel = document.createElement("div");
      panel.className = "analysis";
      panel.hidden = !openAnalysis.has(m.id);
      if (m.analysis_at) {
        const view = document.createElement("button");
        view.type = "button";
        view.className = "btn";
        const sync = () => { view.textContent = t(openAnalysis.has(m.id) ? "node.analysis.hide" : "node.analysis.view"); };
        sync();
        view.addEventListener("click", () => {
          if (openAnalysis.has(m.id)) { openAnalysis.delete(m.id); panel.hidden = true; } else { openAnalysis.add(m.id); panel.hidden = false; showAnalysis(m.id, panel); }
          sync();
        });
        actions.append(view);
        if (openAnalysis.has(m.id)) showAnalysis(m.id, panel);
      }
      li.append(head, dl, reportBlock(m), actions, panel);
      return li;
    }));
  }
  list.querySelectorAll("[data-beat]").forEach((el) => { el.textContent = ago(el.dataset.beat === "" ? null : data.now - Number(el.dataset.beat)); });
  const p = data.pairing;
  $("pairing-banner").hidden = !(p && ["waiting", "exchange", "confirm", "deciding"].includes(p.status));
}

function renderWaiting(data) {
  const list = $("waiting");
  const rows = data.waiting || [];
  $("waiting-empty").hidden = rows.length > 0;
  const busy = !!(data.pairing && ["waiting", "exchange", "confirm", "deciding"].includes(data.pairing.status));
  const key = JSON.stringify([busy, rows.map((w) => [w.id, w.name, w.os, w.arch, w.fingerprint])]);
  if (list.dataset.key !== key) {  // rebuild only when something changed: a button must not be replaced under the owner's pointer
    list.dataset.key = key;
    list.replaceChildren(...rows.map((w) => {
      const li = document.createElement("li");
      li.className = "card";
      const head = document.createElement("div");
      head.className = "card-head";
      const h = document.createElement("h3");
      h.textContent = w.name;
      head.append(h);
      const dl = document.createElement("dl");
      dl.className = "facts";
      fact(dl, "machines.field.os", w.os + " / " + w.arch, false);
      fact(dl, "waiting.since", "", false);
      dl.lastChild.dataset.since = String(w.since);
      fact(dl, "waiting.claimed", w.fingerprint, true);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn primary";
      btn.textContent = t("waiting.pair");
      btn.disabled = busy;
      btn.addEventListener("click", () => offerFor(w.id));
      li.append(head, dl, btn);
      return li;
    }));
  }
  list.querySelectorAll("[data-since]").forEach((el) => { el.textContent = ago(data.now - Number(el.dataset.since)); });
}

async function offerFor(id) {
  notice("");
  try {
    accept(await api("POST", "/api/pair/offer-for", { waiting: id }));
    location.hash = "#/pair";
  } catch (err) { fail(err); }
}

/* ---- pairing ---- */

const PANELS = ["pair-idle", "pair-code", "pair-exchange", "pair-confirm", "pair-result"];

function show(panelId) { PANELS.forEach((id) => { $(id).hidden = id !== panelId; }); }

function tickCountdown() {
  const p = state.data && state.data.pairing;
  const el = $("countdown");
  if (!p || p.status !== "waiting") { el.textContent = ""; return; }
  const left = Math.max(0, Math.ceil(p.expires_at - nowServer()));
  el.textContent = left > 0 ? t("pair.countdown", { m: Math.floor(left / 60), s: String(left % 60).padStart(2, "0") }) : t("pair.expired_now");
}

function renderPairing(data) {
  const p = data.pairing;
  const result = $("pair-result");
  $("pair-for").hidden = true;
  if (!p || (["accepted", "refused", "failed", "expired", "rejected"].includes(p.status) && state.dismissed.has(p.offer))) {
    show("pair-idle");
    return;
  }
  $("pair-for").hidden = !(p.target && ["waiting", "exchange", "confirm", "deciding"].includes(p.status));
  if (p.target) $("pair-for-text").textContent = t("pair.for_machine", { name: p.target });
  if (p.status === "waiting") {
    $("code").textContent = p.code || "";
    show("pair-code");
    tickCountdown();
  } else if (p.status === "exchange" || p.status === "deciding") {
    $("exchange-text").textContent = t(p.status === "deciding" ? "pair.deciding" : "pair.exchange");
    show("pair-exchange");
  } else if (p.status === "confirm") {
    const c = p.candidate || {};
    $("c-name").textContent = c.name || "";
    $("c-os").textContent = c.os || "";
    $("c-arch").textContent = c.arch || "";
    $("c-fp").textContent = c.fingerprint || "";
    $("approve").disabled = $("reject").disabled = state.deciding;
    show("pair-confirm");
    if (state.confirmShownFor !== p.offer) {
      state.confirmShownFor = p.offer;
      const title = $("confirm-title");
      title.tabIndex = -1;
      title.focus();
    }
  } else {
    result.className = "panel " + (p.status === "accepted" ? "ok" : "bad");
    $("result-text").textContent = t("pair.result." + p.status);
    show("pair-result");
  }
}

async function startPairing() {
  notice("");
  try {
    const data = await api("POST", "/api/pair/start", {});
    accept(data);
    location.hash = "#/pair";
  } catch (err) { fail(err); }
}

async function decide(acceptIt) {
  const p = state.data && state.data.pairing;
  if (!p || p.status !== "confirm" || state.deciding) return;
  state.deciding = true;
  $("approve").disabled = $("reject").disabled = true;
  try {
    accept(await api("POST", "/api/pair/decision", { offer: p.offer, accept: acceptIt }));
  } catch (err) { fail(err); }
  state.deciding = false;
}

/* ---- state and routing ---- */

function accept(data) {
  state.data = data;
  state.csrf = data.csrf || state.csrf;
  state.skew = data.now - Date.now() / 1000;
  renderMachines(data);
  renderWaiting(data);
  renderPairing(data);
}

async function poll() {
  if (state.stopped) return;
  try {
    accept(await api("GET", "/api/state"));
    if (state.pollFailed) { notice(""); state.pollFailed = false; }
  } catch (err) { fail(err); state.pollFailed = true; }
  if (!state.stopped) setTimeout(poll, POLL_MS);
}

function route() {
  const pair = location.hash === "#/pair";
  $("view-pair").hidden = !pair;
  $("view-machines").hidden = pair;
  const current = pair ? $("tab-pair") : $("tab-machines");
  const other = pair ? $("tab-machines") : $("tab-pair");
  current.setAttribute("aria-current", "page");
  other.removeAttribute("aria-current");
}

async function main() {
  try {
    const res = await fetch("/strings.json", { credentials: "same-origin", cache: "no-store" });
    state.strings = res.ok ? await res.json() : {};
  } catch (_) { state.strings = {}; }
  applyStrings();
  $("start-from-machines").addEventListener("click", startPairing);
  $("lan-agent").addEventListener("click", askLanAgent);
  $("start-pair").addEventListener("click", startPairing);
  $("again").addEventListener("click", startPairing);
  $("approve").addEventListener("click", () => decide(true));
  $("reject").addEventListener("click", () => decide(false));
  $("dismiss").addEventListener("click", () => {
    const p = state.data && state.data.pairing;
    if (p) state.dismissed.add(p.offer);
    renderPairing(state.data);
  });
  window.addEventListener("hashchange", route);
  route();
  setInterval(tickCountdown, 250);
  poll();
}

main();
