export function automationDismissalStorageKey(ownerId, sessionId) {
  if (typeof ownerId !== "string" || !ownerId ||
      typeof sessionId !== "string" || !sessionId) return undefined;
  return `habab-home-automation-cards-hidden:${encodeURIComponent(ownerId)}:${encodeURIComponent(sessionId)}`;
}

export function readAutomationDismissals(storage, key) {
  if (!storage || !key) return new Set();
  try {
    const raw = storage.getItem(key);
    if (typeof raw !== "string" || raw.length > 32768) return new Set();
    const values = JSON.parse(raw);
    if (!Array.isArray(values) || values.length > 256) return new Set();
    return new Set(values.filter((id) => typeof id === "string" && id.length > 0 && id.length <= 128));
  } catch (_) {
    return new Set();
  }
}

export function writeAutomationDismissals(storage, key, dismissed) {
  if (!storage || !key || !(dismissed instanceof Set)) return false;
  try {
    storage.setItem(key, JSON.stringify([...dismissed]
      .filter((id) => typeof id === "string" && id.length > 0 && id.length <= 128)
      .slice(0, 256)));
    return true;
  } catch (_) {
    return false;
  }
}
