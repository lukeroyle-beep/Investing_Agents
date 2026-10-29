/* Host-side relay for an ALREADY STARTED, separately authorized JSONL process.
 * It cannot launch a worker, access credentials or call broker tools.
 * Bind api.readUsage to the genuine Codex app get_usage_limits tool and
 * api.write to the current host's write_stdin tool. Never bind a cache/fixture
 * for operational use. All tool responses remain private; returned events are
 * allowlisted summaries. The worker retains the original complete response.
 */
async function serveCodexUsageRequests(api, initialCell) {
  let cell = initialCell, buffer = "", sequence = 0, calls = 0, idlePolls = 0;
  const summaries = [];
  const safeEvents = new Set(["usage_probe_complete", "submitted_once", "cycle_complete",
    "journal_recovery_complete", "recovery_cycle_complete", "stopped"]);
  function held(reason) {
    return {status: "held", reason, usageCalls: calls, sessionId: cell.session_id ?? null, events: summaries};
  }
  for (;;) {
    if (cell.output) idlePolls = 0;
    buffer += cell.output || "";
    if (buffer.length > 1024 * 1024) return held("bridge_output_limit");
    let end;
    while ((end = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, end).replace(/\r$/, "");
      buffer = buffer.slice(end + 1);
      let event;
      try { event = JSON.parse(line); }
      catch { return held("bridge_unexpected_output"); }
      if (!event || typeof event !== "object" || Array.isArray(event)) {
        return held("bridge_unexpected_output");
      }
      if (event.event !== "tool_request") {
        if (!safeEvents.has(event.event)) return held("bridge_unknown_event");
        // Retain only fixed known fields, never arbitrary stdout or a raw reply.
        const allowed = ["event", "broker_requests", "activation", "observed_at",
          "window_count", "minimum_remaining_percent", "required_submission_percent",
          "blockers", "source_sha256", "private_evidence_directory", "command_id",
          "state", "new_submission", "unresolved", "position_count", "committed_capital",
          "reason", "error_type", "recovery"];
        summaries.push(Object.fromEntries(allowed.filter(k => k in event).map(k => [k, event[k]])));
        continue;
      }
      if (event.tool !== "usage" || event.sequence !== sequence + 1 ||
          !event.arguments || Array.isArray(event.arguments) ||
          typeof event.arguments !== "object" || Object.keys(event.arguments).length !== 0) {
        return held("bridge_only_fresh_sequential_usage_requests_supported");
      }
      if (!cell.session_id || ++calls > 2) return held("bridge_request_limit_or_missing_process");
      // No retries, cached fallback, transformation or manufactured timestamp.
      let raw;
      try { raw = await api.readUsage({}); }
      catch { return held("bridge_usage_tool_unavailable"); }
      let response;
      try {
        response = JSON.stringify(raw);
        if (!raw || typeof raw !== "object" || Array.isArray(raw) || !response || response.length > 1024 * 1024) {
          return held("bridge_usage_response_unavailable");
        }
      } catch { return held("bridge_usage_response_unavailable"); }
      sequence = event.sequence;
      try {
        cell = await api.write({session_id: cell.session_id, chars: response + "\n",
          yield_time_ms: 1000, max_output_tokens: 10000});
      } catch { return held("bridge_response_delivery_unknown_no_retry"); }
      // Re-enter outer loop to consume the next process output exactly once.
      break;
    }
    if (end >= 0) continue;
    if (!cell.session_id) {
      if (buffer.trim()) return held("bridge_truncated_output");
      const completed = summaries.some(e => e.event !== "submitted_once" && e.event !== "stopped");
      return {status: cell.exit_code === 0 && completed ? "complete" : "held", exitCode: cell.exit_code,
        usageCalls: calls, events: summaries};
    }
    if (++idlePolls > 120) return held("bridge_process_observation_limit");
    try {
      cell = await api.write({session_id: cell.session_id, chars: "",
        yield_time_ms: 1000, max_output_tokens: 10000});
    } catch { return held("bridge_process_observation_failed"); }
  }
}

if (typeof module !== "undefined") module.exports = {serveCodexUsageRequests};
