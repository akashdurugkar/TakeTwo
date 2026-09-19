const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const { buildDocument, filename } = require("../web/report-export.js");
const editPlan = require("../web/edit-plan.js");
globalThis.marked = require("../web/vendor/marked.umd.js");

function fixture() {
  return {
    status: "succeeded", filename: "Fabric study.mp4", goal: "Original goal", target_platform: "linkedin", analyzer_id: "saved-analyzer",
    source_url: "https://example.invalid/private?token=DO-NOT-EXPORT",
    video_summary: { summary: "Fabric swatches", hook_strength: 0, duration_ms: 5000, transcript: [{ start_ms: 1200, text: "Full transcript" }] },
    report: {
      headline: "Original advice", overall_score: 0, ranked_fixes: ["Improve the opening"], warnings: ["Evidence is limited"],
      review: { grounding_checked: false, request_checked: true, claims_to_confirm: ["Verify the material"] },
      copywriting: { captions: [{ text: "Reviewed caption" }] }, copywriting_draft: { captions: [{ text: "Draft caption" }] },
      web_research: [{ query: "Public query", status: "succeeded", searched_at: "2026-09-19", answer: "Sourced finding", citations: [
        { title: "Guide", url: "https://example.com/guide" }, { title: "Repeated", url: "https://example.com/guide" },
        { title: "Unsafe", url: "javascript:alert(1)" }, { title: "Credentials", url: "https://user:secret@example.com" },
      ] }],
    },
    director_chat: { goal: "New goal", turns: [
      { message: "My private question", goal: "New goal", status: "succeeded", answer: "Revised advice", report: { headline: "Revision only" } },
      { message: "Pending question", status: "running", answer: "Incomplete answer" },
      { message: "Failed question", status: "failed", error: "Could not complete" },
    ] },
  };
}

test("report includes complete evidence, warnings, zero scores and clearly labelled drafts", () => {
  const job = fixture();
  const before = JSON.stringify(job);
  const pdf = buildDocument(job, false, new Date("2026-09-19T00:00:00Z"));
  const text = JSON.stringify(pdf);
  for (const value of ["Original goal", "Fabric swatches", "1.2 s", "Full transcript", "Original advice", "Evidence is limited", "Verify the material", "Reviewed caption", "Original Copywriter draft", "Draft caption", "Sourced finding", "2026-09-19"]) assert.ok(text.includes(value), value);
  assert.ok(text.includes('"Editorial score (out of 100): "'));
  assert.ok(text.includes('"0"'));
  assert.ok(text.includes('"No"'));
  for (const excluded of ["My private question", "New goal", "DO-NOT-EXPORT", "javascript:", "user:secret", "Repeated"]) assert.ok(!text.includes(excluded), excluded);
  assert.equal(pdf.content.filter(node => node.link).length, 1);
  assert.equal(pdf.footer(1, 3).stack[1].columns[1].text, "1 / 3");
  assert.equal(JSON.stringify(job), before);
});

test("chat is opt-in and separates successful revisions, pending and failed replies", () => {
  const pdf = buildDocument(fixture(), true);
  const text = JSON.stringify(pdf);
  for (const expected of ["My private question", "New goal", "Revised advice", "Revision only", "Reply in progress", "Could not complete"]) assert.ok(text.includes(expected), expected);
  assert.ok(!text.includes("Incomplete answer"));
  assert.ok(text.indexOf("Original advice") < text.indexOf("Revised advice"));
});

test("partial reports remain exportable, invalid states are rejected and filenames are safe", () => {
  const job = { status: "failed", video_summary: { summary: "Saved extraction" }, error: "Agent unavailable" };
  const text = JSON.stringify(buildDocument(job, true));
  assert.ok(text.includes("Agent unavailable"));
  assert.ok(text.includes("No improvement plan is available"));
  assert.ok(text.includes("No conversation messages yet"));
  assert.throws(() => buildDocument({ status: "succeeded" }), /no saved video extraction/);
  assert.throws(() => buildDocument({ ...job, status: "analyzing" }), /finish before exporting/);
  assert.equal(filename(fixture()), "TakeTwo-Fabric-study.pdf");
  assert.equal(filename({ filename: "../private:name.mp4" }), "TakeTwo-private-name.pdf");
  assert.equal(filename({}), "TakeTwo-video-report.pdf");
});

test("chat Markdown preserves formatting without executable HTML or remote image assets", () => {
  const job = fixture();
  job.director_chat.turns[0].answer = "## Next steps\n\n**Keep the detail.**\n\n1. Trim the pause.\n2. Hold the shot.\n\n[Reference](https://example.com/guide) [Unsafe](javascript:alert(1))\n\n<script>alert(1)</script>\n\n![Frame](https://example.invalid/remote-image.png)";
  const text = JSON.stringify(buildDocument(job, true));
  assert.ok(text.includes('"bold":true'));
  assert.ok(text.includes('"ol":'));
  assert.ok(text.includes('"link":"https://example.com/guide"'));
  for (const excluded of ["javascript:", "<script>", "remote-image.png", '"image":']) assert.ok(!text.includes(excluded), excluded);
});

test("bundled renderer creates a paginated PDF with embedded fonts and source links", async () => {
  const pdfMake = require("../web/vendor/pdfmake.min.js");
  pdfMake.addVirtualFileSystem(require("../web/vendor/vfs_fonts.js"));
  const job = fixture();
  job.report.review.notes = ["Preserve this review note across exports."];
  job.video_summary.transcript = Array.from({ length: 60 }, (_, index) => ({ start_ms: index * 1000, text: "A detailed transcript sentence that should wrap naturally across lines. ".repeat(3) }));
  const before = JSON.stringify(job);
  const buffer = await pdfMake.createPdf(buildDocument(job, true)).getBuffer();
  assert.equal(JSON.stringify(job), before);
  const pdf = buffer.toString("latin1");
  assert.ok(pdf.startsWith("%PDF-"));
  assert.ok(pdf.includes("%%EOF"));
  assert.ok((pdf.match(/\/Type \/Page\b/g) || []).length >= 3);
  assert.ok(pdf.includes("/FontFile2"));
  assert.ok(pdf.includes("/URI (https://example.com/guide)"));
});

function saveHarness(picker) {
  const calls = { pickers: 0, downloads: 0, urls: 0, renders: 0 };
  const blob = new Blob(["test-pdf"]);
  const context = {
    module: { exports: {} },
    EditPlan: editPlan,
    URL: class extends URL {
      static createObjectURL() { calls.urls++; return "blob:test-pdf"; }
      static revokeObjectURL() {}
    },
    setTimeout() {},
    document: {
      createElement: () => ({ remove() {}, click() { calls.downloads++; } }),
      head: { appendChild(script) { queueMicrotask(() => script.onload()); } },
      body: { appendChild() {} },
    },
    pdfMake: { createPdf() { calls.renders++; return { getBlob: async () => blob }; } },
  };
  if (picker) context.showSaveFilePicker = options => { calls.pickers++; return picker(options); };
  runInNewContext(readFileSync(require.resolve("../web/report-export.js"), "utf8"), context);
  return { api: context.module.exports, calls, blob };
}

test("native save opens once, writes once, waits for close and never starts a download", async () => {
  let writes = 0;
  let closes = 0;
  let releaseClose;
  let closeStarted;
  const closing = new Promise(resolve => { closeStarted = resolve; });
  const destination = { createWritable: async () => ({
    write: async value => { writes++; assert.equal(value, harness.blob); },
    close: () => { closes++; closeStarted(); return new Promise(resolve => { releaseClose = resolve; }); },
  }) };
  const harness = saveHarness(options => {
    assert.equal(options.suggestedName, "TakeTwo-Fabric-study.pdf");
    return Promise.resolve(destination);
  });
  const selection = harness.api.chooseDestination(fixture());
  assert.equal(harness.calls.pickers, 1);
  assert.equal(harness.calls.renders, 0);
  let finished = false;
  const saving = harness.api.download(fixture(), false, () => true, await selection).then(result => { finished = true; return result; });
  await closing;
  assert.equal(finished, false);
  releaseClose();
  assert.equal(await saving, true);
  assert.equal(writes, 1);
  assert.equal(closes, 1);
  assert.equal(harness.calls.downloads, 0);
  assert.equal(harness.calls.urls, 0);
});

test("canceling or denying the save picker does not fall back to another dialog", async () => {
  for (const name of ["AbortError", "SecurityError", "NotAllowedError"]) {
    const harness = saveHarness(() => Promise.reject(Object.assign(new Error(name), { name })));
    await assert.rejects(harness.api.chooseDestination(fixture()), { name });
    assert.equal(harness.calls.pickers, 1);
    assert.equal(harness.calls.downloads, 0);
    assert.equal(harness.calls.renders, 0);
  }
});

test("failed writes abort without a download fallback; stale exports do not write", async () => {
  let opened = 0;
  let aborted = 0;
  const destination = { createWritable: async () => {
    opened++;
    return { write: async () => { throw new Error("Disk full"); }, abort: async () => { aborted++; } };
  } };
  const harness = saveHarness(() => Promise.resolve(destination));
  await assert.rejects(harness.api.download(fixture(), false, () => true, destination), /Disk full/);
  assert.equal(aborted, 1);
  assert.equal(harness.calls.downloads, 0);
  assert.equal(await harness.api.download(fixture(), false, () => false, destination), false);
  assert.equal(opened, 1);
});

test("browsers without a native save picker receive exactly one download", async () => {
  const harness = saveHarness();
  const destination = await harness.api.chooseDestination(fixture());
  assert.equal(destination, null);
  assert.equal(await harness.api.download(fixture(), false, () => true, destination), true);
  assert.equal(harness.calls.downloads, 1);
  assert.equal(harness.calls.urls, 1);
  assert.equal(harness.calls.pickers, 0);
});

test("edit plan separates source and destination and flags unexplained stretching", () => {
  const [entry] = editPlan.timelineRows([{ start: "0:00", end: "0:03", source_start_ms: 500, source_end_ms: 1000, action: "trim", timing: "normal" }]);
  assert.equal(entry.position, "0:00 - 0:03");
  assert.equal(entry.source, "0:00.5 - 0:01 (approx.)");
  assert.equal(entry.action, "Trim");
  assert.match(entry.issues[0], /0.5s of source footage is assigned 3s/);
  const [explicit] = editPlan.timelineRows([{ ...entry.beat, timing: "freeze_frame", timing_note: "Play 0.5s then hold the last frame for 2.5s." }]);
  assert.deepEqual(explicit.issues, []);
});

test("legacy plans stay unspecified; new footage and invalid ranges are clearly marked", () => {
  const [legacy] = editPlan.timelineRows([{ start: "0:00", end: "0:03", show: "Shot from 0.5-1.0s" }]);
  assert.equal(legacy.source, "Not specified");
  assert.equal(legacy.action, "Not specified");
  const [newShot] = editPlan.timelineRows([{ start: "0:00", end: "0:03", action: "new_shot" }]);
  assert.equal(newShot.source, "New footage required");
  const [invalid] = editPlan.timelineRows([{ start: "0:01", end: "0:03", source_start_ms: 5000, source_end_ms: 1000 }]);
  assert.ok(invalid.issues.some(issue => issue.includes("gap or overlap")));
  assert.ok(invalid.issues.some(issue => issue.includes("incomplete or invalid")));
  const [zero] = editPlan.timelineRows([{ start: "0:00", end: "0:10", source_start_ms: 0, source_end_ms: 10000, timing: "normal" }]);
  assert.equal(zero.source, "0:00 - 0:10 (approx.)");
  assert.deepEqual(zero.issues, []);
});

test("recommended opening is excluded from alternatives across quotes and whitespace", () => {
  const plan = { chosen_hook: "POV: you chased the wind.", copywriting: { better_hooks: [
    { text: '\u201cPOV: you chased the wind.\u201d' }, { text: "  POV:  you chased the wind " },
    { text: "A different opening" }, { text: "A different opening!" },
  ] } };
  assert.deepEqual(editPlan.otherHooks(plan).map(hook => hook.text), ["A different opening"]);
  assert.equal(editPlan.otherHooks({ ...plan, chosen_hook: "" }).length, 2);
});

test("PDF uses the same edit labels, timing warnings and nonduplicated openings", () => {
  const job = fixture();
  job.report.chosen_hook = "Recommended unique opening";
  job.report.copywriting.better_hooks = [{ text: '\u201cRecommended unique opening\u201d' }, { text: "Alternative unique opening" }];
  job.report.timeline = [{ start: "0:00", end: "0:03", source_start_ms: 500, source_end_ms: 1000, action: "trim", timing: "normal", show: "Windy shot" }];
  const text = JSON.stringify(buildDocument(job));
  for (const expected of ["New edit: 0:00 - 0:03", "Original video", "0:00.5 - 0:01", "Trim", "Timing needs confirmation", "Recommended opening", "Other opening options"]) assert.ok(text.includes(expected), expected);
  assert.equal(text.split("Recommended unique opening").length - 1, 1);
  assert.ok(text.includes("Alternative unique opening"));
});

test("designed report has running furniture, metric strip, explicit chapters and evidence tables", () => {
  const job = fixture();
  job.video_summary.width = 1920;
  job.video_summary.height = 1080;
  job.video_summary.scenes = [{ start_ms: 0, end_ms: 5000, description: "Observed scene", visual_style: "Natural light" }];
  job.report.chosen_hook = "An opening worth using";
  const doc = buildDocument(job);
  const json = JSON.stringify(doc);
  for (const expected of ["Video at a glance", "Original improvement plan", "Source evidence", "DURATION", "RESOLUTION", "1920 x 1080", "PLATFORM", "LinkedIn Video", "Observed scene", "Important caveats", "Needs your confirmation", "Draft material"]) assert.ok(json.includes(expected), expected);
  assert.ok(json.indexOf("Original improvement plan") < json.indexOf("Source evidence"));
  assert.equal(doc.header(1).text, "");
  assert.equal(doc.header(2).columns[0].text, "TAKETWO");
  assert.ok(doc.styles.opening.fontSize > doc.defaultStyle.fontSize);
  assert.equal(doc.pageBreakBefore({ headlineLevel: 2, startPosition: { top: 720 } }, { getFollowingNodesOnPage: () => [{}] }), true);
  assert.equal(doc.pageBreakBefore({ headlineLevel: 2, startPosition: { top: 200 } }, { getFollowingNodesOnPage: () => [{}] }), false);
  assert.equal(doc.pageBreakBefore({ startPosition: { top: 720 } }, { getFollowingNodesOnPage: () => [] }), false);
});

test("long shot descriptions, scene rows and captions can span pages without losing content", async () => {
  const job = fixture();
  const longText = "Long evidence with natural wrapping and enough content to cross a page boundary. ".repeat(150);
  job.video_summary.scenes = [{ start_ms: 0, end_ms: 5000, description: longText + "SCENE_END" }];
  job.report.timeline = [{ start: "0:00", end: "0:05", source_start_ms: 0, source_end_ms: 5000, action: "keep", timing: "normal", show: longText + "SHOT_END" }];
  job.report.copywriting.captions = [{ text: longText + "CAPTION_END" }];
  const doc = buildDocument(job);
  const content = JSON.stringify(doc);
  for (const marker of ["SCENE_END", "SHOT_END", "CAPTION_END"]) assert.ok(content.includes(marker));
  const pdfMake = require("../web/vendor/pdfmake.min.js");
  pdfMake.addVirtualFileSystem(require("../web/vendor/vfs_fonts.js"));
  const pdf = (await pdfMake.createPdf(doc).getBuffer()).toString("latin1");
  assert.ok(pdf.endsWith("%%EOF\n"));
  assert.ok((pdf.match(/\/Type \/Page\b/g) || []).length > 5);
});