(() => {
  let pending = null;
  let active = null;

  async function api(path, options = {}) {
    const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(60000), ...options });
    const data = response.status === 204 ? {} : await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Upload request failed. Please retry.");
    return data;
  }

  function contentType(file) {
    return file.type || ({ mp4: "video/mp4", mov: "video/quicktime", webm: "video/webm", mkv: "video/x-matroska", m4v: "video/x-m4v" })[file.name.split(".").pop().toLowerCase()] || "application/octet-stream";
  }

  async function put(url, body, headers, signal) {
    for (let attempt = 0; attempt < 3; attempt++) {
      signal.throwIfAborted();
      try {
        const response = await fetch(url, {
          method: "PUT", body, headers, credentials: "omit", referrerPolicy: "no-referrer",
          signal: AbortSignal.any([signal, AbortSignal.timeout(120000)]),
        });
        if (response.ok) return;
        if (![408, 429, 500, 502, 503, 504].includes(response.status)) {
          throw Object.assign(new Error(`Blob upload rejected (${response.status}). Check access expiry and storage configuration.`), { permanent: true });
        }
      } catch (error) {
        if (signal.aborted) throw signal.reason;
        if (error.permanent) throw error;
      }
      if (attempt < 2) await new Promise(resolve => setTimeout(resolve, 500 * 2 ** attempt));
    }
    throw new Error("Blob upload interrupted. Check your connection, then retry Analyze to resume.");
  }

  async function cancelPending() {
    if (!pending) return;
    await api(`/api/uploads/${encodeURIComponent(pending.session.upload_id)}`, { method: "DELETE" });
    pending = null;
  }

  async function run(file, options, { signal, progress, stage }) {
    if (pending && (pending.file !== file || pending.session.expires_at * 1000 < Date.now())) await cancelPending();
    if (!pending) {
      stage("Authorizing private Blob upload...", true);
      const session = await api("/api/uploads", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, size: file.size, content_type: contentType(file) }),
      });
      pending = { file, session, offset: 0, blocks: [], committed: false, options: null };
    }
    signal.throwIfAborted();
    const upload = pending;
    const blockSize = upload.session.block_size;
    if (!Number.isInteger(blockSize) || blockSize <= 0 || blockSize > 16 * 1024 * 1024) throw new Error("Invalid upload block size.");
    progress(upload.offset, file.size);
    while (upload.offset < file.size) {
      const blockId = btoa(String(upload.blocks.length).padStart(8, "0"));
      const url = new URL(upload.session.upload_url);
      url.searchParams.set("comp", "block");
      url.searchParams.set("blockid", blockId);
      const end = Math.min(upload.offset + blockSize, file.size);
      await put(url.href, file.slice(upload.offset, end), { "x-ms-version": "2023-11-03", "Content-Type": "application/octet-stream" }, signal);
      upload.blocks.push(blockId);
      upload.offset = end;
      progress(end, file.size);
    }
    if (!upload.committed) {
      stage("Finishing Blob upload...", true);
      const list = document.implementation.createDocument(null, "BlockList");
      for (const blockId of upload.blocks) {
        const block = list.createElement("Latest");
        block.textContent = blockId;
        list.documentElement.appendChild(block);
      }
      const url = new URL(upload.session.upload_url);
      url.searchParams.set("comp", "blocklist");
      await put(url.href, new XMLSerializer().serializeToString(list), {
        "x-ms-version": "2023-11-03", "Content-Type": "application/xml", "x-ms-blob-content-type": contentType(file),
      }, signal);
      upload.committed = true;
    }
    signal.throwIfAborted();
    stage("Starting video analysis...", false);
    upload.options ||= options;
    const result = await api(`/api/uploads/${encodeURIComponent(upload.session.upload_id)}/complete`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(upload.options),
    });
    pending = null;
    return result;
  }

  async function start(...args) {
    if (active) throw new Error("An upload is already in progress.");
    active = run(...args);
    try { return await active; }
    finally { active = null; }
  }

  async function cancel() {
    if (active) await active.catch(() => {});
    await cancelPending();
  }

  const apiObject = { start, cancel, hasPending: () => Boolean(pending) };
  if (typeof module !== "undefined" && module.exports) module.exports = apiObject;
  else window.BlobUpload = apiObject;
})();