// Tool output is plain text, never model-authored HTML or executable markup.
export function createToolCalls(document, calls, copy) {
  const tools = Array.isArray(calls) ? calls.filter((call) =>
    call && typeof call.name === "string" && typeof call.status === "string"
  ).slice(0, 24) : [];
  if (!tools.length) return undefined;
  const group = document.createElement("details");
  group.className = "tool-calls";
  const title = document.createElement("summary");
  title.textContent = copy.toolCallsTitle.replace("{count}", String(tools.length));
  group.append(title);
  for (const tool of tools) {
    const detail = document.createElement("details");
    detail.className = "tool-call";
    const heading = document.createElement("summary");
    const status = ({completed: copy.toolCompleted, error: copy.toolFailed,
      running: copy.toolRunning, pending: copy.toolPending})[tool.status] || copy.toolUnknown;
    heading.textContent = `${tool.name} · ${status}`;
    detail.append(heading);
    for (const [field, label] of [["input", copy.toolInput], ["output", copy.toolOutput], ["error", copy.toolError]]) {
      if (typeof tool[field] !== "string" || !tool[field]) continue;
      const caption = document.createElement("p");
      caption.className = "tool-caption";
      caption.textContent = label;
      const value = document.createElement("pre");
      value.textContent = tool[field].slice(0, 12000);
      detail.append(caption, value);
    }
    if (tool.truncated || ["input", "output", "error"].some((field) =>
      typeof tool[field] === "string" && tool[field].length > 12000)) {
      const note = document.createElement("p");
      note.className = "tool-caption";
      note.textContent = copy.toolTruncated;
      const link = document.createElement("a");
      link.href = "/habab-diagnostics";
      link.textContent = copy.toolLogs;
      note.append(" ", link);
      detail.append(note);
    }
    group.append(detail);
  }
  return group;
}
