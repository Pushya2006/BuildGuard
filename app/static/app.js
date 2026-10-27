"use strict";
// All values from the API are rendered with textContent (never innerHTML).
const $ = (id) => document.getElementById(id);
const TONE = {
  green: ["ok", "PASS", "SUPPORTED", "ACCEPT", "completed"],
  amber: ["finding", "SKIP", "UNSUPPORTED", "REVISE"],
  red: ["failed", "error", "FAIL", "STOP", "build_failed", "MISSING"],
  blue: ["running", "queued"],
};
const tone = (s) => Object.keys(TONE).find((k) => TONE[k].includes(s)) || "gray";
const STATUS_TEXT = { ok: "OK", finding: "RESULT", failed: "FAILED", error: "ERROR", skipped: "SKIPPED",
  pending: "PENDING", running: "RUNNING", completed: "COMPLETED", build_failed: "BUILD FAILED", queued: "QUEUED" };

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) el.append(kid);
  return el;
}
const chip = (status, text) => h("span", { class: "chip " + tone(status) }, text || STATUS_TEXT[status] || String(status));
const clear = (el, ...kids) => { el.replaceChildren(...kids.flat().filter((k) => k != null)); };
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const val = (v) => (v === null || v === undefined || v === "" ? "—" : String(v));

async function api(path, options) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!res.ok) {
    let detail = res.statusText;
    try { const body = await res.json(); detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail); } catch (e) { /* keep status text */ }
    throw new Error(detail);
  }
  return res;
}

let current = null, poll = null, jsonTab = "provenance", logTab = "tests", logCache = {};

function showNotice(msg) { const n = $("notice"); n.hidden = !msg; n.textContent = msg || ""; }

async function loadProjects() {
  const sel = $("projectSelect");
  try {
    const data = await (await api("/api/projects")).json();
    $("projectsInfo").textContent = "Projects root: " + data.projects_root;
    if (!data.projects.length) {
      clear(sel, h("option", {}, "No projects found in the projects root"));
      return;
    }
    clear(sel, data.projects.map((p) => h("option", { value: p.name },
      p.name + (p.is_git ? " · git" : " · no git") + (p.has_tests ? "" : " · no tests found"))));
    sel.disabled = false;
    $("runBtn").disabled = false;
  } catch (e) { showNotice("Could not load projects: " + e.message); }
}

async function startRun() {
  showNotice("");
  $("runBtn").disabled = true;
  try {
    const run = await (await api("/api/runs", { method: "POST", body: JSON.stringify({ project: $("projectSelect").value }) })).json();
    logCache = {}; logTab = "tests";
    await openRun(run.run_id);
  } catch (e) { showNotice(e.message); $("runBtn").disabled = false; }
}

async function openRun(runId) {
  clearInterval(poll);
  try {
    current = await (await api("/api/runs/" + encodeURIComponent(runId))).json();
  } catch (e) { showNotice(e.message); return; }
  render(current);
  loadHistory();
  const active = ["queued", "running"].includes(current.status);
  $("runBtn").disabled = active || $("projectSelect").disabled;
  if (active) poll = setInterval(() => openRun(runId), 1000);
}

function render(run) {
  renderBanner(run); renderStages(run); renderRequirements(run); renderArtifact(run);
  renderChecks(run); renderTamper(run); renderJson(run); renderDownloads(run); renderLogs(run);
}

function renderBanner(run) {
  const b = $("banner"), r = run.reports || {};
  let cls = "idle", label = "", text = "";
  if (run.status === "queued" || run.status === "running") {
    cls = "blue"; label = "Running…"; text = "Current stage: " + val(run.current_stage) + " — project " + run.project;
  } else if (run.status === "build_failed") {
    cls = "red"; label = "BUILD FAILED";
    text = (r.build_status && r.build_status.failure_reason || "The build did not complete.") + " No artifact, provenance or decision was produced. See the test log below.";
  } else if (run.status === "error") {
    cls = "red"; label = "PIPELINE ERROR";
    text = (run.error || "A pipeline stage did not run correctly.") + (run.decision ? " A partial decision (" + run.decision + ") exists but should not be relied on." : "");
  } else if (run.decision) {
    cls = tone(run.decision); label = run.decision; text = run.decision_reason || "";
  } else { label = "No decision"; text = "The run finished without an assurance decision."; }
  b.className = "banner " + cls;
  $("bannerLabel").textContent = label;
  $("bannerText").textContent = text;
}

function renderStages(run) {
  clear($("stages"), run.stages.map((s) => h("li", {},
    h("div", { class: "name" }, s.label),
    h("div", {}, chip(s.status)),
    h("div", { class: "msg" }, [s.message, s.duration_seconds != null ? s.duration_seconds + "s" : "", s.exit_code != null && s.exit_code !== 0 ? "exit " + s.exit_code : ""].filter(Boolean).join(" · ")))));
}

function renderRequirements(run) {
  const a = run.reports.assurance, box = $("reqs");
  box.className = "list";
  if (!a) { box.className = "list muted"; clear(box, "No assurance report for this run."); $("disclaimer").textContent = ""; return; }
  clear(box, a.requirements.map((q) => h("div", {},
    h("div", { class: "head" }, h("strong", {}, q.id), h("span", {}, q.name), chip(q.status), q.mandatory === false ? h("span", { class: "muted small" }, "optional") : null),
    h("div", { class: "expl" }, q.explanation))));
  $("disclaimer").textContent = a.disclaimer || "";
}

function renderArtifact(run) {
  const e = run.reports.evidence, box = $("artifact"), dl = run.downloads.find((d) => d.key === "artifact");
  if (!e) { box.className = "kv muted"; clear(box, run.reports.build_status ? "No artifact: the build did not succeed." : "No evidence for this run."); return; }
  box.className = "kv";
  const local = e.build_environment === "local";
  const commit = e.local_commit_sha || e.commit_sha;
  const rows = [
    ["Project", e.project_name], ["Run ID", e.run_id],
    ["Build environment", local ? "Local build (not GitHub Actions)" : "GitHub Actions"],
    ["Tests", e.tests_status], ["Build status", e.build_status],
    ["Artifact", e.artifact_filename + (dl ? " (" + dl.size + " bytes)" : "")],
    ["SHA-256", h("span", { class: "mono" }, e.artifact_sha256, " ", h("button", { type: "button", onclick: (ev) => copy(e.artifact_sha256, ev.target) }, "Copy"))],
    ["Git commit", commit ? h("span", { class: "mono" }, commit) : "none recorded"],
    ["Working tree", e.source_dirty === true ? "uncommitted changes" : e.source_dirty === false ? "clean" : "unknown"],
    ["Source", e.source_uri || e.repository],
    ["Source trust", local ? "Locally observed, unattested" : "Reported by GitHub Actions environment; provenance unsigned"],
    ["Built (UTC)", e.build_timestamp_utc], ["Python", e.python_version],
  ];
  clear(box, rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, typeof v === "object" && v ? v : val(v))]));
}

async function copy(text, button) {
  try { await navigator.clipboard.writeText(text); button.textContent = "Copied"; } catch (e) { button.textContent = "Copy failed"; }
  setTimeout(() => (button.textContent = "Copy"), 1500);
}

function renderChecks(run) {
  const v = run.reports.verification, box = $("checks");
  if (!v) { box.className = "muted"; clear(box, "No verification report for this run."); return; }
  box.className = "";
  clear(box, h("p", {}, "Overall: ", chip(v.verification_status)),
    h("table", {}, Object.entries(v.checks).map(([name, status]) => h("tr", {},
      h("td", { class: "n" }, name), h("td", {}, chip(status)), h("td", { class: "d" }, (v.details || {})[name] || "")))));
}

function renderTamper(run) {
  const t = run.reports.tamper, box = $("tamper");
  if (!t) { box.className = "kv muted"; clear(box, "No tamper-test report for this run."); return; }
  box.className = "kv";
  // After deliberate tampering, FAIL is the correct (expected) verifier result.
  const after = (status) => (t.tamper_detected && status === "FAIL" ? chip("PASS", "FAIL (expected)") : chip(status));
  clear(box, [["Tamper detected", chip(t.tamper_detected ? "PASS" : "FAIL", t.tamper_detected ? "DETECTED" : "NOT DETECTED")],
    ["Verification after tampering", after(t.verification_status_after_tampering)],
    ["Artifact digest check", after(t.artifact_digest_match)], ["Provenance artifact check", after(t.provenance_artifact_match)],
    ["Method", t.test_method]].flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}

function tabs(container, items, selected, onPick) {
  clear(container, items.map(([key, label]) => h("button", { type: "button", role: "tab", "aria-selected": String(key === selected), onclick: () => onPick(key) }, label)));
}

function renderJson(run) {
  const items = [["provenance", "Provenance"], ["evidence", "Build evidence"], ["verification", "Verification"], ["assurance", "Assurance"], ["build_status", "Build status"]]
    .filter(([k]) => run.reports[k]);
  if (!items.length) { clear($("jsonTabs")); $("jsonView").textContent = "No reports for this run."; return; }
  if (!items.some(([k]) => k === jsonTab)) jsonTab = items[0][0];
  tabs($("jsonTabs"), items, jsonTab, (k) => { jsonTab = k; renderJson(run); });
  $("jsonView").textContent = JSON.stringify(run.reports[jsonTab], null, 2);
}

function renderDownloads(run) {
  const box = $("downloads");
  if (!run.downloads.length) { box.className = "downloads muted"; clear(box, "Nothing to download yet."); return; }
  box.className = "downloads";
  clear(box, run.downloads.map((d) => h("a", { href: "/api/runs/" + run.run_id + "/files/" + d.key, download: "" }, d.filename)));
}

async function renderLogs(run) {
  const items = [["tests", "Test output"], ...run.logs.map((n) => [n, n])];
  tabs($("logTabs"), items, logTab, (k) => { logTab = k; renderLogs(run); });
  const view = $("logView");
  if (logTab === "tests") { view.textContent = run.test_output || "No test output captured."; return; }
  const key = run.run_id + ":" + logTab;
  if (logCache[key] === undefined) {
    let text;
    try { text = await (await api("/api/runs/" + run.run_id + "/logs/" + logTab)).text(); } catch (e) { text = "Could not load log: " + e.message; }
    // Only cache logs of finished runs; a running stage's log still grows.
    if (!["queued", "running"].includes(run.status)) logCache[key] = text;
    view.textContent = text;
    return;
  }
  view.textContent = logCache[key];
}

async function loadHistory() {
  const box = $("history");
  try {
    const { runs } = await (await api("/api/runs")).json();
    if (!runs.length) { box.className = "history muted"; clear(box, "No runs yet."); return; }
    box.className = "history";
    clear(box, runs.map((r) => h("button", { type: "button", "aria-current": String(current && current.run_id === r.run_id), onclick: () => { logCache = {}; logTab = "tests"; openRun(r.run_id); } },
      h("span", {}, h("strong", {}, r.project), " ", h("span", { class: "t" }, r.run_id)),
      chip(r.decision || r.status, r.decision || STATUS_TEXT[r.status] || r.status),
      h("span", { class: "t" }, when(r.created_at)))));
  } catch (e) { showNotice("Could not load history: " + e.message); }
}

$("runBtn").addEventListener("click", startRun);
(async () => {
  await loadProjects();
  await loadHistory();
  try {
    const { runs } = await (await api("/api/runs")).json();
    if (runs.length) await openRun(runs[0].run_id);
  } catch (e) { /* history already reports errors */ }
})();
