/* 水下声呐测线覆盖复核 —— 前端逻辑（原生 JS，无依赖） */
"use strict";

const $ = (sel) => document.querySelector(sel);

const inputEl = $("#input-json");
const statusEl = $("#input-status");
const errorEl = $("#error-banner");
const summaryEl = $("#summary");
const filterBarEl = $("#filter-bar");
const issueListEl = $("#issue-list");
const lineStatsEl = $("#line-stats");
const exportBtn = $("#btn-export");
const canvas = $("#map");
const ctx = canvas.getContext("2d");

const TYPE_LABEL = { gap: "未覆盖缺口", outside: "边界外扫掠", overlap: "过度重叠" };
const TYPE_COLOR = { gap: "#e5484d", outside: "#f5a524", overlap: "#a371f7" };
const SEV_LABEL = { high: "高", medium: "中", low: "低" };
const REASON_LABEL = {
  time_reversal: "时间倒退", seq_duplicate: "序号重复", seq_jump: "序号跳变",
  gap_exceeded: "间隔超限", speed_exceeded: "速度超限", offset_exceeded: "偏离超限",
};
const CT_COLOR = "#22d3ee";
const CL_COLOR = "#fde047";
const CL_STATUS_LABEL = { ok: "闭合正常", anomaly: "闭合异常", indeterminate: "无冗余不可判" };
const CL_SKIP_LABEL = {
  overlapping: "重叠线",
  no_bracketing_samples: "缺少有效包围样本",
  unknown_line: "未知测线",
};

let lastResult = null;   // 最近一次成功复核的响应
let lastInput = null;    // 最近一次成功复核的输入（用于绘制边界/测线）
let typeFilter = "all";
let selectedIssueId = null;
let analyzeTimer = null;
let viewInitialized = false; // 仅在首次成功复核时自动取景，之后保持用户视角

/* 采集连续性状态 */
let ctResult = null;        // 最近一次成功的连续性检查响应
let ctLineFilter = "all";
let ctReasonFilter = "all";
let selectedEventId = null;
let ctTimer = null;

/* 交叉点水深闭合状态 */
let clResult = null;
let clStatusFilter = "all";   // all / ok / anomaly / indeterminate / skipped
let clLineFilter = "all";
let selectedClosureId = null; // XPT-### 或 SKP-###
let clTimer = null;

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- 视图变换（世界坐标 <-> 屏幕坐标） ---------------- */
const view = { cx: 0, cy: 0, s: 1 };

function worldToScreen(wx, wy) {
  return [(wx - view.cx) * view.s + canvas.width / 2,
          canvas.height / 2 - (wy - view.cy) * view.s];
}
function screenToWorld(sx, sy) {
  return [(sx - canvas.width / 2) / view.s + view.cx,
          (canvas.height / 2 - sy) / view.s + view.cy];
}

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
}

function fitBounds(bbox) {
  if (!bbox) return;
  const [minx, miny, maxx, maxy] = bbox;
  const bw = Math.max(maxx - minx, 1e-9), bh = Math.max(maxy - miny, 1e-9);
  view.s = Math.min(canvas.width / (bw * 1.15), canvas.height / (bh * 1.15));
  view.cx = (minx + maxx) / 2;
  view.cy = (miny + maxy) / 2;
}

/* ---------------- GeoJSON 工具 ---------------- */
function eachRing(geom, cb) {
  if (!geom) return;
  const polys = geom.type === "Polygon" ? [geom.coordinates]
    : geom.type === "MultiPolygon" ? geom.coordinates : [];
  for (const poly of polys) for (const ring of poly) cb(ring);
}

function geomBBox(geom, bbox) {
  eachRing(geom, (ring) => {
    for (const [x, y] of ring) {
      bbox[0] = Math.min(bbox[0], x); bbox[1] = Math.min(bbox[1], y);
      bbox[2] = Math.max(bbox[2], x); bbox[3] = Math.max(bbox[3], y);
    }
  });
  return bbox;
}

function lineGeomBBox(geom, bbox) {
  if (geom && geom.type === "LineString") {
    for (const [x, y] of geom.coordinates) {
      bbox[0] = Math.min(bbox[0], x); bbox[1] = Math.min(bbox[1], y);
      bbox[2] = Math.max(bbox[2], x); bbox[3] = Math.max(bbox[3], y);
    }
  }
  return bbox;
}

/* ---------------- 画布绘制 ---------------- */
function pathRing(ring) {
  ring.forEach(([x, y], i) => {
    const [sx, sy] = worldToScreen(x, y);
    if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
  });
  ctx.closePath();
}

function fillStrokeGeom(geom, fill, stroke, lineWidth) {
  ctx.beginPath();
  eachRing(geom, pathRing);
  if (fill) { ctx.fillStyle = fill; ctx.fill("evenodd"); }
  if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = lineWidth || 1.2; ctx.stroke(); }
}

function drawGrid() {
  // 依据缩放选取“好看”的网格步长
  const targetPx = 90;
  const raw = targetPx / view.s;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map(m => m * mag).find(v => v >= raw) || mag * 10;

  const [wx0, wy0] = screenToWorld(0, canvas.height);
  const [wx1, wy1] = screenToWorld(canvas.width, 0);
  ctx.strokeStyle = "rgba(120,150,180,0.12)";
  ctx.fillStyle = "rgba(139,161,184,0.75)";
  ctx.lineWidth = 1;
  ctx.font = `${11 * (window.devicePixelRatio || 1)}px Consolas, monospace`;

  for (let x = Math.ceil(wx0 / step) * step; x <= wx1; x += step) {
    const [sx] = worldToScreen(x, 0);
    ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, canvas.height); ctx.stroke();
    ctx.fillText(fmtNum(x), sx + 3, canvas.height - 6);
  }
  for (let y = Math.ceil(wy0 / step) * step; y <= wy1; y += step) {
    const [, sy] = worldToScreen(0, y);
    ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(canvas.width, sy); ctx.stroke();
    ctx.fillText(fmtNum(y), 4, sy - 4);
  }
}

function fmtNum(v) {
  return Math.abs(v) >= 1000 ? v.toFixed(0) : String(Math.round(v * 100) / 100);
}

function draw() {
  resizeCanvas();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  drawGrid();

  // 扫掠覆盖（并集）
  if (lastResult && lastResult.geometries.coverage) {
    fillStrokeGeom(lastResult.geometries.coverage, "rgba(63,185,80,0.22)", null);
  }
  // 任务边界
  if (lastInput && lastInput.boundary) {
    fillStrokeGeom(lastInput.boundary, "rgba(157,178,200,0.05)", "#9db2c8", 2);
  }
  // 测线中心线 + 编号
  if (lastInput && Array.isArray(lastInput.lines)) {
    ctx.strokeStyle = "#2f81f7";
    ctx.lineWidth = 1.5;
    ctx.fillStyle = "#7db3ff";
    ctx.font = `${12 * (window.devicePixelRatio || 1)}px sans-serif`;
    for (const ln of lastInput.lines) {
      const [x0, y0] = worldToScreen(ln.start[0], ln.start[1]);
      const [x1, y1] = worldToScreen(ln.end[0], ln.end[1]);
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
      ctx.fillText(ln.id, (x0 + x1) / 2 + 4, (y0 + y1) / 2 - 4);
    }
  }
  // 问题区域
  if (lastResult) {
    for (const it of lastResult.issues) {
      if (typeFilter !== "all" && it.type !== typeFilter) continue;
      const selected = it.id === selectedIssueId;
      const color = TYPE_COLOR[it.type];
      fillStrokeGeom(it.geometry, selected ? hexA(color, 0.55) : hexA(color, 0.3),
                     color, selected ? 3 : 1.4);
      if (selected) {
        const [sx, sy] = worldToScreen(it.centroid[0], it.centroid[1]);
        ctx.fillStyle = "#fff";
        ctx.font = `bold ${13 * (window.devicePixelRatio || 1)}px Consolas, monospace`;
        ctx.fillText(it.id, sx + 6, sy - 6);
      }
    }
  }
  // 采集连续性事件轨迹（按当前测线/原因筛选）
  if (ctResult) {
    const dpr = window.devicePixelRatio || 1;
    for (const ev of ctResult.events) {
      if (!ctEventVisible(ev)) continue;
      const selected = ev.id === selectedEventId;
      const color = selected ? "#ffffff" : CT_COLOR;
      const coords = ev.geometry.coordinates;
      ctx.beginPath();
      coords.forEach(([x, y], i) => {
        const [sx, sy] = worldToScreen(x, y);
        if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = selected ? 3.5 : 2;
      ctx.stroke();
      ctx.fillStyle = color;
      for (const [x, y] of coords) {
        const [sx, sy] = worldToScreen(x, y);
        ctx.beginPath();
        ctx.arc(sx, sy, (selected ? 3.5 : 2.5) * dpr, 0, Math.PI * 2);
        ctx.fill();
      }
      if (selected) {
        const [sx, sy] = worldToScreen(ev.centroid[0], ev.centroid[1]);
        ctx.font = `bold ${13 * dpr}px Consolas, monospace`;
        ctx.fillText(ev.id, sx + 6, sy - 6);
      }
    }
  }
  // 交叉点水深闭合：有效交点圆点 + 跳过记录空心圈（按当前筛选）
  if (clResult) {
    const dpr = window.devicePixelRatio || 1;
    for (const xpt of clResult.intersections) {
      if (!clXptVisible(xpt)) continue;
      const selected = xpt.id === selectedClosureId;
      const [sx, sy] = worldToScreen(xpt.point[0], xpt.point[1]);
      const color = xpt.status === "anomaly" ? "#ff6b70"
        : xpt.status === "indeterminate" ? "#8ba1b8" : CL_COLOR;
      ctx.beginPath();
      ctx.arc(sx, sy, (selected ? 7 : 5) * dpr, 0, Math.PI * 2);
      ctx.fillStyle = hexA(color, xpt.status === "anomaly" ? 0.95 : 0.85);
      ctx.fill();
      ctx.lineWidth = (selected ? 2.5 : 1.2) * dpr;
      ctx.strokeStyle = selected ? "#ffffff" : color;
      ctx.stroke();
      if (selected) {
        ctx.fillStyle = "#fff";
        ctx.font = `bold ${13 * dpr}px Consolas, monospace`;
        ctx.fillText(xpt.id, sx + 9, sy - 7);
      }
    }
    for (const sk of clResult.skips) {
      if (!clSkipVisible(sk) || !sk.point) continue;
      const selected = sk.id === selectedClosureId;
      const [sx, sy] = worldToScreen(sk.point[0], sk.point[1]);
      ctx.beginPath();
      ctx.arc(sx, sy, (selected ? 7 : 5) * dpr, 0, Math.PI * 2);
      ctx.strokeStyle = selected ? "#ffffff" : "#f5a524";
      ctx.lineWidth = (selected ? 2.5 : 1.5) * dpr;
      ctx.setLineDash([4 * dpr, 3 * dpr]);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }
  // 声速射线改正：换能器→落点线、改正落点、直线对照点、失败波束红叉
  drawRcOverlay();
}

function hexA(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16),
        b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

/* 画布交互：滚轮缩放 / 拖拽平移 / 双击复位 */
canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const mx = (e.clientX - rect.left) * dpr, my = (e.clientY - rect.top) * dpr;
  const [wx, wy] = screenToWorld(mx, my);
  view.s *= e.deltaY < 0 ? 1.15 : 1 / 1.15;
  view.cx = wx - (mx - canvas.width / 2) / view.s;
  view.cy = wy - (canvas.height / 2 - my) / view.s;
  draw();
}, { passive: false });

let drag = null;
canvas.addEventListener("mousedown", (e) => {
  drag = { x: e.clientX, y: e.clientY };
  canvas.classList.add("dragging");
});
window.addEventListener("mousemove", (e) => {
  if (!drag) return;
  const dpr = window.devicePixelRatio || 1;
  view.cx -= (e.clientX - drag.x) * dpr / view.s;
  view.cy += (e.clientY - drag.y) * dpr / view.s;
  drag = { x: e.clientX, y: e.clientY };
  draw();
});
window.addEventListener("mouseup", () => { drag = null; canvas.classList.remove("dragging"); });
canvas.addEventListener("dblclick", () => { fitToAll(); });
window.addEventListener("resize", () => draw());

function fitToAll() {
  const bbox = [Infinity, Infinity, -Infinity, -Infinity];
  if (lastInput && lastInput.boundary) geomBBox(lastInput.boundary, bbox);
  if (lastResult && lastResult.geometries.coverage) geomBBox(lastResult.geometries.coverage, bbox);
  if (ctResult) for (const ev of ctResult.events) lineGeomBBox(ev.geometry, bbox);
  if (rcResult) {
    for (const b of rcResult.beams) {
      bbox[0] = Math.min(bbox[0], b.x); bbox[1] = Math.min(bbox[1], b.y);
      bbox[2] = Math.max(bbox[2], b.x); bbox[3] = Math.max(bbox[3], b.y);
      if (b.point) {
        bbox[0] = Math.min(bbox[0], b.point[0]); bbox[1] = Math.min(bbox[1], b.point[1]);
        bbox[2] = Math.max(bbox[2], b.point[0]); bbox[3] = Math.max(bbox[3], b.point[1]);
      }
      if (b.straight) {
        bbox[0] = Math.min(bbox[0], b.straight.point[0]); bbox[1] = Math.min(bbox[1], b.straight.point[1]);
        bbox[2] = Math.max(bbox[2], b.straight.point[0]); bbox[3] = Math.max(bbox[3], b.straight.point[1]);
      }
    }
  }
  if (bbox[0] === Infinity) return;
  fitBounds(bbox);
  draw();
}

/* ---------------- 分析请求 ---------------- */
async function runAnalyze(silent) {
  let payload;
  try {
    payload = JSON.parse(inputEl.value);
  } catch (err) {
    setStatus(`JSON 解析失败：${err.message}`, true);
    if (!silent) showError(`JSON 解析失败：${err.message}`);
    return;
  }
  setStatus("复核计算中…");
  try {
    const resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(errDetail(data, resp.status));
    lastResult = data;
    lastInput = payload;
    hideError();
    setStatus(`复核完成：${data.summary.issue_total} 个问题（输入修改后自动更新）`);
    renderAll();
    // 计划测线可能已变化，连续性结果基于最新 lines 静默重查
    if (ctInputEl.value.trim()) runContinuity(true);
    // 水深闭合同样沿用计划测线，测线变化后静默重算
    const clBox = document.querySelector("#cl-input");
    if (clBox && clBox.value.trim()) runClosure(true);
  } catch (err) {
    setStatus(`复核失败：${err.message}`, true);
    showError(err.message);
  }
}

function errDetail(data, status) {
  if (!data || data.detail == null) return `HTTP ${status}`;
  return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
}

function setStatus(msg, isErr) {
  statusEl.textContent = msg;
  statusEl.classList.toggle("err", !!isErr);
}
function showError(msg) { errorEl.textContent = msg; errorEl.hidden = false; }
function hideError() { errorEl.hidden = true; }

/* ---------------- 渲染 ---------------- */
function renderAll() {
  renderSummary();
  renderFilterBar();
  renderIssues();
  renderLineStats();
  exportBtn.disabled = !lastResult;
  if (selectedIssueId && !lastResult.issues.some(i => i.id === selectedIssueId)) {
    selectedIssueId = null;
  }
  if (!viewInitialized) { fitToAll(); viewInitialized = true; } else { draw(); }
}

function statCard(label, value, cls) {
  return `<div class="stat-card"><div class="k">${label}</div><div class="v ${cls || ""}">${value}</div></div>`;
}

function renderSummary() {
  const s = lastResult.summary;
  const c = s.issue_counts;
  summaryEl.innerHTML = [
    statCard("边界面积", s.boundary_area),
    statCard("覆盖率", s.coverage_pct + " %", s.coverage_pct >= 99.9 ? "good" : "warn"),
    statCard("未覆盖缺口", `${c.gap} 处 / ${s.gap_area_total}`, c.gap ? "bad" : "good"),
    statCard("边界外扫掠", `${c.outside} 处 / ${s.outside_area_total}`, c.outside ? "warn" : "good"),
    statCard("过度重叠", `${c.overlap} 对`, c.overlap ? "warn" : "good"),
    statCard("测线数", s.line_count),
  ].join("");
}

function renderFilterBar() {
  const c = lastResult.summary.issue_counts;
  const total = lastResult.summary.issue_total;
  const defs = [
    ["all", "全部", total],
    ["gap", "缺口", c.gap],
    ["outside", "越界", c.outside],
    ["overlap", "重叠", c.overlap],
  ];
  filterBarEl.innerHTML = defs.map(([k, label, n]) =>
    `<button data-f="${k}" class="${typeFilter === k ? "active" : ""}">${label}<span class="cnt">${n}</span></button>`
  ).join("");
  filterBarEl.querySelectorAll("button").forEach(btn =>
    btn.addEventListener("click", () => {
      typeFilter = btn.dataset.f;
      renderFilterBar();
      renderIssues();
      draw();
    }));
}

function basisRows(basis) {
  const skip = new Set(["method"]);
  let rows = "";
  for (const [k, v] of Object.entries(basis)) {
    if (skip.has(k)) continue;
    const val = typeof v === "object" ? JSON.stringify(v) : v;
    rows += `<tr><td>${esc(k)}</td><td>${esc(val)}</td></tr>`;
  }
  return rows;
}

function renderIssues() {
  const issues = lastResult.issues.filter(it => typeFilter === "all" || it.type === typeFilter);
  if (!issues.length) {
    issueListEl.innerHTML = `<div class="empty-note">当前筛选条件下没有问题。${
      lastResult.summary.issue_total === 0 ? "覆盖复核通过。" : ""}</div>`;
    return;
  }
  issueListEl.innerHTML = issues.map(it => `
    <div class="issue-card t-${it.type} ${it.id === selectedIssueId ? "selected" : ""}" data-id="${it.id}">
      <div class="issue-head">
        <span class="id">${it.id}</span>
        <span class="chip ${it.type}">${it.type_label}</span>
        <span class="chip sev-${it.severity}">严重度 ${SEV_LABEL[it.severity]}</span>
      </div>
      <div class="issue-meta">
        <span>面积 <b>${it.area}</b></span>
        <span>质心 <b>[${it.centroid.join(", ")}]</b></span>
        <span>关联测线 <b>${it.lines.map(esc).join(", ") || "—"}</b></span>
      </div>
      <details>
        <summary>计算依据（可复核）</summary>
        <div class="basis">
          <div class="method">${it.basis.method}</div>
          <table>${basisRows(it.basis)}</table>
        </div>
      </details>
    </div>`).join("");

  issueListEl.querySelectorAll(".issue-card").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("details")) return; // 展开依据时不触发定位
      selectIssue(card.dataset.id, true);
    });
  });
}

function selectIssue(id, panTo) {
  selectedIssueId = id;
  issueListEl.querySelectorAll(".issue-card").forEach(c =>
    c.classList.toggle("selected", c.dataset.id === id));
  if (panTo) {
    const it = lastResult.issues.find(i => i.id === id);
    if (it) {
      const bbox = geomBBox(it.geometry, [Infinity, Infinity, -Infinity, -Infinity]);
      const pad = (Math.max(bbox[2] - bbox[0], bbox[3] - bbox[1]) || 1) * 0.6;
      fitBounds([bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad]);
    }
  }
  draw();
}

function renderLineStats() {
  const rows = lastResult.line_stats.map(l =>
    `<tr><td>${esc(l.id)}</td><td>${l.length}</td><td>${l.swath}</td>` +
    `<td>${l.buffer_area}</td><td>${l.inside_area}</td><td>${l.outside_area}</td></tr>`).join("");
  lineStatsEl.innerHTML =
    `<tr><th>测线</th><th>长度</th><th>扫宽</th><th>扫掠面积</th><th>界内面积</th><th>界外面积</th></tr>${rows}`;
}

/* ---------------- 事件绑定 ---------------- */
$("#btn-analyze").addEventListener("click", () => runAnalyze(false));

$("#btn-sample").addEventListener("click", async () => {
  const resp = await fetch("/api/sample");
  const sample = await resp.json();
  inputEl.value = JSON.stringify(sample, null, 2);
  runAnalyze(true);
});

exportBtn.addEventListener("click", () => {
  if (!lastResult) return;
  const blob = new Blob([JSON.stringify(lastResult, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "coverage-review-result.json";
  a.click();
  URL.revokeObjectURL(a.href);
});

// 输入修改后防抖自动复核，保证结果稳定更新
inputEl.addEventListener("input", () => {
  clearTimeout(analyzeTimer);
  analyzeTimer = setTimeout(() => runAnalyze(true), 700);
});

/* ---------------- 启动：载入示例并复核 ---------------- */
(async function init() {
  try {
    const resp = await fetch("/api/sample");
    const sample = await resp.json();
    inputEl.value = JSON.stringify(sample, null, 2);
    await runAnalyze(true);
  } catch (err) {
    setStatus(`初始化失败：${err.message}`, true);
  }
})();

/* ================= 采集连续性 ================= */
const ctInputEl = $("#ct-input");
const ctStatusEl = $("#ct-status");
const ctSummaryEl = $("#ct-summary");
const ctLineFilterEl = $("#ct-line-filter");
const ctReasonFilterEl = $("#ct-reason-filter");
const ctEventsEl = $("#ct-events");
const ctLineStatsEl = $("#ct-line-stats");
const ctExportBtn = $("#btn-ct-export");

function ctSetStatus(msg, isErr) {
  ctStatusEl.textContent = msg;
  ctStatusEl.classList.toggle("err", !!isErr);
}

function ctOptions() {
  const opts = {
    max_gap_s: parseFloat($("#ct-max-gap").value),
    max_speed: parseFloat($("#ct-max-speed").value),
    max_offset: parseFloat($("#ct-max-offset").value),
  };
  for (const [k, v] of Object.entries(opts)) {
    if (!Number.isFinite(v)) throw new Error(`阈值 ${k} 不是有效数字`);
  }
  return opts;
}

/* 计划测线沿用当前任务输入中的 lines（解析失败回退到最近一次成功复核的输入） */
function currentPlannedLines() {
  try {
    const main = JSON.parse(inputEl.value);
    if (main && Array.isArray(main.lines)) return main.lines;
  } catch (e) { /* 回退 lastInput */ }
  return lastInput && Array.isArray(lastInput.lines) ? lastInput.lines : null;
}

function ctEventVisible(ev) {
  if (ctLineFilter !== "all" && ev.line_id !== ctLineFilter) return false;
  if (ctReasonFilter !== "all" && !ev.reasons.includes(ctReasonFilter)) return false;
  return true;
}

async function runContinuity(silent) {
  let parsed;
  try {
    parsed = JSON.parse(ctInputEl.value);
  } catch (err) {
    ctSetStatus(`样本 JSON 解析失败：${err.message}`, true);
    return;
  }
  const samples = Array.isArray(parsed) ? parsed : parsed.samples;
  if (!Array.isArray(samples)) {
    ctSetStatus("样本 JSON 必须是数组，或含 samples 数组的对象", true);
    return;
  }
  const lines = currentPlannedLines();
  if (!lines) {
    ctSetStatus("请先在上方任务输入中提供 lines（计划测线）", true);
    return;
  }
  let options;
  try {
    options = ctOptions();
  } catch (err) {
    ctSetStatus(err.message, true);
    return;
  }
  ctSetStatus("连续性检查中…");
  try {
    const resp = await fetch("/api/continuity", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines, samples, options }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(errDetail(data, resp.status));
    ctResult = data;
    const unknown = data.summary.unknown_line_ids;
    ctSetStatus(`检查完成：${data.summary.event_count} 个事件` +
      (unknown.length ? `（未知测线 ${unknown.join(", ")} 未参与偏离判定）` : ""));
    renderCtAll();
  } catch (err) {
    ctSetStatus(`检查失败：${err.message}`, true);
    if (!silent) ctEventsEl.innerHTML = `<div class="empty-note">检查失败：${esc(err.message)}</div>`;
  }
}

function renderCtAll() {
  renderCtSummary();
  renderCtFilters();
  renderCtEvents();
  renderCtLineStats();
  ctExportBtn.disabled = !ctResult;
  if (selectedEventId && !ctResult.events.some(e => e.id === selectedEventId)) {
    selectedEventId = null;
  }
  draw();
}

function renderCtSummary() {
  const s = ctResult.summary, c = s.reason_counts;
  const seqIssues = c.time_reversal + c.seq_duplicate + c.seq_jump;
  ctSummaryEl.innerHTML = [
    statCard("样本数", s.sample_count),
    statCard("异常事件", s.event_count, s.event_count ? "bad" : "good"),
    statCard("时序/序号", seqIssues, seqIssues ? "bad" : "good"),
    statCard("间隔超限", c.gap_exceeded, c.gap_exceeded ? "warn" : "good"),
    statCard("速度超限", c.speed_exceeded, c.speed_exceeded ? "warn" : "good"),
    statCard("偏离超限", c.offset_exceeded, c.offset_exceeded ? "warn" : "good"),
  ].join("");
}

function renderCtFilters() {
  const events = ctResult.events;
  // 测线筛选（计数随原因筛选联动）
  const lineCounts = {};
  for (const ev of events) {
    if (ctReasonFilter === "all" || ev.reasons.includes(ctReasonFilter)) {
      lineCounts[ev.line_id] = (lineCounts[ev.line_id] || 0) + 1;
    }
  }
  const lines = Object.keys(lineCounts).sort();
  if (ctLineFilter !== "all" && !lines.includes(ctLineFilter)) ctLineFilter = "all";
  const lineDefs = [["all", "全部测线", events.filter(ev => ctReasonFilter === "all" || ev.reasons.includes(ctReasonFilter)).length]]
    .concat(lines.map(l => [l, l, lineCounts[l]]));
  ctLineFilterEl.innerHTML = lineDefs.map(([k, label, n]) =>
    `<button data-l="${esc(k)}" class="${ctLineFilter === k ? "active" : ""}">${esc(label)}<span class="cnt">${n}</span></button>`
  ).join("");
  ctLineFilterEl.querySelectorAll("button").forEach(btn =>
    btn.addEventListener("click", () => { ctLineFilter = btn.dataset.l; renderCtFilters(); renderCtEvents(); draw(); }));

  // 原因筛选（计数随测线筛选联动）
  const reasonCounts = {};
  for (const ev of events) {
    if (ctLineFilter === "all" || ev.line_id === ctLineFilter) {
      for (const r of ev.reasons) reasonCounts[r] = (reasonCounts[r] || 0) + 1;
    }
  }
  const visibleTotal = events.filter(ev => ctLineFilter === "all" || ev.line_id === ctLineFilter).length;
  const reasonDefs = [["all", "全部原因", visibleTotal]]
    .concat(Object.keys(REASON_LABEL).map(r => [r, REASON_LABEL[r], reasonCounts[r] || 0]));
  ctReasonFilterEl.innerHTML = reasonDefs.map(([k, label, n]) =>
    `<button data-r="${k}" class="${ctReasonFilter === k ? "active" : ""}">${label}<span class="cnt">${n}</span></button>`
  ).join("");
  ctReasonFilterEl.querySelectorAll("button").forEach(btn =>
    btn.addEventListener("click", () => { ctReasonFilter = btn.dataset.r; renderCtFilters(); renderCtEvents(); draw(); }));
}

function ctSegmentRows(ev) {
  return ev.segments.map(seg => {
    const m = seg.metrics;
    const metrics = m
      ? `dt=${m.dt}s · 位移=${m.dist} · 速度=${m.speed}` +
        (m.offset_max != null ? ` · 偏离=${m.offset_from}/${m.offset_to}` : "")
      : "倒退区间不计算间隔/速度/偏离";
    return `<tr><td>#${seg.from_index}→#${seg.to_index}</td>` +
      `<td>${seg.reason_labels.map(esc).join("、")}<br><span class="seg-metrics">${esc(metrics)}</span></td></tr>`;
  }).join("");
}

function renderCtEvents() {
  const events = ctResult.events.filter(ctEventVisible);
  if (!events.length) {
    ctEventsEl.innerHTML = `<div class="empty-note">当前筛选条件下没有事件。${
      ctResult.summary.event_count === 0 ? "采集连续性检查通过。" : ""}</div>`;
    return;
  }
  ctEventsEl.innerHTML = events.map(ev => `
    <div class="issue-card ct-event ${ev.id === selectedEventId ? "selected" : ""}" data-id="${ev.id}">
      <div class="issue-head">
        <span class="id">${ev.id}</span>
        <span class="chip line">${esc(ev.line_id)}</span>
        ${ev.reason_labels.map(l => `<span class="chip reason">${esc(l)}</span>`).join("")}
      </div>
      <div class="issue-meta">
        <span>样本 <b>#${ev.start_index} → #${ev.end_index}</b>（${ev.sample_count} 个 / ${ev.segment_count} 区间）</span>
        <span>时间 <b>${ev.start_sample.timestamp} → ${ev.end_sample.timestamp}</b></span>
        <span>ping_seq <b>${ev.start_sample.ping_seq} → ${ev.end_sample.ping_seq}</b></span>
        ${ev.stats.max_speed != null ? `<span>最大速度 <b>${ev.stats.max_speed}</b></span>` : ""}
        ${ev.stats.max_offset != null ? `<span>最大偏离 <b>${ev.stats.max_offset}</b></span>` : ""}
      </div>
      <details>
        <summary>区间明细与计算依据</summary>
        <div class="basis">
          <div class="method">${esc(ev.basis.method)}</div>
          <table>
            <tr><td>阈值</td><td>${esc(JSON.stringify(ev.thresholds))}</td></tr>
            ${ctSegmentRows(ev)}
          </table>
        </div>
      </details>
    </div>`).join("");

  ctEventsEl.querySelectorAll(".issue-card").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("details")) return;
      selectEvent(card.dataset.id, true);
    });
  });
}

function selectEvent(id, panTo) {
  selectedEventId = id;
  ctEventsEl.querySelectorAll(".issue-card").forEach(c =>
    c.classList.toggle("selected", c.dataset.id === id));
  if (panTo) {
    const ev = ctResult.events.find(e => e.id === id);
    if (ev) {
      const bbox = lineGeomBBox(ev.geometry, [Infinity, Infinity, -Infinity, -Infinity]);
      const pad = (Math.max(bbox[2] - bbox[0], bbox[3] - bbox[1]) || 1) * 0.6 + 1;
      fitBounds([bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad]);
      canvas.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
  draw();
}

function renderCtLineStats() {
  const rows = ctResult.line_stats.map(l =>
    `<tr><td>${esc(l.line_id)}${l.planned ? "" : "（未计划）"}</td><td>${l.sample_count}</td>` +
    `<td>${l.event_count}</td><td>${l.first_index}–${l.last_index}</td>` +
    `<td>${l.time_range.join(" → ")}</td><td>${l.seq_range.join(" → ")}</td></tr>`).join("");
  ctLineStatsEl.innerHTML =
    `<tr><th>测线</th><th>样本数</th><th>事件数</th><th>索引范围</th><th>时间范围</th><th>序号范围</th></tr>${rows}`;
}

/* ---------------- 连续性事件绑定 ---------------- */
$("#btn-ct-run").addEventListener("click", () => runContinuity(false));

$("#btn-ct-sample").addEventListener("click", async () => {
  const resp = await fetch("/api/continuity/sample");
  const sample = await resp.json();
  ctInputEl.value = JSON.stringify(sample.samples, null, 2);
  $("#ct-max-gap").value = sample.options.max_gap_s;
  $("#ct-max-speed").value = sample.options.max_speed;
  $("#ct-max-offset").value = sample.options.max_offset;
  runContinuity(false);
});

ctExportBtn.addEventListener("click", () => {
  if (!ctResult) return;
  const blob = new Blob([JSON.stringify(ctResult, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "continuity-result.json";
  a.click();
  URL.revokeObjectURL(a.href);
});

// 样本或阈值修改后防抖自动重查，保证结果稳定更新
function ctAutoRun() {
  clearTimeout(ctTimer);
  ctTimer = setTimeout(() => runContinuity(true), 700);
}
ctInputEl.addEventListener("input", ctAutoRun);
for (const id of ["#ct-max-gap", "#ct-max-speed", "#ct-max-offset"]) {
  $(id).addEventListener("input", ctAutoRun);
}

/* ---------------- 连续性初始化：载入示例并检查 ---------------- */
(async function initContinuity() {
  try {
    const resp = await fetch("/api/continuity/sample");
    const sample = await resp.json();
    ctInputEl.value = JSON.stringify(sample.samples, null, 2);
    $("#ct-max-gap").value = sample.options.max_gap_s;
    $("#ct-max-speed").value = sample.options.max_speed;
    $("#ct-max-offset").value = sample.options.max_offset;
    await runContinuity(true);
  } catch (err) {
    ctSetStatus(`初始化失败：${err.message}`, true);
  }
})();

/* ================= 交叉点水深闭合 ================= */
const clInputEl = $("#cl-input");
const clStatusEl = $("#cl-status");
const clSummaryEl = $("#cl-summary");
const clStatusFilterEl = $("#cl-status-filter");
const clLineFilterEl = $("#cl-line-filter");
const clListEl = $("#cl-list");
const clLineBiasesEl = $("#cl-line-biases");
const clComponentsEl = $("#cl-components");
const clExportBtn = $("#btn-cl-export");

function clSetStatus(msg, isErr) {
  clStatusEl.textContent = msg;
  clStatusEl.classList.toggle("err", !!isErr);
}

function clOptions() {
  const opts = {
    max_enclosure_distance: parseFloat($("#cl-max-enclosure").value),
    huber_delta: parseFloat($("#cl-huber-delta").value),
    residual_threshold: parseFloat($("#cl-residual").value),
  };
  for (const [k, v] of Object.entries(opts)) {
    if (!Number.isFinite(v)) throw new Error(`参数 ${k} 不是有效数字`);
  }
  if (opts.max_enclosure_distance <= 0 || opts.huber_delta <= 0) {
    throw new Error("包围距离与 Huber 阈值必须为正数");
  }
  return opts;
}

function clItemVisible(item) {
  // item: 交点对象（含 status/lines）或跳过对象（kind='skip' 标记）
  const isSkip = item.__skip;
  if (clStatusFilter === "all") {
    // 全部：交点与跳过都显示
  } else if (clStatusFilter === "skipped") {
    if (!isSkip) return false;
  } else if (isSkip || item.status !== clStatusFilter) {
    return false;
  }
  if (clLineFilter !== "all") {
    const lines = isSkip ? (item.lines || (item.line ? [item.line] : [])) : item.lines;
    if (!lines.includes(clLineFilter)) return false;
  }
  return true;
}

// 画布直接使用响应中的原始对象（无 __skip 标记），分别判定
function clLineMatches(lines) {
  return clLineFilter === "all" || lines.includes(clLineFilter);
}
function clXptVisible(xpt) {
  if (clStatusFilter !== "all" && clStatusFilter !== xpt.status) return false;
  return clLineMatches(xpt.lines);
}
function clSkipVisible(sk) {
  if (clStatusFilter !== "all" && clStatusFilter !== "skipped") return false;
  const lines = sk.lines && sk.lines.length ? sk.lines : (sk.line ? [sk.line] : []);
  return clLineMatches(lines);
}

async function runClosure(silent) {
  let parsed;
  try {
    parsed = JSON.parse(clInputEl.value);
  } catch (err) {
    clSetStatus(`样本 JSON 解析失败：${err.message}`, true);
    return;
  }
  const samples = Array.isArray(parsed) ? parsed : parsed.samples;
  if (!Array.isArray(samples)) {
    clSetStatus("样本 JSON 必须是数组，或含 samples 数组的对象", true);
    return;
  }
  const lines = currentPlannedLines();
  if (!lines) {
    clSetStatus("请先在上方任务输入中提供 lines（计划测线）", true);
    return;
  }
  let options;
  try {
    options = clOptions();
  } catch (err) {
    clSetStatus(err.message, true);
    return;
  }
  clSetStatus("水深闭合计算中…");
  try {
    const resp = await fetch("/api/closure", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines, samples, options }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(errDetail(data, resp.status));
    clResult = data;
    const s = data.summary;
    clSetStatus(`闭合完成：${s.intersection_count} 个有效交点（异常 ${s.anomaly_count}、` +
      `不可判 ${s.indeterminate_count}），跳过 ${s.skip_count} 条，连通分量 ${s.component_count} 个` +
      (s.unknown_line_ids.length ? `；未知测线 ${s.unknown_line_ids.join(", ")}` : ""));
    renderClAll();
  } catch (err) {
    clSetStatus(`闭合失败：${err.message}`, true);
    if (!silent) clListEl.innerHTML = `<div class="empty-note">闭合失败：${esc(err.message)}</div>`;
  }
}

function renderClAll() {
  renderClSummary();
  renderClFilters();
  renderClList();
  renderClLineBiases();
  clExportBtn.disabled = !clResult;
  const validIds = new Set(clResult.intersections.map(i => i.id));
  clResult.skips.forEach(s => validIds.add(s.id));
  if (selectedClosureId && !validIds.has(selectedClosureId)) selectedClosureId = null;
  draw();
}

function renderClSummary() {
  const s = clResult.summary, c = s.skip_counts;
  clSummaryEl.innerHTML = [
    statCard("有效交点", s.intersection_count),
    statCard("闭合异常", s.anomaly_count, s.anomaly_count ? "bad" : "good"),
    statCard("正常", s.ok_count, s.ok_count ? "good" : ""),
    statCard("无冗余不可判", s.indeterminate_count, s.indeterminate_count ? "warn" : "good"),
    statCard("跳过", s.skip_count, s.skip_count ? "warn" : "good"),
    statCard("重叠/缺样本/未知线", `${c.overlapping}/${c.no_bracketing_samples}/${c.unknown_line}`),
  ].join("");
}

function clDisplayItems() {
  // 合并交点与跳过记录，保持稳定顺序：交点 XPT 顺序，其后跳过 SKP 顺序
  const items = clResult.intersections.map(i => Object.assign({ __skip: false }, i));
  clResult.skips.forEach(s => items.push(Object.assign({ __skip: true }, s)));
  return items;
}

function renderClFilters() {
  const items = clDisplayItems();
  const s = clResult.summary;
  const statusDefs = [
    ["all", "全部", items.length],
    ["ok", "正常", s.ok_count],
    ["anomaly", "异常", s.anomaly_count],
    ["indeterminate", "不可判", s.indeterminate_count],
    ["skipped", "跳过", s.skip_count],
  ];
  clStatusFilterEl.innerHTML = statusDefs.map(([k, label, n]) =>
    `<button data-f="${k}" class="${clStatusFilter === k ? "active" : ""}">${label}<span class="cnt">${n}</span></button>`
  ).join("");
  clStatusFilterEl.querySelectorAll("button").forEach(btn =>
    btn.addEventListener("click", () => {
      clStatusFilter = btn.dataset.f;
      renderClFilters();
      renderClList();
      draw();
    }));

  // 测线筛选（计数随状态筛选联动）
  const lineCounts = {};
  for (const it of items) {
    if (!clItemVisible(it)) continue;
    const lids = it.__skip
      ? (it.lines && it.lines.length ? it.lines : (it.line ? [it.line] : []))
      : it.lines;
    for (const lid of lids) lineCounts[lid] = (lineCounts[lid] || 0) + 1;
  }
  const linesAll = Object.keys(lineCounts).sort();
  if (clLineFilter !== "all" && !linesAll.includes(clLineFilter)) clLineFilter = "all";
  const visibleCount = items.filter(clItemVisible).length;
  const lineDefs = [["all", "全部测线", visibleCount]]
    .concat(linesAll.map(l => [l, l, lineCounts[l]]));
  clLineFilterEl.innerHTML = lineDefs.map(([k, label, n]) =>
    `<button data-l="${esc(k)}" class="${clLineFilter === k ? "active" : ""}">${esc(label)}<span class="cnt">${n}</span></button>`
  ).join("");
  clLineFilterEl.querySelectorAll("button").forEach(btn =>
    btn.addEventListener("click", () => {
      clLineFilter = btn.dataset.l;
      renderClFilters();
      renderClList();
      draw();
    }));
}

function clInterpTable(xpt) {
  const rows = xpt.lines.map(lid => {
    const v = xpt.interpolations[lid];
    if (!v) return "";
    const [a, b] = v.endpoints;
    return `<tr>
      <td>${esc(lid)}</td>
      <td>${v.station_m}</td>
      <td>#${a.index}→#${b.index}</td>
      <td>${a.alongtrack_distance_m}/${b.alongtrack_distance_m}</td>
      <td>${v.ratio_t}</td>
      <td>${v.interpolated_depth}</td>
    </tr>`;
  }).join("");
  return `<table class="cl-interp">
    <tr><th>测线</th><th>交点里程</th><th>端点(原始索引)</th><th>两端沿线距离</th><th>比例 t</th><th>插值水深</th></tr>
    ${rows}
  </table>`;
}

function renderClList() {
  const items = clDisplayItems().filter(clItemVisible);
  if (!items.length) {
    clListEl.innerHTML = `<div class="empty-note">当前筛选条件下没有记录。</div>`;
    return;
  }
  clListEl.innerHTML = items.map(it => {
    if (it.__skip) return renderClSkipCard(it);
    return renderClXptCard(it);
  }).join("");

  clListEl.querySelectorAll(".issue-card").forEach(card => {
    card.addEventListener("click", (e) => {
      if (e.target.closest("details")) return;
      selectClosure(card.dataset.id, true);
    });
  });
}

function renderClXptCard(xpt) {
  const stCls = `st-${xpt.status}`;
  const wNote = xpt.final_weight < 0.999
    ? `<span>Huber 权重 <b>${xpt.final_weight}</b></span>` : "";
  return `
    <div class="issue-card cl-xpt ${stCls} ${xpt.id === selectedClosureId ? "selected" : ""}" data-id="${xpt.id}">
      <div class="issue-head">
        <span class="id">${xpt.id}</span>
        <span class="chip line">${esc(xpt.lines[0])} × ${esc(xpt.lines[1])}</span>
        <span class="chip ${stCls}">${esc(xpt.status_label)}</span>
        <span class="chip comp">${esc(xpt.component_id)}</span>
      </div>
      <div class="issue-meta">
        <span>交点 <b>[${xpt.point.join(", ")}]</b></span>
        <span>插值水深 <b>${xpt.depth_left} / ${xpt.depth_right}</b></span>
        <span>原始闭合差 <b>${xpt.raw_misclosure}</b></span>
        <span>校正残差 <b>${xpt.corrected_residual}</b></span>
        ${wNote}
        <span>偏置 <b>${esc(JSON.stringify(xpt.biases))}</b></span>
      </div>
      <details>
        <summary>插值端点 / 比例 / 偏置依据</summary>
        <div class="basis">
          <div class="method">${esc(clResult.meta.enclosure_rule)}</div>
          ${clInterpTable(xpt)}
          <div class="method">${esc(clResult.meta.adjustment_rule)}</div>
        </div>
      </details>
    </div>`;
}

function renderClSkipCard(sk) {
  const lineChip = sk.line ? `<span class="chip line">${esc(sk.line)}</span>`
    : (sk.lines && sk.lines.length ? sk.lines.map(l => `<span class="chip line">${esc(l)}</span>`).join("") : "");
  return `
    <div class="issue-card cl-skip ${sk.id === selectedClosureId ? "selected" : ""}" data-id="${sk.id}">
      <div class="issue-head">
        <span class="id">${sk.id}</span>
        <span class="chip skip">${esc(sk.kind_label)}</span>
        ${lineChip}
      </div>
      <div class="issue-meta">
        ${sk.point ? `<span>交点 <b>[${sk.point.join(", ")}]</b></span>` : ""}
        ${sk.intersection_id ? `<span>所属交点 <b>${esc(sk.intersection_id)}</b></span>` : ""}
        ${sk.sample_count != null ? `<span>样本数 <b>${sk.sample_count}</b></span>` : ""}
        ${sk.overlap_length != null ? `<span>重叠长度 <b>${sk.overlap_length}</b></span>` : ""}
      </div>
      <div class="basis"><div class="method">${esc(sk.reason)}</div></div>
    </div>`;
}

function selectClosure(id, panTo) {
  selectedClosureId = id;
  clListEl.querySelectorAll(".issue-card").forEach(c =>
    c.classList.toggle("selected", c.dataset.id === id));
  if (panTo) {
    const xpt = clResult.intersections.find(i => i.id === id);
    const sk = clResult.skips.find(s => s.id === id);
    const pt = xpt ? xpt.point : (sk && sk.point ? sk.point : null);
    if (pt) {
      const pad = 60;
      fitBounds([pt[0] - pad, pt[1] - pad, pt[0] + pad, pt[1] + pad]);
      canvas.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
  draw();
}

function renderClLineBiases() {
  const rows = clResult.line_biases.map(l =>
    `<tr><td>${esc(l.line_id)}${l.is_anchor ? " ⚓" : ""}</td>` +
    `<td>${l.component_id ? esc(l.component_id) : "—"}</td>` +
    `<td>${l.bias == null ? "—" : l.bias}</td>` +
    `<td>${l.sample_count}</td><td>${l.intersection_count}</td></tr>`).join("");
  clLineBiasesEl.innerHTML =
    `<tr><th>测线</th><th>连通分量</th><th>偏置 b（⚓ 固定为零）</th><th>样本数</th><th>有效交点</th></tr>${rows}`;

  clComponentsEl.innerHTML = clResult.components.map(c => `
    <div class="cl-comp">
      <b>${esc(c.id)}</b>：${c.lines.map(esc).join(" → ")}（基准 ${esc(c.anchor_line)}）
      · 节点 ${c.node_count} · 观测 ${c.observation_count} · 冗余度 ${c.redundancy}
      · ${c.redundant ? "有冗余可判异常" : "无冗余（树）不可判异常"}
      · Huber δ=${c.huber_delta} · IRLS 迭代 ${c.iterations} 次
      （${c.converged ? "收敛" : "达到迭代上限"}）· 残差 RMS ${c.residual_rms == null ? "—" : c.residual_rms}
    </div>`).join("") || `<div class="empty-note">暂无连通分量。</div>`;
}

/* ---------------- 闭合面板绑定 ---------------- */
$("#btn-cl-run").addEventListener("click", () => runClosure(false));

$("#btn-cl-sample").addEventListener("click", async () => {
  const resp = await fetch("/api/closure/sample");
  const sample = await resp.json();
  // 示例自带配套任务（网格计划线），同时载入上方任务输入并触发复核，保证“沿用计划测线”
  inputEl.value = JSON.stringify(sample.task, null, 2);
  clInputEl.value = JSON.stringify(sample.samples, null, 2);
  $("#cl-max-enclosure").value = sample.options.max_enclosure_distance;
  $("#cl-huber-delta").value = sample.options.huber_delta;
  $("#cl-residual").value = sample.options.residual_threshold;
  // runAnalyze 成功后会检测到 cl-input 已有内容并自动触发 runClosure(true)
  await runAnalyze(true);
  // 示例替换了任务输入，将画布取景到新任务边界
  try {
    const bbox = geomBBox(sample.task.boundary, [Infinity, Infinity, -Infinity, -Infinity]);
    if (bbox[0] !== Infinity) fitBounds(bbox);
    draw();
  } catch (e) { /* 取景失败可双击复位 */ }
});

clExportBtn.addEventListener("click", () => {
  if (!clResult) return;
  const blob = new Blob([JSON.stringify(clResult, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "closure-result.json";
  a.click();
  URL.revokeObjectURL(a.href);
});

// 样本或参数修改后防抖自动重算
function clAutoRun() {
  clearTimeout(clTimer);
  clTimer = setTimeout(() => runClosure(true), 700);
}
clInputEl.addEventListener("input", clAutoRun);
for (const id of ["#cl-max-enclosure", "#cl-huber-delta", "#cl-residual"]) {
  $(id).addEventListener("input", clAutoRun);
}

/* ---------------- 闭合面板初始化：不抢占上方任务示例，待用户载入示例或粘贴样本 ---------------- */
(function initClosure() {
  clSetStatus("粘贴含 depth 的样本后自动闭合，或点“载入示例”载入配套网格任务演示");
})();

/* ================= 声速射线改正 ================= */
const rcProfileEl = $("#rc-profile");
const rcBeamsEl = $("#rc-beams");
const rcStatusEl = $("#rc-status");
const rcSummaryEl = $("#rc-summary");
const rcStatusFilterEl = $("#rc-status-filter");
const rcListEl = $("#rc-list");
const rcExportBtn = $("#btn-rc-export");
const rcRayViewWrap = $("#rc-rayview-wrap");
const rcRayViewTitle = $("#rc-rayview-title");
const rcRayCanvas = $("#rc-rayview");
const rcRayCtx = rcRayCanvas.getContext("2d");

const RC_COLOR = "#3fb950";
const RC_STRAIGHT_COLOR = "#f5a524";
const RC_FAIL_COLOR = "#e5484d";
const RC_REASON_LABEL = {
  refraction_limit: "折射极限",
  profile_gap: "剖面断档",
  beyond_profile_bottom: "超过剖面底界",
};

let rcResult = null;
let rcFilter = "all";        // all / ok / failed / <failure_reason>
let selectedBeamId = null;
let rcTimer = null;

function rcSetStatus(msg, isErr) {
  rcStatusEl.textContent = msg;
  rcStatusEl.classList.toggle("err", !!isErr);
}

function rcParseBox(text, arrayName) {
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    throw new Error(`${arrayName} JSON 解析失败：${err.message}`);
  }
  if (Array.isArray(parsed)) return parsed;
  if (parsed && Array.isArray(parsed[arrayName])) return parsed[arrayName];
  throw new Error(`${arrayName} JSON 必须是数组，或含 ${arrayName} 数组的对象`);
}

function rcBeamVisible(b) {
  if (rcFilter === "all") return true;
  if (rcFilter === "ok") return b.status === "ok";
  if (rcFilter === "failed") return b.status === "failed";
  return b.status === "failed" && b.failure_reason === rcFilter;
}

async function runRayCorrection(silent) {
  let profile, beams;
  try {
    profile = rcParseBox(rcProfileEl.value, "profile");
    beams = rcParseBox(rcBeamsEl.value, "beams");
  } catch (err) {
    rcSetStatus(err.message, true);
    return;
  }
  rcSetStatus("射线改正计算中…");
  try {
    const resp = await fetch("/api/ray-correction", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile, beams }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(errDetail(data, resp.status));
    rcResult = data;
    const s = data.summary;
    const fc = s.failure_counts;
    rcSetStatus(`改正完成：${s.beam_count} 个波束，成功 ${s.ok_count}、失败 ${s.failed_count}` +
      (s.failed_count ? `（折射极限 ${fc.refraction_limit} / 断档 ${fc.profile_gap} / 超底界 ${fc.beyond_profile_bottom}）` : "") +
      (s.max_deviation != null ? `；与直线对照最大偏差 ${s.max_deviation}` : ""));
    renderRcAll();
  } catch (err) {
    rcSetStatus(`改正失败：${err.message}`, true);
    if (!silent) rcListEl.innerHTML = `<div class="empty-note">改正失败：${esc(err.message)}</div>`;
  }
}

function renderRcAll() {
  renderRcSummary();
  renderRcFilter();
  renderRcList();
  rcExportBtn.disabled = !rcResult;
  if (selectedBeamId && !rcResult.beams.some(b => b.id === selectedBeamId)) {
    selectedBeamId = null;
  }
  // 结果刷新时仅在已展开的情况下重绘剖面图；展开/滚动由用户点击卡片触发
  rcRayViewWrap.hidden = !(selectedBeamId && rcResult.beams.some(b => b.id === selectedBeamId));
  if (!rcRayViewWrap.hidden) drawRcRayView();
  draw();
}

function renderRcSummary() {
  const s = rcResult.summary, fc = s.failure_counts;
  rcSummaryEl.innerHTML = [
    statCard("波束总数", s.beam_count),
    statCard("改正成功", s.ok_count, s.ok_count ? "good" : ""),
    statCard("失败", s.failed_count, s.failed_count ? "bad" : "good"),
    statCard("折射极限", fc.refraction_limit, fc.refraction_limit ? "warn" : "good"),
    statCard("剖面断档", fc.profile_gap, fc.profile_gap ? "warn" : "good"),
    statCard("超剖面底界", fc.beyond_profile_bottom, fc.beyond_profile_bottom ? "warn" : "good"),
    statCard("最大对照偏差", s.max_deviation == null ? "—" : s.max_deviation),
  ].join("");
}

function renderRcFilter() {
  const s = rcResult.summary, fc = s.failure_counts;
  const defs = [
    ["all", "全部", s.beam_count],
    ["ok", "成功", s.ok_count],
    ["failed", "失败", s.failed_count],
    ["refraction_limit", "折射极限", fc.refraction_limit],
    ["profile_gap", "剖面断档", fc.profile_gap],
    ["beyond_profile_bottom", "超底界", fc.beyond_profile_bottom],
  ];
  rcStatusFilterEl.innerHTML = defs.map(([k, label, n]) =>
    `<button data-f="${k}" class="${rcFilter === k ? "active" : ""}">${label}<span class="cnt">${n}</span></button>`
  ).join("");
  rcStatusFilterEl.querySelectorAll("button").forEach(btn =>
    btn.addEventListener("click", () => {
      rcFilter = btn.dataset.f;
      renderRcFilter();
      renderRcList();
      draw();
    }));
}

function rcSegRows(b) {
  if (!b.segments.length) {
    return `<tr><td colspan="9">${b.status === "failed" ? "无已走层段" : "—"}</td></tr>`;
  }
  return b.segments.map(seg => `
    <tr class="${seg.complete_layer ? "" : "partial"}">
      <td>#${seg.layer_index}${seg.complete_layer ? "" : "（部分段）"}</td>
      <td>${seg.sound_speed}</td>
      <td>${seg.angle_deg}°</td>
      <td>${seg.enter_depth} → ${seg.exit_depth}</td>
      <td>${seg.vertical}</td>
      <td>${seg.cross_track}</td>
      <td>${seg.path_length}</td>
      <td>${seg.travel_time}</td>
      <td>${seg.elapsed_one_way_time}</td>
    </tr>`).join("");
}

function renderRcList() {
  const beams = rcResult.beams.filter(rcBeamVisible);
  if (!beams.length) {
    rcListEl.innerHTML = `<div class="empty-note">当前筛选条件下没有波束。</div>`;
    return;
  }
  rcListEl.innerHTML = beams.map(b => {
    const ok = b.status === "ok";
    const reasonChip = ok ? "" : `<span class="chip rc-reason">${esc(RC_REASON_LABEL[b.failure_reason] || b.failure_reason)}</span>`;
    const pointTxt = ok ? `[${b.point.join(", ")}]` : "—（未外推）";
    const depthTxt = ok ? b.footprint_depth : `已至 ${b.reached_depth}`;
    return `
    <div class="issue-card rc-${b.status} ${b.id === selectedBeamId ? "selected" : ""}" data-id="${esc(b.id)}">
      <div class="issue-head">
        <span class="id">${esc(b.id)}</span>
        <span class="chip ${ok ? "rc-ok" : "rc-failed"}">${esc(b.status_label)}</span>
        ${reasonChip}
        <span class="chip line">θ=${b.angle_deg}°</span>
        <span class="chip line">h=${b.heading}°</span>
      </div>
      <div class="issue-meta">
        <span>换能器 <b>[${b.x}, ${b.y}] @ ${b.transducer_depth}</b></span>
        <span>双程/单程 <b>${b.two_way_travel_time} / ${b.one_way_travel_time} s</b></span>
        <span>落点深 <b>${depthTxt}</b></span>
        <span>横距 <b>${ok ? b.cross_track : `已走 ${b.cross_track_traveled}`}</b></span>
        <span>改正落点 <b>${pointTxt}</b></span>
        <span>已耗/剩余 <b>${b.elapsed_one_way_time} / ${b.remaining_one_way_time} s</b></span>
        ${ok ? `<span>对照偏差 <b>${b.deviation}</b></span>` : ""}
      </div>
      ${ok ? "" : `<div class="basis"><div class="method">${esc(b.failure_message || "")}</div></div>`}
      <details>
        <summary>逐层射线路径 / 直线对照（可复核）</summary>
        <div class="basis">
          <div class="method">${esc(rcResult.meta.ray_rule)}</div>
          <table class="rc-seg-table">
            <tr><th>层</th><th>声速</th><th>层内角度</th><th>进入→离开深度</th><th>垂深</th><th>横距</th><th>路径长</th><th>层内耗时</th><th>累计单程耗时</th></tr>
            ${rcSegRows(b)}
          </table>
          ${b.straight ? `<div class="method">直线对照（入射声速 ${b.straight.sound_speed}）：路径长 ${b.straight.path_length}，垂深 ${b.straight.vertical}，横距 ${b.straight.cross_track}，对照深 ${b.straight.footprint_depth}，对照点 [${b.straight.point.join(", ")}]${ok ? `，与改正落点偏差 ${b.deviation}` : ""}</div>`
            : `<div class="method">换能器不在任何声速层内，无法确定入射声速，直线对照不可计算。</div>`}
          ${b.ray_parameter_p != null ? `<div class="method">射线参数 p = ${b.ray_parameter_p}（snell 常量）</div>` : ""}
        </div>
      </details>
    </div>`;
  }).join("");

  rcListEl.querySelectorAll(".issue-card").forEach(card =>
    card.addEventListener("click", (e) => {
      if (e.target.closest("details")) return;
      selectBeam(card.dataset.id, true);
    }));
}

function selectBeam(id, panTo) {
  selectedBeamId = id;
  rcListEl.querySelectorAll(".issue-card").forEach(c =>
    c.classList.toggle("selected", c.dataset.id === id));
  if (panTo) {
    const b = rcResult.beams.find(x => x.id === id);
    if (b) {
      const pt = b.point || (b.straight ? b.straight.point : [b.x, b.y]);
      const pad = 40;
      fitBounds([pt[0] - pad, pt[1] - pad, pt[0] + pad, pt[1] + pad]);
      draw();
    }
    rcRayViewWrap.hidden = false;
    drawRcRayView();
    rcRayViewWrap.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

/* ---------------- 主画布叠加：改正落点 + 直线对照点 ---------------- */
function drawRcOverlay() {
  if (!rcResult) return;
  const dpr = window.devicePixelRatio || 1;
  for (const b of rcResult.beams) {
    if (!rcBeamVisible(b)) continue;
    const selected = b.id === selectedBeamId;
    const [sx0, sy0] = worldToScreen(b.x, b.y);
    // 换能器 → 落点细线
    if (b.point) {
      const [fx, fy] = worldToScreen(b.point[0], b.point[1]);
      ctx.beginPath();
      ctx.moveTo(sx0, sy0); ctx.lineTo(fx, fy);
      ctx.strokeStyle = hexA(RC_COLOR, selected ? 0.95 : 0.45);
      ctx.lineWidth = (selected ? 2.2 : 1) * dpr;
      ctx.stroke();
    }
    // 改正落点（成功）：实心圆
    if (b.point) {
      const [fx, fy] = worldToScreen(b.point[0], b.point[1]);
      ctx.beginPath();
      ctx.arc(fx, fy, (selected ? 6.5 : 4.5) * dpr, 0, Math.PI * 2);
      ctx.fillStyle = RC_COLOR;
      ctx.fill();
      ctx.lineWidth = (selected ? 2.2 : 1) * dpr;
      ctx.strokeStyle = selected ? "#ffffff" : "#0b1119";
      ctx.stroke();
    }
    // 直线对照点：橙色空心菱形
    if (b.straight) {
      const [cx, cy] = worldToScreen(b.straight.point[0], b.straight.point[1]);
      const r = (selected ? 6 : 4.5) * dpr;
      ctx.beginPath();
      ctx.moveTo(cx, cy - r); ctx.lineTo(cx + r, cy);
      ctx.lineTo(cx, cy + r); ctx.lineTo(cx - r, cy); ctx.closePath();
      ctx.strokeStyle = selected ? "#ffffff" : RC_STRAIGHT_COLOR;
      ctx.lineWidth = (selected ? 2.2 : 1.4) * dpr;
      ctx.stroke();
    }
    // 失败波束：换能器处红叉
    if (!b.point) {
      const r = (selected ? 6 : 4.5) * dpr;
      ctx.beginPath();
      ctx.moveTo(sx0 - r, sy0 - r); ctx.lineTo(sx0 + r, sy0 + r);
      ctx.moveTo(sx0 + r, sy0 - r); ctx.lineTo(sx0 - r, sy0 + r);
      ctx.strokeStyle = RC_FAIL_COLOR;
      ctx.lineWidth = (selected ? 2.5 : 1.6) * dpr;
      ctx.stroke();
    }
    if (selected) {
      const lp = b.point || (b.straight ? b.straight.point : [b.x, b.y]);
      const [lx, ly] = worldToScreen(lp[0], lp[1]);
      ctx.fillStyle = "#fff";
      ctx.font = `bold ${13 * dpr}px Consolas, monospace`;
      ctx.fillText(b.id, lx + 9, ly - 7);
    }
  }
}

/* ---------------- 射线路径剖面图（横距-垂深） ---------------- */
function rcResizeRayCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const w = rcRayCanvas.clientWidth, h = rcRayCanvas.clientHeight;
  if (rcRayCanvas.width !== Math.round(w * dpr) || rcRayCanvas.height !== Math.round(h * dpr)) {
    rcRayCanvas.width = Math.round(w * dpr);
    rcRayCanvas.height = Math.round(h * dpr);
  }
  return { dpr, w, h };
}

function drawRcRayView() {
  if (!rcResult || !selectedBeamId) return;
  const { dpr, w, h } = rcResizeRayCanvas();
  const c = rcRayCtx;
  c.clearRect(0, 0, rcRayCanvas.width, rcRayCanvas.height);
  const b = rcResult.beams.find(x => x.id === selectedBeamId);
  if (!b) return;
  rcRayViewTitle.textContent = `射线路径剖面 — ${b.id}（${b.status_label}${b.status === "failed" ? " · " + RC_REASON_LABEL[b.failure_reason] : ""}）`;

  const padL = 56 * dpr, padR = 110 * dpr, padT = 16 * dpr, padB = 34 * dpr;
  const z0 = b.transducer_depth;
  const profile = rcResult.profile;
  let zMin = Math.min(0, z0), zMax = rcResult.summary.profile_bottom;
  let xMin = 0, xMax = 0;
  for (const q of b.ray_path.coordinates) {
    xMin = Math.min(xMin, q[0]); xMax = Math.max(xMax, q[0]);
  }
  if (b.straight) {
    xMin = Math.min(xMin, b.straight.cross_track);
    xMax = Math.max(xMax, b.straight.cross_track);
    zMax = Math.max(zMax, b.straight.footprint_depth);
  }
  zMax = Math.max(zMax, b.reached_depth);
  if (xMin === xMax) { xMin -= 5; xMax += 5; }
  const padX = (xMax - xMin) * 0.08, padZ = (zMax - zMin) * 0.06 + 1e-9;
  xMin -= padX; xMax += padX; zMin = Math.max(0, zMin - padZ); zMax += padZ;

  const X = (cross) => padL + (cross - xMin) / (xMax - xMin) * (w * dpr - padL - padR);
  const Y = (z) => padT + (z - zMin) / (zMax - zMin) * (h * dpr - padT - padB);

  // 声速层带 + 断档红条
  const plotW = w * dpr - padL - padR;
  for (let i = 0; i < profile.length; i++) {
    const ly = profile[i];
    const yt = Y(Math.max(ly.top_depth, zMin)), yb = Y(Math.min(ly.bottom_depth, zMax));
    c.fillStyle = i % 2 === 0 ? "rgba(63,185,80,0.07)" : "rgba(125,179,255,0.06)";
    c.fillRect(padL, yt, plotW, Math.max(0, yb - yt));
    c.strokeStyle = "rgba(120,150,180,0.35)";
    c.lineWidth = 1 * dpr;
    c.beginPath(); c.moveTo(padL, yb); c.lineTo(padL + plotW, yb); c.stroke();
    c.fillStyle = "rgba(139,161,184,0.9)";
    c.font = `${11 * dpr}px Consolas, monospace`;
    c.fillText(`#${ly.layer_index} c=${ly.sound_speed}`, padL + plotW + 8 * dpr, yt + 13 * dpr);
  }
  for (let i = 1; i < profile.length; i++) {
    const gapTop = profile[i - 1].bottom_depth, gapBottom = profile[i].top_depth;
    if (gapBottom - gapTop > 1e-9) {
      const yt = Y(gapTop), yb = Y(Math.min(gapBottom, zMax));
      if (yb > yt) {
        c.fillStyle = "rgba(229,72,77,0.18)";
        c.fillRect(padL, yt, plotW, Math.min(yb, padT + (h * dpr - padT - padB)) - yt);
        c.fillStyle = "#ffb4b6";
        c.font = `${10.5 * dpr}px sans-serif`;
        c.fillText("断档（禁止外推）", padL + 8 * dpr, (yt + yb) / 2 + 3 * dpr);
      }
    }
  }

  // 坐标轴 / 网格
  c.strokeStyle = "rgba(120,150,180,0.25)";
  c.fillStyle = "rgba(139,161,184,0.85)";
  c.lineWidth = 1 * dpr;
  c.font = `${10.5 * dpr}px Consolas, monospace`;
  const zStep = niceStep((zMax - zMin) / 6);
  for (let z = Math.ceil(zMin / zStep) * zStep; z <= zMax + 1e-9; z += zStep) {
    const y = Y(z);
    c.beginPath(); c.moveTo(padL, y); c.lineTo(padL + plotW, y); c.stroke();
    c.fillText(String(Math.round(z * 100) / 100), 8 * dpr, y + 3 * dpr);
  }
  const xStep = niceStep((xMax - xMin) / 6);
  for (let x = Math.ceil(xMin / xStep) * xStep; x <= xMax + 1e-9; x += xStep) {
    const sx = X(x);
    c.beginPath(); c.moveTo(sx, padT); c.lineTo(sx, padT + (h * dpr - padT - padB)); c.stroke();
    c.fillText(String(Math.round(x * 100) / 100), sx - 12 * dpr, padT + (h * dpr - padT - padB) + 16 * dpr);
  }
  // cross = 0 纵轴加粗
  if (xMin < 0 && xMax > 0) {
    c.strokeStyle = "rgba(200,220,240,0.5)";
    c.beginPath(); c.moveTo(X(0), padT); c.lineTo(X(0), padT + (h * dpr - padT - padB)); c.stroke();
  }
  c.fillStyle = "rgba(200,220,240,0.85)";
  c.font = `${11 * dpr}px sans-serif`;
  c.fillText("横距 cross-track（右舷为正）", padL + plotW / 2 - 90 * dpr, h * dpr - 8 * dpr);
  c.save();
  c.translate(14 * dpr, padT + (h * dpr - padT - padB) / 2);
  c.rotate(-Math.PI / 2);
  c.fillText("垂深（向下）", -40 * dpr, 0);
  c.restore();

  // 直线对照（橙虚线）：换能器 (0,z0) → (straight.cross, straight.depth)
  if (b.straight) {
    c.beginPath();
    c.moveTo(X(0), Y(z0));
    c.lineTo(X(b.straight.cross_track), Y(b.straight.footprint_depth));
    c.setLineDash([6 * dpr, 4 * dpr]);
    c.strokeStyle = RC_STRAIGHT_COLOR;
    c.lineWidth = 1.8 * dpr;
    c.stroke();
    c.setLineDash([]);
    const dx = X(b.straight.cross_track), dy = Y(b.straight.footprint_depth), r = 5 * dpr;
    c.beginPath();
    c.moveTo(dx, dy - r); c.lineTo(dx + r, dy); c.lineTo(dx, dy + r); c.lineTo(dx - r, dy); c.closePath();
    c.strokeStyle = RC_STRAIGHT_COLOR; c.lineWidth = 1.6 * dpr; c.stroke();
  }

  // Snell 射线路径（绿折线）
  const pts = b.ray_path.coordinates;
  c.beginPath();
  pts.forEach((q, i) => {
    const sx = X(q[0]), sy = Y(q[1]);
    if (i === 0) c.moveTo(sx, sy); else c.lineTo(sx, sy);
  });
  c.strokeStyle = b.status === "ok" ? RC_COLOR : RC_FAIL_COLOR;
  c.lineWidth = 2.2 * dpr;
  c.stroke();
  // 换能器起点 + 路径节点
  pts.forEach((q, i) => {
    const sx = X(q[0]), sy = Y(q[1]);
    c.beginPath();
    c.arc(sx, sy, (i === 0 ? 4 : 3) * dpr, 0, Math.PI * 2);
    c.fillStyle = i === 0 ? "#ffffff" : (b.status === "ok" ? RC_COLOR : RC_FAIL_COLOR);
    c.fill();
  });
  if (b.status === "ok") {
    const [ex, ey] = [X(pts[pts.length - 1][0]), Y(pts[pts.length - 1][1])];
    c.fillStyle = RC_COLOR;
    c.beginPath(); c.arc(ex, ey, 6 * dpr, 0, Math.PI * 2); c.fill();
    c.strokeStyle = "#fff"; c.lineWidth = 1.5 * dpr; c.stroke();
  } else {
    const [ex, ey] = [X(pts[pts.length - 1][0]), Y(pts[pts.length - 1][1])];
    const r = 6 * dpr;
    c.beginPath();
    c.moveTo(ex - r, ey - r); c.lineTo(ex + r, ey + r);
    c.moveTo(ex + r, ey - r); c.lineTo(ex - r, ey + r);
    c.strokeStyle = RC_FAIL_COLOR; c.lineWidth = 2.2 * dpr; c.stroke();
  }

  // 图例
  c.font = `${11 * dpr}px sans-serif`;
  c.fillStyle = RC_COLOR; c.fillText("● Snell 改正射线", padL + 10 * dpr, padT + 4 * dpr);
  c.fillStyle = RC_STRAIGHT_COLOR; c.fillText("◇ 入射声速直线对照", padL + 150 * dpr, padT + 4 * dpr);
}

function niceStep(raw) {
  if (!(raw > 0) || !isFinite(raw)) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  return [1, 2, 5, 10].map(m => m * mag).find(v => v >= raw) || mag * 10;
}

/* ---------------- 绑定 ---------------- */
$("#btn-rc-run").addEventListener("click", () => runRayCorrection(false));

$("#btn-rc-sample").addEventListener("click", async () => {
  const resp = await fetch("/api/ray-correction/sample");
  const sample = await resp.json();
  rcProfileEl.value = JSON.stringify(sample.profile, null, 2);
  rcBeamsEl.value = JSON.stringify(sample.beams, null, 2);
  runRayCorrection(false);
});

rcExportBtn.addEventListener("click", () => {
  if (!rcResult) return;
  const blob = new Blob([JSON.stringify(rcResult, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "ray-correction-result.json";
  a.click();
  URL.revokeObjectURL(a.href);
});

function rcAutoRun() {
  clearTimeout(rcTimer);
  rcTimer = setTimeout(() => runRayCorrection(true), 700);
}
rcProfileEl.addEventListener("input", rcAutoRun);
rcBeamsEl.addEventListener("input", rcAutoRun);
window.addEventListener("resize", () => { if (rcResult) drawRcRayView(); });

/* ---------------- 初始化：载入示例并改正 ---------------- */
(async function initRayCorrection() {
  try {
    const resp = await fetch("/api/ray-correction/sample");
    const sample = await resp.json();
    rcProfileEl.value = JSON.stringify(sample.profile, null, 2);
    rcBeamsEl.value = JSON.stringify(sample.beams, null, 2);
    await runRayCorrection(true);
  } catch (err) {
    rcSetStatus(`初始化失败：${err.message}`, true);
  }
})();
