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

  function ensureEntry(entries, event) {
    const identity = blockIdentity(eventData(event), event);
    let entry = entries.get(identity.key);
    if (!entry) {
      entry = {
        ...identity,
        text: "",
        finalized: false,
        finished: false,
        row: null,
        flushFrame: null,
        lastEvent: event,
      };
      entries.set(identity.key, entry);
    }
    return entry;
  }

  function entryForFinal(entries, event) {
    const data = eventData(event);
    const identity = blockIdentity(data, event);
    const exact = entries.get(identity.key);
    if (exact) return exact;
    const candidates = [...entries.values()].filter((entry) =>
      entry.responseId === identity.responseId &&
      !entry.finalized &&
      (typeof data.text !== "string" || !entry.text || data.text.startsWith(entry.text))
    );
    if (candidates.length === 1) return candidates[0];
    return ensureEntry(entries, event);
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

  return Object.freeze({
    applyAssistantStreamEvent,
    blockIdentity,
    responseId,
  });
});
