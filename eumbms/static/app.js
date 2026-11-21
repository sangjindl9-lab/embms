"use strict";
const RANK = { viewer: 1, operator: 2, tenant_admin: 3, superadmin: 4 };
const ROLE_KO = { viewer: "조회자", operator: "운영자", tenant_admin: "고객사 관리자", superadmin: "플랫폼 관리자" };
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let me = null, csrf = null, tenantId = null, timer = null;

function store(k, v) { try { v === null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch (_) {} }
function load(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.method && opts.method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
  const r = await fetch(path, { credentials: "same-origin", ...opts, headers });
  if (r.status === 401) { showLogin(); throw new Error("로그인이 필요합니다"); }
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) {
    let msg = body?.detail ?? r.statusText;
    if (Array.isArray(msg)) msg = msg.map(e => (e.loc || []).slice(-1)[0] + ": " + e.msg).join(", ");
    if (r.status === 403 && String(msg).includes("CSRF")) msg = "보안 토큰이 만료되었습니다. 다시 로그인하세요.";
    throw new Error(msg);
  }
  return body;
}

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 4000); }
function fmtTs(v) { return v ? new Date(v).toLocaleString("ko-KR") : "—"; }
function cell(tr, text, cls) { const td = document.createElement("td"); td.textContent = text ?? "—"; if (cls) td.className = cls; tr.appendChild(td); return td; }
function fill(tbody, rows, cols) {
  const tb = $(tbody); tb.replaceChildren();
  if (!rows.length) { const tr = tb.insertRow(); const td = cell(tr, "데이터 없음", "muted"); td.colSpan = 9; return; }
  for (const r of rows) { const tr = tb.insertRow(); for (const c of cols) { const [v, cls] = c(r); cell(tr, v, cls); } }
}
function options(sel, rows, label, keepFirst) {
  for (const el of $$(sel)) {
    const first = keepFirst ? el.firstElementChild : null;
    el.replaceChildren(...(first ? [first] : []));
    for (const r of rows) { const o = document.createElement("option"); o.value = r.id; o.textContent = label(r); el.appendChild(o); }
  }
}
const tq = () => (me?.role === "superadmin" && tenantId ? "?tenant_id=" + tenantId : "");

function showLogin() { clearInterval(timer); $("#app-view").hidden = true; $("#login-view").hidden = false; }
async function showApp() {
  $("#login-view").hidden = true; $("#app-view").hidden = false;
  $("#whoami").textContent = me.username + " · " + ROLE_KO[me.role];
  for (const el of $$("[data-min-role]")) el.hidden = RANK[me.role] < RANK[el.dataset.minRole];
  for (const el of $$("[data-super-only]")) el.hidden = me.role !== "superadmin";
  const sel = $("#tenant-select");
  if (me.role === "superadmin") {
    const ts = await api("/api/tenants");
    sel.replaceChildren(...ts.map(t => { const o = document.createElement("option"); o.value = t.id; o.textContent = t.name; return o; }));
    if (!ts.length) { const o = document.createElement("option"); o.textContent = "고객사 없음 — 사용자 탭에서 추가"; o.value = ""; sel.appendChild(o); }
    tenantId = ts.some(t => String(t.id) === load("tenant")) ? Number(load("tenant")) : (ts[0]?.id ?? null);
    sel.value = tenantId ?? ""; sel.hidden = false;
  }
  await refresh(); clearInterval(timer); timer = setInterval(() => refresh().catch(() => {}), 10000);
}

async function refresh() {
  if (me.role === "superadmin" && !tenantId) return;
  const [sites, devices, tags, gws] = await Promise.all(["/api/sites", "/api/devices", "/api/tags", "/api/gateways"].map(p => api(p + tq())));
  const dev = Object.fromEntries(devices.map(d => [d.id, d])), site = Object.fromEntries(sites.map(s => [s.id, s]));
  const gw = Object.fromEntries(gws.map(g => [g.id, g]));
  $("#k-sites").textContent = sites.length; $("#k-devices").textContent = devices.length; $("#k-tags").textContent = tags.length;
  $("#k-good").textContent = tags.filter(t => t.quality === "Good").length; $("#k-gw").textContent = gws.length;
  fill("#t-live", tags, [t => [t.name], t => [dev[t.device_id]?.name], t => [t.value], t => [t.unit], t => [t.quality, "q-" + t.quality], t => [fmtTs(t.source_ts)]]);
  fill("#t-sites", sites, [s => [s.id], s => [s.name], s => [s.address]]);
  fill("#t-devices", devices, [d => [d.id], d => [d.name], d => [d.protocol === "modbus_tcp" ? "Modbus TCP" : "Modbus RTU"],
    d => [d.protocol === "modbus_tcp" ? `${d.host}:${d.port} / u${d.unit_id}` : `${d.serial_port} ${d.baudrate} / u${d.unit_id}`],
    d => [gw[d.gateway_id]?.name ?? "미지정"], d => [d.enabled ? "사용" : "중지"]]);
  fill("#t-tags", tags, [t => [t.id], t => [t.name], t => ["FC" + t.function_code], t => [t.address],
    t => [`${t.data_type} ${t.word_order[0].toUpperCase()}${t.byte_order[0].toUpperCase()}`], t => [t.value], t => [t.quality, "q-" + t.quality]]);
  fill("#t-gateways", gws, [g => [g.id], g => [g.name], g => [site[g.site_id]?.name], g => [fmtTs(g.last_seen_at)], g => [g.agent_version || "—"]]);
  options(".sel-sites", sites, s => s.name); options(".sel-devices", devices, d => d.name); options(".sel-gateways", gws, g => g.name, true);
  if (RANK[me.role] >= RANK.tenant_admin) {
    const [users, audit] = await Promise.all([api("/api/users" + tq()), api("/api/audit" + tq())]);
    fill("#t-users", users, [u => [u.id], u => [u.username], u => [ROLE_KO[u.role]], u => [u.tenant_id], u => [u.active ? "활성" : "중지"], u => [fmtTs(u.last_login_at)]]);
    fill("#t-audit", audit, [a => [fmtTs(a.created_at)], a => [a.actor], a => [a.action], a => [a.target], a => [a.ip]]);
  }
  if (me.role === "superadmin") fill("#t-tenants", await api("/api/tenants"), [t => [t.id], t => [t.name]]);
}

const NUM = ["site_id", "device_id", "gateway_id", "port", "unit_id", "address", "scale", "offset", "function_code"];
function formData(form) {
  const o = Object.fromEntries(new FormData(form));
  for (const k of NUM) if (k in o) o[k] = o[k] === "" ? null : Number(o[k]);
  if (me.role === "superadmin" && tenantId) o.tenant_id = tenantId;
  return o;
}
function bind(id, path, after) {
  $(id).addEventListener("submit", async e => {
    e.preventDefault();
    try { const res = await api(path, { method: "POST", body: JSON.stringify(formData(e.target)) }); e.target.reset(); after?.(res); toast("저장되었습니다"); await refresh(); }
    catch (err) { toast(err.message); }
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#login-form").addEventListener("submit", async e => {
    e.preventDefault(); $("#login-error").textContent = "";
    try {
      const res = await api("/api/auth/login", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(e.target))) });
      me = res.user; csrf = res.csrf_token; store("csrf", csrf); e.target.reset(); await showApp();
    } catch (err) { $("#login-error").textContent = err.message === "Invalid username or password" ? "아이디 또는 비밀번호가 올바르지 않습니다" : err.message; }
  });
  $("#logout").addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {} store("csrf", null); me = null; showLogin(); });
  $("#nav").addEventListener("click", e => {
    const b = e.target.closest("button[data-view]"); if (!b) return;
    for (const x of $$("#nav button")) x.classList.toggle("active", x === b);
    for (const p of $$("[data-panel]")) p.hidden = p.dataset.panel !== b.dataset.view;
  });
  $("#tenant-select").addEventListener("change", e => { tenantId = Number(e.target.value) || null; store("tenant", String(tenantId)); refresh().catch(err => toast(err.message)); });
  bind("#f-site", "/api/sites"); bind("#f-device", "/api/devices"); bind("#f-tag", "/api/tags"); bind("#f-user", "/api/users");
  bind("#f-tenant", "/api/tenants", () => showApp());
  bind("#f-gateway", "/api/gateways", res => { const p = $("#gw-token"); p.textContent = `게이트웨이 ID ${res.id}\nEUM_GATEWAY_TOKEN=${res.token}\n\n${res.note}`; p.hidden = false; });
  csrf = load("csrf");
  try { me = await api("/api/auth/me"); await showApp(); } catch (_) { showLogin(); }
});
