const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");

function harness(respond = () => null) {
  const requests = [];
  const context = {
    module: { exports: {} }, URL, AbortSignal, Date, console,
    btoa: value => Buffer.from(value).toString("base64"),
    setTimeout: callback => queueMicrotask(callback),
    document: { implementation: { createDocument: () => ({
      documentElement: { children: [], appendChild(node) { this.children.push(node); } },
      createElement: name => ({ name, textContent: "" }),
    }) } },
    XMLSerializer: class { serializeToString(doc) { return `<BlockList>${doc.documentElement.children.map(node => `<Latest>${node.textContent}</Latest>`).join("")}</BlockList>`; } },
    fetch: async (url, options = {}) => {
      requests.push({ url: String(url), ...options });
      const response = await respond(String(url), options, requests);
      if (response) return response;
      if (url === "/api/uploads") return Response.json({ upload_id: "owned-id", upload_url: "https://account.blob.core.windows.net/taketwo-uploads/staging/owned-id/video?sig=test", block_size: 8 * 1024 * 1024, expires_at: Date.now() / 1000 + 7200 });
      if (String(url).endsWith("/complete")) return Response.json({ job_id: "one-job" }, { status: 202 });
      if (options.method === "DELETE") return new Response(null, { status: 204 });
      return new Response(null, { status: 201 });
    },
  };
  runInNewContext(readFileSync(require.resolve("../web/blob-upload.js"), "utf8"), context);
  return { api: context.module.exports, requests };
}

function file(size = 1024 ** 3) {
  return { size, name: "video.mp4", type: "video/mp4", slice(start, end) { assert.ok(end - start <= 8 * 1024 * 1024); return { size: end - start }; } };
}

function callbacks(controller = new AbortController()) {
  return { signal: controller.signal, progress() {}, stage() {} };
}

test("1 GB uses 128 bounded blocks and one commit and analysis request", async () => {
  const { api, requests } = harness();
  let lastProgress;
  const result = await api.start(file(), { goal: "Review" }, { ...callbacks(), progress: (...values) => { lastProgress = values; } });
  assert.equal(result.job_id, "one-job");
  const blocks = requests.filter(request => request.url.includes("comp=block&"));
  assert.equal(blocks.length, 128);
  assert.equal(new Set(blocks.map(request => new URL(request.url).searchParams.get("blockid"))).size, 128);
  assert.ok(blocks.every(request => request.body.size === 8 * 1024 * 1024));
  assert.equal(requests.filter(request => request.url.includes("comp=blocklist")).length, 1);
  assert.equal(requests.filter(request => request.url.endsWith("/complete")).length, 1);
  assert.deepEqual(lastProgress, [1024 ** 3, 1024 ** 3]);
  assert.equal(api.hasPending(), false);
});

test("transient block retries reuse the same block ID", async () => {
  let attempts = 0;
  const { api, requests } = harness(url => url.includes("comp=block&") && attempts++ === 0 ? new Response(null, { status: 503 }) : null);
  await api.start(file(10), {}, callbacks());
  const blocks = requests.filter(request => request.url.includes("comp=block&"));
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].url, blocks[1].url);
});

test("ambiguous completion retries do not reupload or change the original options", async () => {
  let completions = 0;
  const { api, requests } = harness(url => url.endsWith("/complete") && completions++ === 0 ? Response.json({ detail: "Retry" }, { status: 503 }) : null);
  const selected = file(10);
  await assert.rejects(api.start(selected, { goal: "First goal" }, callbacks()), /Retry/);
  assert.equal(api.hasPending(), true);
  await api.start(selected, { goal: "Changed goal" }, callbacks());
  assert.equal(requests.filter(request => request.url.includes("comp=block&")).length, 1);
  const attempts = requests.filter(request => request.url.endsWith("/complete"));
  assert.equal(attempts.length, 2);
  assert.equal(attempts[0].body, attempts[1].body);
});

test("cancellation during authorization waits for the session then deletes it without uploading", async () => {
  let release;
  const ready = new Promise(resolve => { release = resolve; });
  const { api, requests } = harness(async url => { if (url === "/api/uploads") await ready; });
  const controller = new AbortController();
  const uploading = api.start(file(10), {}, callbacks(controller));
  const rejected = assert.rejects(uploading, { name: "AbortError" });
  controller.abort();
  const canceled = api.cancel();
  release();
  await Promise.all([rejected, canceled]);
  assert.equal(requests.filter(request => request.method === "PUT").length, 0);
  assert.equal(requests.filter(request => request.method === "DELETE").length, 1);
  assert.equal(api.hasPending(), false);
});

test("access errors do not retry or expose a SAS URL in the message", async () => {
  const { api, requests } = harness(url => url.includes("comp=block&") ? new Response(null, { status: 403 }) : null);
  await assert.rejects(api.start(file(10), {}, callbacks()), error => error.message.includes("403") && !error.message.includes("sig="));
  assert.equal(requests.filter(request => request.method === "PUT").length, 1);
  await api.cancel();
  assert.equal(api.hasPending(), false);
});