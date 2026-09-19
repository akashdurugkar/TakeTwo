(() => {
  const editPlan = globalThis.EditPlan || (typeof require === "function" ? require("./edit-plan.js") : null);
  const ink = { text: "#202b30", muted: "#5f7076", teal: "#146b70", pale: "#eef5f4", coral: "#b7493e", warning: "#fcf0eb", line: "#d8e3e1" };
  const labels = {
    overall_score: "Editorial score (out of 100)", hook_strength: "Hook strength (out of 10)",
    call_to_action: "Current call to action", cta_suggestions: "Calls to action",
    ranked_fixes: "Editing priorities", chosen_hook: "Recommended opening",
    chosen_hook_reason: "Why this opening", copywriting: "Director-reviewed copy",
    copywriting_draft: "Original Copywriter draft", critique: "Original Critic assessment",
    music: "Original Music Curator draft", review: "Director AI review",
    claims_to_confirm: "Needs your confirmation", brief: "Creative direction",
    timeline: "Suggested timeline", bpm_range: "BPM range", do_not: "Avoid",
  };

  function label(key) {
    return labels[key] || key.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase());
  }

  function present(value) {
    return value !== null && value !== undefined && value !== "" &&
      (!Array.isArray(value) || value.length > 0);
  }

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  }

  function paragraph(text, extra = {}) {
    return { text: String(text), margin: [0, 0, 0, 6], ...extra };
  }

  function heading(text, level = 2) {
    return paragraph(text, { style: level === 1 ? "section" : "heading", headlineLevel: level });
  }

  function divider() {
    return { canvas: [{ type: "line", x1: 0, y1: 0, x2: 499.28, y2: 0, lineWidth: 0.7, lineColor: ink.line }], margin: [0, 8, 0, 12] };
  }

  function chapter(number, title, subtitle, pageBreak = false) {
    return {
      stack: [
        { columns: [{ text: number, width: 32, style: "chapterNumber" }, { text: title, style: "section" }], columnGap: 10 },
        ...(subtitle ? [paragraph(subtitle, { style: "muted", margin: [42, 2, 0, 10] })] : []),
        divider(),
      ],
      unbreakable: true, headlineLevel: 1, margin: [0, 8, 0, 4], ...(pageBreak ? { pageBreak: "before" } : {}),
    };
  }

  const tableLayout = {
    hLineWidth: (index, node) => index === 0 || index === node.table.body.length ? 0 : 0.5,
    vLineWidth: () => 0,
    hLineColor: () => ink.line,
    paddingLeft: () => 10, paddingRight: () => 10, paddingTop: () => 8, paddingBottom: () => 8,
  };

  function table(headers, rows, widths) {
    return {
      table: { headerRows: 1, widths, dontBreakRows: false, body: [headers.map(text => ({ text, style: "tableHeader", fillColor: ink.teal })), ...rows] },
      layout: tableLayout, margin: [0, 2, 0, 12],
    };
  }

  function callout(title, nodes, warning = false) {
    return {
      table: { widths: ["*"], body: [[{ stack: [paragraph(title, { style: "eyebrow", color: warning ? ink.coral : ink.teal }), ...nodes], fillColor: warning ? ink.warning : ink.pale }]] },
      layout: { ...tableLayout, hLineWidth: () => 0, paddingLeft: () => 14, paddingRight: () => 14, paddingTop: () => 12, paddingBottom: () => 10 },
      margin: [0, 5, 0, 14],
    };
  }

  function numbered(items) {
    return {
      table: { widths: [24, "*"], body: items.map((text, index) => [
        { text: String(index + 1).padStart(2, "0"), color: ink.teal, bold: true, fontSize: 12 },
        { text: String(text), lineHeight: 1.2 },
      ]) }, layout: tableLayout, margin: [0, 0, 0, 12],
    };
  }

  function stamp(milliseconds) {
    const seconds = Math.max(0, Number(milliseconds || 0)) / 1000;
    return `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
  }

  function markdownContent(text) {
    if (!globalThis.marked) return [paragraph(text)];
    const inline = tokens => tokens.flatMap(token => {
      if (token.type === "html") return [];
      const node = { text: token.tokens ? inline(token.tokens) : token.text || "" };
      if (token.type === "strong") node.bold = true;
      if (token.type === "em") node.italics = true;
      if (token.type === "br") node.text = "\n";
      if (token.type === "link") {
        const url = safeUrl(token.href);
        if (url) { node.link = url; node.color = "#185f7a"; }
      }
      return [node];
    });
    const blocks = tokens => tokens.flatMap(token => {
      if (["space", "html", "hr"].includes(token.type)) return [];
      if (token.type === "list") return [{ [token.ordered ? "ol" : "ul"]: token.items.map(item => ({ stack: blocks(item.tokens) })), margin: [0, 0, 0, 8] }];
      if (token.type === "blockquote") return [{ stack: blocks(token.tokens), margin: [12, 0, 0, 8], color: "#56616d" }];
      if (token.type === "table") return [token.header, ...token.rows].map(row => ({ text: row.flatMap((cell, index) => [...(index ? [{ text: " | " }] : []), ...inline(cell.tokens)]), margin: [0, 0, 0, 6] }));
      return [{ text: inline(token.tokens || [{ text: token.text || "" }]), margin: [0, 0, 0, 6], ...(token.type === "heading" ? { style: "heading" } : {}) }];
    });
    return blocks(globalThis.marked.lexer(text));
  }

  function fields(value, depth = 0) {
    const nodes = [];
    for (const [key, entry] of Object.entries(value || {})) {
      if (!present(entry)) continue;
      if (typeof entry !== "object") {
        const display = key.endsWith("_ms") ? `${(Number(entry) / 1000).toFixed(1)} s` :
          typeof entry === "boolean" ? (entry ? "Yes" : "No") : String(entry);
        nodes.push({ text: [{ text: `${label(key.replace(/_ms$/, ""))}: `, bold: true, color: ink.muted }, display], margin: [Math.min(depth, 3) * 8, 0, 0, 7] });
      } else {
        nodes.push(heading(label(key)));
        if (Array.isArray(entry)) {
          entry.forEach((item, index) => {
            if (typeof item === "object" && item !== null) {
              nodes.push(paragraph(String(index + 1).padStart(2, "0"), { style: "eyebrow", color: ink.teal, margin: [0, 8, 0, 4] }));
              nodes.push(...fields(item, depth + 1));
            } else {
              nodes.push(paragraph(`${index + 1}. ${item}`, { margin: [depth * 8, 0, 0, 6] }));
            }
          });
        } else nodes.push(...fields(entry, depth + 1));
      }
    }
    return nodes;
  }

  function planContent(plan) {
    if (!plan) return [paragraph("No improvement plan is available.")];
    const nodes = [];
    for (const key of ["headline", "response", "overall_score", "verdict", "warnings", "ranked_fixes", "chosen_hook", "chosen_hook_reason", "review", "brief", "timeline", "copywriting", "conflicts_resolved"]) {
      if (key === "headline" && plan.headline) nodes.push(paragraph(plan.headline, { style: "recommendation" }));
      else if (key === "warnings" && plan.warnings?.length) nodes.push(callout("Important caveats", plan.warnings.map(text => paragraph(text)), true));
      else if (key === "ranked_fixes" && plan.ranked_fixes?.length) nodes.push(heading("Editing priorities"), numbered(plan.ranked_fixes));
      else if (key === "chosen_hook" && plan.chosen_hook) nodes.push(callout("Recommended opening", [paragraph(plan.chosen_hook, { style: "opening" }), ...(plan.chosen_hook_reason ? [paragraph(plan.chosen_hook_reason, { style: "muted" })] : [])]));
      else if (key === "chosen_hook_reason" && plan.chosen_hook) continue;
      else if (key === "review" && plan.review) {
        nodes.push(heading("Director AI review"), ...fields({ grounding_checked: plan.review.grounding_checked, request_checked: plan.review.request_checked, reviewed_specialists: plan.review.reviewed_specialists }));
        if (plan.review.notes?.length) nodes.push({ ul: plan.review.notes.map(note => String(note)), margin: [0, 0, 0, 10] });
        if (plan.review.claims_to_confirm?.length) nodes.push(callout("Needs your confirmation", plan.review.claims_to_confirm.map(text => paragraph(text)), true));
      }
      else if (key === "response" && plan.response) nodes.push(heading("Director response"), ...markdownContent(plan.response));
      else if (key === "timeline" && plan.timeline?.length) {
        nodes.push(heading("Shot-by-shot edit plan"));
        for (const [index, entry] of editPlan.timelineRows(plan.timeline).entries()) {
          const details = [
            [{ text: `Original video\n${entry.source}`, fontSize: 9, color: ink.muted }, { stack: [paragraph(entry.beat.show || "Shot not specified", { bold: true }), ...fields({ action: entry.action, timing: entry.timing, duration_adjustment: entry.beat.timing_note, speech_audio: entry.beat.say, on_screen_text: entry.beat.overlay, reason: entry.beat.why })] }],
          ];
          nodes.push(table([`SHOT ${String(index + 1).padStart(2, "0")}`, `New edit: ${entry.position}${entry.duration ? ` (${entry.duration})` : ""}`], details, [116, "*"]));
          if (entry.issues.length) nodes.push(callout("Timing check", entry.issues.map(issue => paragraph(issue)), true));
        }
      } else if (key === "copywriting" && plan.copywriting) {
        const { better_hooks, ...copy } = plan.copywriting;
        const alternatives = editPlan.otherHooks(plan);
        if (alternatives.length) {
          nodes.push(heading(plan.chosen_hook ? "Other opening options" : "Opening options"));
          alternatives.forEach(hook => nodes.push(...fields(hook)));
        }
        if (copy.captions?.length) {
          nodes.push(heading("Captions"));
          copy.captions.forEach((caption, index) => nodes.push(callout(`Caption ${index + 1} / Director-reviewed`, [paragraph(caption.text, { fontSize: 11 }), ...fields({ tone: caption.tone, character_count: caption.character_count })])));
        }
        const { captions, ...remaining } = copy;
        if (Object.values(remaining).some(present)) nodes.push(...fields({ copywriting: remaining }));
      }
      else nodes.push(...fields({ [key]: plan[key] }));
    }
    if (plan.critique || plan.copywriting_draft || plan.music) {
      nodes.push(divider(), heading("Original specialist drafts"));
      nodes.push(callout("Draft material", [paragraph("These are original specialist outputs, not the Director's final copy. AI review is not independent verification.", { style: "muted" })]));
      for (const key of ["critique", "copywriting_draft", "music"]) nodes.push(...fields({ [key]: plan[key] }));
    }
    if (plan.web_research?.length) {
      nodes.push(heading("Web research and sources"));
      nodes.push(paragraph("Search results are dated evidence, not a guarantee of current trends or music licensing rights.", { style: "muted" }));
      for (const research of plan.web_research) {
        nodes.push(paragraph(research.query || "Research query", { bold: true, margin: [0, 8, 0, 4] }));
        nodes.push(paragraph([research.agent, research.status, research.searched_at].filter(Boolean).join(" / "), { style: "muted" }));
        if (research.status === "succeeded") {
          nodes.push(paragraph(research.answer || ""));
          const seen = new Set();
          for (const citation of research.citations || []) {
            const url = safeUrl(citation.url);
            if (!url || seen.has(url)) continue;
            seen.add(url);
            nodes.push(paragraph(`${citation.title || "Source"}\n${url}`, { link: url, color: ink.teal, fontSize: 9, margin: [12, 2, 0, 8] }));
          }
        } else nodes.push(paragraph(research.error || "Research unavailable; current information was not verified."));
      }
    }
    if (plan.agent_runs?.length) {
      nodes.push(heading("Agent activity"));
      nodes.push(table(["Agent", "Status", "Notes"], plan.agent_runs.map(run => [run.name || "", run.status || "", [run.reason, run.error].filter(Boolean).join("\n")]), [104, 58, "*"]));
    }
    return nodes;
  }

  function buildDocument(job, includeChat = false, exportedAt = new Date()) {
    if (!job?.video_summary) throw new Error("This report has no saved video extraction to export.");
    if (!["succeeded", "failed"].includes(job.status)) throw new Error("Wait for the analysis to finish before exporting.");
    const summary = job.video_summary;
    const platform = { general: "No specific platform / Other", instagram_reels: "Instagram Reels", youtube_shorts: "YouTube Shorts", youtube: "YouTube", website: "Website / landing page", tiktok: "TikTok", linkedin: "LinkedIn Video" }[job.target_platform] || job.target_platform || "Not specified";
    const content = [
      { columns: [{ text: "TAKETWO / CREATIVE REPORT", style: "eyebrow" }, { text: exportedAt.toISOString().slice(0, 10), alignment: "right", style: "muted" }], margin: [0, 0, 0, 20] },
      paragraph("TakeTwo", { style: "title" }),
      paragraph("Video analysis & creative direction", { fontSize: 15, color: ink.muted, margin: [0, 0, 0, 18] }),
      paragraph(job.filename || "Video from URL", { bold: true, fontSize: 12 }),
      ...fields({ video_purpose: job.video_purpose || "General review" }),
      ...(job.goal ? [paragraph(job.goal, { fontSize: 11, margin: [0, 0, 0, 12] })] : []),
      table(["DURATION", "RESOLUTION", "PLATFORM"], [[
        { text: summary.duration_ms > 0 ? `${(summary.duration_ms / 1000).toFixed(1)} s` : "Not available", fontSize: 13, bold: true },
        { text: summary.width && summary.height ? `${summary.width} x ${summary.height}` : "Not available", fontSize: 13, bold: true },
        { text: platform, fontSize: 12, bold: true },
      ]], ["*", "*", "*"]),
      paragraph(`Analysis: ${job.status} / Analyzer: ${job.analyzer_id || "Not recorded"}`, { style: "muted" }),
      paragraph(`Exported ${exportedAt.toISOString()}`, { style: "fine" }),
      paragraph("Generated from saved analysis. AI recommendations are suggestions, not verified performance claims. Original media is not included.", { style: "muted" }),
    ];
    if (job.error) content.push(callout("Analysis incomplete", [paragraph(job.error)], true));
    content.push(chapter("01", "Video at a glance", "Extracted observations"));
    content.push(paragraph(summary.summary || "No summary returned.", { fontSize: 11, lineHeight: 1.3, margin: [0, 0, 0, 12] }));
    for (const key of ["hook", "hook_strength", "pacing", "mood", "content_category", "target_audience", "call_to_action", "audio_style", "spoken_topics", "on_screen_text", "brands_products"]) {
      content.push(...fields({ [key]: summary[key] }));
    }
    content.push(chapter("02", "Creative direction", "Original improvement plan", true), ...planContent(job.report));
    if (summary.scenes?.length || summary.transcript?.length) {
      content.push(chapter("03", "Source evidence", "Scenes and transcript from the saved extraction", true));
      if (summary.scenes?.length) {
        content.push(heading("Scene breakdown"));
        content.push(table(["Original video", "Observed scene"], summary.scenes.map(scene => [
          { text: `${stamp(scene.start_ms)} - ${stamp(scene.end_ms)}`, color: ink.teal, bold: true, fontSize: 9 },
          { stack: [paragraph(scene.description || "No description returned."), ...fields({ on_screen_text: scene.on_screen_text, visual_style: scene.visual_style })] },
        ]), [96, "*"]));
      }
      if (summary.transcript?.length) {
        content.push(heading("Transcript"));
        content.push(table(["Original time", "Speech"], summary.transcript.map(line => [
          { text: `${(Number(line.start_ms || 0) / 1000).toFixed(1)} s${line.end_ms ? ` - ${(line.end_ms / 1000).toFixed(1)} s` : ""}`, color: ink.teal, fontSize: 9 },
          { stack: [...(line.speaker ? [paragraph(line.speaker, { bold: true, fontSize: 9 })] : []), paragraph(line.text || "")] },
        ]), [96, "*"]));
      }
    }
    if (includeChat) {
      content.push(chapter(summary.scenes?.length || summary.transcript?.length ? "04" : "03", "Director conversation", "Follow-up discussion and revisions", true));
      content.push(paragraph("Follow-up recommendations are revisions based on the same extraction. They do not replace the original report above.", { style: "muted" }));
      content.push(...fields({ current_conversation_goal: job.director_chat?.goal }));
      const turns = job.director_chat?.turns || [];
      if (!turns.length) content.push(paragraph("No conversation messages yet."));
      turns.forEach((turn, index) => {
        content.push(divider(), heading(`Conversation ${String(index + 1).padStart(2, "0")}`));
        content.push(...fields({ date: turn.created_at, goal: turn.goal, status: turn.status, web_research_enabled: turn.use_web_search }));
        content.push(callout("You", [paragraph(turn.message || `Update goal: ${turn.goal}`)]));
        if (turn.status === "succeeded") {
          content.push(paragraph("Director", { bold: true }), ...markdownContent(turn.answer || "No reply text returned."));
          if (turn.report) content.push(heading("Recommendations and sources for this turn"), ...planContent(turn.report));
        } else content.push(paragraph(turn.status === "running" ? "Reply in progress at export time." : turn.error || "This reply failed."));
      });
    }
    return {
      pageSize: "A4", pageMargins: [48, 60, 48, 52],
      info: { title: `TakeTwo - ${job.filename || "Video report"}`, author: "TakeTwo", subject: "Saved video analysis and recommendations" },
      defaultStyle: { font: "Roboto", fontSize: 10, lineHeight: 1.2, color: ink.text },
      styles: {
        title: { fontSize: 36, bold: true, color: ink.teal, margin: [0, 0, 0, 4] },
        section: { fontSize: 22, bold: true, color: ink.text, margin: [0, 0, 0, 4] },
        chapterNumber: { fontSize: 22, color: ink.coral, bold: true },
        heading: { fontSize: 13, bold: true, color: ink.teal, margin: [0, 16, 0, 8] },
        recommendation: { fontSize: 19, bold: true, lineHeight: 1.15, margin: [0, 2, 0, 12] },
        opening: { fontSize: 16, bold: true, lineHeight: 1.2, margin: [0, 0, 0, 8] },
        eyebrow: { fontSize: 9, bold: true, color: ink.teal, margin: [0, 0, 0, 6] },
        tableHeader: { fontSize: 9, bold: true, color: "#ffffff" },
        muted: { fontSize: 9, color: ink.muted },
        fine: { fontSize: 8, color: ink.muted },
      },
      content,
      header: currentPage => currentPage === 1 ? { text: "" } : { columns: [
        { text: "TAKETWO", bold: true, color: ink.teal },
        { text: "VIDEO ANALYSIS / CREATIVE DIRECTION", alignment: "right", color: ink.muted },
      ], margin: [48, 24, 48, 0], fontSize: 8 },
      footer: (currentPage, pageCount) => ({ stack: [
        { canvas: [{ type: "line", x1: 0, y1: 0, x2: 499.28, y2: 0, lineWidth: 0.5, lineColor: ink.line }] },
        { columns: [{ text: "Saved analysis / AI-assisted recommendations", color: ink.muted }, { text: `${currentPage} / ${pageCount}`, alignment: "right", bold: true, color: ink.teal }], margin: [0, 8, 0, 0] },
      ], margin: [48, 12, 48, 0], fontSize: 8 }),
      pageBreakBefore: (currentNode, nodeContainer) => Boolean(currentNode.headlineLevel) &&
        (currentNode.startPosition?.top > 700 || nodeContainer.getFollowingNodesOnPage().length === 0),
    };
  }

  function filename(job) {
    const stem = (job.filename || "video-report").replace(/\.[^.]+$/, "").replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80) || "video-report";
    return `TakeTwo-${stem}.pdf`;
  }

  function chooseDestination(job) {
    if (typeof globalThis.showSaveFilePicker !== "function") return Promise.resolve(null);
    return globalThis.showSaveFilePicker({
      suggestedName: filename(job),
      types: [{ description: "PDF document", accept: { "application/pdf": [".pdf"] } }],
      excludeAcceptAllOption: true,
    });
  }

  let libraryPromise;
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = () => { script.remove(); reject(new Error("Could not load the PDF library. Refresh and try again.")); };
      document.head.appendChild(script);
    });
  }

  async function download(job, includeChat, isCurrent = () => true, destination = null) {
    if (!libraryPromise) {
      libraryPromise = (async () => {
        await loadScript("/static/vendor/pdfmake.min.js?v=0.3.11");
        await loadScript("/static/vendor/vfs_fonts.js?v=0.3.11");
      })().catch(error => { libraryPromise = null; throw error; });
    }
    await libraryPromise;
    if (!isCurrent()) return false;
    const blob = await globalThis.pdfMake.createPdf(buildDocument(job, includeChat)).getBlob();
    if (!isCurrent()) return false;
    if (destination) {
      const writable = await destination.createWritable();
      try {
        await writable.write(blob);
        await writable.close();
      } catch (error) {
        await writable.abort().catch(() => {});
        throw error;
      }
      return true;
    }
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename(job);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    return true;
  }

  const api = { buildDocument, filename, chooseDestination, download };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.ReportExport = api;
})();