import { describe, expect, it } from "vitest";
import {
  buildYandexRasterStyle,
  classifyYandexTileStatus,
  yandexMapMessages,
  yandexTilesAttribution,
  yandexTilesUrl,
} from "../../../src/common/map/yandex-map";

describe("Yandex Tiles configuration", () => {
  const config = { apiKey: "test key", language: "ru_RU" as const };

  it("builds only the fixed raster endpoint and keeps the tile placeholders", () => {
    const url = new URL(yandexTilesUrl(config));

    expect(url.origin).toBe("https://tiles.api-maps.yandex.ru");
    expect(url.pathname).toBe("/v1/tiles/");
    expect(url.searchParams.get("apikey")).toBe("test key");
    expect(url.searchParams.get("lang")).toBe("ru_RU");
    expect(url.searchParams.get("projection")).toBe("web_mercator");
    expect(url.searchParams.get("l")).toBe("map");
    expect(url.search).toContain("x={x}");
    expect(url.search).toContain("y={y}");
    expect(url.search).toContain("z={z}");
  });

  it("builds a bounded 256px raster source with required attribution", () => {
    const style = buildYandexRasterStyle(config);
    const source = style.sources["habab-yandex-tiles"] as {
      attribution?: string;
      maxzoom?: number;
      minzoom?: number;
      tileSize?: number;
      type?: string;
    };

    expect(source).toMatchObject({
      type: "raster",
      tileSize: 256,
      minzoom: 0,
      maxzoom: 20,
    });
    expect(source.attribution).toContain("yandex.ru/maps");
    expect(source.attribution).toContain("/legal/maps_api/ru/");
    expect(style.layers).toHaveLength(1);
  });

  it.each([
    [401, "invalid_key"],
    [403, "invalid_key"],
    [429, "rate_limited"],
    [500, "unavailable"],
    [undefined, "unavailable"],
  ] as const)("maps HTTP %s to a bounded UI error", (status, expected) => {
    expect(classifyYandexTileStatus(status)).toBe(expected);
  });

  it("provides Russian and English extension-owned labels", () => {
    expect(yandexMapMessages("ru-RU").settingsTitle).toBe("Настройки карты");
    expect(yandexMapMessages("en-US").settingsTitle).toBe("Map settings");
    expect(yandexTilesAttribution("en_US")).toContain("Yandex</a>");
  });
});
