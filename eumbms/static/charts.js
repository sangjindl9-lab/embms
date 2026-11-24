"use strict";
/* Dependency-free SVG charts and single-line diagram renderer (CSP-safe: no inline code). */
const SVGNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, parent) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function svgText(parent, x, y, text, cls, anchor = "start") {
  const t = svgEl("text", { x, y, class: cls || "", "text-anchor": anchor }, parent);
  t.textContent = text;
  return t;
}
function niceNum(v) {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const a = Math.abs(v);
  return a >= 1000 ? v.toLocaleString("ko-KR", { maximumFractionDigits: 0 }) : a >= 100 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
}

/** Multi-series time chart. series: [{name, unit, points:[{t, avg, min, max}]}] */
function lineChart(container, series, { start, end } = {}) {
  container.replaceChildren();
  const W = Math.max(container.clientWidth || 800, 320), H = 340, m = { l: 56, r: 16, t: 16, b: 34 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart-svg", role: "img", "aria-label": "트렌드 차트" }, container);
  const all = series.flatMap(s => s.points);
  if (!all.length) { svgText(svg, W / 2, H / 2, "선택한 기간에 데이터가 없습니다", "axis", "middle"); return; }
  const t0 = start ? new Date(start).getTime() : Math.min(...all.map(p => +new Date(p.t)));
  const t1 = end ? new Date(end).getTime() : Math.max(...all.map(p => +new Date(p.t)));
  // one shared axis when units match, otherwise normalise each series to its own range
  const units = [...new Set(series.map(s => s.unit))];
  const shared = units.length === 1;
  const rng = s => { const lo = Math.min(...s.points.map(p => p.min)), hi = Math.max(...s.points.map(p => p.max)); const pad = (hi - lo) * 0.08 || Math.abs(hi) * 0.05 || 1; return [lo - pad, hi + pad]; };
  const globalR = shared ? rng({ points: all }) : [0, 1];
  const X = t => m.l + (t - t0) / Math.max(t1 - t0, 1) * (W - m.l - m.r);
  const mkY = r => v => m.t + (1 - (v - r[0]) / (r[1] - r[0])) * (H - m.t - m.b);
  // grid + axes
  const Yg = mkY(globalR);
  for (let i = 0; i <= 4; i++) {
    const y = m.t + i * (H - m.t - m.b) / 4;
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: "grid" }, svg);
    if (shared) svgText(svg, m.l - 6, y + 4, niceNum(globalR[1] - i * (globalR[1] - globalR[0]) / 4), "axis", "end");
  }
  if (!shared) svgText(svg, m.l - 6, m.t + 10, "정규화", "axis", "end");
  const spanH = (t1 - t0) / 3.6e6;
  for (let i = 0; i <= 5; i++) {
    const t = t0 + i * (t1 - t0) / 5, d = new Date(t);
    const lab = spanH > 48 ? `${d.getMonth() + 1}/${d.getDate()}` : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    svgText(svg, X(t), H - 12, lab, "axis", "middle");
  }
  series.forEach((s, i) => {
    if (!s.points.length) return;
    const Y = shared ? Yg : mkY(rng(s));
    const band = s.points.map(p => `${X(+new Date(p.t))},${Y(p.max)}`).concat([...s.points].reverse().map(p => `${X(+new Date(p.t))},${Y(p.min)}`));
    svgEl("polygon", { points: band.join(" "), class: `band s${i}` }, svg);
    svgEl("polyline", { points: s.points.map(p => `${X(+new Date(p.t))},${Y(p.avg)}`).join(" "), class: `line s${i}` }, svg);
  });
  // hover readout
  const cursor = svgEl("line", { y1: m.t, y2: H - m.b, class: "cursor", visibility: "hidden" }, svg);
  const tip = document.createElement("div"); tip.className = "chart-tip"; tip.hidden = true; container.appendChild(tip);
  svg.addEventListener("pointermove", e => {
    const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) * W / r.width;
    const t = t0 + (x - m.l) / (W - m.l - m.r) * (t1 - t0);
    if (x < m.l || x > W - m.r) { cursor.setAttribute("visibility", "hidden"); tip.hidden = true; return; }
    cursor.setAttribute("x1", x); cursor.setAttribute("x2", x); cursor.setAttribute("visibility", "visible");
    tip.replaceChildren();
    const head = document.createElement("div"); head.textContent = new Date(t).toLocaleString("ko-KR"); tip.appendChild(head);
    series.forEach((s, i) => {
      if (!s.points.length) return;
      const p = s.points.reduce((a, b) => Math.abs(+new Date(b.t) - t) < Math.abs(+new Date(a.t) - t) ? b : a);
      const row = document.createElement("div"); row.className = `tip-row s${i}`;
      row.textContent = `${s.name}: ${niceNum(p.avg)} ${s.unit || ""} (최소 ${niceNum(p.min)} / 최대 ${niceNum(p.max)})`;
      tip.appendChild(row);
    });
    tip.hidden = false;
    tip.style.left = Math.min(e.clientX - r.left + 12, r.width - 260) + "px";
  });
  svg.addEventListener("pointerleave", () => { cursor.setAttribute("visibility", "hidden"); tip.hidden = true; });
  const legend = document.createElement("div"); legend.className = "legend";
  series.forEach((s, i) => { const l = document.createElement("span"); l.className = `s${i}`; l.textContent = `${s.name}${s.unit ? " (" + s.unit + ")" : ""}`; legend.appendChild(l); });
  container.appendChild(legend);
}

/** Single-line diagram. tags: {id: {value, unit, quality, name}} */
function renderDiagram(container, spec, tags) {
  container.replaceChildren();
  const els = spec.elements || [], lines = spec.lines || [];
  if (!els.length) { const p = document.createElement("p"); p.className = "muted"; p.textContent = "이 사업장에는 결선도가 없습니다. 편집에서 만들 수 있습니다."; container.appendChild(p); return; }
  const xs = els.flatMap(e => [e.x, e.x + (e.w || 0)]).concat(lines.flatMap(l => l.points.map(p => p[0])));
  const ys = els.map(e => e.y).concat(lines.flatMap(l => l.points.map(p => p[1])));
  const minX = Math.min(...xs) - 120, maxX = Math.max(...xs) + 160, minY = Math.min(...ys) - 40, maxY = Math.max(...ys) + 60;
  const svg = svgEl("svg", { viewBox: `${minX} ${minY} ${maxX - minX} ${maxY - minY}`, class: "sld", role: "img", "aria-label": "단선결선도" }, container);
  for (const l of lines) svgEl("polyline", { points: l.points.map(p => p.join(",")).join(" "), class: "wire" }, svg);
  const val = id => { const t = tags[id]; if (!t) return null; return t; };
  for (const e of els) {
    const g = svgEl("g", { class: `el el-${e.type}` }, svg);
    const t = e.tag_id ? val(e.tag_id) : null;
    const bad = t && t.quality !== "Good";
    if (e.type === "bus") {
      svgEl("rect", { x: e.x, y: e.y - 4, width: e.w || 400, height: 8, class: "bus" + (bad ? " bad" : "") }, g);
      svgText(g, e.x, e.y - 12, e.label, "lbl");
      if (t) svgText(g, e.x + (e.w || 400), e.y - 12, `${niceNum(t.value)} ${t.unit}`, "val" + (bad ? " bad" : ""), "end");
      continue;
    }
    if (e.type === "label") { svgText(g, e.x, e.y, e.label, "lbl big"); continue; }
    if (e.type === "breaker") {
      const st = e.state_tag_id ? val(e.state_tag_id) : null;
      const cls = !st ? "unknown" : st.quality !== "Good" ? "bad" : st.value >= 0.5 ? "closed" : "open";
      svgEl("rect", { x: e.x - 16, y: e.y - 20, width: 32, height: 40, rx: 3, class: `brk ${cls}` }, g);
      svgText(g, e.x + 26, e.y - 4, e.label, "lbl");
      svgText(g, e.x + 26, e.y + 12, { closed: "투입", open: "개방", bad: "통신불량", unknown: "상태 미연결" }[cls], `st ${cls}`);
      continue;
    }
    if (e.type === "transformer") {
      svgEl("circle", { cx: e.x, cy: e.y - 12, r: 18, class: "tr" }, g);
      svgEl("circle", { cx: e.x, cy: e.y + 12, r: 18, class: "tr" }, g);
      svgText(g, e.x + 28, e.y - 4, e.label, "lbl");
    } else if (e.type === "meter") {
      svgEl("circle", { cx: e.x, cy: e.y, r: 18, class: "mtr" }, g);
      svgText(g, e.x, e.y + 5, "M", "sym", "middle");
      svgText(g, e.x + 28, e.y - 4, e.label, "lbl");
    } else if (e.type === "generator") {
      svgEl("circle", { cx: e.x, cy: e.y, r: 20, class: "mtr" }, g);
      svgText(g, e.x, e.y + 5, "G", "sym", "middle");
      svgText(g, e.x + 30, e.y - 4, e.label, "lbl");
    } else if (e.type === "load") {
      svgEl("polygon", { points: `${e.x - 16},${e.y - 12} ${e.x + 16},${e.y - 12} ${e.x},${e.y + 14}`, class: "load" }, g);
      svgText(g, e.x, e.y + 34, e.label, "lbl", "middle");
    }
    if (t) svgText(g, e.type === "load" ? e.x : e.x + 28, e.type === "load" ? e.y + 52 : e.y + 14, `${niceNum(t.value)} ${t.unit}`, "val" + (bad ? " bad" : ""), e.type === "load" ? "middle" : "start");
  }
}
