(function exposeAssistantStream(root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.AIFlowAssistantStream = api;
})(typeof globalThis === "object" ? globalThis : this, function createAssistantStream() {
  "use strict";

  function eventData(event) {
    return event?.data && typeof event.data === "object" ? event.data : (event || {});
  }

  function responseId(data, event) {
    return String(
      data.response_id ||
      data.message_id ||
      data.message_uuid ||
      data.parent_tool_use_id ||
      event?.sequence ||
      "current"
    );
  }

  function blockIdentity(data, event) {
    const parsedIndex = Number(data.block_index);
    const blockIndex = data.block_index !== null &&
      data.block_index !== undefined &&
      Number.isInteger(parsedIndex) ? parsedIndex : 0;
    const currentResponseId = responseId(data, event);
    return {
      responseId: currentResponseId,
      blockIndex,
      key: `${currentResponseId}:${blockIndex}`,
    };
  }

  function ensureContentEntry(entries, event, field) {
    const identity = blockIdentity(eventData(event), event);
    let entry = entries.get(identity.key);
    if (!entry) {
      entry = {
        ...identity,
        [field]: "",
        finalized: false,
        finished: false,
        partial: false,
        row: null,
        flushFrame: null,
        lastEvent: event,
      };
      entries.set(identity.key, entry);
    }
    return entry;
  }

  function ensureEntry(entries, event) {
    return ensureContentEntry(entries, event, "text");
  }

  function ensureReasoningEntry(entries, event) {
    return ensureContentEntry(entries, event, "thinking");
  }

  function entryForFinal(entries, event, field = "text", ensure = ensureEntry) {
    const data = eventData(event);
    const identity = blockIdentity(data, event);
    const exact = entries.get(identity.key);
    if (exact) return exact;
    const finalContent = data[field];
    const candidates = [...entries.values()].filter((entry) =>
      entry.responseId === identity.responseId &&
      !entry.finalized &&
      !entry.partial &&
      (typeof finalContent !== "string" || !entry[field] || finalContent.startsWith(entry[field]))
    );
    if (candidates.length === 1) return candidates[0];
    return ensure(entries, event);
  }

  function applyAssistantStreamEvent(entries, type, event) {
    const data = eventData(event);
    if (type === "assistant_message_started") return [];

    if (type === "assistant_text_delta") {
      const entry = ensureEntry(entries, event);
      if (data.finalized !== false || entry.finalized) return [];
      entry.lastEvent = event;
      entry.text += data.text || "";
      return [{ kind: "delta", entry }];
    }

    if (type === "assistant_message") {
      const entry = entryForFinal(entries, event);
      entry.lastEvent = event;
      if (typeof data.text === "string") entry.text = data.text;
      entry.finalized = true;
      return [{ kind: "final", entry }];
    }

    if (type === "assistant_message_finished") {
      const currentResponseId = responseId(data, event);
      const updates = [];
      for (const entry of entries.values()) {
        if (entry.responseId !== currentResponseId) continue;
        entry.lastEvent = event;
        entry.finalized = true;
        entry.finished = true;
        updates.push({ kind: "finished", entry });
      }
      return updates;
    }

    return [];
  }

  function applyReasoningStreamEvent(entries, type, event) {
    const data = eventData(event);

    if (type === "agent_reasoning" && data.finalized === false) {
      const entry = ensureReasoningEntry(entries, event);
      if (entry.finalized || entry.partial) return [];
      entry.lastEvent = event;
      entry.thinking += data.thinking || "";
      return [{ kind: "delta", entry }];
    }

    if (type === "agent_reasoning" && data.finalized === true) {
      const entry = entryForFinal(entries, event, "thinking", ensureReasoningEntry);
      entry.lastEvent = event;
      if (typeof data.thinking === "string") entry.thinking = data.thinking;
      entry.finalized = true;
      entry.partial = false;
      return [{ kind: "final", entry }];
    }

    if (type === "agent_partial_capture" && data.block_type === "thinking") {
      const entry = entryForFinal(entries, event, "thinking", ensureReasoningEntry);
      entry.lastEvent = event;
      if (typeof data.thinking === "string") entry.thinking = data.thinking;
      entry.finalized = false;
      entry.partial = true;
      return [{ kind: "partial", entry }];
    }

    return [];
  }

  return Object.freeze({
    applyAssistantStreamEvent,
    applyReasoningStreamEvent,
    blockIdentity,
    responseId,
  });
});
