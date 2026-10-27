function hashText(value) {
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    first = Math.imul(first ^ code, 0x01000193);
    second = Math.imul(second ^ code, 0x85ebca6b);
  }
  return `${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`;
}

export function chatDismissalStorageKey(ownerId, sessionId) {
  if (typeof ownerId !== "string" || !ownerId || typeof sessionId !== "string" || !sessionId) return undefined;
  return `habab.setup-chat.dismissed-details.v1.${hashText(`${ownerId}\0${sessionId}`)}`;
}

export function chatDetailDismissalId(messageIndex, detailIndex, messageText, summaryText) {
  return hashText(`${messageIndex}\0${detailIndex}\0${summaryText}\0${messageText}`);
}

export function readChatDismissals(storage, key) {
  if (!storage || !key) return new Set();
  try {
    const values = JSON.parse(storage.getItem(key) || "[]");
    return new Set(Array.isArray(values) ? values.filter((value) => typeof value === "string") : []);
  } catch (_) {
    return new Set();
  }
}

export function writeChatDismissals(storage, key, values) {
  if (!storage || !key) return false;
  try {
    storage.setItem(key, JSON.stringify([...values]));
    return true;
  } catch (_) {
    return false;
  }
}

export function decorateChatDetails(markdown, {
  messageIndex,
  messageText,
  dismissalKey,
  dismissed,
  storage,
  hideLabel,
  onDismiss,
}) {
  const root = markdown?.shadowRoot;
  const content = root?.querySelector("ha-markdown-element");
  if (!root || !content) return 0;

  if (!root.querySelector("style[data-chat-dismissal]")) {
    const style = markdown.ownerDocument.createElement("style");
    style.dataset.chatDismissal = "";
    style.textContent = `.message-detail{display:flex;align-items:flex-start;gap:8px;max-width:100%}.message-detail>details{min-width:0;flex:1}.message-detail-dismiss{min-height:32px;padding:4px 8px;border:1px solid var(--divider-color,#c8cccf);border-radius:8px;color:var(--secondary-text-color,#5f6368);background:var(--card-background-color,#fff);font:inherit;font-size:12px;cursor:pointer}.message-detail-dismiss:focus-visible{outline:2px solid var(--primary-color,#007a9e);outline-offset:2px}`;
    root.append(style);
  }

  const detailsList = [...content.querySelectorAll("details")];
  detailsList.forEach((details, detailIndex) => {
    const summary = details.querySelector("summary");
    if (!summary) return;
    const summaryText = summary.textContent.trim();
    const id = chatDetailDismissalId(messageIndex, detailIndex, messageText, summaryText);
    const wrapper = markdown.ownerDocument.createElement("div");
    wrapper.className = "message-detail";
    const button = markdown.ownerDocument.createElement("button");
    button.type = "button";
    button.className = "message-detail-dismiss";
    button.textContent = hideLabel;
    button.setAttribute("aria-label", `${hideLabel}: ${summaryText}`);
    details.replaceWith(wrapper);
    wrapper.append(details, button);
    wrapper.hidden = dismissed.has(id);
    button.addEventListener("click", () => {
      dismissed.add(id);
      writeChatDismissals(storage, dismissalKey, dismissed);
      wrapper.hidden = true;
      onDismiss?.(dismissed.size);
    });
  });
  return detailsList.length;
}
