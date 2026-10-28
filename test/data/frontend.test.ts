import { describe, expect, it } from "vitest";
import { withMapBasemapUserData } from "../../src/data/frontend";

describe("withMapBasemapUserData", () => {
  it("updates only map preferences and preserves other user data", () => {
    const current = {
      default_panel: "map",
      showEntityIdPicker: true,
      mapBasemap: { provider: "yandex" as const, yandexApiKey: "old-test-key" },
    };

    expect(withMapBasemapUserData(current, { provider: "osm" })).toEqual({
      default_panel: "map",
      showEntityIdPicker: true,
      mapBasemap: { provider: "osm" },
    });
    expect(current.mapBasemap.yandexApiKey).toBe("old-test-key");
  });
});
