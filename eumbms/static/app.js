"use strict";
const RANK = { viewer: 1, operator: 2, tenant_admin: 3, superadmin: 4 };
const ROLE_KO = { viewer: "조회자", operator: "운영자", tenant_admin: "고객사 관리자", superadmin: "플랫폼 관리자" };
const SEV_KO = { info: "정보", minor: "경", major: "중", critical: "중대" };
const KIND_KO = { high: "상한", low: "하한", state: "상태", comm: "통신두절" };
const PROTO_KO = { modbus_tcp: "Modbus TCP", modbus_rtu: "Modbus RTU", simulator: "가상 장치" };
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let me = null, csrf = null, tenantId = null, timer = null, view = "overview";
const S = { sites: [], devices: [], tags: [], gateways: [], templates: [], dash: null };

function store(k, v) { try { v === null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch (_) {} }
function load(k) { try { return sessionStorage.getItem(k); } catch (_) { return null; } }

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.method && opts.method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
  const r = await fetch(path, { credentials: "same-origin", ...opts, headers });
  if (r.status === 401 && !path.endsWith("/login")) { showLogin(); throw new Error("로그인이 필요합니다"); }
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) {
    let msg = body?.detail ?? r.statusText;
    if (Array.isArray(msg)) msg = msg.map(e => (e.loc || []).slice(-1)[0] + ": " + e.msg).join(", ");
    if (r.status === 403 && String(msg).includes("CSRF")) msg = "보안 토큰이 만료되었습니다. 다시 로그인하세요.";
    throw new Error(msg);
  }
  return body;
}
const tq = (sep = "?") => (me?.role === "superadmin" && tenantId ? `${sep}tenant_id=${tenantId}` : "");
const scoped = list => me?.role === "superadmin" && tenantId ? list.filter(x => x.tenant_id === undefined || x.tenant_id === tenantId) : list;

function toast(msg) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 4000); }
function fmtTs(v) { return v ? new Date(v).toLocaleString("ko-KR") : "—"; }
function ago(v) { if (!v) return "—"; const s = (Date.now() - new Date(v)) / 1000; return s < 60 ? `${Math.round(s)}초 전` : s < 3600 ? `${Math.round(s / 60)}분 전` : s < 86400 ? `${Math.round(s / 3600)}시간 전` : fmtTs(v); }
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function cell(tr, text, cls) { const td = el("td", cls, text ?? "—"); tr.appendChild(td); return td; }
function fill(tbody, rows, cols, empty = "데이터 없음") {
  const tb = $(tbody); tb.replaceChildren();
  if (!rows.length) { const tr = tb.insertRow(); const td = cell(tr, empty, "muted"); td.colSpan = 9; return; }
  for (const r of rows) { const tr = tb.insertRow(); for (const c of cols) { const out = c(r, tr); if (out instanceof Node) { const td = el("td"); td.appendChild(out); tr.appendChild(td); } else { const [v, cls] = out; cell(tr, v, cls); } } }
}
function options(sel, rows, label, keepFirst) {
  for (const s of $$(sel)) {
    const prev = s.value, first = keepFirst ? s.firstElementChild : null;
    s.replaceChildren(...(first ? [first] : []));
    for (const r of rows) { const o = el("option", "", label(r)); o.value = r.id; s.appendChild(o); }
    if ([...s.options].some(o => o.value === prev)) s.value = prev;
  }
}
function btn(text, onClick, cls = "small") { const b = el("button", cls, text); b.type = "button"; b.addEventListener("click", onClick); return b; }

function showLogin() { clearInterval(timer); $("#app-view").hidden = true; $("#login-view").hidden = false; }
async function showApp() {
  $("#login-view").hidden = true; $("#app-view").hidden = false;
  $("#whoami").textContent = me.username + " · " + ROLE_KO[me.role];
  for (const e of $$("[data-min-role]")) e.hidden = RANK[me.role] < RANK[e.dataset.minRole];
  for (const e of $$("[data-super-only]")) e.hidden = me.role !== "superadmin";
  const sel = $("#tenant-select");
  if (me.role === "superadmin") {
    const ts = await api("/api/tenants");
    sel.replaceChildren(...ts.map(t => { const o = el("option", "", t.name); o.value = t.id; return o; }));
    if (!ts.length) { const o = el("option", "", "고객사 없음 — 사용자 탭에서 추가"); o.value = ""; sel.appendChild(o); }
    tenantId = ts.some(t => String(t.id) === load("tenant")) ? Number(load("tenant")) : (ts[0]?.id ?? null);
    sel.value = tenantId ?? ""; sel.hidden = false;
  }
  S.templates = await api("/api/device-templates");
  options("#dev-template", S.templates.map(t => ({ id: t.key, name: t.name })), t => t.name, true);
  await refresh(true);
  clearInterval(timer); timer = setInterval(() => refresh().catch(() => {}), 10000);
}

async function refresh(full = false) {
  $("#clock").textContent = "· 갱신 " + new Date().toLocaleTimeString("ko-KR");
  if (me.role === "superadmin" && !tenantId) return;
  const [dash, sites, devices, tags, gws] = await Promise.all([
    api("/api/dashboard" + tq()), api("/api/sites" + tq()), api("/api/devices"), api("/api/tags"), api("/api/gateways")]);
  const siteIds = new Set(sites.map(s => s.id));
  Object.assign(S, { dash, sites, devices: devices.filter(d => siteIds.has(d.site_id)), gateways: gws.filter(g => siteIds.has(g.site_id)) });
  const devIds = new Set(S.devices.map(d => d.id));
  S.tags = tags.filter(t => devIds.has(t.device_id));
  options(".sel-sites", S.sites, s => s.name); options(".sel-devices", S.devices, d => d.name);
  options(".sel-gateways", S.gateways, g => g.name, true); options(".sel-tags", S.tags, t => `${devName(t.device_id)} · ${t.name}`);
  options("#live-site", S.sites, s => s.name, true); options("#dg-site", S.sites, s => s.name);
  if (full || !$("#tr-tags").options.length) options("#tr-tags", S.tags, t => `${devName(t.device_id)} · ${t.name}${t.unit ? " (" + t.unit + ")" : ""}`);
  const badge = $("#alarm-badge"); badge.textContent = dash.totals.active_alarms; badge.hidden = !dash.totals.active_alarms;
  renderOverview();
  await renderView();
}
const devName = id => S.devices.find(d => d.id === id)?.name ?? "—";

function renderOverview() {
  const d = S.dash, t = d.totals;
  const sum = k => { const v = d.sites.map(s => s[k]).filter(x => x !== null); return v.length ? v.reduce((a, b) => a + b, 0) : null; };
  const nf = v => v === null || v === undefined ? "–" : niceNum(v);
  $("#k-power").textContent = nf(sum("power_kw")); $("#k-today").textContent = nf(sum("today_kwh"));
  $("#k-peak").textContent = nf(d.sites.reduce((m, s) => s.peak_kw_today !== null && (m === null || s.peak_kw_today > m) ? s.peak_kw_today : m, null));
  $("#k-online").textContent = `${t.devices_online}/${t.devices}`; $("#k-online-sub").textContent = "통신 정상/전체";
  $("#k-alarms").textContent = t.active_alarms; $("#k-unack").textContent = t.unacked_alarms ? `미확인 ${t.unacked_alarms}` : "";
  $("#kpi-alarm").classList.toggle("alert", t.active_alarms > 0);
  const cards = $("#site-cards"); cards.replaceChildren();
  for (const s of d.sites) {
    const c = el("div", "site-card" + (s.active_alarms ? " sev-" + s.worst_severity : ""));
    c.appendChild(el("h3", "", s.name));
    const dl = el("dl");
    for (const [k, v] of [["전력", s.power_kw === null ? "—" : niceNum(s.power_kw) + " kW"], ["금일", s.today_kwh === null ? "—" : niceNum(s.today_kwh) + " kWh"],
      ["최대", s.peak_kw_today === null ? "—" : niceNum(s.peak_kw_today) + " kW"], ["장치", `${s.devices_online}/${s.devices_total} 통신`], ["경보", s.active_alarms ? `${s.active_alarms}건 (${SEV_KO[s.worst_severity]})` : "없음"]]) {
      dl.appendChild(el("dt", "", k)); dl.appendChild(el("dd", "", v));
    }
    c.appendChild(dl);
    c.appendChild(btn("결선도", () => { $("#dg-site").value = s.id; go("diagram"); }, "small ghost"));
    cards.appendChild(c);
  }
  $("#empty-hint").hidden = d.sites.some(s => s.devices_total > 0);
  const ul = $("#recent-alarms"); ul.replaceChildren();
  if (!d.recent_alarms.length) ul.appendChild(el("li", "muted", "활성 경보 없음"));
  for (const a of d.recent_alarms) {
    const li = el("li", "sev-" + a.severity);
    li.appendChild(el("span", "sev", SEV_KO[a.severity])); li.appendChild(el("span", "", a.message)); li.appendChild(el("small", "muted", ago(a.raised_at)));
    ul.appendChild(li);
  }
  // live values grouped by device
  const siteF = Number($("#live-site").value) || null;
  const box = $("#live-groups"); box.replaceChildren();
  const online = Object.fromEntries(d.devices.map(x => [x.id, x]));
  for (const dev of S.devices.filter(x => !siteF || x.site_id === siteF)) {
    const tags = S.tags.filter(t => t.device_id === dev.id);
    const g = el("div", "live-group");
    const h = el("div", "live-head"); h.appendChild(el("strong", "", dev.name));
    const st = online[dev.id]; h.appendChild(el("span", "pill " + (!dev.enabled ? "off" : st?.online ? "ok" : "bad"), !dev.enabled ? "수집 중지" : st?.online ? "통신 정상" : "통신 두절"));
    g.appendChild(h);
    if (!tags.length) g.appendChild(el("p", "muted", "태그 없음"));
    const grid = el("div", "live-grid");
    for (const t of tags) {
      const c = el("div", "live-cell q-" + t.quality);
      c.appendChild(el("span", "", t.name));
      const v = t.category === "state" && t.value !== null ? (t.value >= 0.5 ? "투입" : "개방") : (t.value === null ? "—" : niceNum(t.value));
      const b = el("b", "", v); c.appendChild(b); c.appendChild(el("small", "", t.unit || ""));
      c.title = `품질 ${t.quality} · ${fmtTs(t.source_ts)}`;
      grid.appendChild(c);
    }
    g.appendChild(grid); box.appendChild(g);
  }
}

async function renderView() {
  if (view === "diagram") return renderDiagramView();
  if (view === "alarms") return renderAlarms();
  if (view === "devices") return renderDevices();
  if (view === "sites") return renderSites();
  if (view === "users" && RANK[me.role] >= RANK.tenant_admin) {
    fill("#t-users", await api("/api/users" + tq()), [u => [u.id], u => [u.username], u => [ROLE_KO[u.role]], u => [u.tenant_id], u => [u.active ? "활성" : "중지"], u => [fmtTs(u.last_login_at)]]);
    if (me.role === "superadmin") fill("#t-tenants", await api("/api/tenants"), [t => [t.id], t => [t.name]]);
  }
  if (view === "audit" && RANK[me.role] >= RANK.tenant_admin)
    fill("#t-audit", await api("/api/audit" + tq()), [a => [fmtTs(a.created_at)], a => [a.actor], a => [a.action], a => [a.target], a => [a.ip]]);
}

let dgEditing = false;
async function renderDiagramView() {
  if (dgEditing) return;
  const sid = Number($("#dg-site").value);
  if (!sid) { $("#dg-view").replaceChildren(el("p", "muted", "사업장이 없습니다")); return; }
  const dg = await api(`/api/diagrams/${sid}`);
  const tags = Object.fromEntries(S.tags.map(t => [t.id, t]));
  renderDiagram($("#dg-view"), dg.spec, tags);
  $("#dg-view")._spec = dg.spec;
}

async function renderAlarms() {
  const rows = await api(`/api/alarms?state=${$("#al-state").value}` + tq("&"));
  const canAck = RANK[me.role] >= RANK.operator;
  fill("#t-alarms", rows, [a => [SEV_KO[a.severity], "sev-cell sev-" + a.severity], a => [fmtTs(a.raised_at)], a => [a.message],
    a => [a.value === null ? "—" : niceNum(a.value)], a => [a.active ? "발생 중" : fmtTs(a.cleared_at), a.active ? "bad-text" : ""],
    a => a.acked_at ? [`${a.acked_by} · ${ago(a.acked_at)}`] : canAck ? btn("확인", async () => { try { await api(`/api/alarms/${a.id}/ack`, { method: "POST" }); refresh(); } catch (e) { toast(e.message); } }) : ["미확인"]],
    "경보가 없습니다");
  const rules = await api("/api/alarm-rules");
  const tagIds = new Set(S.tags.map(t => t.id));
  const tagName = id => { const t = S.tags.find(x => x.id === id); return t ? `${devName(t.device_id)} · ${t.name}` : id; };
  fill("#t-rules", rules.filter(r => tagIds.has(r.tag_id)), [r => [tagName(r.tag_id)], r => [KIND_KO[r.kind]], r => [r.kind === "comm" ? "—" : r.threshold], r => [r.deadband], r => [SEV_KO[r.severity]], r => [r.message || "—"],
    r => canAck ? btn("삭제", async () => { if (!confirm("이 경보 설정을 삭제할까요?")) return; await api(`/api/alarm-rules/${r.id}`, { method: "DELETE" }); refresh(); }, "small ghost") : [""]], "경보 설정 없음");
}

function renderDevices() {
  const online = Object.fromEntries((S.dash?.devices || []).map(x => [x.id, x]));
  const canEdit = RANK[me.role] >= RANK.operator;
  fill("#t-devices", S.devices, [d => [d.id], d => [d.name], d => [PROTO_KO[d.protocol]],
    d => [d.protocol === "modbus_tcp" ? `${d.host}:${d.port} / 국번 ${d.unit_id}` : d.protocol === "modbus_rtu" ? `${d.serial_port} ${d.baudrate}bps / 국번 ${d.unit_id}` : "—"],
    d => [S.gateways.find(g => g.id === d.gateway_id)?.name ?? (d.protocol === "simulator" ? "서버" : "미지정")],
    d => [!d.enabled ? "—" : online[d.id]?.online ? "정상" : "두절", online[d.id]?.online ? "ok-text" : d.enabled ? "bad-text" : ""],
    d => canEdit ? btn(d.enabled ? "수집 중지" : "수집 시작", async () => { try { await api(`/api/devices/${d.id}`, { method: "PATCH", body: JSON.stringify({ enabled: !d.enabled }) }); refresh(); } catch (e) { toast(e.message); } }, "small ghost") : [d.enabled ? "사용" : "중지"]]);
  fill("#t-tags", S.tags, [t => [t.id], t => [t.name], t => [devName(t.device_id)], t => [`FC${t.function_code} / ${t.address}`],
    t => [`${t.data_type} ${t.word_order[0].toUpperCase()}${t.byte_order[0].toUpperCase()}`], t => [t.value === null ? "—" : `${niceNum(t.value)} ${t.unit}`], t => [t.quality, "q-" + t.quality], t => [ago(t.source_ts)]]);
}
function renderSites() {
  fill("#t-sites", S.sites, [s => [s.id], s => [s.name], s => [s.address]]);
  fill("#t-gateways", S.gateways, [g => [g.id], g => [g.name], g => [S.sites.find(s => s.id === g.site_id)?.name], g => [ago(g.last_seen_at)], g => [g.agent_version || "—"]]);
}

async function loadTrend() {
  const ids = [...$("#tr-tags").selectedOptions].map(o => o.value).slice(0, 4);
  if (!ids.length) { toast("태그를 선택하세요"); return; }
  const hours = $("#tr-hours").value;
  const r = await api(`/api/trend?tag_ids=${ids.join(",")}&hours=${hours}&points=240`);
  lineChart($("#tr-chart"), r.series, r);
  $("#tr-info").textContent = `구간 ${Math.round(r.bucket_seconds)}초 평균 · 원본 ${r.series.map(s => s.raw_count).reduce((a, b) => a + b, 0).toLocaleString()}건 · 음영은 구간 최소~최대`;
}

const NUM = ["site_id", "device_id", "gateway_id", "tag_id", "port", "unit_id", "address", "scale", "offset", "function_code", "baudrate", "poll_interval_s", "threshold", "deadband"];
function formData(form) {
  const o = Object.fromEntries(new FormData(form));
  for (const k of NUM) if (k in o) o[k] = o[k] === "" ? null : Number(o[k]);
  for (const k of ["template", "host", "serial_port", "message"]) if (o[k] === "") delete o[k];
  if (me.role === "superadmin" && tenantId) o.tenant_id = tenantId;
  return o;
}
function bind(id, path, after, method = "POST") {
  $(id).addEventListener("submit", async e => {
    e.preventDefault();
    try { const res = await api(typeof path === "function" ? path() : path, { method, body: JSON.stringify(formData(e.target)) }); e.target.reset(); after?.(res); toast("저장되었습니다"); await refresh(true); }
    catch (err) { toast(err.message); }
  });
}
function go(v) {
  view = v;
  for (const x of $$("#nav button")) x.classList.toggle("active", x.dataset.view === v);
  for (const p of $$("[data-panel]")) p.hidden = p.dataset.panel !== v;
  renderView().catch(e => toast(e.message));
  if (v === "trend" && $("#tr-tags").selectedOptions.length) loadTrend().catch(e => toast(e.message));
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
  $("#nav").addEventListener("click", e => { const b = e.target.closest("button[data-view]"); if (b) go(b.dataset.view); });
  $("#tenant-select").addEventListener("change", e => { tenantId = Number(e.target.value) || null; store("tenant", String(tenantId)); refresh(true).catch(err => toast(err.message)); });
  $("#live-site").addEventListener("change", renderOverview);
  $("#dg-site").addEventListener("change", () => renderDiagramView().catch(e => toast(e.message)));
  $("#al-state").addEventListener("change", () => renderAlarms().catch(e => toast(e.message)));
  $("#tr-load").addEventListener("click", () => loadTrend().catch(e => toast(e.message)));
  $("#tr-csv").addEventListener("click", () => {
    const id = $("#tr-tags").selectedOptions[0]?.value; if (!id) { toast("태그를 선택하세요"); return; }
    location.href = `/api/tags/${id}/export.csv?hours=${$("#tr-hours").value}`;
  });
  $("#btn-demo").addEventListener("click", async () => {
    try { const r = await api("/api/demo", { method: "POST", body: JSON.stringify(me.role === "superadmin" ? { tenant_id: tenantId } : {}) }); toast(`${r.name}를 만들었습니다`); await refresh(true); }
    catch (e) { toast(e.message); }
  });
  $("#dev-template").addEventListener("change", e => {
    const t = S.templates.find(x => x.key === e.target.value);
    $("#tpl-note").textContent = t ? `${t.name} — ${t.notes} · 생성 태그: ${t.tags.join(", ")} · 검증 상태: ${t.verification}` : "";
    if (t) { $("#dev-protocol").value = t.protocol; const f = $("#f-device"); if (t.baudrate) f.baudrate.value = t.baudrate; if (t.unit_id) f.unit_id.value = t.unit_id; if (t.protocol === "simulator") f.poll_interval_s.value = 60; }
  });
  // diagram editor
  $("#dg-edit").addEventListener("click", () => { dgEditing = true; $("#dg-json").value = JSON.stringify($("#dg-view")._spec || { elements: [], lines: [] }, null, 1); $("#dg-editor").hidden = false; });
  $("#dg-cancel").addEventListener("click", () => { dgEditing = false; $("#dg-editor").hidden = true; renderDiagramView(); });
  $("#dg-tags").addEventListener("click", () => { const p = $("#dg-taglist"); p.textContent = S.tags.map(t => `${t.id}\t${devName(t.device_id)} · ${t.name} (${t.category})`).join("\n"); p.hidden = !p.hidden; });
  $("#dg-save").addEventListener("click", async () => {
    let spec; try { spec = JSON.parse($("#dg-json").value); } catch (e) { toast("JSON 형식 오류: " + e.message); return; }
    try { await api(`/api/diagrams/${$("#dg-site").value}`, { method: "PUT", body: JSON.stringify({ spec }) }); dgEditing = false; $("#dg-editor").hidden = true; toast("결선도를 저장했습니다"); renderDiagramView(); }
    catch (e) { toast(e.message); }
  });
  bind("#f-site", "/api/sites"); bind("#f-device", "/api/devices"); bind("#f-tag", "/api/tags"); bind("#f-user", "/api/users"); bind("#f-rule", "/api/alarm-rules");
  bind("#f-tenant", "/api/tenants", () => showApp());
  bind("#f-gateway", "/api/gateways", res => { const p = $("#gw-token"); p.textContent = `게이트웨이 ID ${res.id}\nEUM_GATEWAY_TOKEN=${res.token}\n\n${res.note}`; p.hidden = false; });
  $("#f-password").addEventListener("submit", async e => {
    e.preventDefault();
    try { await api("/api/auth/password", { method: "POST", body: JSON.stringify(Object.fromEntries(new FormData(e.target))) }); e.target.reset(); toast("비밀번호를 변경했습니다. 다른 기기의 로그인은 해제됩니다."); }
    catch (err) { toast(err.message === "Current password is incorrect" ? "현재 비밀번호가 올바르지 않습니다" : err.message); }
  });
  window.addEventListener("resize", () => { if (view === "trend" && $("#tr-chart").childElementCount) loadTrend().catch(() => {}); });
  csrf = load("csrf");
  try { me = await api("/api/auth/me"); await showApp(); } catch (_) { showLogin(); }
});
