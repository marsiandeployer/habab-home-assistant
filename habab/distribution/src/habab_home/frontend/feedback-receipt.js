// Shared pure helpers for presenting and copying safe feedback receipt metadata.
export function isCanonicalFeedbackUuid(value) {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}

export function feedbackReceiptIdFromSearch(search) {
  const value = new URLSearchParams(typeof search === "string" ? search : "").get("feedback");
  return isCanonicalFeedbackUuid(value) ? value : undefined;
}

export function validFeedbackIssueUrl(value) {
  if (typeof value !== "string" || value.length > 300) return undefined;
  try {
    const url = new URL(value);
    const path = /^\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\/issues\/[1-9][0-9]*$/;
    if (url.protocol !== "https:" || url.hostname !== "github.com" || url.port || url.username || url.password ||
        url.search || url.hash || !path.test(url.pathname) || url.href !== value) return undefined;
    return value;
  } catch (_) { return undefined; }
}

export function feedbackReceiptPresentation(receipt, expectedLocalId) {
  const localId = receipt?.receipt_id;
  if (!isCanonicalFeedbackUuid(localId) || receipt.state !== "saved_locally" ||
      (expectedLocalId !== undefined && localId !== expectedLocalId)) return undefined;
  const serverId = isCanonicalFeedbackUuid(receipt.server_receipt_id) ? receipt.server_receipt_id : undefined;
  const issueUrl = validFeedbackIssueUrl(receipt.server_issue_url);
  const issueNumber = issueUrl && Number.isSafeInteger(receipt.server_issue_number) && receipt.server_issue_number > 0
    ? receipt.server_issue_number : undefined;
  const deliveryStatus = new Set(["saved_on_server", "pending", "not_configured", "rejected"]).has(receipt.delivery_status)
    ? receipt.delivery_status : "pending";
  const traceStatus = new Set(["available", "truncated", "unavailable"]).has(receipt.trace_status)
    ? receipt.trace_status : "unavailable";
  return {
    localId,
    serverId,
    issueUrl: issueNumber ? issueUrl : undefined,
    issueNumber,
    deliveryStatus,
    traceStatus,
  };
}

export function feedbackShareMessage(receipt, copy) {
  const details = feedbackReceiptPresentation(receipt);
  if (!details || typeof copy?.feedbackShareText !== "string") return "";
  const path = `/habab-diagnostics?feedback=${encodeURIComponent(details.localId)}`;
  let receiptUrl = path;
  try {
    const origin = globalThis.location?.origin;
    if (typeof origin === "string" && /^https?:\/\//.test(origin)) receiptUrl = new URL(path, origin).href;
  } catch (_) { /* Keep the relative deep link when no browser origin is available. */ }
  return copy.feedbackShareText.replace("{id}", details.serverId || details.localId)
    .replace("{url}", receiptUrl).replace("{issue}", details.issueUrl ? ` · ${details.issueUrl}` : "").trim();
}

export async function copyFeedbackText(value) {
  if (typeof globalThis.navigator?.clipboard?.writeText === "function") {
    await globalThis.navigator.clipboard.writeText(value);
    return;
  }
  if (typeof document === "undefined" || !document.body || typeof document.execCommand !== "function") {
    throw new Error("clipboard_unavailable");
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.left = "-10000px";
  document.body.append(input);
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("clipboard_failed");
}
