import type { StyleSpecification } from "maplibre-gl";
import type { MapBaseMapError, YandexTilesLanguage } from "./map-engine";

export const YANDEX_TILE_SOURCE_ID = "habab-yandex-tiles";
export const YANDEX_TILES_ORIGIN = "https://tiles.api-maps.yandex.ru";
export const YANDEX_MAPS_URL = "https://yandex.ru/maps";
export const YANDEX_MAPS_EN_URL = "https://yandex.com/maps";
export const YANDEX_TERMS_URL = "https://yandex.ru/legal/maps_api/ru/";

export interface YandexTilesConfig {
  apiKey: string;
  language: YandexTilesLanguage;
}

export const yandexTilesUrl = ({ apiKey, language }: YandexTilesConfig) =>
  `${YANDEX_TILES_ORIGIN}/v1/tiles/?apikey=${encodeURIComponent(apiKey.trim())}` +
  `&lang=${language}&x={x}&y={y}&z={z}&l=map&projection=web_mercator`;

export const yandexTilesAttribution = (language: YandexTilesLanguage) =>
  language === "ru_RU"
    ? `&copy; <a href="${YANDEX_MAPS_URL}" target="_blank" rel="noopener noreferrer">Яндекс</a> · <a href="${YANDEX_TERMS_URL}" target="_blank" rel="noopener noreferrer">Условия использования</a>`
    : `&copy; <a href="${YANDEX_MAPS_EN_URL}" target="_blank" rel="noopener noreferrer">Yandex</a> · <a href="${YANDEX_TERMS_URL}" target="_blank" rel="noopener noreferrer">Terms of use</a>`;

export const buildYandexRasterStyle = (
  config: YandexTilesConfig
): StyleSpecification => ({
  version: 8,
  sources: {
    [YANDEX_TILE_SOURCE_ID]: {
      type: "raster",
      tiles: [yandexTilesUrl(config)],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 20,
      attribution: yandexTilesAttribution(config.language),
    },
  },
  layers: [
    {
      id: YANDEX_TILE_SOURCE_ID,
      type: "raster",
      source: YANDEX_TILE_SOURCE_ID,
      minzoom: 0,
      maxzoom: 20,
    },
  ],
});

export const classifyYandexTileStatus = (
  status: number | undefined
): MapBaseMapError =>
  status === 401 || status === 403
    ? "invalid_key"
    : status === 429
      ? "rate_limited"
      : "unavailable";

export interface YandexMapMessages {
  providerError: string;
  invalidKey: string;
  rateLimited: string;
  settingsHint: string;
  openInMaps: string;
  settingsTitle: string;
  providerLabel: string;
  openStreetMap: string;
  yandexMaps: string;
  apiKeyLabel: string;
  apiKeyHelper: string;
  keyConfigured: string;
  removeKey: string;
  saveError: string;
  enterApiKey: string;
}

const MAP_MESSAGES: Record<"ru" | "en", YandexMapMessages> = {
  ru: {
    providerError:
      "Не удалось загрузить Яндекс Карты. Проверьте подключение, ключ API и разрешённый домен.",
    invalidKey:
      "Яндекс Карты отклонили ключ или домен. Проверьте ограничения ключа в кабинете разработчика.",
    rateLimited:
      "Яндекс Карты временно ограничили запросы по этому ключу. Проверьте его лимит и тариф.",
    settingsHint:
      "Откройте настройки карты, чтобы проверить ключ или переключиться на OpenStreetMap.",
    openInMaps: "Открыть в Картах",
    settingsTitle: "Настройки карты",
    providerLabel: "Поставщик карты",
    openStreetMap: "OpenStreetMap",
    yandexMaps: "Яндекс Карты",
    apiKeyLabel: "Личный ключ API Яндекс Карт",
    apiKeyHelper:
      "Используйте личный ключ для API «Тайлы» и разрешите домен Home Assistant. Браузер обращается к Яндекс Картам напрямую, поэтому ключ виден в запросах; он хранится в настройках вашего пользователя Home Assistant.",
    keyConfigured: "Ключ сохранён",
    removeKey: "Удалить сохранённый ключ",
    saveError: "Не удалось сохранить настройки карты.",
    enterApiKey: "Введите личный ключ API Яндекс Карт.",
  },
  en: {
    providerError:
      "Yandex Maps could not load. Check your connection, API key, and allowed domain.",
    invalidKey:
      "Yandex Maps rejected the key or domain. Check the key restrictions in the developer dashboard.",
    rateLimited:
      "Yandex Maps temporarily limited requests for this key. Check its quota and plan.",
    settingsHint:
      "Open map settings to check the key or switch to OpenStreetMap.",
    openInMaps: "Open in Maps",
    settingsTitle: "Map settings",
    providerLabel: "Map provider",
    openStreetMap: "OpenStreetMap",
    yandexMaps: "Yandex Maps",
    apiKeyLabel: "Your Yandex Maps API key",
    apiKeyHelper:
      "Use your own key for the Tiles API and allow your Home Assistant domain. Your browser requests tiles directly, so the key is visible in those requests; it is stored in your Home Assistant user settings.",
    keyConfigured: "Key saved",
    removeKey: "Remove saved key",
    saveError: "Could not save map settings.",
    enterApiKey: "Enter your Yandex Maps API key.",
  },
};

export const yandexMapMessages = (language: string): YandexMapMessages =>
  MAP_MESSAGES[language.toLowerCase().startsWith("ru") ? "ru" : "en"];

export const yandexLogoUrl = (language: string) =>
  language.toLowerCase().startsWith("ru")
    ? "/static/images/map/yandex-logo-ru.png"
    : "/static/images/map/yandex-logo-en.png";

export const yandexMapsHomeUrl = (language: string) =>
  language.toLowerCase().startsWith("ru")
    ? YANDEX_MAPS_URL
    : YANDEX_MAPS_EN_URL;
