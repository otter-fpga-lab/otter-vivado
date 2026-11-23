import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";

const RESOURCE_KEYS = {
  lut: ["Slice LUTs", "CLB LUTs"],
  ff: ["Slice Registers", "CLB Registers", "Register as Flip Flop"],
  bram: ["Block RAM Tile"],
  dsp: ["DSPs"],
};

const STATUS_LABELS = {
  running: "运行中",
  queued: "排队中",
  completed: "已完成",
  succeeded: "已成功",
  stage_complete: "阶段完成",
  failed: "失败",
  error: "异常",
  cancelled: "已取消",
  timeout: "等待超时",
  idle: "未启动",
  connected: "已连接",
  busy: "会话忙碌",
  disconnected: "已断开",
  waiting: "等待采样",
  stale: "可能过期",
  unobserved: "未观测",
  unknown: "未知",
};

const app = new App({ name: "Vivado Console", version: "1.0.0" }, {});

const ui = {
  state: null,
  hostCaps: {},
  selectedUid: "",
  activeTab: "overview", // overview | diagnostics | reports | sessions
  statusFilter: "", // "" | running | completed | failed
  railSearch: "",
  diagSev: "ALL", // ALL | ERROR | CRITICAL WARNING | WARNING
  diagSearch: "",
  reportFilter: "",
  reportPreview: null, // { path, name, total_lines, returned_lines, start_line, truncated, tail, lines, loading, error }
  reportReqId: 0,
  inFlightState: false,
  autoTimer: null,
  toastTimer: null,
};

function byId(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function basename(pathStr) {
  if (!pathStr) return "";
  const parts = String(pathStr).replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || pathStr;
}

function fmtSlack(val) {
  if (typeof val !== "number" || !Number.isFinite(val)) return "—";
  const sign = val > 0 ? "+" : "";
  return `${sign}${val.toFixed(3)}`;
}

function fmtDuration(sec) {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec < 0) return "—";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function fmtBytes(size) {
  if (typeof size !== "number" || !Number.isFinite(size)) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}

function fmtTime(epochSec) {
  if (typeof epochSec !== "number" || !Number.isFinite(epochSec) || epochSec <= 0) return "—";
  try {
    return new Date(epochSec * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  } catch {
    return "—";
  }
}

function statusBucket(status) {
  const s = String(status || "").toLowerCase();
  if (s === "running" || s === "queued" || s === "busy" || s === "waiting") return "running";
  if (s === "completed" || s === "succeeded" || s === "connected" || s === "stage_complete") return "completed";
  if (s === "failed" || s === "error" || s === "disconnected" || s === "cancelled" || s === "timeout") return "failed";
  return "other";
}

function ledClass(status, timing) {
  const bucket = statusBucket(status);
  if (bucket === "running") return "led-running";
  if (bucket === "failed") return "led-fail";
  if (bucket === "completed") {
    if (timing && timing.parse_status === "ok" && timing.met === false) return "led-warn";
    return "led-ok";
  }
  return "";
}

function showToast(message, kind = "info") {
  const el = byId("toast-banner");
  if (!el) return;
  if (ui.toastTimer) clearTimeout(ui.toastTimer);
  el.textContent = message;
  el.dataset.kind = kind;
  el.hidden = false;
  ui.toastTimer = setTimeout(() => {
    el.hidden = true;
  }, 5000);
}

function setConnState(state, text) {
  const badge = byId("conn-badge");
  if (!badge) return;
  badge.dataset.state = state;
  badge.textContent = text;
}

function extractMonitorTiming(monitor) {
  const reports = Array.isArray(monitor.reports) ? monitor.reports : [];
  let chosen = null;
  let score = -1;
  for (const r of reports) {
    if (r?.freshness === "stale" || r?.freshness === "mismatched") continue;
    const sum = r?.summary;
    if (!sum || sum.kind !== "timing") continue;
    const s = sum.summary || {};
    if (s.parse_status !== "ok") continue;
    const stageScore = sum.source_stage === "post-route" ? 3 : sum.source_stage === "post-place" ? 2 : 1;
    if (stageScore > score) {
      score = stageScore;
      chosen = {
        wns: s.wns,
        tns: s.tns,
        whs: s.whs,
        ths: s.ths,
        met: s.timing_met,
        failing_endpoints: s.failing_endpoints,
        total_endpoints: s.total_endpoints,
        stage: sum.source_stage || "unknown",
        parse_status: "ok",
        source_report: r.name || "",
      };
    }
  }
  return chosen || { wns: null, tns: null, whs: null, ths: null, met: null, stage: "unknown", parse_status: "missing" };
}

function extractMonitorUtil(monitor) {
  const reports = Array.isArray(monitor.reports) ? monitor.reports : [];
  let chosen = null;
  for (const r of reports) {
    if (r?.freshness === "stale" || r?.freshness === "mismatched") continue;
    const sum = r?.summary;
    if (!sum || sum.kind !== "utilization" || !Array.isArray(sum.resources)) continue;
    const map = {};
    for (const row of sum.resources) {
      map[String(row.name || "").toLowerCase()] = row;
    }
    const out = { lut: null, ff: null, bram: null, dsp: null };
    for (const [k, names] of Object.entries(RESOURCE_KEYS)) {
      const hit = names.map((n) => map[n.toLowerCase()]).find(Boolean);
      if (hit) {
        out[k] = { used: hit.used, avail: hit.available, pct: Number(hit.percent || 0) };
      }
    }
    if (Object.values(out).some(Boolean)) {
      chosen = out;
    }
  }
  return chosen || { lut: null, ff: null, bram: null, dsp: null };
}

function extractMonitorDiagnostics(monitor) {
  const run = monitor.run || {};
  const diag = run.diagnostics || {};
  const tail = Array.isArray(run.tail) ? run.tail : [];
  const issues = [];
  const warnItems = [];
  for (const lineObj of tail) {
    const lineNo = lineObj?.lineno || 0;
    const txt = String(lineObj?.text || "").trim();
    const mId = txt.match(/\[(\w+[\s\-][\w\-]+)\]/);
    const id = mId ? mId[1] : "LOG";
    if (txt.startsWith("ERROR:")) {
      issues.push({ severity: "ERROR", id, count: 1, line: lineNo, message: txt });
    } else if (txt.startsWith("CRITICAL WARNING:")) {
      issues.push({ severity: "CRITICAL WARNING", id, count: 1, line: lineNo, message: txt });
    } else if (txt.startsWith("WARNING:")) {
      warnItems.push({ severity: "WARNING", id, count: 1, line: lineNo, message: txt });
    }
  }
  return {
    errors: typeof diag.errors === "number" ? diag.errors : 0,
    critical_warnings: typeof diag.critical_warnings === "number" ? diag.critical_warnings : 0,
    warnings: typeof diag.warnings === "number" ? diag.warnings : 0,
    all_issues: issues,
    warning_items: warnItems,
    scope: diag.scope || "",
  };
}

function buildItems(state) {
  const items = [];
  if (!state) return items;

  for (const mon of state.run_monitors || []) {
    const run = mon.run || {};
    const timing = extractMonitorTiming(mon);
    const utilization = extractMonitorUtil(mon);
    const diagnostics = extractMonitorDiagnostics(mon);
    const status = run.state || mon.connection || "unknown";
    items.push({
      uid: `run:${mon.session_id}:${mon.run_name}:${mon.target_step}`,
      category: "run_monitor",
      categoryLabel: "工程构建 Runs",
      kindBadge: mon.target_step || "run",
      title: `${mon.run_name} · ${run.top || run.project || mon.session_id}`,
      subtitle: `${mon.session_id} · ${run.status || mon.connection || ""}`,
      status,
      top: run.top || "",
      part: "",
      project: run.project || "",
      version: run.version || "",
      elapsedText: run.elapsed || "—",
      progressPct: typeof run.progress_percent === "number" ? run.progress_percent : null,
      timing,
      utilization,
      diagnostics,
      reports: (mon.reports || []).map((r) => ({
        name: r.name,
        path: r.path,
        size: r.size,
        kind: "rpt",
        stage: r.stage,
        freshness: r.freshness,
      })),
      phases: (run.phases || []).map((p) => ({ line: p.lineno || 0, text: p.text || "" })),
      currentPhase: run.current_phase || "",
      logTail: (run.tail || []).map((t) => ({ line: t.lineno || 0, text: t.text || "" })),
      outputDir: run.directory || "",
      logPath: run.log_path || "",
      xprPath: run.project_file || "",
      raw: mon,
    });
  }

  for (const job of state.batch_jobs || []) {
    const isOoc = job.kind === "ooc_synth";
    items.push({
      uid: `job:${job.job_id}`,
      jobId: job.job_id,
      category: "batch_job",
      categoryLabel: "Batch / OOC 任务",
      kindBadge: isOoc ? "OOC 综合" : "Batch Tcl",
      title: job.top || basename(job.tcl_path) || job.job_id,
      subtitle: `${job.part || "未指定 Part"} · ${job.job_id}`,
      status: job.status || "unknown",
      top: job.top || "",
      part: job.part || "",
      project: job.project || "",
      version: job.vivado_version || "",
      elapsedText: fmtDuration(job.elapsed_sec),
      progressPct: null,
      timing: job.timing || { wns: null, tns: null, whs: null, ths: null, met: null, parse_status: "missing" },
      utilization: job.utilization || { lut: null, ff: null, bram: null, dsp: null },
      diagnostics: job.diagnostics || { errors: 0, critical_warnings: 0, warnings: 0, all_issues: [], warning_items: [] },
      forensics: job.forensics || null,
      reports: job.reports || [],
      phases: job.phases || [],
      currentPhase: job.current_phase || "",
      logTail: job.log_tail || [],
      outputDir: job.output_dir || "",
      logPath: job.log_path || "",
      tclPath: job.tcl_path || "",
      exitCode: job.exit_code,
      pid: job.pid,
      errorMessage: job.error_message || "",
      raw: job,
    });
  }

  for (const insp of state.inspected_targets || []) {
    const kindLabel = insp.kind === "xpr_project" ? "离线 .xpr" : insp.kind === "runs_dir" ? "离线 .runs" : "离线目录";
    items.push({
      uid: `inspected:${insp.target_path}`,
      targetPath: insp.target_path,
      category: "inspected",
      categoryLabel: "离线工程 / 报告",
      kindBadge: kindLabel,
      title: insp.project || insp.top || basename(insp.target_path),
      subtitle: `${insp.part || "未识别 Part"} · ${insp.target_path}`,
      status: insp.status || "unknown",
      top: insp.top || "",
      part: insp.part || "",
      project: insp.project || "",
      version: insp.vivado_version || "",
      elapsedText: insp.inspected_at ? `检视于 ${fmtTime(insp.inspected_at)}` : "—",
      progressPct: null,
      timing: insp.timing || { wns: null, tns: null, whs: null, ths: null, met: null, parse_status: "missing" },
      utilization: insp.utilization || { lut: null, ff: null, bram: null, dsp: null },
      diagnostics: insp.diagnostics || { errors: 0, critical_warnings: 0, warnings: 0, all_issues: [], warning_items: [] },
      reports: insp.reports || [],
      phases: insp.phases || [],
      currentPhase: insp.current_phase || "",
      logTail: insp.log_tail || [],
      outputDir: insp.output_dir || "",
      logPath: insp.log_path || "",
      xprPath: insp.xpr_path || "",
      runs: insp.runs || [],
      raw: insp,
    });
  }

  for (const dbg of state.debug_sessions || []) {
    const hw = dbg.hardware || {};
    const ilas = Array.isArray(hw.ilas) ? hw.ilas.length : 0;
    const vios = Array.isArray(hw.vios) ? hw.vios.length : 0;
    items.push({
      uid: `debug:${dbg.session_id}`,
      category: "debug_session",
      categoryLabel: "硬件调试 / ILA",
      kindBadge: `控制权: ${dbg.control || "manual"}`,
      title: `${dbg.session_id} · ${dbg.selection?.device || "未选器件"}`,
      subtitle: `ILA ${ilas} · VIO ${vios} · rev ${dbg.revision ?? 0}`,
      status: dbg.connection || "unobserved",
      top: "",
      part: hw.part || "",
      project: "",
      version: "",
      elapsedText: dbg.observed_at ? fmtTime(dbg.observed_at) : "—",
      progressPct: null,
      timing: { wns: null, tns: null, whs: null, ths: null, met: null, parse_status: "missing" },
      utilization: { lut: null, ff: null, bram: null, dsp: null },
      diagnostics: { errors: dbg.error ? 1 : 0, critical_warnings: 0, warnings: 0, all_issues: dbg.error ? [{ severity: "ERROR", id: "DEBUG", count: 1, line: 0, message: dbg.error }] : [], warning_items: [] },
      reports: [],
      phases: [],
      currentPhase: "",
      logTail: [],
      outputDir: "",
      logPath: "",
      raw: dbg,
    });
  }

  return items;
}

function updateSummaryCounts(items) {
  let running = 0;
  let completed = 0;
  let failed = 0;
  for (const item of items) {
    const b = statusBucket(item.status);
    if (b === "running") running += 1;
    else if (b === "completed") completed += 1;
    else if (b === "failed") failed += 1;
  }
  byId("cnt-running").textContent = String(running);
  byId("cnt-completed").textContent = String(completed);
  byId("cnt-failed").textContent = String(failed);

  for (const chip of document.querySelectorAll(".sum-chip")) {
    chip.dataset.active = ui.statusFilter === chip.dataset.filter ? "true" : "false";
  }

  const finishedBatchCount = (ui.state?.batch_jobs || []).filter((j) => j.status !== "running").length;
  byId("btn-clear-finished").hidden = finishedBatchCount === 0;
}

function renderRail(items) {
  const railEl = byId("rail-list");
  if (!railEl) return;

  const query = ui.railSearch.trim().toLowerCase();
  const filtered = items.filter((item) => {
    if (ui.statusFilter && statusBucket(item.status) !== ui.statusFilter) return false;
    if (!query) return true;
    const haystack = `${item.title} ${item.subtitle} ${item.part} ${item.top} ${item.status}`.toLowerCase();
    return haystack.includes(query);
  });

  if (!filtered.length) {
    railEl.innerHTML = `<p class="muted" style="padding: 8px 4px;">${items.length ? "没有匹配过滤条件的条目。" : "暂无运行或检视条目。"}</p>`;
    return;
  }

  const groupOrder = [
    ["run_monitor", "工程构建 Runs"],
    ["batch_job", "Batch / OOC 任务"],
    ["inspected", "离线工程 / 报告"],
    ["debug_session", "硬件调试 / ILA"],
  ];

  const html = [];
  for (const [cat, label] of groupOrder) {
    const groupItems = filtered.filter((i) => i.category === cat);
    if (!groupItems.length) continue;
    html.push(`<div class="rail-group">`);
    html.push(`<div class="rail-group-title"><span>${escapeHtml(label)}</span><span>${groupItems.length}</span></div>`);
    html.push(`<div class="rail-cards">`);
    for (const item of groupItems) {
      const selected = item.uid === ui.selectedUid ? "true" : "false";
      const hasTiming = item.timing && item.timing.parse_status === "ok" && typeof item.timing.wns === "number";
      const wnsText = hasTiming ? `WNS ${fmtSlack(item.timing.wns)}` : "WNS —";
      const metAttr = hasTiming ? (item.timing.met ? "true" : "false") : "na";
      const led = ledClass(item.status, item.timing);
      html.push(`
        <button type="button" class="nav-card" data-uid="${escapeHtml(item.uid)}" data-selected="${selected}">
          <div class="nav-card-top">
            <div class="nav-card-title">
              <span class="led ${led}"></span>
              <span class="name">${escapeHtml(item.title)}</span>
            </div>
            <span class="wns-pill" data-met="${metAttr}">${escapeHtml(wnsText)}</span>
          </div>
          <div class="nav-card-meta">
            <span>${escapeHtml(item.subtitle)}</span>
            <span>${escapeHtml(item.elapsedText)}</span>
          </div>
        </button>
      `);
    }
    html.push(`</div></div>`);
  }
  railEl.innerHTML = html.join("");
}

function renderEmptyState() {
  const emptyEl = byId("empty-stage");
  const detailEl = byId("detail-shell");
  detailEl.hidden = true;
  emptyEl.hidden = false;

  const installs = ui.state?.vivado_installs?.installations || [];
  const selectedVivado = ui.state?.vivado_installs?.selected_path || "";
  const sessions = ui.state?.sessions || [];

  const installRows = installs.length
    ? installs
        .map(
          (inst) => `
          <tr>
            <td class="mono">${escapeHtml(inst.version_from_path || "unknown")}</td>
            <td class="mono">${escapeHtml(inst.path || "")}${inst.path === selectedVivado ? ' <span class="kind-chip">默认</span>' : ""}</td>
            <td>${escapeHtml((inst.sources || []).join(", "))}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="3" class="muted">未在默认路径或 PATH 检测到 Vivado 安装。</td></tr>`;

  emptyEl.innerHTML = `
    <div class="empty-hero">
      <div class="empty-art" aria-hidden="true">
        <svg viewBox="0 0 32 32" width="28" height="28" fill="none">
          <rect x="7" y="7" width="18" height="18" rx="3" stroke="currentColor" stroke-width="1.8"/>
          <path d="M10 17h3l2-5 2.5 8 2-4H22" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
      <div>
        <h2>Vivado 控制台已就绪</h2>
        <p>在上方输入任意 <code>.xpr</code> 工程、<code>.runs</code> 目录或批处理 / OOC 综合输出目录路径，即可零 Token 查看时序裕量、LUT/FF/BRAM/DSP 资源占用、去重告警和报告原文。AI 调用 <code>run_ooc_synth</code>、<code>run_batch_tcl</code> 或 <code>open_run_monitor</code> 时也会实时同步到这里。</p>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h3>本机检测到的 Vivado 安装（仅路径探测，未启动进程）</h3>
        <span class="kind-chip">${installs.length} 个安装 · ${sessions.length} 个活跃会话</span>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>版本</th><th>可执行路径</th><th>发现来源</th></tr></thead>
          <tbody>${installRows}</tbody>
        </table>
      </div>
    </div>
  `;
}

function renderHero(item) {
  const hero = byId("detail-hero");
  const statusText = STATUS_LABELS[item.status] || item.status || "未知";
  const led = ledClass(item.status, item.timing);
  const canCancel = item.category === "batch_job" && item.status === "running";
  const canRemove = item.category === "inspected";
  const copyTarget = item.xprPath || item.outputDir || item.logPath || item.targetPath || "";

  const canSendMsg = Boolean(ui.hostCaps?.message);
  const canUpdateCtx = Boolean(ui.hostCaps?.updateModelContext);

  const errNote = item.errorMessage
    ? `<div class="toast-banner" data-kind="error" style="margin-top:2px;">${escapeHtml(item.errorMessage)}</div>`
    : "";

  hero.innerHTML = `
    <div class="hero-row">
      <div class="hero-title-group">
        <div class="hero-badges">
          <span class="status-badge" data-status="${escapeHtml(item.status)}">
            <span class="led ${led}"></span>
            ${escapeHtml(statusText)}
          </span>
          <span class="kind-chip">${escapeHtml(item.kindBadge)}</span>
          ${item.version ? `<span class="kind-chip">Vivado ${escapeHtml(item.version)}</span>` : ""}
          ${item.timing?.stage && item.timing.stage !== "unknown" ? `<span class="kind-chip">${escapeHtml(item.timing.stage)}</span>` : ""}
        </div>
        <h2 class="hero-title">${escapeHtml(item.title)}</h2>
        <div class="hero-sub">${escapeHtml(item.subtitle)}</div>
      </div>

      <div class="hero-actions">
        ${canCancel ? `<button type="button" class="btn btn-danger btn-sm" id="hero-cancel-btn">取消任务</button>` : ""}
        ${canRemove ? `<button type="button" class="btn btn-sm" id="hero-remove-btn">移除检视</button>` : ""}
        ${copyTarget ? `<button type="button" class="btn btn-sm" id="hero-copy-btn">复制路径</button>` : ""}
        ${canUpdateCtx ? `<button type="button" class="btn btn-sm" id="hero-ctx-btn" title="附加上下文给下次对话">附加上下文</button>` : ""}
        ${canSendMsg ? `<button type="button" class="btn btn-primary btn-sm" id="hero-ask-btn">发到对话</button>` : ""}
      </div>
    </div>
    ${errNote}
  `;

  if (canCancel) {
    byId("hero-cancel-btn")?.addEventListener("click", () => handleCancelJob(item.jobId));
  }
  if (canRemove) {
    byId("hero-remove-btn")?.addEventListener("click", () => handleRemoveInspected(item.targetPath));
  }
  if (copyTarget) {
    byId("hero-copy-btn")?.addEventListener("click", () => copyText(copyTarget));
  }
  if (canUpdateCtx) {
    byId("hero-ctx-btn")?.addEventListener("click", () => handleAttachContext(item));
  }
  if (canSendMsg) {
    byId("hero-ask-btn")?.addEventListener("click", () => handleSendToChat(item));
  }
}

function renderOverview(item) {
  const el = byId("tab-panel-overview");
  if (item.category === "debug_session") {
    const dbg = item.raw || {};
    const hw = dbg.hardware || {};
    const ilas = hw.ilas || [];
    const vios = hw.vios || [];
    el.innerHTML = `
      <div class="stack">
        <div class="panel">
          <div class="panel-head"><h3>调试会话状态</h3><span class="kind-chip">revision ${escapeHtml(dbg.revision ?? 0)}</span></div>
          <div class="meta-grid">
            <div class="meta-item"><span class="meta-k">目标 Target</span><span class="meta-v">${escapeHtml(dbg.selection?.target || "—")}</span></div>
            <div class="meta-item"><span class="meta-k">器件 Device</span><span class="meta-v">${escapeHtml(dbg.selection?.device || "—")}</span></div>
            <div class="meta-item"><span class="meta-k">控制权 Control</span><span class="meta-v">${escapeHtml(dbg.control || "manual")}</span></div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-head"><h3>调试核概览</h3><span class="muted">ILA ${ilas.length} / VIO ${vios.length}</span></div>
          <p class="muted">完整探针与操作历史请在「调试与会话」标签页查看。</p>
        </div>
      </div>
    `;
    return;
  }

  const t = item.timing || {};
  const timingOk = t.parse_status === "ok";
  const tiles = [
    { key: "wns", label: "WNS · Setup 最差", val: timingOk ? t.wns : null },
    { key: "tns", label: "TNS · Setup 总负裕量", val: timingOk ? t.tns : null },
    { key: "whs", label: "WHS · Hold 最差", val: timingOk ? t.whs : null },
    { key: "ths", label: "THS · Hold 总负裕量", val: timingOk ? t.ths : null },
  ];

  const timingHtml = tiles
    .map((tile) => {
      const valid = typeof tile.val === "number" && Number.isFinite(tile.val);
      const tone = valid ? (tile.val < 0 ? "fail" : "ok") : "na";
      return `
        <div class="timing-tile" data-tone="${tone}">
          <span class="timing-label">${escapeHtml(tile.label)}</span>
          <span class="timing-val">${escapeHtml(fmtSlack(tile.val))}${valid ? '<span class="timing-unit">ns</span>' : ""}</span>
        </div>
      `;
    })
    .join("");

  const timingNote =
    t.parse_status === "no_timing_data"
      ? "报告摘要为 NA（无时序约束或无可分析端点）"
      : timingOk
        ? `${t.met ? "时序收敛 (MET)" : "存在时序违例"} · 阶段 ${t.stage || "unknown"}${typeof t.failing_endpoints === "number" ? ` · 失败端点 ${t.failing_endpoints}/${t.total_endpoints ?? "?"}` : ""}`
        : "尚未解析到有效时序报告（未知保持 —，绝不伪造 0）";

  const resMeta = [
    ["lut", "LUT 查找表"],
    ["ff", "FF 寄存器"],
    ["bram", "Block RAM"],
    ["dsp", "DSP 单元"],
  ];
  const util = item.utilization || {};
  const utilHtml = resMeta
    .map(([key, label]) => {
      const r = util[key];
      if (!r || typeof r.pct !== "number") {
        return `
          <div class="util-row">
            <div class="util-row-top">
              <span class="util-name">${escapeHtml(label)}</span>
              <span class="util-nums">—</span>
            </div>
            <div class="util-bar-track"><div class="util-bar-fill" style="width:0%"></div></div>
          </div>
        `;
      }
      const pct = Math.max(0, Math.min(100, Number(r.pct)));
      const level = pct >= 90 ? "crit" : pct >= 70 ? "warn" : "ok";
      return `
        <div class="util-row">
          <div class="util-row-top">
            <span class="util-name">${escapeHtml(label)}</span>
            <span class="util-nums">${escapeHtml(r.used)} / ${escapeHtml(r.avail)} (${pct.toFixed(2)}%)</span>
          </div>
          <div class="util-bar-track"><div class="util-bar-fill" data-level="${level}" style="width:${pct}%"></div></div>
        </div>
      `;
    })
    .join("");

  const progressBlock =
    typeof item.progressPct === "number"
      ? `<div style="margin-bottom:8px;">
           <div class="util-row-top" style="margin-bottom:4px;"><span>Vivado 原生进度</span><span class="util-nums">${item.progressPct}%</span></div>
           <div class="util-bar-track"><div class="util-bar-fill" style="width:${Math.max(0, Math.min(100, item.progressPct))}%"></div></div>
         </div>`
      : "";

  const phases = item.phases || [];
  const phaseHtml = phases.length
    ? `<ol class="timeline">${phases
        .slice(-16)
        .map(
          (p) =>
            `<li><span class="timeline-ln">L${escapeHtml(p.line || "?")}</span><span>${escapeHtml(p.text)}</span></li>`
        )
        .join("")}</ol>`
    : `<p class="muted">暂未观测到 Phase 阶段行。</p>`;

  const runsRows = (item.runs || []).length
    ? `<div class="panel">
         <div class="panel-head"><h3>工程 Runs 状态</h3><span class="kind-chip">${item.runs.length} 个</span></div>
         <div class="table-wrap">
           <table><thead><tr><th>Run</th><th>状态</th><th>目录</th></tr></thead><tbody>
           ${item.runs
             .map(
               (r) =>
                 `<tr><td class="mono">${escapeHtml(r.run_id)}</td><td><span class="status-badge" data-status="${escapeHtml(r.status)}">${escapeHtml(STATUS_LABELS[r.status] || r.status)}</span></td><td class="mono">${escapeHtml(r.dir)}</td></tr>`
             )
             .join("")}
           </tbody></table>
         </div>
       </div>`
    : "";

  el.innerHTML = `
    <div class="stack">
      <div class="panel">
        <div class="panel-head">
          <h3>时序裕量 (Timing)</h3>
          <span class="muted">${escapeHtml(t.source_report || "")}</span>
        </div>
        <div class="timing-strip">${timingHtml}</div>
        <p class="muted" style="margin-top:8px;">${escapeHtml(timingNote)}</p>
      </div>

      <div class="panel">
        <div class="panel-head">
          <h3>资源占用 (Utilization)</h3>
          <span class="muted">${escapeHtml(item.raw?.utilization_source || "")}</span>
        </div>
        <div class="util-grid">${utilHtml}</div>
      </div>

      <div class="panel">
        <div class="panel-head"><h3>元数据与路径</h3></div>
        <div class="meta-grid">
          <div class="meta-item"><span class="meta-k">Top 模块</span><span class="meta-v">${escapeHtml(item.top || "—")}</span></div>
          <div class="meta-item"><span class="meta-k">器件 Part</span><span class="meta-v">${escapeHtml(item.part || "—")}</span></div>
          <div class="meta-item"><span class="meta-k">耗时 / 状态</span><span class="meta-v">${escapeHtml(item.elapsedText)}${typeof item.exitCode === "number" ? ` (rc=${item.exitCode})` : ""}</span></div>
          <div class="meta-item"><span class="meta-k">输出目录</span><span class="meta-v">${escapeHtml(item.outputDir || "—")}</span></div>
          <div class="meta-item"><span class="meta-k">主日志路径</span><span class="meta-v">${escapeHtml(item.logPath || "—")}</span></div>
          <div class="meta-item"><span class="meta-k">${item.xprPath ? "工程 .xpr" : "Tcl 脚本"}</span><span class="meta-v">${escapeHtml(item.xprPath || item.tclPath || "—")}</span></div>
        </div>
      </div>

      ${runsRows}

      <div class="panel">
        <div class="panel-head">
          <h3>阶段与进度时间线</h3>
          <span class="muted">${escapeHtml(item.currentPhase || "—")}</span>
        </div>
        ${progressBlock}
        ${phaseHtml}
      </div>
    </div>
  `;
}

function renderDiagnostics(item) {
  const el = byId("tab-panel-diagnostics");
  const d = item.diagnostics || {};
  const issues = [...(d.all_issues || d.top_issues || []), ...(d.warning_items || [])];
  const query = ui.diagSearch.trim().toLowerCase();

  const filtered = issues.filter((issue) => {
    if (ui.diagSev !== "ALL" && issue.severity !== ui.diagSev) return false;
    if (!query) return true;
    const hay = `${issue.id} ${issue.category} ${issue.message} ${(issue.files || []).join(" ")}`.toLowerCase();
    return hay.includes(query);
  });

  const forensics = item.forensics;
  const forensicsHtml = forensics && forensics.silent_exit_suspected
    ? `<div class="diag-card" data-sev="ERROR">
         <div class="diag-card-head">
           <div class="diag-chips">
             <span class="sev-tag" data-sev="ERROR">静默退出取证</span>
             <span class="id-tag">Vivado_init.tcl / WER</span>
           </div>
         </div>
         <p class="diag-msg">${escapeHtml(forensics.reason || "子进程非零退出且无标准 ERROR 行")}</p>
         <div class="diag-sug">${escapeHtml(
           (forensics.vivado_init || [])
             .map((f) => `${f.path}: ${f.exists ? (f.max_threads ? `maxThreads=${f.max_threads}` : "未设 maxThreads") : "不存在"}`)
             .join(" | ")
         )}</div>
       </div>`
    : "";

  const cardsHtml = filtered.length
    ? filtered
        .map(
          (issue) => `
          <div class="diag-card" data-sev="${escapeHtml(issue.severity)}">
            <div class="diag-card-head">
              <div class="diag-chips">
                <span class="sev-tag" data-sev="${escapeHtml(issue.severity)}">${escapeHtml(issue.severity)}</span>
                <span class="id-tag">[${escapeHtml(issue.id || "UNKNOWN")}]</span>
                ${issue.count > 1 ? `<span class="count-tag">×${escapeHtml(issue.count)}</span>` : ""}
                ${issue.line ? `<span class="muted">L${escapeHtml(issue.line)}</span>` : ""}
              </div>
              <span class="muted">${escapeHtml((issue.files || []).join(", "))}</span>
            </div>
            <p class="diag-msg">${escapeHtml(issue.message || "")}</p>
            ${issue.suggestion && issue.category !== "UNKNOWN" ? `<div class="diag-sug">${escapeHtml(issue.suggestion)}</div>` : ""}
          </div>
        `
        )
        .join("")
    : `<div class="panel"><p class="muted">当前过滤条件下无告警或错误条目。${d.scope ? ` (${escapeHtml(d.scope)})` : ""}</p></div>`;

  el.innerHTML = `
    <div class="stack">
      <div class="panel">
        <div class="diag-toolbar">
          <div class="filter-pills">
            <button type="button" class="pill-btn" data-sev="ALL" data-active="${ui.diagSev === "ALL"}">全部 (${issues.length})</button>
            <button type="button" class="pill-btn" data-sev="ERROR" data-active="${ui.diagSev === "ERROR"}">ERROR (${d.errors ?? 0})</button>
            <button type="button" class="pill-btn" data-sev="CRITICAL WARNING" data-active="${ui.diagSev === "CRITICAL WARNING"}">CRITICAL WARNING (${d.critical_warnings ?? 0})</button>
            <button type="button" class="pill-btn" data-sev="WARNING" data-active="${ui.diagSev === "WARNING"}">WARNING (${d.warnings ?? 0})</button>
          </div>
          <input id="diag-search-input" class="diag-search" type="search" value="${escapeHtml(ui.diagSearch)}" placeholder="搜索消息、ID 或文件..." aria-label="搜索诊断条目" />
        </div>
      </div>
      ${forensicsHtml}
      <div class="diag-list">${cardsHtml}</div>
    </div>
  `;

  for (const btn of el.querySelectorAll(".pill-btn")) {
    btn.addEventListener("click", () => {
      ui.diagSev = btn.dataset.sev;
      renderDiagnostics(item);
    });
  }
  const searchEl = byId("diag-search-input");
  if (searchEl) {
    searchEl.addEventListener("input", (ev) => {
      ui.diagSearch = ev.target.value;
      const pos = ev.target.selectionStart;
      renderDiagnostics(item);
      const nextEl = byId("diag-search-input");
      if (nextEl) {
        nextEl.focus();
        nextEl.setSelectionRange(pos, pos);
      }
    });
  }
}

function classifyLogLine(text) {
  const s = String(text || "").trimStart();
  if (s.startsWith("ERROR:")) return "err";
  if (s.startsWith("CRITICAL WARNING:")) return "cw";
  return "";
}

function renderReports(item) {
  const el = byId("tab-panel-reports");
  const reports = [...(item.reports || [])];
  if (item.logPath && !reports.some((r) => r.path === item.logPath)) {
    reports.unshift({ name: basename(item.logPath), path: item.logPath, kind: "log", size: 0 });
  }
  if (item.tclPath && !reports.some((r) => r.path === item.tclPath)) {
    reports.push({ name: basename(item.tclPath), path: item.tclPath, kind: "tcl", size: 0 });
  }

  const chipsHtml = reports.length
    ? reports
        .map((r) => {
          const selected = ui.reportPreview?.path === r.path;
          return `<button type="button" class="rpt-chip" data-rpath="${escapeHtml(r.path)}" data-selected="${selected}">
            <span>${escapeHtml(r.name)}</span>
            ${r.size ? `<span class="muted">${escapeHtml(fmtBytes(r.size))}</span>` : ""}
          </button>`;
        })
        .join("")
    : `<p class="muted">当前目标下未发现 .rpt 或 .log 文件。</p>`;

  let viewerTitle = "日志尾部预览";
  let viewerMeta = `${(item.logTail || []).length} 行`;
  let viewerLines = item.logTail || [];
  let isTruncated = false;
  let activePath = "";
  let viewerError = "";
  let isLoading = false;

  if (ui.reportPreview) {
    viewerTitle = ui.reportPreview.name || basename(ui.reportPreview.path);
    activePath = ui.reportPreview.path || "";
    isLoading = Boolean(ui.reportPreview.loading);
    viewerError = ui.reportPreview.error || "";
    if (!isLoading && !viewerError) {
      viewerLines = ui.reportPreview.lines || [];
      isTruncated = Boolean(ui.reportPreview.truncated);
      viewerMeta = `第 ${ui.reportPreview.start_line}–${ui.reportPreview.start_line + viewerLines.length - 1} 行 / 共 ${ui.reportPreview.total_lines} 行${isTruncated ? " (已截断)" : ""}`;
    }
  }

  const kw = ui.reportFilter.trim().toLowerCase();
  const visibleLines = kw
    ? viewerLines.filter((ln) => String(ln.text || "").toLowerCase().includes(kw))
    : viewerLines;

  const linesBody = isLoading
    ? `<div style="padding:14px;color:var(--terminal-muted);">正在按需加载报告正文...</div>`
    : viewerError
      ? `<div style="padding:14px;color:#ffb4ae;">加载失败: ${escapeHtml(viewerError)}</div>`
      : visibleLines.length
        ? visibleLines
            .map(
              (ln) =>
                `<div class="term-line" data-level="${classifyLogLine(ln.text)}"><span class="term-ln">${escapeHtml(ln.line || "")}</span><span class="term-txt">${escapeHtml(ln.text || "")}</span></div>`
            )
            .join("")
        : `<div style="padding:14px;color:var(--terminal-muted);">没有匹配的行或内容为空。</div>`;

  el.innerHTML = `
    <div class="stack">
      <div class="panel">
        <div class="panel-head">
          <h3>报告与脚本文件</h3>
          <span class="muted">点击文件名按需读取，不占用对话 Token</span>
        </div>
        <div class="report-chips">${chipsHtml}</div>
      </div>

      <div class="terminal-box">
        <div class="terminal-header">
          <div>
            <strong style="color:var(--terminal-fg);">${escapeHtml(viewerTitle)}</strong>
            <span style="margin-left:8px;">${escapeHtml(viewerMeta)}</span>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <input id="report-filter-input" class="diag-search" style="max-width:220px;background:#161d26;color:#e2e8f0;border-color:#2b3544;" type="search" value="${escapeHtml(ui.reportFilter)}" placeholder="行内关键字过滤..." aria-label="过滤报告行" />
            ${activePath && isTruncated ? `<button type="button" class="btn btn-sm" id="btn-load-more-rpt">加载 1500 行</button>` : ""}
          </div>
        </div>
        <div class="terminal-lines">${linesBody}</div>
      </div>
    </div>
  `;

  for (const btn of el.querySelectorAll(".rpt-chip")) {
    btn.addEventListener("click", () => handleReadReport(btn.dataset.rpath, 300));
  }
  const loadMoreBtn = byId("btn-load-more-rpt");
  if (loadMoreBtn && activePath) {
    loadMoreBtn.addEventListener("click", () => handleReadReport(activePath, 1500));
  }
  const rptFilterEl = byId("report-filter-input");
  if (rptFilterEl) {
    rptFilterEl.addEventListener("input", (ev) => {
      ui.reportFilter = ev.target.value;
      const pos = ev.target.selectionStart;
      renderReports(item);
      const nextEl = byId("report-filter-input");
      if (nextEl) {
        nextEl.focus();
        nextEl.setSelectionRange(pos, pos);
      }
    });
  }
}

function renderSessionsTab() {
  const el = byId("tab-panel-sessions");
  const sessions = ui.state?.sessions || [];
  const debugs = ui.state?.debug_sessions || [];
  const installs = ui.state?.vivado_installs?.installations || [];
  const selectedVivado = ui.state?.vivado_installs?.selected_path || "";

  const sessRows = sessions.length
    ? sessions
        .map(
          (s) => `
          <tr>
            <td class="mono">${escapeHtml(s.session_id || "")}</td>
            <td><span class="kind-chip">${escapeHtml(s.mode || "")}</span></td>
            <td><span class="status-badge" data-status="${escapeHtml(s.is_alive ? (s.state || "ready") : "disconnected")}">${escapeHtml(s.state || (s.is_alive ? "ready" : "dead"))}</span></td>
            <td class="mono">${escapeHtml(s.port ?? "—")}</td>
            <td class="mono">${escapeHtml(s.runtime_version || "—")}</td>
            <td class="mono">${escapeHtml(s.vivado_path || "—")}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="6" class="muted">当前没有活跃的 Vivado Tcl / GUI 会话。</td></tr>`;

  const dbgHtml = debugs.length
    ? debugs
        .map((d) => {
          const hw = d.hardware || {};
          const ilas = (hw.ilas || [])
            .map((ila) => `<li><span class="mono">${escapeHtml(ila.name)}</span> · 状态: <strong>${escapeHtml(ila.status || "—")}</strong> · 探针 ${(ila.probes || []).length} 个</li>`)
            .join("");
          const vios = (hw.vios || [])
            .map((vio) => `<li><span class="mono">${escapeHtml(vio.name)}</span> · 探针 ${(vio.probes || []).length} 个</li>`)
            .join("");
          return `
            <div class="panel">
              <div class="panel-head">
                <h3>硬件调试会话: ${escapeHtml(d.session_id)}</h3>
                <span class="status-badge" data-status="${escapeHtml(d.connection)}">${escapeHtml(STATUS_LABELS[d.connection] || d.connection)}</span>
              </div>
              <p class="muted">控制权: <code>${escapeHtml(d.control)}</code> · 目标: <code>${escapeHtml(d.selection?.target || "未选")}</code> / <code>${escapeHtml(d.selection?.device || "未选")}</code> · stop_ila=${escapeHtml(String(Boolean(d.capabilities?.stop_ila)))}</p>
              ${ilas ? `<h4>ILA 核心</h4><ul style="margin:4px 0 8px;padding-left:18px;">${ilas}</ul>` : ""}
              ${vios ? `<h4>VIO 核心</h4><ul style="margin:4px 0;padding-left:18px;">${vios}</ul>` : ""}
            </div>`;
        })
        .join("")
    : `<div class="panel"><p class="muted">暂无活动的硬件调试 / ILA 面板。</p></div>`;

  const instRows = installs.length
    ? installs
        .map(
          (i) => `
          <tr>
            <td class="mono">${escapeHtml(i.version_from_path || "unknown")}</td>
            <td class="mono">${escapeHtml(i.path || "")}${i.path === selectedVivado ? ' <span class="kind-chip">默认</span>' : ""}</td>
            <td>${escapeHtml((i.sources || []).join(", "))}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="3" class="muted">未检测到本地 Vivado 安装。</td></tr>`;

  el.innerHTML = `
    <div class="stack">
      <div class="panel">
        <div class="panel-head"><h3>活跃 Vivado 会话 (Tcl / GUI / External)</h3><span class="kind-chip">${sessions.length} 个</span></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>会话 ID</th><th>模式</th><th>状态</th><th>端口</th><th>版本</th><th>路径</th></tr></thead>
            <tbody>${sessRows}</tbody>
          </table>
        </div>
      </div>

      ${dbgHtml}

      <div class="panel">
        <div class="panel-head"><h3>本机 Vivado 安装探测</h3><span class="kind-chip">${installs.length} 个</span></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>版本</th><th>路径</th><th>来源</th></tr></thead>
            <tbody>${instRows}</tbody>
          </table>
        </div>
      </div>
    </div>
  `;
}

function updateTabVisibility(item) {
  for (const btn of document.querySelectorAll(".tab-btn")) {
    const active = btn.dataset.tab === ui.activeTab;
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }
  byId("tab-panel-overview").hidden = ui.activeTab !== "overview";
  byId("tab-panel-diagnostics").hidden = ui.activeTab !== "diagnostics";
  byId("tab-panel-reports").hidden = ui.activeTab !== "reports";
  byId("tab-panel-sessions").hidden = ui.activeTab !== "sessions";

  const d = item?.diagnostics || {};
  const issueTotal = (d.errors || 0) + (d.critical_warnings || 0);
  const diagBadge = byId("tab-diag-badge");
  diagBadge.hidden = issueTotal === 0;
  diagBadge.textContent = String(issueTotal);
  diagBadge.dataset.tone = (d.errors || 0) > 0 ? "danger" : "normal";

  const rptCount = (item?.reports || []).length;
  const rptBadge = byId("tab-rpt-badge");
  rptBadge.hidden = rptCount === 0;
  rptBadge.textContent = String(rptCount);

  const sessCount = (ui.state?.sessions || []).length + (ui.state?.debug_sessions || []).length;
  const sessBadge = byId("tab-sess-badge");
  sessBadge.hidden = sessCount === 0;
  sessBadge.textContent = String(sessCount);
}

function captureViewportState() {
  const active = document.activeElement;
  const activeId = active && active.id ? active.id : "";
  const canSelect =
    active &&
    typeof active.selectionStart === "number" &&
    typeof active.selectionEnd === "number";
  return {
    activeId,
    selStart: canSelect ? active.selectionStart : null,
    selEnd: canSelect ? active.selectionEnd : null,
    railScroll: byId("rail-list")?.scrollTop ?? 0,
    stageScroll: document.querySelector(".detail-stage")?.scrollTop ?? 0,
    termScroll: document.querySelector(".terminal-lines")?.scrollTop ?? 0,
    timelineScroll: document.querySelector(".timeline")?.scrollTop ?? 0,
    diagScroll: document.querySelector(".diag-list")?.scrollTop ?? 0,
  };
}

function restoreViewportState(snap) {
  if (!snap) return;
  const rail = byId("rail-list");
  if (rail) rail.scrollTop = snap.railScroll;
  const stage = document.querySelector(".detail-stage");
  if (stage) stage.scrollTop = snap.stageScroll;
  const term = document.querySelector(".terminal-lines");
  if (term) term.scrollTop = snap.termScroll;
  const timeline = document.querySelector(".timeline");
  if (timeline) timeline.scrollTop = snap.timelineScroll;
  const diag = document.querySelector(".diag-list");
  if (diag) diag.scrollTop = snap.diagScroll;
  if (snap.activeId) {
    const el = byId(snap.activeId);
    if (el && document.activeElement !== el && typeof el.focus === "function") {
      el.focus({ preventScroll: true });
      if (
        snap.selStart !== null &&
        snap.selEnd !== null &&
        typeof el.setSelectionRange === "function"
      ) {
        try {
          el.setSelectionRange(snap.selStart, snap.selEnd);
        } catch {
          // 某些输入控件不支持 setSelectionRange
        }
      }
    }
  }
}

function renderAll() {
  const snap = captureViewportState();
  const items = buildItems(ui.state);
  updateSummaryCounts(items);

  if (items.length && !items.some((i) => i.uid === ui.selectedUid)) {
    ui.selectedUid = items[0].uid;
    ui.reportPreview = null;
    ui.reportReqId += 1;
  } else if (!items.length) {
    ui.selectedUid = "";
    ui.reportPreview = null;
    ui.reportReqId += 1;
  }

  renderRail(items);

  const activeItem = items.find((i) => i.uid === ui.selectedUid) || null;
  if (!activeItem) {
    renderEmptyState();
    restoreViewportState(snap);
    return;
  }

  byId("empty-stage").hidden = true;
  byId("detail-shell").hidden = false;

  renderHero(activeItem);
  updateTabVisibility(activeItem);

  if (ui.activeTab === "overview") renderOverview(activeItem);
  else if (ui.activeTab === "diagnostics") renderDiagnostics(activeItem);
  else if (ui.activeTab === "reports") renderReports(activeItem);
  else if (ui.activeTab === "sessions") renderSessionsTab();

  restoreViewportState(snap);
}

function applyIncomingState(payload) {
  if (!payload || typeof payload !== "object") return;
  ui.state = payload;
  if (payload.selected_job_id) {
    const nextUid = `job:${payload.selected_job_id}`;
    if (nextUid !== ui.selectedUid) {
      ui.selectedUid = nextUid;
      ui.reportPreview = null;
      ui.reportReqId += 1;
    }
  } else if (payload.selected_inspected_path) {
    const nextUid = `inspected:${payload.selected_inspected_path}`;
    if (nextUid !== ui.selectedUid) {
      ui.selectedUid = nextUid;
      ui.reportPreview = null;
      ui.reportReqId += 1;
    }
  }
  if (payload.inspect_error) {
    showToast(`路径检视失败: ${payload.inspect_error}`, "error");
  }
  setConnState("ready", `已同步 ${fmtTime(payload.server_time)}`);
  renderAll();
}

async function refreshState({ silent = false } = {}) {
  if (ui.inFlightState) return;
  ui.inFlightState = true;
  try {
    const res = await app.callServerTool({
      name: "vivado_console_state",
      arguments: {},
    });
    if (res?.isError && !res?.structuredContent) {
      throw new Error(res.content?.[0]?.text || "刷新失败");
    }
    if (res?.structuredContent) {
      applyIncomingState(res.structuredContent);
    }
  } catch (err) {
    if (!silent) {
      showToast(`刷新失败: ${err?.message || String(err)}`, "error");
    }
    setConnState("error", "同步失败");
  } finally {
    ui.inFlightState = false;
  }
}

async function handleInspectPath(pathValue) {
  const clean = String(pathValue || "").trim();
  if (!clean) {
    showToast("请输入 .xpr 文件、.runs 目录或输出目录路径", "error");
    return;
  }
  const btn = byId("btn-inspect");
  btn.disabled = true;
  try {
    const res = await app.callServerTool({
      name: "vivado_console_action",
      arguments: { action: "inspect_path", path: clean },
    });
    const sc = res?.structuredContent;
    if (res?.isError || sc?.error) {
      throw new Error(sc?.error || res?.content?.[0]?.text || "检视失败");
    }
    if (sc?.state) {
      applyIncomingState(sc.state);
    }
    byId("inspect-input").value = "";
    showToast(`已加载离线检视目标: ${basename(clean)}`, "success");
  } catch (err) {
    showToast(`检视失败: ${err?.message || String(err)}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function handleRemoveInspected(targetPath) {
  try {
    const res = await app.callServerTool({
      name: "vivado_console_action",
      arguments: { action: "remove_inspected", path: targetPath },
    });
    const sc = res?.structuredContent;
    if (res?.isError || sc?.error) {
      throw new Error(sc?.error || "移除失败");
    }
    if (sc?.state) {
      applyIncomingState(sc.state);
    }
    showToast("已从离线检视列表移除", "info");
  } catch (err) {
    showToast(`移除失败: ${err?.message || String(err)}`, "error");
  }
}

async function handleCancelJob(jobId) {
  if (!jobId) return;
  try {
    const res = await app.callServerTool({
      name: "vivado_console_action",
      arguments: { action: "cancel_job", job_id: jobId },
    });
    const sc = res?.structuredContent;
    if (res?.isError || sc?.error) {
      throw new Error(sc?.error || "取消任务失败");
    }
    if (sc?.state) {
      applyIncomingState(sc.state);
    }
    showToast(`已取消任务 ${jobId}`, "info");
  } catch (err) {
    showToast(`取消任务失败: ${err?.message || String(err)}`, "error");
  }
}

async function handleClearFinished() {
  try {
    const res = await app.callServerTool({
      name: "vivado_console_action",
      arguments: { action: "clear_finished" },
    });
    const sc = res?.structuredContent;
    if (res?.isError || sc?.error) {
      throw new Error(sc?.error || "清理失败");
    }
    if (sc?.state) {
      applyIncomingState(sc.state);
    }
    showToast(`已清理 ${sc?.cleared ?? 0} 条已结束任务记录`, "info");
  } catch (err) {
    showToast(`清理失败: ${err?.message || String(err)}`, "error");
  }
}

async function handleReadReport(reportPath, maxLines = 300) {
  if (!reportPath) return;
  const reqId = ++ui.reportReqId;
  const targetUid = ui.selectedUid;
  ui.reportPreview = { path: reportPath, name: basename(reportPath), loading: true };
  renderAll();
  try {
    const res = await app.callServerTool({
      name: "vivado_console_action",
      arguments: { action: "read_report", report_path: reportPath, max_lines: maxLines },
    });
    if (reqId !== ui.reportReqId || targetUid !== ui.selectedUid) return;
    const sc = res?.structuredContent;
    if (res?.isError || sc?.error) {
      throw new Error(sc?.error || res?.content?.[0]?.text || "读取失败");
    }
    ui.reportPreview = { ...sc.report, loading: false };
  } catch (err) {
    if (reqId !== ui.reportReqId || targetUid !== ui.selectedUid) return;
    ui.reportPreview = {
      path: reportPath,
      name: basename(reportPath),
      loading: false,
      error: err?.message || String(err),
    };
  }
  renderAll();
}

async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      showToast("已复制到剪贴板", "success");
      return;
    }
  } catch {
    // fallback below
  }
  showToast(`路径: ${text}`, "info");
}

function buildCompactContextText(item) {
  const t = item.timing || {};
  const d = item.diagnostics || {};
  const wnsStr = t.parse_status === "ok" ? `${fmtSlack(t.wns)} ns` : "未知";
  return [
    `[Vivado 控制台目标] ${item.title}`,
    `- 状态: ${item.status} | Top: ${item.top || "—"} | Part: ${item.part || "—"}`,
    `- 时序: WNS=${wnsStr} (${t.stage || "unknown"}) | ERROR=${d.errors ?? 0}, CW=${d.critical_warnings ?? 0}`,
    `- 目录: ${item.outputDir || item.targetPath || "—"}`,
  ].join("\n");
}

async function handleAttachContext(item) {
  if (!ui.hostCaps?.updateModelContext) return;
  try {
    await app.updateModelContext({
      content: [{ type: "text", text: buildCompactContextText(item) }],
    });
    showToast("已将当前目标摘要附加上下文", "success");
  } catch (err) {
    showToast(`附加上下文失败: ${err?.message || String(err)}`, "error");
  }
}

async function handleSendToChat(item) {
  if (!ui.hostCaps?.message) return;
  try {
    const text = `请查看我在 Vivado 控制台选中的目标并协助分析：\n${buildCompactContextText(item)}`;
    const res = await app.sendMessage({
      role: "user",
      content: [{ type: "text", text }],
    });
    if (res?.isError) throw new Error("宿主拒绝发送消息");
    showToast("已发送到当前对话", "success");
  } catch (err) {
    showToast(`发送失败: ${err?.message || String(err)}`, "error");
  }
}

function syncAutoRefresh() {
  if (ui.autoTimer) {
    clearInterval(ui.autoTimer);
    ui.autoTimer = null;
  }
  const toggle = byId("auto-refresh-toggle");
  if (toggle?.checked) {
    ui.autoTimer = setInterval(() => {
      refreshState({ silent: true });
    }, 5000);
  }
}

function bindEvents() {
  byId("btn-inspect").addEventListener("click", () => {
    handleInspectPath(byId("inspect-input").value);
  });
  byId("inspect-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      handleInspectPath(ev.target.value);
    }
  });

  byId("btn-refresh").addEventListener("click", () => refreshState({ silent: false }));
  byId("btn-clear-finished").addEventListener("click", handleClearFinished);
  byId("auto-refresh-toggle").addEventListener("change", syncAutoRefresh);

  byId("rail-search").addEventListener("input", (ev) => {
    ui.railSearch = ev.target.value;
    renderRail(buildItems(ui.state));
  });

  for (const chip of document.querySelectorAll(".sum-chip")) {
    chip.addEventListener("click", () => {
      const f = chip.dataset.filter;
      ui.statusFilter = ui.statusFilter === f ? "" : f;
      renderAll();
    });
  }

  byId("rail-list").addEventListener("click", (ev) => {
    const card = ev.target.closest(".nav-card");
    if (!card) return;
    const uid = card.dataset.uid;
    if (uid && uid !== ui.selectedUid) {
      ui.selectedUid = uid;
      ui.reportPreview = null;
      ui.reportReqId += 1;
      ui.reportFilter = "";
      renderAll();
    }
  });

  for (const btn of document.querySelectorAll(".tab-btn")) {
    btn.addEventListener("click", () => {
      ui.activeTab = btn.dataset.tab;
      renderAll();
    });
  }
}

function applyHostCtx(ctx) {
  if (!ctx) return;
  if (ctx.theme) applyDocumentTheme(ctx.theme);
  if (ctx.styles?.variables) applyHostStyleVariables(ctx.styles.variables);
}

// 必须在 connect() 之前注册所有处理器，避免错过初始 toolinput / toolresult
app.ontoolinput = (params) => {
  const args = params?.arguments || {};
  if (args.job_id) ui.selectedUid = `job:${args.job_id}`;
  if (args.inspect_path) ui.selectedUid = `inspected:${args.inspect_path}`;
  setConnState("connecting", "加载中");
};

app.ontoolresult = (result) => {
  if (result?.structuredContent) {
    applyIncomingState(result.structuredContent);
  }
};

app.ontoolcancelled = () => {
  setConnState("error", "已取消");
};

app.onhostcontextchanged = (ctx) => {
  applyHostCtx(ctx);
};

async function init() {
  if (window.matchMedia?.("(prefers-color-scheme: dark)").matches) {
    applyDocumentTheme("dark");
  }
  bindEvents();
  try {
    await app.connect();
    ui.hostCaps = app.getHostCapabilities() || {};
    applyHostCtx(app.getHostContext());
    if (!ui.state) {
      await refreshState({ silent: true });
    } else {
      renderAll();
    }
    syncAutoRefresh();
  } catch (err) {
    setConnState("error", "连接失败");
    showToast(`MCP App 握手失败: ${err?.message || String(err)}`, "error");
    renderEmptyState();
  }
}

init();
