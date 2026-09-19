(() => {
  const actions = { keep: "Keep", trim: "Trim", move: "Move to a new position", reshoot: "Reshoot", new_shot: "Record a new shot" };
  const treatments = { normal: "Normal speed", slow_motion: "Slow motion", speed_up: "Speed up", freeze_frame: "Freeze frame", loop: "Repeat / loop" };

  function clock(milliseconds) {
    const seconds = milliseconds / 1000;
    return `${Math.floor(seconds / 60)}:${(seconds % 60).toFixed(3).replace(/\.?0+$/, "").padStart(2, "0").replace(/^(\d)\./, "0$1.")}`;
  }

  function parseClock(value) {
    const match = /^(\d+):([0-5]\d)(?:\.(\d{1,3}))?$/.exec(String(value || ""));
    return match ? Number(match[1]) * 60 + Number(match[2]) + Number(`0.${match[3] || "0"}`) : null;
  }

  function duration(seconds) {
    return `${Number(seconds.toFixed(3))}s`;
  }

  function timelineRows(beats) {
    let previousEnd = 0;
    return (beats || []).map(beat => {
      const start = parseClock(beat.start);
      const end = parseClock(beat.end);
      const outputValid = start !== null && end !== null && end > start;
      const sourceValid = Number.isInteger(beat.source_start_ms) && Number.isInteger(beat.source_end_ms) && beat.source_start_ms >= 0 && beat.source_end_ms > beat.source_start_ms;
      const newFootage = ["reshoot", "new_shot"].includes(beat.action);
      const issues = [];
      if (!outputValid) issues.push("Proposed edit range is invalid; confirm the timing.");
      if (outputValid && previousEnd !== null && Math.abs(start - previousEnd) > 0.001) issues.push("This beat leaves a gap or overlap in the proposed edit.");
      previousEnd = outputValid ? end : null;
      if (!sourceValid && (beat.source_start_ms != null || beat.source_end_ms != null)) issues.push("Original video range is incomplete or invalid.");
      if (sourceValid && beat.action === "new_shot") issues.push("A new shot should not be labelled as footage from the original video.");
      if (sourceValid && outputValid && !newFootage) {
        const sourceDuration = (beat.source_end_ms - beat.source_start_ms) / 1000;
        const outputDuration = end - start;
        if (Math.abs(sourceDuration - outputDuration) > 0.01 && (!beat.timing || ["normal", "unspecified"].includes(beat.timing) || !beat.timing_note?.trim())) {
          issues.push(`Timing needs confirmation: ${duration(sourceDuration)} of source footage is assigned ${duration(outputDuration)} in the new edit without an explicit duration adjustment.`);
        }
      }
      return {
        beat,
        position: `${beat.start || "?"} - ${beat.end || "?"}`,
        duration: outputValid ? duration(end - start) : "",
        source: sourceValid ? `${clock(beat.source_start_ms)} - ${clock(beat.source_end_ms)} (approx.)` : newFootage ? "New footage required" : "Not specified",
        action: actions[beat.action] || "Not specified",
        timing: treatments[beat.timing] || "Not specified",
        issues,
      };
    });
  }

  function hookKey(text) {
    return String(text || "").normalize("NFKC").trim().replace(/[\u2018\u2019]/g, "'")
      .replace(/^[\s"'\u201c\u201d]+|[\s"'\u201c\u201d]+$/g, "").replace(/[.!?]+$/, "").replace(/\s+/g, " ").toLowerCase();
  }

  function otherHooks(plan) {
    const seen = new Set([hookKey(plan.chosen_hook)]);
    return (plan.copywriting?.better_hooks || []).filter(hook => {
      const key = hookKey(hook.text);
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  const api = { timelineRows, otherHooks };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.EditPlan = api;
})();