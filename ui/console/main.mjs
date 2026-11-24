import { App } from "@modelcontextprotocol/ext-apps";

const THEMES = [
  { id: "macos", name: "macOS 深色", source: "Apple HIG 系统色" },
  { id: "macos-light", name: "macOS 浅色", source: "Apple HIG 浅色" },
  { id: "graphite", name: "macOS 石墨", source: "Apple HIG · Graphite" },
  { id: "geist", name: "Geist", source: "Vercel Geist" },
  { id: "tokyo-night", name: "Tokyo Night", source: "enkia/tokyo-night" },
  { id: "catppuccin", name: "Catppuccin Mocha", source: "catppuccin/catppuccin" },
  { id: "nord", name: "Nord", source: "nordtheme/nord" },
];

const THEME_STORAGE_KEY = "otter_vivado_console_theme";

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
  ended: "已结束",
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

const app = new App({ name: "Vivado Console", version: "2.0.0" }, {});

const ui = {
  state: null,
  transportMode: "mcp", // "mcp" | "http"
  hostCaps: {},
  theme: "macos",
  manualTheme: false,
  themeMenuOpen: false,
  selectedUid: "",
  pendingScrollSelection: false,
  lastSelectionSeq: 0,
  activeTab: "overview", // overview | diagnostics | reports | sessions
  statusFilter: "", // "" | running | completed | failed
  clientFilter: "", // "" | clientSource
  railSearch: "",
  diagSev: "ALL", // ALL | ERROR | CRITICAL WARNING | WARNING
  diagSearch: "",
  reportFilter: "",
  reportPreview: null, // { path, name, total_lines, returned_lines, start_line, truncated, tail, lines, loading, error }
  reportReqId: 0,
  inFlightState: false,
  inFlightPromise: null,
  autoRefresh: true,
  connState: "connecting",
  autoTimer: null,
  toastTimer: null,
  localUrlCopied: false,
  localCopyTimer: null,
  copiedServiceUrl: "",
  serviceCopyTimer: null,
  pendingKillPid: null,
  pendingKillTimer: null,
  killingPid: null,
  cleaningRegistry: false,
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

function fmtRssMb(rssMb) {
  if (typeof rssMb !== "number" || !Number.isFinite(rssMb) || rssMb < 0) return "—";
  if (rssMb >= 1024) return `${(rssMb / 1024).toFixed(2)} GB`;
  return `${Math.round(rssMb)} MB`;
}

function fmtUptime(sec) {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec < 0) return "—";
  const total = Math.floor(sec);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function normPathKey(pathStr) {
  return String(pathStr || "")
    .trim()
    .replace(/\\/g, "/")
    .replace(/\/+$/, "")
    .toLowerCase();
}

function normalizeLogLines(lines) {
  if (!Array.isArray(lines)) return [];
  return lines.map((entry) =>
    typeof entry === "string"
      ? { line: 0, text: entry }
      : { line: entry?.line ?? entry?.lineno ?? 0, text: String(entry?.text || "") }
  );
}

function inferPhasesFromTail(logTail, currentPhase) {
  const re = /^(?:Phase\s+\d+|Starting\s+|Finished\s+|Running\s+|Command:\s+)/i;
  const out = [];
  for (const row of logTail || []) {
    const txt = String(row?.text || "").trim();
    if (re.test(txt)) {
      out.push({ line: row.line || 0, text: txt });
    }
  }
  if (!out.length && currentPhase) {
    out.push({ line: "—", text: currentPhase });
  }
  return out;
}

function statusBucket(status) {
  const s = String(status || "").toLowerCase();
  if (s === "running" || s === "queued" || s === "busy" || s === "waiting") return "running";
  if (s === "completed" || s === "succeeded" || s === "connected" || s === "stage_complete" || s === "ended") return "completed";
  if (s === "failed" || s === "error" || s === "disconnected" || s === "cancelled" || s === "timeout") return "failed";
  return "other";
}

function pickPrimaryIssues(d) {
  if (!d || typeof d !== "object") return [];
  if (Array.isArray(d.all_issues) && d.all_issues.length) return d.all_issues;
  if (Array.isArray(d.top_issues) && d.top_issues.length) return d.top_issues;
  return [];
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

function setConnState(state, text, detail = "") {
  ui.connState = state;
  const badge = byId("conn-badge");
  if (!badge) return;
  badge.dataset.state = state;
  badge.title = detail ? `${text} ${detail}` : text;
  badge.innerHTML = detail
    ? `<span>${escapeHtml(text)}</span><span class="sb-conn-detail"> ${escapeHtml(detail)}</span>`
    : escapeHtml(text);
}

function getThemeMeta(themeId) {
  return THEMES.find((t) => t.id === themeId) || THEMES[0];
}

function renderThemeMenu() {
  const menu = byId("theme-menu");
  if (!menu) return;
  const itemsHtml = THEMES.map(
    (t) => `
      <button
        type="button"
        role="menuitemradio"
        aria-checked="${ui.theme === t.id ? "true" : "false"}"
        class="menu-item theme-item${ui.theme === t.id ? " checked" : ""}"
        data-theme-id="${escapeHtml(t.id)}"
        title="${escapeHtml(t.name)} · ${escapeHtml(t.source)}"
      >
        <span class="menu-check" aria-hidden="true">${
          ui.theme === t.id
            ? '<svg viewBox="0 0 12 12" width="12" height="12" fill="none"><path d="M2.2 6.4 4.8 9l5-6" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
            : ""
        }</span>
        <span class="theme-preview" data-swatch="${escapeHtml(t.id)}" aria-hidden="true"><i></i><i></i><i></i></span>
        <span class="theme-text">
          <span class="theme-name">${escapeHtml(t.name)}</span>
          <span class="theme-source">${escapeHtml(t.source)}</span>
        </span>
      </button>
    `
  ).join("");
  menu.innerHTML = `<div class="menu-label">外观主题</div>${itemsHtml}`;
}

function setThemeMenuOpen(open) {
  ui.themeMenuOpen = Boolean(open);
  const menu = byId("theme-menu");
  const btn = byId("btn-theme");
  if (menu) menu.hidden = !ui.themeMenuOpen;
  if (btn) btn.setAttribute("aria-expanded", ui.themeMenuOpen ? "true" : "false");
}

function applyTheme(themeId, { manual = false } = {}) {
  const meta = getThemeMeta(themeId);
  ui.theme = meta.id;
  if (manual) {
    ui.manualTheme = true;
    if (ui.transportMode === "http") {
      try {
        window.localStorage?.setItem(THEME_STORAGE_KEY, meta.id);
      } catch {
        // 忽略受限存储上下文
      }
    }
  }
  document.documentElement.dataset.theme = meta.id;
  const sw = byId("theme-trigger-swatch");
  if (sw) sw.dataset.swatch = meta.id;
  const triggerName = byId("theme-trigger-name");
  if (triggerName) triggerName.textContent = meta.name;
  const sbTheme = byId("sb-theme");
  if (sbTheme) sbTheme.textContent = meta.name;
  renderThemeMenu();
}

function readStoredTheme() {
  try {
    const saved = window.localStorage?.getItem(THEME_STORAGE_KEY) || "";
    if (THEMES.some((t) => t.id === saved)) return saved;
  } catch {
    // ignore
  }
  return "";
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

function extractIssuesFromTail(tailLines) {
  const issues = [];
  const warnItems = [];
  let errors = 0;
  let criticalWarnings = 0;
  let warnings = 0;
  for (const lineObj of tailLines || []) {
    const lineNo = lineObj?.lineno ?? lineObj?.line ?? 0;
    const txt = String(lineObj?.text || "").trim();
    const mId = txt.match(/\[(\w+[\s\-][\w\-]+)\]/);
    const id = mId ? mId[1] : "LOG";
    if (txt.startsWith("ERROR:")) {
      errors += 1;
      issues.push({ severity: "ERROR", id, count: 1, line: lineNo, message: txt });
    } else if (txt.startsWith("CRITICAL WARNING:")) {
      criticalWarnings += 1;
      issues.push({ severity: "CRITICAL WARNING", id, count: 1, line: lineNo, message: txt });
    } else if (txt.startsWith("WARNING:")) {
      warnings += 1;
      warnItems.push({ severity: "WARNING", id, count: 1, line: lineNo, message: txt });
    }
  }
  return {
    errors,
    critical_warnings: criticalWarnings,
    warnings,
    all_issues: issues,
    warning_items: warnItems,
  };
}

function extractMonitorDiagnostics(monitor) {
  const run = monitor.run || {};
  const diag = run.diagnostics || {};
  const parsed = extractIssuesFromTail(run.tail || []);
  return {
    errors: typeof diag.errors === "number" ? diag.errors : parsed.errors,
    critical_warnings:
      typeof diag.critical_warnings === "number"
        ? diag.critical_warnings
        : parsed.critical_warnings,
    warnings: typeof diag.warnings === "number" ? diag.warnings : parsed.warnings,
    all_issues: parsed.all_issues,
    warning_items: parsed.warning_items,
    scope: diag.scope || "",
  };
}

function buildItems(state) {
  const items = [];
  if (!state) return items;

  const localClient =
    String(
      state.client ||
        (state.batch_jobs || []).find((j) => j.is_local_owner === true && j.client)?.client ||
        ""
    ).trim() || "本会话";

  const localRunKeys = new Set();
  const seenUids = new Set();

  for (const mon of state.run_monitors || []) {
    const run = mon.run || {};
    const timing = extractMonitorTiming(mon);
    const utilization = extractMonitorUtil(mon);
    const diagnostics = extractMonitorDiagnostics(mon);
    const status = run.state || mon.connection || "unknown";
    const runDirKey = normPathKey(run.directory);
    const runNameKey = String(mon.run_name || "").trim().toLowerCase();
    if (runDirKey) {
      localRunKeys.add(`${runDirKey}|${runNameKey}`);
    }
    const uid = `run:${mon.session_id}:${mon.run_name}:${mon.target_step}`;
    seenUids.add(uid);
    items.push({
      uid,
      category: "run_monitor",
      categoryLabel: "工程构建",
      kindBadge: mon.target_step || "run",
      clientSource: mon.client || localClient,
      isLocalOwner: true,
      isRemote: false,
      sessionId: mon.session_id || "",
      mode: mon.session_mode || "",
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
      phases: (run.phases || []).map((p) => ({
        line: p.lineno ?? p.line ?? 0,
        text: p.text || "",
      })),
      currentPhase: run.current_phase || "",
      logTail: normalizeLogLines(run.tail),
      outputDir: run.directory || "",
      logPath: run.log_path || "",
      xprPath: run.project_file || "",
      monitorUrl: mon.url || mon.local_url || mon.view_url || "",
      raw: mon,
    });
  }

  for (const rr of state.remote_runs || []) {
    const rrDirKey = normPathKey(rr.run_dir);
    const rrNameKey = String(rr.run_name || "").trim().toLowerCase();
    if (rrDirKey && localRunKeys.has(`${rrDirKey}|${rrNameKey}`)) {
      continue;
    }
    const fallbackId = `${rr.owner_pid || 0}:${rr.session_id || ""}:${rr.run_name || rr.run_dir || ""}`;
    const uid = `remote:${rr.entry_id || fallbackId}`;
    if (seenUids.has(uid)) continue;
    seenUids.add(uid);

    const logTail = normalizeLogLines(rr.log_tail);
    const repTiming = extractMonitorTiming({ reports: rr.reports || [] });
    const timing =
      rr.timing && rr.timing.parse_status === "ok"
        ? rr.timing
        : repTiming.parse_status === "ok"
          ? repTiming
          : rr.timing || { wns: null, tns: null, whs: null, ths: null, met: null, parse_status: "missing" };

    const rawUtil = rr.utilization || {};
    const hasDirectUtil = Boolean(rawUtil.lut || rawUtil.ff || rawUtil.bram || rawUtil.dsp);
    const utilization = hasDirectUtil ? rawUtil : extractMonitorUtil({ reports: rr.reports || [] });

    const tailDiag = extractIssuesFromTail(logTail);
    const d = rr.diagnostics || {};
    const primaryIssues = pickPrimaryIssues(d);
    const diagnostics = {
      errors: typeof d.errors === "number" ? d.errors : tailDiag.errors,
      critical_warnings:
        typeof d.critical_warnings === "number" ? d.critical_warnings : tailDiag.critical_warnings,
      warnings: typeof d.warnings === "number" ? d.warnings : tailDiag.warnings,
      all_issues: primaryIssues.length ? primaryIssues : tailDiag.all_issues,
      top_issues: Array.isArray(d.top_issues) ? d.top_issues : [],
      warning_items:
        Array.isArray(d.warning_items) && d.warning_items.length
          ? d.warning_items
          : tailDiag.warning_items,
      scope: d.scope || "",
    };

    const projLabel = rr.project_name || basename(rr.xpr_path) || "";
    const title =
      rr.run_name && projLabel
        ? `${rr.run_name} · ${projLabel}`
        : rr.run_name || projLabel || rr.session_id || rr.entry_id || "远程工程构建";
    const status = rr.status || rr.state || (rr.alive ? "connected" : "disconnected");
    const subtitleParts = [rr.session_id || rr.mode || "session", rr.phase || rr.state || status];
    const phases =
      Array.isArray(rr.phases) && rr.phases.length
        ? rr.phases.map((p) => ({ line: p.lineno ?? p.line ?? 0, text: p.text || "" }))
        : inferPhasesFromTail(logTail, rr.phase || "");

    items.push({
      uid,
      category: "run_monitor",
      categoryLabel: "工程构建",
      kindBadge: rr.target_step || rr.mode || "remote",
      clientSource: rr.client || "外部",
      isLocalOwner: false,
      isRemote: true,
      ownerPid: typeof rr.owner_pid === "number" ? rr.owner_pid : null,
      vivadoPid: typeof rr.vivado_pid === "number" ? rr.vivado_pid : null,
      alive: rr.alive !== false,
      sessionId: rr.session_id || "",
      mode: rr.mode || "",
      port: rr.port ?? null,
      title,
      subtitle: subtitleParts.filter(Boolean).join(" · "),
      status,
      top: rr.top || "",
      part: rr.part || "",
      project: rr.project_name || "",
      version: rr.vivado_version || "",
      elapsedText:
        typeof rr.elapsed_sec === "number" && Number.isFinite(rr.elapsed_sec)
          ? fmtDuration(rr.elapsed_sec)
          : rr.updated_at
            ? fmtTime(rr.updated_at)
            : "—",
      progressPct:
        typeof rr.progress === "number" && Number.isFinite(rr.progress) ? rr.progress : null,
      timing,
      utilization,
      diagnostics,
      reports: (rr.reports || []).map((r) => ({
        name: r.name || basename(r.path),
        path: r.path || "",
        size: r.size || 0,
        kind: r.kind || "rpt",
        stage: r.stage || "",
        freshness: r.freshness || "",
      })),
      phases,
      currentPhase: rr.phase || "",
      logTail,
      outputDir: rr.run_dir || "",
      logPath: rr.log_path || "",
      xprPath: rr.xpr_path || "",
      monitorUrl: rr.monitor_url || "",
      debugUrl: rr.debug_url || "",
      updatedAt: rr.updated_at || null,
      raw: rr,
    });
  }

  for (const job of state.batch_jobs || []) {
    const isOoc = job.kind === "ooc_synth";
    const isLocalOwner = job.is_local_owner !== false;
    items.push({
      uid: `job:${job.job_id}`,
      jobId: job.job_id,
      category: "batch_job",
      categoryLabel: "Batch / OOC",
      kindBadge: isOoc ? "OOC 综合" : "Batch Tcl",
      clientSource: job.client || (isLocalOwner ? localClient : "外部"),
      isLocalOwner,
      isRemote: !isLocalOwner,
      ownerPid: typeof job.owner_pid === "number" ? job.owner_pid : null,
      vivadoPid: typeof job.pid === "number" ? job.pid : null,
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
      logTail: normalizeLogLines(job.log_tail),
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
      categoryLabel: "离线目录",
      kindBadge: kindLabel,
      clientSource: insp.client || "",
      title: insp.project || insp.top || basename(insp.target_path),
      subtitle: `${insp.part || "未识别 Part"} · ${basename(insp.target_path)}`,
      status: insp.status || "unknown",
      top: insp.top || "",
      part: insp.part || "",
      project: insp.project || "",
      version: insp.vivado_version || "",
      elapsedText: insp.inspected_at ? fmtTime(insp.inspected_at) : "—",
      progressPct: null,
      timing: insp.timing || { wns: null, tns: null, whs: null, ths: null, met: null, parse_status: "missing" },
      utilization: insp.utilization || { lut: null, ff: null, bram: null, dsp: null },
      diagnostics: insp.diagnostics || { errors: 0, critical_warnings: 0, warnings: 0, all_issues: [], warning_items: [] },
      reports: insp.reports || [],
      phases: insp.phases || [],
      currentPhase: insp.current_phase || "",
      logTail: normalizeLogLines(insp.log_tail),
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
      clientSource: dbg.client || "",
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
      diagnostics: {
        errors: dbg.error ? 1 : 0,
        critical_warnings: 0,
        warnings: 0,
        all_issues: dbg.error ? [{ severity: "ERROR", id: "DEBUG", count: 1, line: 0, message: dbg.error }] : [],
        warning_items: [],
      },
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

function updateSummaryAndStatus(items) {
  let running = 0;
  let completed = 0;
  let failed = 0;
  for (const item of items) {
    const b = statusBucket(item.status);
    if (b === "running") running += 1;
    else if (b === "completed") completed += 1;
    else if (b === "failed") failed += 1;
  }
  byId("cnt-all").textContent = String(items.length);
  byId("cnt-running").textContent = String(running);
  byId("cnt-completed").textContent = String(completed);
  byId("cnt-failed").textContent = String(failed);

  for (const chip of document.querySelectorAll("#summary-chips .filter-chip")) {
    chip.dataset.active = ui.statusFilter === chip.dataset.filter ? "true" : "false";
  }

  renderClientFilters(items);

  const finishedBatchCount = (ui.state?.batch_jobs || []).filter((j) => j.status !== "running").length;
  const footerActs = byId("sidebar-footer-actions");
  if (footerActs) footerActs.hidden = finishedBatchCount === 0;

  const installsCount = (ui.state?.vivado_installs?.installations || []).length;
  const sessionsCount = (ui.state?.sessions || []).length;
  renderSystemStatusBar(installsCount, sessionsCount);

  renderLocalWebStatus();
  updateDynamicIsland(items, { running, completed, failed, installsCount });
}

function renderClientFilters(items) {
  const bar = byId("client-filter-bar");
  if (!bar) return;

  const counts = new Map();
  for (const item of items) {
    if (!item.clientSource) continue;
    counts.set(item.clientSource, (counts.get(item.clientSource) || 0) + 1);
  }
  const clients = [...counts.keys()];
  if (clients.length <= 1) {
    ui.clientFilter = "";
    bar.hidden = true;
    bar.innerHTML = "";
    return;
  }

  if (ui.clientFilter && !counts.has(ui.clientFilter)) {
    ui.clientFilter = "";
  }

  const pills = [
    `<button type="button" class="filter-chip client-chip" data-client="" data-active="${ui.clientFilter === "" ? "true" : "false"}">全部来源</button>`,
    ...clients.map(
      (c) =>
        `<button type="button" class="filter-chip client-chip" data-client="${escapeHtml(c)}" data-active="${ui.clientFilter === c ? "true" : "false"}">${escapeHtml(c)} <strong>${counts.get(c)}</strong></button>`
    ),
  ];
  bar.hidden = false;
  bar.innerHTML = pills.join("");
}

function renderSystemStatusBar(installsCount, sessionsCount) {
  const sys = ui.state?.system;
  const sysWrap = byId("sb-sys-wrap");
  const ramFill = byId("sb-ram-fill");
  const ramText = byId("sb-ram-text");
  const orphanBtn = byId("btn-sb-orphans");
  const sbVivado = byId("sb-vivado");

  const hasMem = Boolean(sys && typeof sys.ram_total_gb === "number" && sys.ram_total_gb > 0);
  if (sysWrap && ramFill && ramText && hasMem) {
    const used = Number(sys.ram_used_gb || 0);
    const total = Number(sys.ram_total_gb || 0);
    const avail = Number(sys.ram_avail_gb || 0);
    const pct =
      typeof sys.ram_pct === "number" && Number.isFinite(sys.ram_pct)
        ? sys.ram_pct
        : total > 0
          ? (used / total) * 100
          : 0;
    const vivCount = Number(sys.vivado_count || 0);
    const edaCount = Number(sys.eda_count || 0);
    const totalFmt = Number.isInteger(total) ? String(total) : total.toFixed(1);
    const level = pct >= 85 ? "crit" : pct >= 75 ? "warn" : "normal";

    sysWrap.hidden = false;
    sysWrap.dataset.level = level;
    ramFill.style.width = `${Math.max(0, Math.min(100, pct)).toFixed(1)}%`;
    const fullText = `RAM ${used.toFixed(1)}/${totalFmt} GB (${Math.round(pct)}%) · ${vivCount} Vivado`;
    const compactText = `RAM ${Math.round(pct)}% · ${vivCount}V`;
    ramText.innerHTML = `<span class="sb-ram-full">${escapeHtml(fullText)}</span><span class="sb-ram-compact">${escapeHtml(compactText)}</span>`;
    sysWrap.title = `主机内存 ${used.toFixed(1)} / ${total.toFixed(1)} GB (${pct.toFixed(1)}%) · 可用 ${avail.toFixed(1)} GB · ${vivCount} Vivado / ${edaCount} EDA`;
    if (sbVivado) sbVivado.hidden = true;
  } else {
    if (sysWrap) sysWrap.hidden = true;
    if (sbVivado) {
      sbVivado.hidden = false;
      sbVivado.textContent = `${installsCount} 个 Vivado · ${sessionsCount} 个会话`;
    }
  }

  if (orphanBtn) {
    const orphanCount = Number(sys?.orphan_count || 0);
    if (orphanCount > 0) {
      orphanBtn.hidden = false;
      orphanBtn.textContent = `${orphanCount} 残留`;
    } else {
      orphanBtn.hidden = true;
    }
  }
}

function getLocalWebInfo() {
  const lw = ui.state?.local_web;
  let url =
    lw && lw.running !== false && typeof lw.url === "string" && lw.url
      ? lw.url
      : typeof ui.state?.url === "string"
        ? ui.state.url
        : "";
  let hostPort = lw && typeof lw.host_port === "string" && lw.host_port ? lw.host_port : "";
  if (!url && ui.transportMode === "http" && window.location.hostname === "127.0.0.1") {
    url = `${window.location.origin}/`;
    hostPort = window.location.host;
  }
  if (url && !hostPort) {
    try {
      hostPort = new URL(url).host;
    } catch {
      hostPort = "";
    }
  }
  const running = Boolean(url && (lw ? lw.running !== false : true));
  return {
    running,
    url: running ? url : "",
    hostPort: running ? hostPort : "",
    sharedHub: Boolean(lw?.shared_hub),
    preferredPort: lw?.preferred_port || null,
    error: lw?.error || "",
  };
}

function collectLocalServices() {
  const info = getLocalWebInfo();
  const seenUrls = new Set();
  const list = [
    {
      id: "console",
      kind: "console",
      title: "Vivado 控制台",
      badge: ui.transportMode === "http" ? "Local Web" : "MCP App",
      sharedHub: info.sharedHub,
      status: info.running ? "completed" : "idle",
      statusLabel: info.running ? "运行中" : "未启动",
      url: info.url,
      error: info.error,
    },
  ];
  if (info.url) seenUrls.add(info.url);

  for (const mon of ui.state?.run_monitors || []) {
    const url = mon?.url || mon?.local_url || mon?.view_url || "";
    if (typeof url === "string" && url.startsWith("http://127.0.0.1:") && !seenUrls.has(url)) {
      seenUrls.add(url);
      list.push({
        id: `mon:${mon.session_id || ""}:${mon.run_name || ""}:${mon.target_step || ""}`,
        kind: "monitor",
        title: `运行观察 · ${mon.run_name || "run"}`,
        badge: `${mon.session_id || "default"} · ${mon.target_step || "monitor"}`,
        status: "completed",
        statusLabel: "运行中",
        url,
        error: "",
      });
    }
  }
  for (const dbg of ui.state?.debug_sessions || []) {
    const url = dbg?.url || dbg?.local_url || dbg?.view_url || "";
    if (typeof url === "string" && url.startsWith("http://127.0.0.1:") && !seenUrls.has(url)) {
      seenUrls.add(url);
      list.push({
        id: `dbg:${dbg.session_id || ""}`,
        kind: "debug",
        title: `硬件调试面板 · ${dbg.session_id || "default"}`,
        badge: "ILA / VIO",
        status: "completed",
        statusLabel: "运行中",
        url,
        error: "",
      });
    }
  }
  for (const rr of ui.state?.remote_runs || []) {
    const monUrl = rr?.monitor_url || "";
    if (typeof monUrl === "string" && monUrl.startsWith("http://127.0.0.1:") && !seenUrls.has(monUrl)) {
      seenUrls.add(monUrl);
      list.push({
        id: `rmon:${rr.entry_id || rr.session_id || ""}`,
        kind: "monitor",
        title: `远程运行观察 · ${rr.run_name || rr.project_name || "run"}`,
        badge: `${rr.client || "外部"} · ${rr.target_step || "monitor"}`,
        status: "completed",
        statusLabel: "运行中",
        url: monUrl,
        error: "",
      });
    }
    const dbgUrl = rr?.debug_url || "";
    if (typeof dbgUrl === "string" && dbgUrl.startsWith("http://127.0.0.1:") && !seenUrls.has(dbgUrl)) {
      seenUrls.add(dbgUrl);
      list.push({
        id: `rdbg:${rr.entry_id || rr.session_id || ""}`,
        kind: "debug",
        title: `远程调试面板 · ${rr.session_id || "default"}`,
        badge: `${rr.client || "外部"} · ILA / VIO`,
        status: "completed",
        statusLabel: "运行中",
        url: dbgUrl,
        error: "",
      });
    }
  }
  return list;
}

function renderLocalWebStatus() {
  const wrap = byId("sb-local-wrap");
  const copyBtn = byId("btn-sb-copy-url");
  const addrEl = byId("sb-local-addr");
  const sharedEl = byId("sb-shared-badge");
  const copiedEl = byId("sb-local-copied");
  const openBtn = byId("btn-sb-open-browser");
  if (!wrap || !copyBtn || !addrEl || !copiedEl || !openBtn) return;

  const info = getLocalWebInfo();
  if (!info.running || !info.hostPort) {
    wrap.hidden = true;
    openBtn.hidden = true;
    return;
  }

  wrap.hidden = false;
  addrEl.textContent = info.hostPort;
  if (sharedEl) {
    sharedEl.hidden = !info.sharedHub;
    if (info.sharedHub) {
      sharedEl.title = "当前正共享跨智能体统一本地控制台枢纽";
    }
  }
  copyBtn.title = info.sharedHub
    ? `点击复制 ${info.url}（多智能体共享控制台枢纽）`
    : `点击复制 ${info.url}`;
  copyBtn.dataset.copied = ui.localUrlCopied ? "true" : "false";
  copiedEl.hidden = !ui.localUrlCopied;
  openBtn.hidden = ui.transportMode !== "mcp";
}

function updateDynamicIsland(items, counts) {
  const island = byId("dynamic-island");
  const titleEl = byId("island-title");
  const detailEl = byId("island-detail");
  const meterEl = byId("island-meter");
  const badgeEl = byId("island-badge");
  if (!island || !titleEl || !detailEl || !meterEl || !badgeEl) return;

  const { running, completed, failed, installsCount } = counts;
  const activeItem = items.find((i) => i.uid === ui.selectedUid) || null;
  const runningItems = items.filter((i) => statusBucket(i.status) === "running");

  island.classList.remove("idle", "online", "running", "alert");
  island.title = "点击跳转到运行中任务或刷新状态";

  let badgeSource = null;

  if (ui.connState === "error") {
    island.classList.add("alert");
    titleEl.textContent = "同步失败";
    detailEl.textContent = "点击立即重连";
    meterEl.hidden = true;
    meterEl.classList.remove("live");
    badgeEl.hidden = true;
    return;
  }

  if (running > 0) {
    island.classList.add("running");
    const runningClients = [];
    for (const r of runningItems) {
      if (r.clientSource && !runningClients.includes(r.clientSource)) {
        runningClients.push(r.clientSource);
      }
    }
    const runItem =
      (activeItem && statusBucket(activeItem.status) === "running" ? activeItem : null) ||
      runningItems[0];
    titleEl.textContent = `${running} 运行中`;
    if (runningClients.length > 1) {
      detailEl.textContent = runningClients.join("/");
      island.title = runningItems
        .map((r) => `${r.clientSource ? `[${r.clientSource}] ` : ""}${r.title}`)
        .join(" | ");
      if (activeItem && statusBucket(activeItem.status) === "running") {
        badgeSource = activeItem;
      } else {
        badgeSource =
          runningItems.find(
            (r) => r.timing?.parse_status === "ok" && typeof r.timing.wns === "number"
          ) || null;
      }
    } else if (runItem) {
      const prefix =
        runItem.clientSource && runItem.clientSource !== "本会话" ? `${runItem.clientSource} · ` : "";
      detailEl.textContent = `${prefix}${runItem.title}${runItem.currentPhase ? ` · ${runItem.currentPhase}` : ""}`;
      badgeSource = runItem;
    } else {
      detailEl.textContent = `${completed} 完成 · ${failed} 异常`;
    }
    meterEl.hidden = false;
    meterEl.classList.add("live");
  } else if (failed > 0) {
    island.classList.add("alert");
    titleEl.textContent = `${failed} 异常`;
    detailEl.textContent = completed > 0 ? `${completed} 完成 · 点击刷新` : (activeItem ? activeItem.title : "检查告警与日志");
    badgeSource = completed > 0 ? null : activeItem;
    meterEl.hidden = true;
    meterEl.classList.remove("live");
  } else if (items.length > 0) {
    island.classList.add("online");
    titleEl.textContent = completed > 0 ? `${completed} 完成` : `${items.length} 目标`;
    detailEl.textContent = activeItem ? activeItem.title : "全部就绪";
    badgeSource = activeItem;
    meterEl.hidden = true;
    meterEl.classList.remove("live");
  } else {
    island.classList.add("idle");
    titleEl.textContent = "就绪";
    detailEl.textContent = ui.state
      ? `${installsCount} 个本地 Vivado · 零 Token`
      : "正在连接控制台";
    meterEl.hidden = true;
    meterEl.classList.remove("live");
  }

  if (badgeSource?.timing?.parse_status === "ok" && typeof badgeSource.timing.wns === "number") {
    badgeEl.hidden = false;
    badgeEl.textContent = `WNS ${fmtSlack(badgeSource.timing.wns)}ns`;
    badgeEl.dataset.tone = badgeSource.timing.met ? "ok" : "fail";
  } else if (items.length > 0 && running > 0 && completed > 0) {
    badgeEl.hidden = false;
    badgeEl.textContent = `${completed} 完成`;
    badgeEl.dataset.tone = "neutral";
  } else {
    badgeEl.hidden = true;
  }
}

function renderRail(items) {
  const railEl = byId("rail-list");
  if (!railEl) return;

  const query = ui.railSearch.trim().toLowerCase();
  const filtered = items.filter((item) => {
    if (ui.statusFilter && statusBucket(item.status) !== ui.statusFilter) return false;
    if (ui.clientFilter && item.clientSource !== ui.clientFilter) return false;
    if (!query) return true;
    const haystack = `${item.title} ${item.subtitle} ${item.part} ${item.top} ${item.project} ${item.clientSource} ${item.status}`.toLowerCase();
    return haystack.includes(query);
  });

  if (!filtered.length) {
    railEl.innerHTML = `<p class="rail-empty">${items.length ? "没有匹配过滤条件的条目。" : "暂无运行或检视条目。"}</p>`;
    return;
  }

  const groupOrder = [
    ["run_monitor", "工程构建"],
    ["batch_job", "Batch / OOC"],
    ["inspected", "离线目录"],
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
      const wnsText = hasTiming ? `${fmtSlack(item.timing.wns)}` : "—";
      const metAttr = hasTiming ? (item.timing.met ? "true" : "false") : "na";
      const led = ledClass(item.status, item.timing);
      const clientPill = item.clientSource
        ? `<span class="client-pill">${escapeHtml(item.clientSource)}</span>`
        : "";
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
            ${clientPill}
            <span class="nav-card-sub">${escapeHtml(item.subtitle)}</span>
            <span>${escapeHtml(item.elapsedText)}</span>
          </div>
        </button>
      `);
    }
    html.push(`</div></div>`);
  }
  railEl.innerHTML = html.join("");
  if (ui.pendingScrollSelection) {
    ui.pendingScrollSelection = false;
    const activeCard = railEl.querySelector('.nav-card[data-selected="true"]');
    if (activeCard && typeof activeCard.scrollIntoView === "function") {
      activeCard.scrollIntoView({ inline: "nearest", block: "nearest" });
    }
  }
}

function renderEmptyState() {
  const emptyEl = byId("empty-stage");
  const detailEl = byId("detail-shell");
  detailEl.hidden = true;
  emptyEl.hidden = false;

  const installs = ui.state?.vivado_installs?.installations || [];
  const selectedVivado = ui.state?.vivado_installs?.selected_path || "";
  const sessions = ui.state?.sessions || [];
  const orphanCount = Number(ui.state?.system?.orphan_count || 0);
  const edaCount = Number(ui.state?.system?.eda_count || 0);

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

  const procActionBtn =
    orphanCount > 0
      ? `<button type="button" class="btn destructive" id="btn-empty-open-proc">检测到 ${orphanCount} 个疑似残留进程 · 前往处理</button>`
      : `<button type="button" class="btn" id="btn-empty-open-proc">进程与调试面板${edaCount > 0 ? ` (${edaCount} EDA)` : ""}</button>`;

  emptyEl.innerHTML = `
    <div class="empty-hero">
      <div class="empty-art" aria-hidden="true">
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none">
          <rect x="5" y="5" width="14" height="14" rx="2.5" stroke="currentColor" stroke-width="1.6"/>
          <path d="M8 13.5h2l1.2-3.5 1.6 5 1.2-2.5H16" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
      <div>
        <h2>Vivado 控制台已就绪</h2>
        <p>在左下角输入任意 <code>.xpr</code> 工程、<code>.runs</code> 目录或 Batch / OOC 输出目录路径，即可零 Token 查看时序裕量、LUT/FF/BRAM/DSP 资源占用、去重告警和报告原文。其他智能体（Codex / Antigravity / CLI）发起的运行与调试也会实时聚合到此处。</p>
        <div class="empty-hero-actions">${procActionBtn}</div>
      </div>
    </div>

    <div>
      <div class="group-head">
        <span>本机 Vivado 安装（仅路径探测，未启动进程）</span>
        <span class="group-head-sub">${installs.length} 个安装 · ${sessions.length} 个活跃会话</span>
      </div>
      <div class="group">
        <div class="table-wrap">
          <table>
            <thead><tr><th>版本</th><th>可执行路径</th><th>发现来源</th></tr></thead>
            <tbody>${installRows}</tbody>
          </table>
        </div>
      </div>
    </div>
  `;

  byId("btn-empty-open-proc")?.addEventListener("click", () => {
    ui.activeTab = "sessions";
    renderAll();
  });
}

function renderPaneBar(item) {
  const titleEl = byId("pane-title");
  const pillEl = byId("pane-status-pill");
  const clientPillEl = byId("pane-client-pill");
  const metaEl = byId("pane-kind-meta");
  const actsEl = byId("pane-actions");

  if (!item) {
    const orphanCount = Number(ui.state?.system?.orphan_count || 0);
    const vivCount = Number(ui.state?.system?.vivado_count || 0);
    if (titleEl) {
      titleEl.textContent = "主机性能与进程调试";
      titleEl.title = "主机性能与进程调试";
    }
    if (pillEl) {
      pillEl.dataset.status = orphanCount > 0 ? "busy" : "connected";
      pillEl.innerHTML = `<span class="led ${orphanCount > 0 ? "led-warn" : "led-ok"}"></span>${escapeHtml(orphanCount > 0 ? `${orphanCount} 疑似残留` : `${vivCount} Vivado`)}`;
    }
    if (clientPillEl) {
      clientPillEl.hidden = true;
    }
    if (metaEl) {
      metaEl.textContent = "无选中构建目标 · 主机进程与本机服务";
    }
    if (actsEl) {
      actsEl.innerHTML = "";
    }
    return;
  }

  const statusText = STATUS_LABELS[item.status] || item.status || "未知";
  const led = ledClass(item.status, item.timing);

  if (titleEl) {
    titleEl.textContent = item.title;
    titleEl.title = item.title;
  }
  if (pillEl) {
    pillEl.dataset.status = item.status || "unknown";
    pillEl.innerHTML = `<span class="led ${led}"></span>${escapeHtml(statusText)}`;
  }
  if (clientPillEl) {
    if (item.clientSource) {
      clientPillEl.hidden = false;
      clientPillEl.textContent = item.clientSource;
    } else {
      clientPillEl.hidden = true;
    }
  }
  if (metaEl) {
    const parts = [item.kindBadge];
    if (item.version) parts.push(`Vivado ${item.version}`);
    if (item.timing?.stage && item.timing.stage !== "unknown") parts.push(item.timing.stage);
    metaEl.textContent = parts.filter(Boolean).join(" · ");
  }

  const canCancel = item.category === "batch_job" && item.status === "running";
  const canRemove = item.category === "inspected";
  const copyTarget = item.xprPath || item.outputDir || item.logPath || item.targetPath || "";
  const isMcp = ui.transportMode === "mcp";
  const canSendMsg = isMcp && Boolean(ui.hostCaps?.message);
  const canUpdateCtx = isMcp && Boolean(ui.hostCaps?.updateModelContext);

  if (actsEl) {
    actsEl.innerHTML = `
      ${canCancel ? `<button type="button" class="btn destructive" id="hero-cancel-btn">取消任务</button>` : ""}
      ${canRemove ? `<button type="button" class="btn" id="hero-remove-btn">移除</button>` : ""}
      ${copyTarget ? `<button type="button" class="btn" id="hero-copy-btn">复制路径</button>` : ""}
      ${canUpdateCtx ? `<button type="button" class="btn" id="hero-ctx-btn" title="附加上下文给下次对话">附加上下文</button>` : ""}
      ${canSendMsg ? `<button type="button" class="btn primary" id="hero-ask-btn">发到对话</button>` : ""}
    `;
  }

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

function applyProgressWidths(container) {
  for (const fill of container.querySelectorAll(".util-bar-fill[data-pct]")) {
    fill.style.width = `${fill.dataset.pct}%`;
  }
}

function renderOverview(item) {
  const el = byId("tab-panel-overview");
  const errNote = item.errorMessage
    ? `<div class="toast-banner" data-kind="error">${escapeHtml(item.errorMessage)}</div>`
    : "";

  if (item.category === "debug_session") {
    const dbg = item.raw || {};
    const hw = dbg.hardware || {};
    const ilas = hw.ilas || [];
    const vios = hw.vios || [];
    el.innerHTML = `
      <div class="stack">
        ${errNote}
        <div>
          <div class="group-head">
            <span>调试会话状态</span>
            <span class="group-head-sub">revision ${escapeHtml(dbg.revision ?? 0)}</span>
          </div>
          <div class="group">
            <div class="meta-row"><span class="meta-k">目标 Target</span><span class="meta-v">${escapeHtml(dbg.selection?.target || "—")}</span></div>
            <div class="meta-row"><span class="meta-k">器件 Device</span><span class="meta-v">${escapeHtml(dbg.selection?.device || "—")}</span></div>
            <div class="meta-row"><span class="meta-k">控制权 Control</span><span class="meta-v">${escapeHtml(dbg.control || "manual")}</span></div>
            <div class="meta-row"><span class="meta-k">调试核数量</span><span class="meta-v">ILA ${ilas.length} / VIO ${vios.length}</span></div>
          </div>
        </div>
      </div>
    `;
    return;
  }

  const t = item.timing || {};
  const timingOk = t.parse_status === "ok";
  const tiles = [
    { key: "wns", label: "WNS · Setup 最差", val: timingOk ? t.wns : null },
    { key: "tns", label: "TNS · Setup 总负", val: timingOk ? t.tns : null },
    { key: "whs", label: "WHS · Hold 最差", val: timingOk ? t.whs : null },
    { key: "ths", label: "THS · Hold 总负", val: timingOk ? t.ths : null },
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
          <div class="util-cell">
            <div class="util-row-top">
              <span class="util-name">${escapeHtml(label)}</span>
              <span class="util-nums">—</span>
            </div>
            <div class="util-bar-track"><div class="util-bar-fill" data-pct="0"></div></div>
          </div>
        `;
      }
      const pct = Math.max(0, Math.min(100, Number(r.pct)));
      const level = pct >= 85 ? "crit" : pct >= 70 ? "warn" : "ok";
      return `
        <div class="util-cell">
          <div class="util-row-top">
            <span class="util-name">${escapeHtml(label)}</span>
            <span class="util-nums">${escapeHtml(r.used)} / ${escapeHtml(r.avail)} (${pct.toFixed(2)}%)</span>
          </div>
          <div class="util-bar-track"><div class="util-bar-fill" data-level="${level}" data-pct="${pct}"></div></div>
        </div>
      `;
    })
    .join("");

  const progressBlock =
    typeof item.progressPct === "number"
      ? `<div class="progress-head-wrap">
           <div class="util-row-top">
             <span class="util-name">Vivado 原生进度</span>
             <span class="util-nums">${item.progressPct}%</span>
           </div>
           <div class="util-bar-track"><div class="util-bar-fill" data-pct="${Math.max(0, Math.min(100, item.progressPct))}"></div></div>
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
    : `<p class="muted group-pad">暂未观测到 Phase 阶段行。</p>`;

  const runsRows = (item.runs || []).length
    ? `<div>
         <div class="group-head">
           <span>工程 Runs 状态</span>
           <span class="group-head-sub">${item.runs.length} 个</span>
         </div>
         <div class="group">
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
         </div>
       </div>`
    : "";

  const agentParts = [];
  if (item.clientSource) agentParts.push(item.clientSource);
  if (item.isLocalOwner === true && item.clientSource !== "本会话") agentParts.push("本会话");
  if (item.ownerPid) agentParts.push(`宿主 PID ${item.ownerPid}`);
  if (item.vivadoPid) agentParts.push(`Vivado PID ${item.vivadoPid}`);
  const agentMetaRow = agentParts.length
    ? `<div class="meta-row"><span class="meta-k">来源智能体</span><span class="meta-v">${escapeHtml(agentParts.join(" · "))}</span></div>`
    : "";

  const sessionParts = [item.sessionId, item.mode, item.port ? `port ${item.port}` : ""].filter(Boolean);
  const sessionMetaRow = sessionParts.length
    ? `<div class="meta-row"><span class="meta-k">会话 / 模式</span><span class="meta-v">${escapeHtml(sessionParts.join(" · "))}</span></div>`
    : "";

  const projectMetaRow =
    item.project && !item.top
      ? `<div class="meta-row"><span class="meta-k">所属工程</span><span class="meta-v">${escapeHtml(item.project)}</span></div>`
      : "";

  el.innerHTML = `
    <div class="stack">
      ${errNote}

      <div>
        <div class="group-head">
          <span>时序裕量 · Timing</span>
          <span class="group-head-sub">${escapeHtml(t.source_report || "")}</span>
        </div>
        <div class="timing-strip">${timingHtml}</div>
        <p class="timing-caption">${escapeHtml(timingNote)}</p>
      </div>

      <div>
        <div class="group-head">
          <span>资源占用 · Utilization</span>
          <span class="group-head-sub">${escapeHtml(item.raw?.utilization_source || "")}</span>
        </div>
        <div class="group">
          <div class="util-grid">${utilHtml}</div>
        </div>
      </div>

      <div>
        <div class="group-head"><span>元数据与路径</span></div>
        <div class="group">
          ${agentMetaRow}
          ${sessionMetaRow}
          ${projectMetaRow}
          <div class="meta-row"><span class="meta-k">Top 模块</span><span class="meta-v">${escapeHtml(item.top || "—")}</span></div>
          <div class="meta-row"><span class="meta-k">器件 Part</span><span class="meta-v">${escapeHtml(item.part || "—")}</span></div>
          <div class="meta-row"><span class="meta-k">Vivado 版本</span><span class="meta-v">${escapeHtml(item.version || "—")}</span></div>
          <div class="meta-row"><span class="meta-k">耗时 / 状态</span><span class="meta-v">${escapeHtml(item.elapsedText)}${typeof item.exitCode === "number" ? ` (rc=${item.exitCode})` : ""}</span></div>
          <div class="meta-row"><span class="meta-k">输出目录</span><span class="meta-v">${escapeHtml(item.outputDir || "—")}</span></div>
          <div class="meta-row"><span class="meta-k">主日志路径</span><span class="meta-v">${escapeHtml(item.logPath || "—")}</span></div>
          <div class="meta-row"><span class="meta-k">${item.xprPath ? "工程 .xpr" : "Tcl 脚本"}</span><span class="meta-v">${escapeHtml(item.xprPath || item.tclPath || "—")}</span></div>
        </div>
      </div>

      ${runsRows}

      <div>
        <div class="group-head">
          <span>阶段与进度时间线</span>
          <span class="group-head-sub">${escapeHtml(item.currentPhase || "—")}</span>
        </div>
        <div class="group">
          ${progressBlock}
          ${phaseHtml}
        </div>
      </div>
    </div>
  `;

  applyProgressWidths(el);
}

function renderDiagnostics(item) {
  const el = byId("tab-panel-diagnostics");
  const d = item.diagnostics || {};
  const issues = [...pickPrimaryIssues(d), ...(Array.isArray(d.warning_items) ? d.warning_items : [])];
  const query = ui.diagSearch.trim().toLowerCase();

  const filtered = issues.filter((issue) => {
    if (ui.diagSev !== "ALL" && issue.severity !== ui.diagSev) return false;
    if (!query) return true;
    const hay = `${issue.id} ${issue.category} ${issue.message} ${(issue.files || []).join(" ")}`.toLowerCase();
    return hay.includes(query);
  });

  const forensics = item.forensics;
  const forensicsHtml = forensics && forensics.silent_exit_suspected
    ? `<div class="group">
         <div class="diag-row" data-sev="ERROR">
           <div class="diag-row-head">
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
         </div>
       </div>`
    : "";

  const rowsHtml = filtered.length
    ? `<div class="group diag-list">${filtered
        .map(
          (issue) => `
          <div class="diag-row" data-sev="${escapeHtml(issue.severity)}">
            <div class="diag-row-head">
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
          </div>`
        )
        .join("")}</div>`
    : `<div class="group group-pad"><p class="muted">当前过滤条件下无告警或错误条目。${d.scope ? ` (${escapeHtml(d.scope)})` : ""}</p></div>`;

  el.innerHTML = `
    <div class="stack">
      <div class="diag-toolbar">
        <div class="seg seg-sm" role="group" aria-label="严重度筛选">
          <button type="button" class="pill-btn${ui.diagSev === "ALL" ? " active" : ""}" data-sev="ALL">全部 (${issues.length})</button>
          <button type="button" class="pill-btn${ui.diagSev === "ERROR" ? " active" : ""}" data-sev="ERROR">ERROR (${d.errors ?? 0})</button>
          <button type="button" class="pill-btn${ui.diagSev === "CRITICAL WARNING" ? " active" : ""}" data-sev="CRITICAL WARNING">CRITICAL (${d.critical_warnings ?? 0})</button>
          <button type="button" class="pill-btn${ui.diagSev === "WARNING" ? " active" : ""}" data-sev="WARNING">WARNING (${d.warnings ?? 0})</button>
        </div>
        <div class="search-field diag-search-box">
          <svg class="search-icon" viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
            <circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.4"/>
            <path d="m10.5 10.5 3 3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
          </svg>
          <input id="diag-search-input" type="search" value="${escapeHtml(ui.diagSearch)}" placeholder="搜索消息、ID 或文件…" aria-label="搜索诊断条目" />
        </div>
      </div>
      ${forensicsHtml}
      ${rowsHtml}
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
        nextEl.focus({ preventScroll: true });
        if (typeof pos === "number") nextEl.setSelectionRange(pos, pos);
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
          const staleNote = r.freshness === "stale" ? " (过期)" : r.freshness === "mismatched" ? " (不匹配)" : "";
          return `<button type="button" class="rpt-chip" data-rpath="${escapeHtml(r.path)}" data-selected="${selected}">
            <span>${escapeHtml(r.name)}${escapeHtml(staleNote)}</span>
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
    ? `<div class="terminal-msg">正在按需加载报告正文…</div>`
    : viewerError
      ? `<div class="terminal-msg err">加载失败: ${escapeHtml(viewerError)}</div>`
      : visibleLines.length
        ? visibleLines
            .map(
              (ln) =>
                `<div class="term-line" data-level="${classifyLogLine(ln.text)}"><span class="term-ln">${escapeHtml(ln.line || "")}</span><span class="term-txt">${escapeHtml(ln.text || "")}</span></div>`
            )
            .join("")
        : `<div class="terminal-msg">没有匹配的行或内容为空。</div>`;

  el.innerHTML = `
    <div class="stack">
      <div>
        <div class="group-head">
          <span>报告与脚本文件</span>
          <span class="group-head-sub">点击文件名按需读取 · 零 Token</span>
        </div>
        <div class="group">
          <div class="report-chips">${chipsHtml}</div>
        </div>
      </div>

      <div class="terminal-box">
        <div class="terminal-header">
          <div class="terminal-title-wrap">
            <span class="terminal-title">${escapeHtml(viewerTitle)}</span>
            <span>${escapeHtml(viewerMeta)}</span>
          </div>
          <div class="terminal-tools">
            <div class="search-field">
              <svg class="search-icon" viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
                <circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.4"/>
                <path d="m10.5 10.5 3 3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
              </svg>
              <input id="report-filter-input" type="search" value="${escapeHtml(ui.reportFilter)}" placeholder="行内关键字过滤…" aria-label="过滤报告行" />
            </div>
            ${activePath && isTruncated ? `<button type="button" class="btn" id="btn-load-more-rpt">加载 1500 行</button>` : ""}
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
        nextEl.focus({ preventScroll: true });
        if (typeof pos === "number") nextEl.setSelectionRange(pos, pos);
      }
    });
  }
}

function renderSessionsTab() {
  const el = byId("tab-panel-sessions");
  const sys = ui.state?.system || null;
  const procs = Array.isArray(sys?.eda_processes) ? sys.eda_processes : [];
  if (ui.pendingKillPid !== null && !procs.some((p) => Number(p.pid) === ui.pendingKillPid)) {
    ui.pendingKillPid = null;
    if (ui.pendingKillTimer) {
      clearTimeout(ui.pendingKillTimer);
      ui.pendingKillTimer = null;
    }
  }

  const sessions = ui.state?.sessions || [];
  const remoteRuns = ui.state?.remote_runs || [];
  const debugs = ui.state?.debug_sessions || [];
  const installs = ui.state?.vivado_installs?.installations || [];
  const selectedVivado = ui.state?.vivado_installs?.selected_path || "";
  const localServices = collectLocalServices();
  const activeServicesCount = localServices.filter((s) => Boolean(s.url)).length;

  const hasSys = Boolean(sys && typeof sys.ram_total_gb === "number" && sys.ram_total_gb > 0);
  const ramUsed = Number(sys?.ram_used_gb || 0);
  const ramTotal = Number(sys?.ram_total_gb || 0);
  const ramAvail = Number(sys?.ram_avail_gb || 0);
  const ramPct =
    typeof sys?.ram_pct === "number" && Number.isFinite(sys.ram_pct)
      ? Math.max(0, Math.min(100, sys.ram_pct))
      : ramTotal > 0
        ? Math.max(0, Math.min(100, (ramUsed / ramTotal) * 100))
        : 0;
  const ramLevel = ramPct >= 85 ? "crit" : ramPct >= 75 ? "warn" : "ok";
  const cpuCount = sys?.cpu_count ?? "—";
  const vivCount = sys?.vivado_count ?? 0;
  const edaCount = sys?.eda_count ?? procs.length;
  const orphanCount = sys?.orphan_count ?? 0;

  const sysOverviewHtml = `
    <div>
      <div class="group-head">
        <span>主机资源概览</span>
        <div class="group-head-actions">
          <span class="group-head-sub">${hasSys ? `可用 ${ramAvail.toFixed(1)} GB · ${cpuCount} 核` : "等待系统遥测"}</span>
          <button type="button" class="btn" id="btn-cleanup-registry"${ui.cleaningRegistry ? " disabled" : ""}>${ui.cleaningRegistry ? "清理中…" : "清理已结束记录"}</button>
        </div>
      </div>
      <div class="group">
        <div class="progress-head-wrap">
          <div class="util-row-top">
            <span class="util-name">主机物理内存</span>
            <span class="util-nums">${hasSys ? `${ramUsed.toFixed(1)} / ${ramTotal.toFixed(1)} GB (${ramPct.toFixed(1)}%) · 可用 ${ramAvail.toFixed(1)} GB` : "—"}</span>
          </div>
          <div class="util-bar-track"><div class="util-bar-fill" data-level="${hasSys ? ramLevel : "ok"}" data-pct="${hasSys ? ramPct.toFixed(1) : "0"}"></div></div>
        </div>
        <div class="sys-stat-grid">
          <div class="sys-stat-cell">
            <span class="sys-stat-k">CPU 逻辑核心</span>
            <span class="sys-stat-v">${escapeHtml(hasSys ? `${cpuCount} 核` : "—")}</span>
          </div>
          <div class="sys-stat-cell">
            <span class="sys-stat-k">Vivado 进程</span>
            <span class="sys-stat-v">${escapeHtml(sys ? `${vivCount} 个` : "—")}</span>
          </div>
          <div class="sys-stat-cell">
            <span class="sys-stat-k">EDA 进程总数</span>
            <span class="sys-stat-v">${escapeHtml(sys ? `${edaCount} 个` : "—")}</span>
          </div>
          <div class="sys-stat-cell">
            <span class="sys-stat-k">疑似残留</span>
            <span class="sys-stat-v" data-tone="${orphanCount > 0 ? "warn" : "ok"}">${escapeHtml(sys ? `${orphanCount} 个` : "—")}</span>
          </div>
        </div>
      </div>
    </div>
  `;

  const procRowsHtml = procs.length
    ? procs
        .map((proc) => {
          const pid = Number(proc.pid);
          const isOrphan = Boolean(proc.is_orphan);
          const isPending = ui.pendingKillPid === pid;
          const isKilling = ui.killingPid === pid;
          const ownerTag = isOrphan
            ? `<span class="proc-tag" data-tone="orphan">疑似残留</span>`
            : proc.owner_client
              ? `<span class="client-pill">${escapeHtml(proc.owner_client)}</span>`
              : "";
          const assocTag =
            proc.associated_label && proc.associated_label !== proc.owner_client
              ? `<span class="kind-chip">${escapeHtml(proc.associated_label)}</span>`
              : "";
          const killHtml =
            proc.killable !== false
              ? `<button type="button" class="btn destructive${isPending || isKilling ? " confirming" : ""} proc-kill-btn" data-kill-pid="${escapeHtml(pid)}"${isKilling ? " disabled" : ""}>${isKilling ? "终止中…" : isPending ? "确认终止?" : "终止进程"}</button>`
              : `<span class="kind-chip">仅观测</span>`;
          return `
            <div class="proc-row" data-orphan="${isOrphan ? "true" : "false"}">
              <div class="proc-main">
                <div class="proc-title-line">
                  <span class="proc-name mono">${escapeHtml(proc.name || "process")} · PID ${escapeHtml(pid)}</span>
                  ${ownerTag}
                  ${assocTag}
                </div>
                <div class="proc-meta-line">
                  <span class="mono">内存 ${escapeHtml(fmtRssMb(proc.rss_mb))}</span>
                  <span class="mono">运行时长 ${escapeHtml(fmtUptime(proc.uptime_sec))}</span>
                  ${proc.owner_pid ? `<span class="mono">来源 PID ${escapeHtml(proc.owner_pid)}</span>` : ""}
                  ${proc.command_hint ? `<span class="proc-hint" title="${escapeHtml(proc.command_hint)}">${escapeHtml(proc.command_hint)}</span>` : ""}
                </div>
              </div>
              <div class="proc-actions">${killHtml}</div>
            </div>
          `;
        })
        .join("")
    : `<p class="muted group-pad">当前未检测到运行中的 Vivado / EDA 相关进程。</p>`;

  const localServiceRows = localServices
    .map((svc) => {
      const copied = Boolean(svc.url) && ui.copiedServiceUrl === svc.url;
      const urlHtml = svc.url
        ? `<code class="service-url mono">${escapeHtml(svc.url)}</code>`
        : `<span class="service-url muted">${escapeHtml(svc.error ? `未启动 (${svc.error})` : "尚未启动本机 HTTP 服务")}</span>`;
      const actionsHtml = svc.url
        ? `<button type="button" class="btn" data-copy-service-url="${escapeHtml(svc.url)}">${copied ? "已复制" : "复制地址"}</button>
           <button type="button" class="btn" data-open-service-url="${escapeHtml(svc.url)}" data-service-kind="${escapeHtml(svc.kind)}">浏览器打开</button>`
        : `<button type="button" class="btn" id="btn-ensure-console-web">启动本机服务</button>
           <button type="button" class="btn" data-open-service-url="" data-service-kind="console">浏览器打开</button>`;
      return `
        <div class="service-row">
          <div class="service-main">
            <div class="service-title-line">
              <span class="service-name">${escapeHtml(svc.title)}</span>
              <span class="kind-chip">${escapeHtml(svc.badge)}</span>
              ${svc.sharedHub ? `<span class="kind-chip">多 Agent 共享枢纽</span>` : ""}
              <span class="status-badge" data-status="${escapeHtml(svc.status)}">${escapeHtml(svc.statusLabel)}</span>
            </div>
            ${urlHtml}
          </div>
          <div class="service-actions">${actionsHtml}</div>
        </div>
      `;
    })
    .join("");

  const mergedSessions = [];
  const seenSessKeys = new Set();
  const localClient =
    String(
      ui.state?.client ||
        (ui.state?.batch_jobs || []).find((j) => j.is_local_owner === true && j.client)?.client ||
        ""
    ).trim() || "本会话";

  for (const s of sessions) {
    const key = s.port ? `port:${s.port}` : `local:${s.session_id || ""}`;
    seenSessKeys.add(key);
    mergedSessions.push({
      session_id: s.session_id || "",
      client: localClient,
      mode: s.mode || "",
      status: s.is_alive ? s.state || "ready" : "disconnected",
      statusText: s.state || (s.is_alive ? "ready" : "dead"),
      port: s.port ?? "—",
      version: s.runtime_version || "—",
      detail: s.vivado_path || "—",
    });
  }
  for (const rr of remoteRuns) {
    if (!rr.session_id && !rr.port) continue;
    const key = rr.port ? `port:${rr.port}` : `remote:${rr.owner_pid || 0}:${rr.session_id || ""}`;
    if (seenSessKeys.has(key)) continue;
    seenSessKeys.add(key);
    const st = rr.alive ? rr.state || rr.status || "ready" : rr.status || "disconnected";
    mergedSessions.push({
      session_id: rr.session_id || rr.entry_id || "",
      client: rr.client || "外部",
      mode: rr.mode || "remote",
      status: rr.alive ? st : "disconnected",
      statusText: st,
      port: rr.port ?? "—",
      version: rr.vivado_version || "—",
      detail: `${rr.xpr_path || rr.project_name || "—"}${rr.vivado_pid ? ` (PID ${rr.vivado_pid})` : ""}`,
    });
  }

  const sessRows = mergedSessions.length
    ? mergedSessions
        .map(
          (s) => `
          <tr>
            <td class="mono">${escapeHtml(s.session_id)}</td>
            <td><span class="client-pill">${escapeHtml(s.client)}</span></td>
            <td><span class="kind-chip">${escapeHtml(s.mode)}</span></td>
            <td><span class="status-badge" data-status="${escapeHtml(s.status)}">${escapeHtml(s.statusText)}</span></td>
            <td class="mono">${escapeHtml(s.port)}</td>
            <td class="mono">${escapeHtml(s.version)}</td>
            <td class="mono">${escapeHtml(s.detail)}</td>
          </tr>`
        )
        .join("")
    : `<tr><td colspan="7" class="muted">当前没有活跃的 Vivado Tcl / GUI 会话。</td></tr>`;

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
            <div>
              <div class="group-head">
                <span>硬件调试会话: ${escapeHtml(d.session_id)}</span>
                <span class="status-badge" data-status="${escapeHtml(d.connection)}">${escapeHtml(STATUS_LABELS[d.connection] || d.connection)}</span>
              </div>
              <div class="group group-pad">
                <p class="muted">控制权: <code>${escapeHtml(d.control)}</code> · 目标: <code>${escapeHtml(d.selection?.target || "未选")}</code> / <code>${escapeHtml(d.selection?.device || "未选")}</code> · stop_ila=${escapeHtml(String(Boolean(d.capabilities?.stop_ila)))}</p>
                ${ilas ? `<ul class="dbg-list">${ilas}</ul>` : ""}
                ${vios ? `<ul class="dbg-list">${vios}</ul>` : ""}
              </div>
            </div>`;
        })
        .join("")
    : `<div>
         <div class="group-head"><span>硬件调试 / ILA</span></div>
         <div class="group group-pad"><p class="muted">暂无活动的硬件调试 / ILA 面板。</p></div>
       </div>`;

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
      ${sysOverviewHtml}

      <div>
        <div class="group-head">
          <span>本机 Vivado / EDA 进程管理</span>
          <span class="group-head-sub">${procs.length} 个活动进程${orphanCount > 0 ? ` · ${orphanCount} 个疑似残留` : ""}</span>
        </div>
        <div class="group">${procRowsHtml}</div>
      </div>

      <div>
        <div class="group-head">
          <span>本机服务地址 (127.0.0.1)</span>
          <span class="group-head-sub">${activeServicesCount} 个可用</span>
        </div>
        <div class="group">${localServiceRows}</div>
      </div>

      <div>
        <div class="group-head">
          <span>活跃 Vivado 会话 (Tcl / GUI / External)</span>
          <span class="group-head-sub">${mergedSessions.length} 个</span>
        </div>
        <div class="group">
          <div class="table-wrap">
            <table>
              <thead><tr><th>会话 ID</th><th>来源</th><th>模式</th><th>状态</th><th>端口</th><th>版本</th><th>路径 / PID</th></tr></thead>
              <tbody>${sessRows}</tbody>
            </table>
          </div>
        </div>
      </div>

      ${dbgHtml}

      <div>
        <div class="group-head">
          <span>本机 Vivado 安装探测</span>
          <span class="group-head-sub">${installs.length} 个</span>
        </div>
        <div class="group">
          <div class="table-wrap">
            <table>
              <thead><tr><th>版本</th><th>路径</th><th>来源</th></tr></thead>
              <tbody>${instRows}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  `;

  applyProgressWidths(el);

  byId("btn-cleanup-registry")?.addEventListener("click", handleCleanupStaleRegistry);
  for (const btn of el.querySelectorAll("[data-kill-pid]")) {
    btn.addEventListener("click", () => {
      handleProcKillClick(Number(btn.dataset.killPid));
    });
  }
  for (const btn of el.querySelectorAll("[data-copy-service-url]")) {
    btn.addEventListener("click", () => {
      handleCopyLocalUrl(btn.dataset.copyServiceUrl, { source: "service" });
    });
  }
  for (const btn of el.querySelectorAll("[data-open-service-url]")) {
    btn.addEventListener("click", () => {
      handleOpenLocalUrl(btn.dataset.openServiceUrl, {
        isConsole: btn.dataset.serviceKind === "console",
      });
    });
  }
  byId("btn-ensure-console-web")?.addEventListener("click", handleEnsureLocalWeb);
}

function updateTabVisibility(item) {
  for (const btn of document.querySelectorAll(".tab-btn")) {
    const active = btn.dataset.tab === ui.activeTab;
    btn.classList.toggle("active", active);
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

  const orphanCount = Number(ui.state?.system?.orphan_count || 0);
  const edaCount = Number(ui.state?.system?.eda_count || 0);
  const baseCount = (ui.state?.sessions || []).length + (ui.state?.debug_sessions || []).length;
  const sessBadge = byId("tab-sess-badge");
  if (orphanCount > 0) {
    sessBadge.hidden = false;
    sessBadge.textContent = String(baseCount + orphanCount);
    sessBadge.dataset.tone = "warn";
  } else {
    const count = baseCount > 0 ? baseCount : edaCount;
    sessBadge.hidden = count === 0;
    sessBadge.textContent = String(count);
    sessBadge.dataset.tone = "normal";
  }
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
    stageScroll: byId("detail-stage")?.scrollTop ?? 0,
    termScroll: document.querySelector(".terminal-lines")?.scrollTop ?? 0,
    timelineScroll: document.querySelector(".timeline")?.scrollTop ?? 0,
    diagScroll: document.querySelector(".diag-list")?.scrollTop ?? 0,
  };
}

function restoreViewportState(snap) {
  if (!snap) return;
  const rail = byId("rail-list");
  if (rail) rail.scrollTop = snap.railScroll;
  const stage = byId("detail-stage");
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

  if (items.length && !items.some((i) => i.uid === ui.selectedUid)) {
    ui.selectedUid = items[0].uid;
    ui.reportPreview = null;
    ui.reportReqId += 1;
  } else if (!items.length) {
    ui.selectedUid = "";
    ui.reportPreview = null;
    ui.reportReqId += 1;
  }

  updateSummaryAndStatus(items);
  renderRail(items);

  const activeItem = items.find((i) => i.uid === ui.selectedUid) || null;
  if (!activeItem && ui.activeTab !== "sessions") {
    renderEmptyState();
    restoreViewportState(snap);
    return;
  }

  byId("empty-stage").hidden = true;
  byId("detail-shell").hidden = false;

  renderPaneBar(activeItem);
  updateTabVisibility(activeItem);

  if (!activeItem || ui.activeTab === "sessions") renderSessionsTab();
  else if (ui.activeTab === "overview") renderOverview(activeItem);
  else if (ui.activeTab === "diagnostics") renderDiagnostics(activeItem);
  else if (ui.activeTab === "reports") renderReports(activeItem);

  restoreViewportState(snap);
}

function applyIncomingState(payload) {
  if (!payload || typeof payload !== "object") return;
  ui.state = payload;
  const seq = typeof payload.selection_seq === "number" ? payload.selection_seq : null;
  const shouldApplySelection = seq === null || seq !== ui.lastSelectionSeq;
  if (seq !== null) {
    ui.lastSelectionSeq = seq;
  }
  if (shouldApplySelection) {
    if (payload.selected_job_id) {
      const nextUid = `job:${payload.selected_job_id}`;
      if (nextUid !== ui.selectedUid) {
        ui.selectedUid = nextUid;
        ui.pendingScrollSelection = true;
        ui.reportPreview = null;
        ui.reportReqId += 1;
      }
    } else if (payload.selected_inspected_path) {
      const nextUid = `inspected:${payload.selected_inspected_path}`;
      if (nextUid !== ui.selectedUid) {
        ui.selectedUid = nextUid;
        ui.pendingScrollSelection = true;
        ui.reportPreview = null;
        ui.reportReqId += 1;
      }
    }
  }
  if (payload.inspect_error) {
    showToast(`路径检视失败: ${payload.inspect_error}`, "error");
  }
  setConnState("ready", "已同步", fmtTime(payload.server_time));
  renderAll();
}

async function transportGetState() {
  if (ui.transportMode === "http") {
    const resp = await fetch("/api/state", { cache: "no-store" });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      throw new Error(data?.error || `HTTP ${resp.status}`);
    }
    return data;
  }
  const res = await app.callServerTool({
    name: "vivado_console_state",
    arguments: {},
  });
  if (res?.isError && !res?.structuredContent) {
    throw new Error(res.content?.[0]?.text || "刷新失败");
  }
  return res?.structuredContent || null;
}

async function transportRunAction(args) {
  if (ui.transportMode === "http") {
    const resp = await fetch("/api/action", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(args),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || data?.error) {
      throw new Error(data?.error || `HTTP ${resp.status}`);
    }
    return data;
  }
  const res = await app.callServerTool({
    name: "vivado_console_action",
    arguments: args,
  });
  const sc = res?.structuredContent;
  if (res?.isError || sc?.error) {
    throw new Error(sc?.error || res?.content?.[0]?.text || "操作失败");
  }
  return sc;
}

async function refreshState({ silent = false, force = false } = {}) {
  if (ui.inFlightState) {
    if (!force) return;
    try {
      await ui.inFlightPromise;
    } catch {
      // ignore previous failure
    }
  }
  ui.inFlightState = true;
  const task = (async () => {
    try {
      const state = await transportGetState();
      if (state) {
        applyIncomingState(state);
      }
    } catch (err) {
      if (!silent) {
        showToast(`刷新失败: ${err?.message || String(err)}`, "error");
      }
      setConnState("error", "同步失败");
      updateSummaryAndStatus(buildItems(ui.state));
    } finally {
      ui.inFlightState = false;
      ui.inFlightPromise = null;
    }
  })();
  ui.inFlightPromise = task;
  await task;
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
    const sc = await transportRunAction({ action: "inspect_path", path: clean });
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
    const sc = await transportRunAction({ action: "remove_inspected", path: targetPath });
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
    const sc = await transportRunAction({ action: "cancel_job", job_id: jobId });
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
    const sc = await transportRunAction({ action: "clear_finished" });
    if (sc?.state) {
      applyIncomingState(sc.state);
    }
    showToast(`已清理 ${sc?.cleared ?? 0} 条已结束任务记录`, "info");
  } catch (err) {
    showToast(`清理失败: ${err?.message || String(err)}`, "error");
  }
}

async function handleCleanupStaleRegistry() {
  if (ui.cleaningRegistry) return;
  ui.cleaningRegistry = true;
  if (ui.activeTab === "sessions") renderSessionsTab();
  try {
    const sc = await transportRunAction({ action: "cleanup_stale_registry" });
    const removed = Number(sc?.removed_entries ?? 0);
    if (sc?.state) {
      applyIncomingState(sc.state);
    } else {
      await refreshState({ silent: true, force: true });
    }
    showToast(
      removed > 0 ? `已清理 ${removed} 条已结束跨智能体记录` : "暂无需要清理的已结束记录",
      removed > 0 ? "success" : "info"
    );
  } catch (err) {
    showToast(`清理记录失败: ${err?.message || String(err)}`, "error");
  } finally {
    ui.cleaningRegistry = false;
    if (ui.activeTab === "sessions") renderSessionsTab();
  }
}

function handleProcKillClick(pid) {
  if (!Number.isFinite(pid) || pid <= 0 || ui.killingPid === pid) return;
  if (ui.pendingKillPid !== pid) {
    if (ui.pendingKillTimer) {
      clearTimeout(ui.pendingKillTimer);
      ui.pendingKillTimer = null;
    }
    ui.pendingKillPid = pid;
    ui.pendingKillTimer = setTimeout(() => {
      if (ui.pendingKillPid === pid) {
        ui.pendingKillPid = null;
        ui.pendingKillTimer = null;
        if (ui.activeTab === "sessions") renderSessionsTab();
      }
    }, 4000);
    if (ui.activeTab === "sessions") renderSessionsTab();
    return;
  }

  if (ui.pendingKillTimer) {
    clearTimeout(ui.pendingKillTimer);
    ui.pendingKillTimer = null;
  }
  ui.pendingKillPid = null;
  executeKillProcess(pid);
}

async function executeKillProcess(pid) {
  ui.killingPid = pid;
  if (ui.activeTab === "sessions") renderSessionsTab();
  try {
    const sc = await transportRunAction({ action: "kill_process", pid });
    const label = sc?.name ? `${sc.name} (PID ${sc.pid ?? pid})` : `PID ${pid}`;
    showToast(sc?.message || `已终止进程 ${label}`, "success");
    if (sc?.state) {
      applyIncomingState(sc.state);
    } else {
      await refreshState({ silent: true, force: true });
    }
  } catch (err) {
    showToast(`终止进程失败: ${err?.message || String(err)}`, "error");
  } finally {
    ui.killingPid = null;
    if (ui.activeTab === "sessions") renderSessionsTab();
  }
}

async function handleReadReport(reportPath, maxLines = 300) {
  if (!reportPath) return;
  const reqId = ++ui.reportReqId;
  const targetUid = ui.selectedUid;
  ui.reportPreview = { path: reportPath, name: basename(reportPath), loading: true };
  renderAll();
  try {
    const sc = await transportRunAction({
      action: "read_report",
      report_path: reportPath,
      max_lines: maxLines,
    });
    if (reqId !== ui.reportReqId || targetUid !== ui.selectedUid) return;
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

function isClipboardWriteAllowed() {
  try {
    if (typeof document.permissionsPolicy?.allowsFeature === "function") {
      return document.permissionsPolicy.allowsFeature("clipboard-write");
    }
    if (typeof document.featurePolicy?.allowsFeature === "function") {
      return document.featurePolicy.allowsFeature("clipboard-write");
    }
  } catch {
    // ignore
  }
  return ui.transportMode !== "mcp";
}

async function copyWithFallback(text) {
  if (navigator.clipboard?.writeText && isClipboardWriteAllowed()) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fallback below
    }
  }
  try {
    const ta = document.createElement("textarea");
    ta.className = "sr-copy-input";
    ta.value = text;
    ta.setAttribute("readonly", "");
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return Boolean(ok);
  } catch {
    return false;
  }
}

async function copyText(text) {
  if (await copyWithFallback(text)) {
    showToast("已复制到剪贴板", "success");
    return;
  }
  showToast(`路径: ${text}`, "info");
}

async function handleCopyLocalUrl(url, { source = "status" } = {}) {
  const targetUrl = url || getLocalWebInfo().url;
  if (!targetUrl) return;
  const ok = await copyWithFallback(targetUrl);
  if (source === "status") {
    ui.localUrlCopied = true;
    if (ui.localCopyTimer) clearTimeout(ui.localCopyTimer);
    ui.localCopyTimer = setTimeout(() => {
      ui.localUrlCopied = false;
      renderLocalWebStatus();
    }, 1800);
    renderLocalWebStatus();
  } else {
    ui.copiedServiceUrl = targetUrl;
    if (ui.serviceCopyTimer) clearTimeout(ui.serviceCopyTimer);
    ui.serviceCopyTimer = setTimeout(() => {
      ui.copiedServiceUrl = "";
      if (ui.activeTab === "sessions") renderSessionsTab();
    }, 1800);
    if (ui.activeTab === "sessions") renderSessionsTab();
  }
  showToast(
    ok ? `已复制本机服务地址: ${targetUrl}` : `请手动复制地址: ${targetUrl}`,
    ok ? "success" : "info"
  );
}

async function handleEnsureLocalWeb() {
  if (ui.transportMode !== "mcp") return;
  try {
    const sc = await transportRunAction({ action: "ensure_local_web" });
    if (sc?.local_web) {
      ui.state = { ...(ui.state || {}), local_web: sc.local_web, url: sc.url || sc.local_web.url };
      renderAll();
    }
    if (sc?.url) {
      showToast(`已启动本机服务: ${sc.url}`, "success");
    }
  } catch (err) {
    showToast(`启动本机服务失败: ${err?.message || String(err)}`, "error");
  }
}

async function handleOpenLocalUrl(url, { isConsole = true } = {}) {
  let targetUrl = url || (isConsole ? getLocalWebInfo().url : "");
  if (ui.transportMode === "http") {
    if (targetUrl) {
      window.open(targetUrl, "_blank", "noopener,noreferrer");
    }
    return;
  }

  if (isConsole && !targetUrl) {
    try {
      const sc = await transportRunAction({ action: "ensure_local_web" });
      if (sc?.local_web) {
        ui.state = { ...(ui.state || {}), local_web: sc.local_web, url: sc.url || sc.local_web.url };
        renderLocalWebStatus();
      }
      targetUrl = sc?.url || sc?.local_web?.url || "";
    } catch {
      // fall through to open_local_browser
    }
  }

  if (targetUrl && ui.hostCaps?.openLinks && typeof app.openLink === "function") {
    try {
      const res = await app.openLink({ url: targetUrl });
      if (!res?.isError) return;
    } catch {
      // fall back below
    }
  }

  if (isConsole) {
    try {
      const sc = await transportRunAction({ action: "open_local_browser" });
      if (sc?.local_web) {
        ui.state = { ...(ui.state || {}), local_web: sc.local_web, url: sc.url || sc.local_web.url };
        renderLocalWebStatus();
      }
      const openedUrl = sc?.url || sc?.local_web?.url || targetUrl;
      showToast(`已在浏览器打开: ${openedUrl}`, "success");
    } catch (err) {
      showToast(`打开浏览器失败: ${err?.message || String(err)}`, "error");
    }
    return;
  }

  if (targetUrl) {
    await copyWithFallback(targetUrl);
    showToast(`已复制地址，请在浏览器打开: ${targetUrl}`, "info");
  }
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
  if (ui.transportMode !== "mcp" || !ui.hostCaps?.updateModelContext) return;
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
  if (ui.transportMode !== "mcp" || !ui.hostCaps?.message) return;
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
  const btn = byId("btn-auto-refresh");
  if (btn) {
    btn.classList.toggle("on", ui.autoRefresh);
    btn.setAttribute("aria-pressed", ui.autoRefresh ? "true" : "false");
  }
  if (ui.autoRefresh) {
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
  byId("btn-clear-finished")?.addEventListener("click", handleClearFinished);
  byId("btn-sb-copy-url")?.addEventListener("click", () => handleCopyLocalUrl("", { source: "status" }));
  byId("btn-sb-open-browser")?.addEventListener("click", () => handleOpenLocalUrl("", { isConsole: true }));
  byId("btn-sb-orphans")?.addEventListener("click", () => {
    ui.activeTab = "sessions";
    renderAll();
  });
  byId("btn-auto-refresh").addEventListener("click", () => {
    ui.autoRefresh = !ui.autoRefresh;
    syncAutoRefresh();
  });

  byId("dynamic-island")?.addEventListener("click", () => {
    const items = buildItems(ui.state);
    const runningItem = items.find((i) => statusBucket(i.status) === "running" && i.uid !== ui.selectedUid);
    if (runningItem) {
      ui.selectedUid = runningItem.uid;
      if (ui.activeTab === "sessions") ui.activeTab = "overview";
      ui.pendingScrollSelection = true;
      ui.reportPreview = null;
      ui.reportReqId += 1;
      ui.reportFilter = "";
      renderAll();
      return;
    }
    refreshState({ silent: false });
  });

  byId("btn-theme")?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    setThemeMenuOpen(!ui.themeMenuOpen);
  });

  byId("theme-menu")?.addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-theme-id]");
    if (!btn) return;
    applyTheme(btn.dataset.themeId, { manual: true });
    setThemeMenuOpen(false);
  });

  document.addEventListener("click", (ev) => {
    if (!ui.themeMenuOpen) return;
    if (!ev.target.closest("#theme-menu-wrap")) {
      setThemeMenuOpen(false);
    }
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && ui.themeMenuOpen) {
      setThemeMenuOpen(false);
    }
  });

  byId("rail-search").addEventListener("input", (ev) => {
    ui.railSearch = ev.target.value;
    renderRail(buildItems(ui.state));
  });

  for (const chip of document.querySelectorAll("#summary-chips .filter-chip")) {
    chip.addEventListener("click", () => {
      ui.statusFilter = chip.dataset.filter || "";
      renderAll();
    });
  }

  byId("client-filter-bar")?.addEventListener("click", (ev) => {
    const chip = ev.target.closest("[data-client]");
    if (!chip) return;
    ui.clientFilter = chip.dataset.client || "";
    renderAll();
  });

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
  if (!ctx || ui.manualTheme) return;
  if (ctx.theme === "light") {
    applyTheme("macos-light", { manual: false });
  } else if (ctx.theme === "dark") {
    applyTheme("macos", { manual: false });
  }
}

// 必须在 connect() 之前注册 MCP App 处理器，避免错过初始 toolinput / toolresult
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

function setTransportMode(mode) {
  ui.transportMode = mode;
  const sb = byId("sb-transport");
  if (sb) sb.textContent = mode === "http" ? "Local Web" : "MCP App";
  renderLocalWebStatus();
}

async function initHttpMode() {
  setTransportMode("http");
  ui.hostCaps = {};
  const stored = readStoredTheme();
  if (stored) {
    applyTheme(stored, { manual: true });
  } else {
    applyTheme("macos", { manual: false });
  }
  await refreshState({ silent: false });
  syncAutoRefresh();
}

async function init() {
  applyTheme("macos", { manual: false });
  bindEvents();

  const isEmbedded = (() => {
    try {
      return window.parent !== window;
    } catch {
      return true;
    }
  })();

  const isLoopbackHttp =
    (window.location.protocol === "http:" || window.location.protocol === "https:") &&
    window.location.hostname === "127.0.0.1";

  if (!isEmbedded) {
    await initHttpMode();
    return;
  }

  try {
    if (isLoopbackHttp) {
      await Promise.race([
        app.connect(),
        new Promise((_, reject) => setTimeout(() => reject(new Error("MCP handshake timeout")), 1200)),
      ]);
    } else {
      await app.connect();
    }
    setTransportMode("mcp");
    ui.hostCaps = app.getHostCapabilities() || {};
    applyHostCtx(app.getHostContext());
    if (!ui.state) {
      await refreshState({ silent: true });
    } else {
      renderAll();
    }
    syncAutoRefresh();
  } catch (err) {
    if (isLoopbackHttp) {
      await initHttpMode();
      return;
    }
    setConnState("error", "连接失败");
    showToast(`MCP App 握手失败: ${err?.message || String(err)}`, "error");
    renderEmptyState();
  }
}

init();
