(() => {
  const element = (id) => document.getElementById(id);
  const panel = element("director-chat");
  const form = element("director-chat-form");
  const goal = element("chat-goal");
  const message = element("chat-message");
  const web = element("chat-web-search");
  const error = element("chat-error");
  const status = element("chat-status");
  const turns = element("chat-turns");
  const scroller = element("chat-scroll");
  const reportTab = element("report-tab");
  const chatTab = element("chat-tab");
  const rendered = new Map();
  let jobId = null;
  let activeGoal = "";
  let generation = 0;
  let loadSequence = 0;
  let timer = null;
  let busy = false;
  let ready = false;
  let available = false;
  let pendingRequest = null;
  let turnCount = 0;
  let currentView = "report";
  let openedChat = false;

  function textNode(tag, text, className = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    node.className = className;
    return node;
  }

  function showView(view, scroll = false) {
    if (view === "chat" && !jobId) return;
    currentView = view;
    const chatting = view === "chat";
    element("report-view").classList.toggle("hidden", chatting);
    panel.classList.toggle("hidden", !chatting);
    for (const [tab, selected] of [[reportTab, !chatting], [chatTab, chatting]]) {
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    }
    if (scroll) element("results").scrollIntoView({ behavior: "instant", block: "start" });
    if (chatting) {
      resizeMessage();
      if (!openedChat) scrollLatest();
      else updateLatest();
      openedChat = true;
    }
  }

  function updateControls() {
    const locked = busy || !ready || !jobId;
    for (const id of ["chat-message", "chat-goal", "chat-reextract", "chat-edit-goal"]) element(id).disabled = locked;
    web.disabled = locked || !available;
    element("chat-send").disabled = locked || turnCount >= 20 || !message.value.trim();
    element("chat-update-goal").disabled = locked || turnCount >= 20 || !goal.value.trim() || goal.value.trim() === activeGoal;
    for (const button of element("chat-empty").querySelectorAll("button")) button.disabled = locked;
    for (const button of turns.querySelectorAll(".chat-retry")) button.disabled = locked || turnCount >= 20;
    element("chat-character-count").textContent = `${message.value.length} / 2000`;
    element("chat-web-notice").classList.toggle("hidden", !web.checked || !available);
    element("chat-send").textContent = busy ? "Wait" : "Send";
  }

  function setBusy(value) {
    busy = value;
    updateControls();
    document.dispatchEvent(new CustomEvent("director-chat:busy", { detail: { busy: value, jobId } }));
  }

  function resizeMessage() {
    message.style.height = "auto";
    message.style.height = `${Math.max(65, Math.min(message.scrollHeight, 132))}px`;
    updateControls();
  }

  function nearBottom() {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 90;
  }

  function updateLatest() {
    const button = element("chat-latest");
    button.style.bottom = `${form.offsetHeight + 10}px`;
    button.classList.toggle("hidden", !turns.childNodes.length || nearBottom() || currentView !== "chat");
  }

  function scrollLatest() {
    scroller.scrollTop = scroller.scrollHeight;
    updateLatest();
  }

  function editGoal(open) {
    element("chat-goal-editor").classList.toggle("hidden", !open);
    element("chat-edit-goal").setAttribute("aria-expanded", String(open));
    if (open) {
      goal.value = activeGoal;
      goal.focus();
      goal.select();
    }
    updateControls();
  }

  function disclosure(label, key, turnId, openKeys) {
    const details = document.createElement("details");
    details.dataset.disclosure = `${turnId}:${key}`;
    details.open = openKeys.has(details.dataset.disclosure);
    details.appendChild(textNode("summary", label));
    return details;
  }

  function turnNode(turn, previousGoal, openKeys) {
    const row = textNode("li", "", "chat-turn");
    row.dataset.turn = turn.id;
    if (turn.goal !== previousGoal) row.appendChild(textNode("p", `Goal updated: ${turn.goal}`, "chat-goal-change"));
    const user = textNode("div", "", "chat-user");
    user.appendChild(textNode("p", "You", "chat-speaker"));
    user.appendChild(textNode("p", turn.message, "chat-text"));
    row.appendChild(user);
    const assistant = textNode("div", "", "chat-assistant");
    assistant.appendChild(textNode("p", turn.use_web_search ? "Director · Web research enabled" : "Director", "chat-speaker"));
    if (turn.status === "failed") {
      assistant.appendChild(textNode("p", turn.error || "Couldn't finish this reply. Your saved analysis is unchanged.", "warning"));
      const retry = textNode("button", "Retry reply", "copy-btn chat-retry");
      retry.type = "button";
      retry.addEventListener("click", () => send(turn.message, turn.goal, turn.use_web_search));
      assistant.appendChild(retry);
    } else {
      const answer = textNode("div", "", "chat-text");
      if (turn.answer && window.marked && window.DOMPurify) {
        answer.classList.add("chat-markdown");
        answer.innerHTML = DOMPurify.sanitize(marked.parse(turn.answer, { breaks: true }), {
          ALLOWED_TAGS: ["p", "br", "strong", "em", "ul", "ol", "li", "blockquote", "code", "pre", "a", "h1", "h2", "h3", "h4", "hr", "table", "thead", "tbody", "tr", "th", "td"],
          ALLOWED_ATTR: ["href", "title", "start"],
        });
        for (const link of answer.querySelectorAll("a")) {
          const safe = safeSourceLink({ url: link.getAttribute("href"), title: link.title }, link.textContent);
          if (safe) link.replaceWith(safe);
          else link.replaceWith(document.createTextNode(link.textContent));
        }
      } else {
        answer.textContent = turn.answer || turn.stage_message || "Thinking...";
      }
      if (turn.status === "running") answer.classList.add("chat-pending");
      assistant.appendChild(answer);
    }
    if (turn.report) {
      const details = disclosure("Recommendations & sources", "report", turn.id, openKeys);
      const content = document.createElement("div");
      renderPlan(turn.report, content);
      details.appendChild(content);
      assistant.appendChild(details);
    }
    if (turn.agent_runs?.length) {
      const used = turn.agent_runs.filter(run => run.status === "succeeded").length;
      const details = disclosure(`Agent activity${used ? ` · ${used} completed` : ""}`, "activity", turn.id, openKeys);
      const trace = document.createElement("ol");
      trace.className = "trace";
      renderTrace(trace, turn.agent_runs);
      details.appendChild(trace);
      assistant.appendChild(details);
    }
    row.appendChild(assistant);
    return row;
  }

  function renderConversation(conversation) {
    const follow = nearBottom() || turnCount === 0;
    const openKeys = new Set([...turns.querySelectorAll("details[open]")].map(node => node.dataset.disclosure));
    const incomingIds = new Set(conversation.turns.map(turn => turn.id));
    for (const [id, entry] of rendered) {
      if (!incomingIds.has(id)) {
        entry.node.remove();
        rendered.delete(id);
      }
    }
    turns.querySelector("[data-pending]")?.remove();
    for (const [index, turn] of conversation.turns.entries()) {
      const signature = JSON.stringify(turn);
      const old = rendered.get(turn.id);
      if (old?.signature === signature) continue;
      const previousGoal = index ? conversation.turns[index - 1].goal : turn.goal;
      const node = turnNode(turn, previousGoal, openKeys);
      if (old) old.node.replaceWith(node);
      else turns.appendChild(node);
      rendered.set(turn.id, { signature, node });
    }
    turnCount = conversation.turns.length;
    const editing = !element("chat-goal-editor").classList.contains("hidden") && !busy;
    activeGoal = conversation.goal;
    element("chat-active-goal").textContent = activeGoal;
    element("chat-active-goal").title = activeGoal;
    if (!editing) goal.value = activeGoal;
    const running = conversation.turns.find(turn => turn.status === "running");
    ready = true;
    setBusy(Boolean(running));
    element("chat-empty").classList.toggle("hidden", turnCount > 0);
    status.textContent = running ? "Director is responding..." : turnCount >= 20 ? "20-turn limit reached" : "";
    if (follow) scrollLatest();
    else updateLatest();
  }

  async function load(version = generation) {
    clearTimeout(timer);
    const sequence = ++loadSequence;
    const id = jobId;
    if (!id) return;
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}/chat`, { cache: "no-store" });
      const payload = await response.json();
      if (version !== generation || sequence !== loadSequence) return;
      if (!response.ok) {
        if (response.status === 404) {
          ready = false;
          setBusy(false);
        }
        throw new Error(typeof payload.detail === "string" ? payload.detail : "Couldn't load the conversation. Use Options to refresh.");
      }
      error.textContent = "";
      renderConversation(payload);
      if (busy) timer = setTimeout(() => load(version), 1500);
    } catch (failure) {
      if (version !== generation || sequence !== loadSequence) return;
      error.textContent = failure.message || "Connection lost. Refresh the conversation from Options.";
      status.textContent = "Connection interrupted";
    }
  }

  async function send(userMessage, nextGoal, useWebSearch) {
    if (busy || !ready || !jobId || turnCount >= 20) return;
    userMessage = userMessage.trim();
    nextGoal = nextGoal.trim();
    if (!nextGoal || (!userMessage && nextGoal === activeGoal)) {
      error.textContent = "Enter a message or change the goal.";
      return;
    }
    const goalUpdate = !userMessage;
    web.checked = available && useWebSearch;
    const body = { message: userMessage, goal: nextGoal, use_web_search: available && useWebSearch };
    const signature = JSON.stringify(body);
    if (!pendingRequest || pendingRequest.signature !== signature) pendingRequest = { signature, body: { ...body, request_id: crypto.randomUUID() } };
    const version = generation;
    ++loadSequence;
    clearTimeout(timer);
    setBusy(true);
    error.textContent = "";
    status.textContent = "Sending...";
    element("chat-empty").classList.add("hidden");
    const pending = turnNode({ id: "pending", message: userMessage || `Update recommendations for: ${nextGoal}`, goal: nextGoal, status: "running", stage_message: "Thinking..." }, activeGoal, new Set());
    pending.dataset.pending = "true";
    turns.appendChild(pending);
    scrollLatest();
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pendingRequest.body),
      });
      const payload = await response.json();
      if (version !== generation) return;
      if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "Message rejected. Check the message and goal lengths.");
      pendingRequest = null;
      if (!goalUpdate) message.value = "";
      if (goalUpdate) editGoal(false);
      resizeMessage();
      await load(version);
      if (!busy && currentView === "chat") message.focus({ preventScroll: true });
    } catch (failure) {
      if (version !== generation) return;
      pending.remove();
      element("chat-empty").classList.toggle("hidden", turnCount > 0);
      setBusy(false);
      error.textContent = failure.message || "Couldn't send. Your draft is still here; retry or refresh from Options.";
      status.textContent = "Not sent";
    }
  }

  function reset() {
    ++generation;
    ++loadSequence;
    clearTimeout(timer);
    timer = null;
    ready = false;
    setBusy(false);
    jobId = null;
    activeGoal = "";
    pendingRequest = null;
    turnCount = 0;
    openedChat = false;
    rendered.clear();
    turns.replaceChildren();
    message.value = "";
    message.style.height = "";
    goal.value = "";
    error.textContent = "";
    status.textContent = "";
    element("chat-context").textContent = "";
    element("chat-active-goal").textContent = "";
    element("chat-options").open = false;
    editGoal(false);
    element("chat-empty").classList.remove("hidden");
    element("chat-latest").classList.add("hidden");
    chatTab.disabled = true;
    showView("report");
  }

  window.DirectorChat = {
    open(job, webAvailable) {
      if (!job.video_summary || !job.id || jobId === job.id) return;
      reset();
      jobId = job.id;
      available = webAvailable;
      activeGoal = job.director_chat?.goal || job.goal || "General video review";
      goal.value = activeGoal;
      element("chat-active-goal").textContent = activeGoal;
      web.checked = false;
      chatTab.disabled = false;
      element("chat-context").textContent = job.filename || "Video from URL";
      element("chat-context").title = `${job.filename || "Video URL"} · ${job.analyzer_id || "Saved extraction"}`;
      status.textContent = "Loading conversation...";
      updateControls();
      load();
    },
    reset,
    showView,
  };

  for (const [tab, view] of [[reportTab, "report"], [chatTab, "chat"]]) {
    tab.addEventListener("click", () => showView(view, true));
    tab.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || chatTab.disabled) return;
      event.preventDefault();
      const next = event.key === "Home" ? reportTab : event.key === "End" ? chatTab : tab === reportTab ? chatTab : reportTab;
      showView(next === chatTab ? "chat" : "report");
      next.focus();
    });
  }
  element("discuss-report").addEventListener("click", () => {
    showView("chat", true);
    message.focus({ preventScroll: true });
  });
  form.addEventListener("submit", event => {
    event.preventDefault();
    if (message.value.trim()) send(message.value, activeGoal, web.checked);
  });
  message.addEventListener("input", resizeMessage);
  message.addEventListener("keydown", event => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
    if (matchMedia("(pointer: coarse)").matches && !event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    if (!element("chat-send").disabled) form.requestSubmit();
  });
  for (const starter of element("chat-empty").querySelectorAll("[data-chat-prompt]")) {
    starter.addEventListener("click", () => {
      message.value = starter.dataset.chatPrompt;
      resizeMessage();
      message.focus();
    });
  }
  goal.addEventListener("input", updateControls);
  goal.addEventListener("keydown", event => {
    if (event.key === "Escape") { editGoal(false); element("chat-edit-goal").focus(); }
    if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); element("chat-update-goal").click(); }
  });
  web.addEventListener("change", () => { updateControls(); updateLatest(); });
  element("chat-edit-goal").addEventListener("click", () => editGoal(true));
  element("chat-cancel-goal").addEventListener("click", () => { goal.value = activeGoal; editGoal(false); element("chat-edit-goal").focus(); });
  element("chat-update-goal").addEventListener("click", () => send("", goal.value, web.checked));
  element("chat-refresh").addEventListener("click", () => { element("chat-options").open = false; load(); });
  element("chat-reextract").addEventListener("click", () => {
    element("chat-options").open = false;
    if (!busy) document.dispatchEvent(new CustomEvent("director-chat:reextract", { detail: { jobId, goal: activeGoal } }));
  });
  element("chat-latest").addEventListener("click", scrollLatest);
  scroller.addEventListener("scroll", updateLatest, { passive: true });
  new ResizeObserver(updateLatest).observe(form);
  element("chat-options").addEventListener("keydown", event => {
    if (event.key === "Escape") { element("chat-options").open = false; element("chat-options").querySelector("summary").focus(); }
  });
})();