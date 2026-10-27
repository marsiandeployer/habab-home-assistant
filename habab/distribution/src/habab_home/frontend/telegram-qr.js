import QRCode from "./vendor/qrcode.js";

export const MAX_PAIRING_URL_LENGTH = 2048;

const QR_TEXT = {
  ru: {
    ownerQrLabel: "QR-код для привязки личного Telegram-чата",
    groupQrLabel: "QR-код для добавления Telegram-бота в группу",
    copyOwnerLink: "Скопировать ссылку привязки",
    copyGroupLink: "Скопировать ссылку для группы",
    copied: "Ссылка скопирована.",
    copyFailed: "Не удалось скопировать ссылку. Откройте её вручную.",
    qrFailed: "Не удалось построить QR-код. Используйте ссылку ниже.",
  },
  en: {
    ownerQrLabel: "QR code to pair your private Telegram chat",
    groupQrLabel: "QR code to add the Telegram bot to a group",
    copyOwnerLink: "Copy pairing link",
    copyGroupLink: "Copy group link",
    copied: "Link copied.",
    copyFailed: "Could not copy the link. Open it manually.",
    qrFailed: "Could not create the QR code. Use the link below.",
  },
};

export function telegramQrText(language, key) {
  const copy = QR_TEXT[language === "ru" ? "ru" : "en"];
  return copy[key] || "";
}

export function normalizeTelegramPairingUrl(value, kind) {
  const parameter = kind === "owner" ? "start" : kind === "group" ? "startgroup" : "";
  if (!parameter) return "";
  if (typeof value !== "string" || value.length === 0 || value.length > MAX_PAIRING_URL_LENGTH) return "";
  let url;
  try {
    url = new URL(value);
  } catch (_error) {
    return "";
  }
  const token = url.searchParams.get(parameter);
  if (url.protocol !== "https:" || url.hostname !== "t.me" || url.port || url.username || url.password ||
      url.hash || !/^\/[A-Za-z0-9_]{1,64}$/.test(url.pathname) ||
      !token || !/^[A-Za-z0-9_-]{1,256}$/.test(token) ||
      [...url.searchParams.keys()].some((key) => key !== parameter)) return "";
  return url.toString();
}

export function createTelegramPairingQr(value, kind) {
  const url = normalizeTelegramPairingUrl(value, kind);
  if (!url) throw new Error("invalid_pairing_url");
  const code = QRCode.create(url, { errorCorrectionLevel: "M" });
  return { url, size: code.modules.size, data: code.modules.data };
}

export function renderTelegramPairingQr(canvas, value, kind) {
  const qr = createTelegramPairingQr(value, kind);
  if (!canvas || typeof canvas.getContext !== "function") throw new Error("canvas_unavailable");
  const context = canvas.getContext("2d");
  if (!context) throw new Error("canvas_unavailable");

  const quietZone = 4;
  const scale = 4;
  const side = (qr.size + quietZone * 2) * scale;
  canvas.width = side;
  canvas.height = side;
  canvas.setAttribute("role", "img");
  const pixels = context.createImageData(side, side);
  for (let y = 0; y < side; y++) {
    const moduleY = Math.floor(y / scale) - quietZone;
    for (let x = 0; x < side; x++) {
      const moduleX = Math.floor(x / scale) - quietZone;
      const dark = moduleX >= 0 && moduleY >= 0 && moduleX < qr.size && moduleY < qr.size &&
        qr.data[moduleY * qr.size + moduleX] === 1;
      const pixel = (y * side + x) * 4;
      const shade = dark ? 0 : 255;
      pixels.data[pixel] = shade;
      pixels.data[pixel + 1] = shade;
      pixels.data[pixel + 2] = shade;
      pixels.data[pixel + 3] = 255;
    }
  }
  context.putImageData(pixels, 0, 0);
  return qr.size;
}

export async function copyTelegramPairingUrl(value) {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
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
