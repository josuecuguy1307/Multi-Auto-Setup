/* Owner's screen. No framework, no remote resource. Text from the world (machine names) is only ever put into the page with textContent. All copy lives in strings.es.json. */
"use strict";

const POLL_MS = 1500;
const state = { strings: {}, data: null, skew: 0, csrf: "", dismissed: new Set(), deciding: false, confirmShownFor: null, stopped: false };
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

function kv(obj) { return Object.entries(obj || {}).map(([k, v]) => k + ": " + (typeof v === "object" ? JSON.stringify(v) : String(v))); }

function renderAnalysis(panel, a) {
  const ai = a.ai || {};
  const prog = a.programs || {};
  const aiRows = [];
  (ai.agents || []).forEach((x) => aiRows.push([x.name, x.state, x.category].filter(Boolean).join(" · ")));
  (ai.model_servers || []).forEach((x) => aiRows.push([x.kind || x.name, x.version, x.port].filter(Boolean).join(" · ")));
  const fit = ai.largest_model_that_fits;
  if (fit) aiRows.push(t("node.analysis.fit", { p: fit.params_b, q: fit.quant, l: fit.limited_by }));
  const names = (ai.api_key_variable_names || []).map((x) => x.name);
  if (names.length) aiRows.push(t("node.analysis.keys", { names: names.join(", ") }));
  const progRows = kv({ tools_found: prog.tools_found, ...(prog.by_category || {}) });
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

async function runTask(id, type) {
  notice("");
  try { accept(await api("POST", "/api/task/run", { node: id, type })); } catch (err) { fail(err); }
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
  const key = JSON.stringify(data.machines.map((m) => [m.id, m.name, m.os, m.arch, m.status, m.fingerprint, m.report_at, m.analysis_at, m.alerts, running.has(m.id)]));
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
