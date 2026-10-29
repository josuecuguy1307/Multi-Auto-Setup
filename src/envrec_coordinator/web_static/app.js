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

function renderMachines(data) {
  const list = $("machines");
  const online = data.machines.filter((m) => m.status === "online").length;
  $("machines-count").textContent = data.machines.length ? t("machines.count", { n: data.machines.length, online }) : "";
  $("machines-empty").hidden = data.machines.length > 0;
  const items = data.machines.map((m) => {
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
    fact(dl, "machines.field.heartbeat", ago(m.last_heartbeat === null ? null : data.now - m.last_heartbeat), false);
    fact(dl, "machines.field.fingerprint", m.fingerprint, true);
    li.append(head, dl);
    return li;
  });
  list.replaceChildren(...items);
  const p = data.pairing;
  $("pairing-banner").hidden = !(p && ["waiting", "exchange", "confirm", "deciding"].includes(p.status));
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
  if (!p || (["accepted", "refused", "failed", "expired", "rejected"].includes(p.status) && state.dismissed.has(p.offer))) {
    show("pair-idle");
    return;
  }
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
