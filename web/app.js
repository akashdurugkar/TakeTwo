const $ = (id) => document.getElementById(id);

const form = $("analyze-form");
const fileInput = $("file");
const dropzone = $("dropzone");
const dropzoneLabel = $("dropzone-label");
const urlInput = $("video-url");
const platformSelect = $("platform");
const purposeSelect = $("video-purpose");
const customPurposeInput = $("custom-purpose");
const goalInput = $("goal");
const webSearchInput = $("web-search");
const submitBtn = $("submit");
const clearJobBtn = $("clear-job");
const cleanupMessage = $("cleanup-message");
const formError = $("form-error");
const statusCard = $("status-card");
const statusTitle = $("status-title");
const statusMessage = $("status-message");
const results = $("results");

const STATUS_LABEL = {
  queued: "Queued…",
  analyzing: "Content Understanding is analyzing the video…",
  briefing: "Creative Director is assessing your request...",
  advising: "Requested specialists are working...",
  synthesizing: "Preparing your response...",
};

let pollTimer = null;
let pollVersion = 0;
let currentJobId = null;
let currentJobStatus = null;
let currentJobFilename = "";
let webSearchAvailable = false;
let chatBusy = false;
let hasSavedExtraction = false;
let exportBusy = false;
let blobUploadEnabled = false;
let maxDirectUploadBytes = 200 * 1024 * 1024;
let maxBlobUploadBytes = 0;
let uploadBusy = false;
let uploadController = null;
let cancelingUpload = false;
let reviewContextAvailable = false;
let savedReviewContext = null;

function rememberJob(jobId) {
  try {
    if (jobId) sessionStorage.setItem("taketwo.currentJob", jobId);
    else sessionStorage.removeItem("taketwo.currentJob");
  } catch {}
}

document.addEventListener("director-chat:busy", (event) => {
  if (event.detail.jobId && event.detail.jobId !== currentJobId) return;
  chatBusy = event.detail.busy;
  submitBtn.disabled = uploadBusy || chatBusy || Boolean(currentJobId && !["succeeded", "failed", "missing"].includes(currentJobStatus));
  updateClearButton();
});

document.addEventListener("director-chat:reextract", (event) => {
  if (event.detail.jobId !== currentJobId || chatBusy) return;
  if (savedReviewContext) restoreReviewContext(savedReviewContext);
  goalInput.value = event.detail.goal || goalInput.value;
  form.requestSubmit();
});

// ---------------------------------------------------------------- helpers

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function seconds(ms) {
  return (Number(ms || 0) / 1000).toFixed(1) + "s";
}

function updatePurposeInput() {
  const custom = purposeSelect.value === "other";
  $("custom-purpose-field").classList.toggle("hidden", !custom);
  customPurposeInput.required = custom;
  customPurposeInput.disabled = !custom || uploadBusy;
}

function restoreReviewContext(job) {
  const purpose = job.video_purpose || "General review";
  const preset = [...purposeSelect.options].some(option => option.value !== "other" && option.value === purpose);
  purposeSelect.value = preset ? purpose : "other";
  customPurposeInput.value = preset ? "" : purpose;
  platformSelect.value = job.target_platform || "general";
  updatePurposeInput();
}

purposeSelect.addEventListener("change", () => {
  updatePurposeInput();
  if (purposeSelect.value === "other") customPurposeInput.focus();
});

// ---------------------------------------------------------------- bootstrap

async function bootstrap() {
  try {
    const [health, platforms] = await Promise.all([
      fetch("/api/health").then((r) => r.json()),
      fetch("/api/platforms").then((r) => r.json()),
    ]);

    for (const [key, name] of Object.entries(platforms)) {
      const option = el("option", null, name);
      option.value = key;
      platformSelect.appendChild(option);
    }
    reviewContextAvailable = Object.hasOwn(platforms, "general");
    if (reviewContextAvailable) platformSelect.value = "general";

    const missing = [];
    webSearchAvailable = health.web_search_enabled === true;
    blobUploadEnabled = health.blob_upload_enabled === true;
    maxDirectUploadBytes = health.max_upload_mb * 1024 * 1024;
    maxBlobUploadBytes = health.max_blob_upload_bytes || 0;
    webSearchInput.disabled = !webSearchAvailable;
    webSearchInput.checked = webSearchAvailable;
    if (!health.content_understanding_configured) missing.push("Content Understanding (CU_ENDPOINT / CU_KEY)");
    if (!health.agent_configured) missing.push("Azure OpenAI (AOAI_ENDPOINT / AOAI_KEY / AOAI_DEPLOYMENT)");

    const healthEl = $("health");
    if (missing.length) {
      healthEl.appendChild(el("span", "bad", `Not configured: ${missing.join(" · ")}. Fill in .env and restart.`));
    } else {
      healthEl.textContent = `Ready · TakeTwo · uploads up to ${blobUploadEnabled ? `${(maxBlobUploadBytes / 1e9).toFixed(1)} GB` : `${health.max_upload_mb} MB`}`;
    }
    try {
      const savedJob = sessionStorage.getItem("taketwo.currentJob");
      if (savedJob) poll(savedJob);
    } catch {}
  } catch (err) {
    $("health").textContent = "Could not reach the API.";
  }
}

// ---------------------------------------------------------------- upload UI

["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("drag");
  })
);

["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag");
  })
);

dropzone.addEventListener("drop", (e) => {
  if (!fileInput.disabled && e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    showFileName();
  }
});

fileInput.addEventListener("change", showFileName);

function showFileName() {
  const file = fileInput.files[0];
  dropzoneLabel.textContent = file
    ? `${file.name} · ${(file.size / 1048576).toFixed(1)} MB`
    : "Drop a video here, or click to browse";
  if (file) urlInput.value = "";
}

// ---------------------------------------------------------------- submit

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (submitBtn.disabled) return;
  formError.textContent = "";
  cleanupMessage.textContent = "";

  if (!reviewContextAvailable) {
    formError.textContent = "This server is running an older version. Restart this app instance to enable video purpose and platform-neutral reviews.";
    return;
  }
  const videoPurpose = purposeSelect.value === "other" ? customPurposeInput.value.trim() : purposeSelect.value;
  if (!videoPurpose) {
    formError.textContent = "Describe the video purpose or choose a preset.";
    customPurposeInput.focus();
    return;
  }
  const file = fileInput.files[0];
  const url = urlInput.value.trim();
  if (!file && !url) {
    formError.textContent = "Choose a video file or paste a direct video URL. Chat can continue without re-uploading.";
    return;
  }
  if (hasSavedExtraction && !confirm("Analyze the selected video again? This creates a NEW extraction and incurs Azure analysis charges. To discuss results or change the goal without extraction, use Director chat below.")) return;

  const useBlob = file && file.size > maxDirectUploadBytes;
  if (useBlob && (!blobUploadEnabled || file.size > maxBlobUploadBytes)) {
    formError.textContent = blobUploadEnabled ? `File exceeds the ${(maxBlobUploadBytes / 1e9).toFixed(1)} GB limit.` : "This file needs Blob upload, which is not configured on this server.";
    return;
  }

  const body = new FormData();
  if (file) body.append("file", file);
  body.append("video_url", url);
  body.append("target_platform", platformSelect.value);
  body.append("video_purpose", videoPurpose);
  body.append("goal", goalInput.value);
  body.append("use_web_search", String(webSearchAvailable && webSearchInput.checked));

  submitBtn.disabled = true;
  clearJobBtn.disabled = true;
  results.classList.add("hidden");
  statusCard.classList.remove("hidden");
  statusTitle.textContent = "Uploading…";
  statusMessage.textContent = "";
  clear($("trace"));

  try {
    let payload;
    uploadBusy = true;
    for (const control of [fileInput, urlInput, platformSelect, purposeSelect, customPurposeInput, goalInput, webSearchInput]) control.disabled = true;
    updateClearButton();
    if (useBlob) {
      uploadController = new AbortController();
      $("cancel-upload").classList.remove("hidden");
      $("upload-progress").classList.remove("hidden");
      payload = await window.BlobUpload.start(file, {
        target_platform: platformSelect.value, video_purpose: videoPurpose, goal: goalInput.value, use_web_search: webSearchAvailable && webSearchInput.checked,
      }, {
        signal: uploadController.signal,
        progress: (sent, total) => {
          statusTitle.textContent = "Uploading to private Blob storage...";
          statusMessage.textContent = `${(sent / 1048576).toFixed(0)} / ${(total / 1048576).toFixed(0)} MB`;
          $("upload-progress").value = sent / total;
        },
        stage: (text, canCancel) => { statusTitle.textContent = text; $("cancel-upload").disabled = !canCancel; },
      });
    } else {
      if (window.BlobUpload.hasPending()) await window.BlobUpload.cancel();
      const response = await fetch("/api/analyze", { method: "POST", body });
      payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Request failed.");
    }
    window.DirectorChat.reset();
    hasSavedExtraction = false;
    poll(payload.job_id);
  } catch (err) {
    submitBtn.disabled = false;
    clearJobBtn.disabled = false;
    statusCard.classList.add("hidden");
    if (hasSavedExtraction) results.classList.remove("hidden");
    formError.textContent = err.name === "AbortError" ? "Upload canceled." : err.message;
  } finally {
    uploadBusy = false;
    for (const control of [fileInput, urlInput, platformSelect, purposeSelect, goalInput]) control.disabled = false;
    updatePurposeInput();
    webSearchInput.disabled = !webSearchAvailable;
    uploadController = null;
    $("cancel-upload").classList.add("hidden");
    $("upload-progress").classList.add("hidden");
    submitBtn.disabled = cancelingUpload || chatBusy || Boolean(currentJobId && !["succeeded", "failed", "missing"].includes(currentJobStatus));
    updateClearButton();
  }
});

$("cancel-upload").addEventListener("click", async () => {
  cancelingUpload = true;
  $("cancel-upload").disabled = true;
  uploadController?.abort();
  try {
    await window.BlobUpload.cancel();
  } catch (error) {
    formError.textContent = error.message;
  } finally {
    cancelingUpload = false;
    if (!uploadBusy) submitBtn.disabled = chatBusy;
  }
});

function poll(jobId) {
  clearTimeout(pollTimer);
  const version = ++pollVersion;
  currentJobId = jobId;
  currentJobStatus = "queued";
  submitBtn.disabled = true;
  rememberJob(jobId);
  updateClearButton();
  const tick = async () => {
    try {
      const response = await fetch(`/api/jobs/${jobId}`);
      if (version !== pollVersion) return;
      if (!response.ok) {
        if (response.status === 404) {
          currentJobStatus = "missing";
          rememberJob(null);
          window.DirectorChat.reset();
        }
        throw new Error(response.status === 404 ? "Job not found." : "Could not read job status.");
      }
      const job = await response.json();
      if (version !== pollVersion) return;
      if (job.blob_upload_active && ["succeeded", "failed"].includes(job.status)) {
        currentJobStatus = "synthesizing";
        updateClearButton();
        statusCard.classList.remove("hidden");
        statusTitle.textContent = "Cleaning up temporary upload...";
        statusMessage.textContent = "";
        pollTimer = setTimeout(tick, 2000);
        return;
      }
      currentJobStatus = job.status;
      updateClearButton();

      statusTitle.textContent = STATUS_LABEL[job.status] || job.status;
      statusMessage.textContent = job.stage_message || "";
      renderTrace($("trace"), job.agent_runs);

      if (job.status === "succeeded") {
        statusCard.classList.add("hidden");
        submitBtn.disabled = false;
        render(job);
        return;
      }
      if (job.status === "failed") {
        statusCard.classList.add("hidden");
        submitBtn.disabled = false;
        formError.textContent = job.error || "Analysis failed.";
        if (job.video_summary) render(job);
        return;
      }
      pollTimer = setTimeout(tick, 2000);
    } catch (err) {
      if (version !== pollVersion) return;
      submitBtn.disabled = false;
      statusCard.classList.add("hidden");
      formError.textContent = err.message;
      updateClearButton();
    }
  };
  tick();
}

function updateClearButton() {
  const finished = currentJobId && ["succeeded", "failed", "missing"].includes(currentJobStatus);
  clearJobBtn.classList.toggle("hidden", !finished);
  clearJobBtn.disabled = !finished || chatBusy || uploadBusy;
  const canExport = currentJobId && ["succeeded", "failed"].includes(currentJobStatus) && !chatBusy && !uploadBusy && !exportBusy;
  $("export-pdf").disabled = !canExport;
  $("export-chat").disabled = !canExport;
}

$("export-pdf").addEventListener("click", async () => {
  if ($("export-pdf").disabled || !currentJobId) return;
  const jobId = currentJobId;
  const version = pollVersion;
  const includeChat = $("export-chat").checked;
  const isCurrent = () => currentJobId === jobId && pollVersion === version;
  exportBusy = true;
  updateClearButton();
  $("export-status").textContent = "Starting PDF export...";
  try {
    const destination = await window.ReportExport.chooseDestination({ filename: currentJobFilename });
    if (!isCurrent()) return;
    $("export-status").textContent = "Preparing PDF...";
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    if (!isCurrent()) return;
    if (!response.ok) throw new Error(response.status === 404 ? "This report is no longer available. It may have been deleted or the server restarted." : "Could not load the saved report. Please retry.");
    const job = await response.json();
    if (!isCurrent()) return;
    const downloaded = await window.ReportExport.download(job, includeChat, isCurrent, destination);
    if (downloaded && isCurrent()) $("export-status").textContent = destination ? "PDF saved." : "PDF download started.";
  } catch (error) {
    if (isCurrent()) $("export-status").textContent = error.name === "AbortError" ? "PDF export canceled." : error.message || "Could not create the PDF. Please retry.";
  } finally {
    exportBusy = false;
    updateClearButton();
  }
});

clearJobBtn.addEventListener("click", async () => {
  if (clearJobBtn.disabled || !currentJobId) return;
  if (!confirm("Delete this local report and Director conversation, remove any remaining TakeTwo-staged upload, and clear the video selection? Your local original, externally supplied Blob URLs, and provider-side records will NOT be deleted. Storage recovery retention may apply.")) return;

  const jobId = currentJobId;
  const controls = [fileInput, urlInput, goalInput, platformSelect, purposeSelect, customPurposeInput, webSearchInput, submitBtn, clearJobBtn];
  controls.forEach((control) => { control.disabled = true; });
  formError.textContent = "";
  cleanupMessage.textContent = "Deleting local report...";
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
    if (!response.ok && response.status !== 404) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "Could not delete the local report. Please retry.");
    }
    ++pollVersion;
    clearTimeout(pollTimer);
    pollTimer = null;
    currentJobId = null;
    currentJobStatus = null;
    savedReviewContext = null;
    hasSavedExtraction = false;
    $("export-status").textContent = "";
    $("export-chat").checked = false;
    rememberJob(null);
    window.DirectorChat.reset();
    fileInput.value = "";
    urlInput.value = "";
    goalInput.value = "General video review";
    restoreReviewContext({ target_platform: "general", video_purpose: "General review" });
    showFileName();
    dropzone.classList.remove("drag");
    results.classList.add("hidden");
    statusCard.classList.add("hidden");
    for (const id of ["trace", "trace-done", "facts", "summary-text", "scenes", "transcript", "report-sections", "warnings", "analyzer-id", "score", "headline", "verdict", "report-goal", "report-context", "status-title", "status-message"]) {
      clear($(id));
    }
    for (const id of ["scenes-details", "transcript-details"]) {
      $(id).open = false;
      $(id).classList.add("hidden");
    }
    cleanupMessage.textContent = "Local report and any remaining TakeTwo-staged upload deleted. Local original and external source files unchanged; storage recovery retention may apply.";
  } catch (err) {
    cleanupMessage.textContent = "";
    formError.textContent = err.message || "Could not delete the local report. Please retry.";
  } finally {
    controls.forEach((control) => { control.disabled = false; });
    updatePurposeInput();
    webSearchInput.disabled = !webSearchAvailable;
    updateClearButton();
    if (!currentJobId) urlInput.focus();
  }
});

// ---------------------------------------------------------------- agent trace

function renderTrace(root, runs) {
  clear(root);
  (runs || []).forEach((run) => {
    const li = el("li", run.status);
    li.appendChild(el("span", "dot"));
    li.appendChild(el("span", "agent-name", run.name));
    const detail = el("div", "agent-detail");
    detail.appendChild(el("span", "agent-role", run.role));
    if (run.reason) detail.appendChild(el("span", "agent-reason", run.reason));
    if (run.error) detail.appendChild(el("span", "agent-error", run.error));
    if (run.task) detail.title = run.task;
    li.appendChild(detail);
    const labels = { pending: "Pending", running: "Running", succeeded: "Done", failed: "Failed", skipped: "Skipped" };
    const duration = run.duration_ms ? ` (${(run.duration_ms / 1000).toFixed(1)}s)` : "";
    li.appendChild(el("span", "agent-time", `${labels[run.status] || run.status}${duration}`));
    root.appendChild(li);
  });
}

// ---------------------------------------------------------------- rendering

function render(job) {
  hasSavedExtraction = Boolean(job.video_summary);
  currentJobFilename = job.filename || "";
  savedReviewContext = { target_platform: job.target_platform, video_purpose: job.video_purpose };
  restoreReviewContext(savedReviewContext);
  $("export-status").textContent = "";
  renderSummary(job);
  $("report-goal").textContent = `Goal: ${job.goal?.trim() || "General video review"}`;
  const platformLabel = [...platformSelect.options].find(option => option.value === job.target_platform)?.textContent || job.target_platform || "No specific platform / Other";
  $("report-context").textContent = `Purpose: ${job.video_purpose || "General review"} · Platform: ${platformLabel}`;
  renderTrace($("trace-done"), job.agent_runs);
  renderPlan(job.report);
  if (job.blob_cleanup_pending) $("warnings").appendChild(el("div", "warning", "Temporary upload cleanup failed. Delete the report to retry Blob cleanup."));
  results.classList.remove("hidden");
  $("report-card").classList.toggle("hidden", !job.report);
  window.DirectorChat.open(job, webSearchAvailable);
  results.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderSummary(job) {
  const s = job.video_summary || {};
  $("analyzer-id").textContent = job.analyzer_id || "—";

  const facts = $("facts");
  clear(facts);
  const entries = [
    ["Hook strength", `${s.hook_strength ?? 0}/10`],
    ["Pacing", s.pacing || "—"],
    ["Mood", s.mood || "—"],
    ["Format", s.content_category || "—"],
    ["Duration", seconds(s.duration_ms)],
    ["Resolution", s.width ? `${s.width}×${s.height}` : "—"],
    ["Shot changes", (s.camera_shot_times_ms || []).length],
    ["Audio", s.audio_style || "—"],
  ];
  for (const [label, value] of entries) {
    const fact = el("div", "fact");
    fact.appendChild(el("b", null, label));
    fact.appendChild(document.createTextNode(String(value)));
    facts.appendChild(fact);
  }

  const summaryText = $("summary-text");
  clear(summaryText);
  summaryText.appendChild(el("strong", null, "Summary: "));
  summaryText.appendChild(document.createTextNode(s.summary || "No summary returned."));
  if (s.hook) {
    summaryText.appendChild(el("br"));
    summaryText.appendChild(el("strong", null, "Current hook: "));
    summaryText.appendChild(document.createTextNode(s.hook));
  }
  if (s.call_to_action) {
    summaryText.appendChild(el("br"));
    summaryText.appendChild(el("strong", null, "Current CTA: "));
    summaryText.appendChild(document.createTextNode(s.call_to_action));
  }

  const scenes = $("scenes");
  clear(scenes);
  (s.scenes || []).forEach((scene) => {
    const li = el("li");
    li.appendChild(el("strong", null, `${seconds(scene.start_ms)}–${seconds(scene.end_ms)} `));
    li.appendChild(document.createTextNode(scene.description || ""));
    if (scene.on_screen_text) li.appendChild(el("div", null, `Text: ${scene.on_screen_text}`));
    if (scene.visual_style) li.appendChild(el("div", null, `Style: ${scene.visual_style}`));
    scenes.appendChild(li);
  });
  $("scenes-details").classList.toggle("hidden", !(s.scenes || []).length);

  const transcript = $("transcript");
  clear(transcript);
  (s.transcript || []).forEach((line) => {
    transcript.appendChild(el("li", null, `[${seconds(line.start_ms)}] ${line.speaker}: ${line.text}`));
  });
  $("transcript-details").classList.toggle("hidden", !(s.transcript || []).length);
}

function section(title, byline, nodes) {
  const wrap = document.createDocumentFragment();
  const heading = el("h3", null, title);
  if (byline) heading.appendChild(el("span", "byline", ` · ${byline}`));
  wrap.appendChild(heading);
  nodes.forEach((node) => wrap.appendChild(node));
  return wrap;
}

function itemCard(head, lines, priority) {
  const card = el("div", `item${priority ? " " + priority : ""}`);
  card.appendChild(el("div", "head", head));
  (lines || []).filter(Boolean).forEach((line) => card.appendChild(el("div", "meta", line)));
  return card;
}

function copyButton(text) {
  const btn = el("button", "copy-btn", "Copy");
  btn.type = "button";
  btn.addEventListener("click", () => {
    navigator.clipboard.writeText(text).then(() => {
      btn.textContent = "Copied";
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    });
  });
  return btn;
}

function safeSourceLink(citation, label) {
  try {
    const url = new URL(citation.url);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    const link = el("a", null, label);
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = citation.title || url.hostname;
    return link;
  } catch {
    return null;
  }
}

function renderWebResearch(root, entries) {
  if (!entries?.length) return;
  const nodes = [];
  for (const entry of entries) {
    const block = el("div", "item web-research");
    block.appendChild(el("div", "head", entry.query));
    block.appendChild(el("div", "meta", `${entry.agent} · ${entry.status} · Searched ${entry.searched_at}`));
    if (entry.status !== "succeeded") {
      block.appendChild(el("p", "warning", entry.error || "Current information was not verified."));
    } else {
      const answer = el("p", "research-answer");
      const text = Array.from(entry.answer || "");
      const citations = [...(entry.citations || [])].sort((left, right) => left.start_index - right.start_index);
      let cursor = 0;
      for (const citation of citations) {
        const { start_index: start, end_index: end } = citation;
        if (!Number.isInteger(start) || !Number.isInteger(end) || start < cursor || end <= start || end > text.length) continue;
        const link = safeSourceLink(citation, text.slice(start, end).join(""));
        if (!link) continue;
        answer.appendChild(document.createTextNode(text.slice(cursor, start).join("")));
        answer.appendChild(link);
        cursor = end;
      }
      answer.appendChild(document.createTextNode(text.slice(cursor).join("")));
      block.appendChild(answer);
      const sources = el("ul", "research-sources");
      const seen = new Set();
      for (const citation of citations) {
        if (seen.has(citation.url)) continue;
        const link = safeSourceLink(citation, citation.title || citation.url);
        if (!link) continue;
        seen.add(citation.url);
        const item = el("li");
        item.appendChild(link);
        sources.appendChild(item);
      }
      block.appendChild(sources);
    }
    nodes.push(block);
  }
  root.appendChild(section("Web research", "Sources and freshness", nodes));
}

function renderPlan(plan, target = null) {
  if (!plan) return;

  const root = target || $("report-sections");
  clear(root);
  if (target) {
    if (plan.headline) root.appendChild(el("p", "headline", plan.headline));
    if (plan.overall_score != null) root.appendChild(el("p", "source", `Editorial score: ${plan.overall_score}/100`));
    if (plan.verdict) root.appendChild(el("p", null, plan.verdict));
  } else {
    clear($("warnings"));
    $("score").textContent = plan.overall_score ?? "—";
    $("score").classList.toggle("hidden", plan.overall_score == null);
    $("headline").textContent = plan.headline || (plan.warnings?.length ? "Partial response" : "");
    $("verdict").textContent = plan.verdict || "";
  }
  (plan.warnings || []).forEach((warning) => (target || $("warnings")).appendChild(el("div", "warning", warning)));

  if (plan.ranked_fixes?.length) {
    const list = el("ol", "fix-list");
    plan.ranked_fixes.forEach((fix) => list.appendChild(el("li", null, fix)));
    root.appendChild(section("Editing priorities", null, [list]));
  }

  if (plan.chosen_hook) {
    const card = itemCard(plan.chosen_hook, [plan.chosen_hook_reason], "high");
    card.appendChild(copyButton(plan.chosen_hook));
    root.appendChild(section("Recommended opening", null, [card]));
  }

  if (plan.review) {
    const review = plan.review;
    const status = review.grounding_checked && review.request_checked ? "Checks recorded" : "Incomplete";
    root.appendChild(section("AI review", "Creative Director", [itemCard(status, review.notes)]));
    if (review.claims_to_confirm?.length) {
      const questions = el("ul");
      review.claims_to_confirm.forEach((claim) => questions.appendChild(el("li", null, claim)));
      root.appendChild(section("Needs your confirmation", null, [questions]));
    }
  }

  const drafts = el("details", "specialist-drafts");
  drafts.appendChild(el("summary", null, "Original specialist drafts"));
  const draftRoot = el("div", "draft-content");
  drafts.appendChild(draftRoot);

  const brief = plan.brief;
  if (brief) {
    const card = itemCard(brief.angle, [
      brief.core_promise && `Promise: ${brief.core_promise}`,
      brief.target_emotion && `Target emotion: ${brief.target_emotion}`,
    ]);
    (brief.do_not || []).forEach((d) => card.appendChild(el("div", "meta", `Avoid: ${d}`)));
    root.appendChild(section("Creative direction", "Creative Director", [card]));
  }

  if (plan.timeline?.length) {
    const list = el("div", "timeline");
    EditPlan.timelineRows(plan.timeline).forEach((entry) => {
      const beat = entry.beat;
      const row = el("div", "beat");
      const time = el("div", "time");
      time.appendChild(el("span", "beat-label", "New edit"));
      time.appendChild(el("span", null, entry.position));
      if (entry.duration) time.appendChild(el("span", "beat-duration", entry.duration));
      row.appendChild(time);

      const body = el("div");
      [
        ["Original video", entry.source],
        ["Action", entry.action],
        ["Shot", beat.show],
        ["Timing", entry.timing],
        ["Duration adjustment", beat.timing_note],
        ["Speech / audio", beat.say],
        ["On-screen text", beat.overlay],
      ].forEach(([label, value]) => {
        if (!value || value === "-") return;
        const line = el("div", "line");
        line.appendChild(el("b", null, `${label}: `));
        line.appendChild(document.createTextNode(value));
        body.appendChild(line);
      });
      entry.issues.forEach(issue => body.appendChild(el("div", "warning", issue)));
      if (beat.why) body.appendChild(el("div", "why", beat.why));
      row.appendChild(body);
      list.appendChild(row);
    });
    root.appendChild(section("Shot-by-shot edit plan", null, [list]));
  }

  const critique = plan.critique;
  if (critique) {
    const nodes = [];
    if (critique.strengths?.length || critique.risks?.length) {
      const card = el("div", "item low");
      (critique.strengths || []).forEach((s) => card.appendChild(el("div", "meta", `Working: ${s}`)));
      (critique.risks || []).forEach((r) => card.appendChild(el("div", "meta", `Risk: ${r}`)));
      nodes.push(card);
    }
    (critique.edit_suggestions || []).forEach((e) =>
      nodes.push(itemCard(`${e.timestamp} — ${e.change}`, [e.impact && `Impact: ${e.impact}`], e.priority))
    );
    if (nodes.length) draftRoot.appendChild(section("Original video assessment", "Critic draft", nodes));

    if (critique.cover_text || critique.cover_reason) {
      const card = itemCard(critique.cover_text || "Cover frame", [
        critique.cover_frame_ms != null ? `Frame at ${seconds(critique.cover_frame_ms)}` : "",
        critique.cover_reason,
      ]);
      draftRoot.appendChild(section("Cover frame", "Critic draft", [card]));
    }
  }

  const copy = plan.copywriting;
  if (copy) {
    const alternatives = EditPlan.otherHooks(plan);
    if (alternatives.length) {
      const cards = alternatives.map(hook => {
        const card = itemCard(hook.text, [hook.rationale, hook.delivery && `Delivery: ${hook.delivery}`]);
        card.appendChild(copyButton(hook.text));
        return card;
      });
      if (plan.chosen_hook) {
        const options = el("details", "opening-options");
        options.appendChild(el("summary", null, `Other opening options (${alternatives.length})`));
        cards.forEach(card => options.appendChild(card));
        root.appendChild(options);
      } else root.appendChild(section("Opening options", "Director-reviewed", cards));
    }
    if (copy.captions?.length) {
      root.appendChild(
        section(
          "Captions",
          "Director-reviewed",
          copy.captions.map((c) => {
            const card = itemCard(c.text, [
              [c.tone && `Tone: ${c.tone}`, c.character_count && `${c.character_count} characters`]
                .filter(Boolean)
                .join(" · "),
            ]);
            card.appendChild(copyButton(c.text));
            return card;
          })
        )
      );
    }
    if (copy.overlay_text?.length) {
      root.appendChild(
        section(
          "On-screen text",
          "Director-reviewed",
          copy.overlay_text.map((o) =>
            itemCard(`${o.timestamp} — ${o.text}`, [o.placement && `Placement: ${o.placement}`])
          )
        )
      );
    }
    if (copy.cta_suggestions?.length) {
      root.appendChild(
        section(
          "Call to action",
          "Director-reviewed",
          copy.cta_suggestions.map((c) => {
            const card = itemCard(c, []);
            card.appendChild(copyButton(c));
            return card;
          })
        )
      );
    }
    if (copy.hashtags?.length) {
      const list = el("div", "tag-list");
      copy.hashtags.forEach((tag) => list.appendChild(el("span", "tag", tag)));
      const wrap = el("div");
      wrap.appendChild(list);
      wrap.appendChild(copyButton(copy.hashtags.join(" ")));
      root.appendChild(section("Hashtags", "Director-reviewed", [wrap]));
    }
  }

  const draftCopy = plan.copywriting_draft;
  if (draftCopy) {
    const groups = [
      ["Hook drafts", (draftCopy.better_hooks || []).map((hook) => hook.text)],
      ["Caption drafts", (draftCopy.captions || []).map((caption) => caption.text)],
      ["Overlay drafts", (draftCopy.overlay_text || []).map((overlay) => `${overlay.timestamp}: ${overlay.text}`)],
      ["CTA drafts", draftCopy.cta_suggestions || []],
      ["Hashtag drafts", draftCopy.hashtags || []],
    ];
    for (const [title, entries] of groups) {
      if (!entries.length) continue;
      const list = el("ul");
      entries.forEach((entry) => list.appendChild(el("li", null, entry)));
      draftRoot.appendChild(section(title, "Copywriter draft", [list]));
    }
  }

  const music = plan.music;
  if (music?.music?.length) {
    const nodes = music.music.map((m) =>
      itemCard(m.track_style, [
        m.bpm_range && `BPM ${m.bpm_range}`,
        m.reason,
        m.search_terms?.length && `Search: ${m.search_terms.join(", ")}`,
      ])
    );
    if (music.cut_to_beat) nodes.push(itemCard("Cutting to the beat", [music.cut_to_beat], "low"));
    draftRoot.appendChild(section("Music direction", "Music Curator draft", nodes));
  }

  if (plan.conflicts_resolved?.length) {
    root.appendChild(
      section(
        "Conflicts resolved",
        "Creative Director",
        plan.conflicts_resolved.map((c) => itemCard(c, [], "low"))
      )
    );
  }
  if (draftRoot.childNodes.length) root.appendChild(drafts);
  renderWebResearch(root, plan.web_research);
}

bootstrap();
