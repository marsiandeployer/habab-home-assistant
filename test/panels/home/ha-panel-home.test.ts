import { afterEach, describe, expect, it, vi } from "vitest";
import { provideHass } from "../../../src/fake_data/provide_hass";
import type { HomeFrontendSystemData } from "../../../src/data/frontend";
import "../../../src/panels/home/ha-panel-home";

type HomePanel = HTMLElement & {
  hass: unknown;
  updateComplete: Promise<boolean>;
  _showBanner: boolean;
  _loadConfigPromise?: Promise<void>;
  [key: string]: unknown;
};

for (const tag of ["ha-button", "ha-svg-icon"]) {
  if (!customElements.get(tag)) {
    customElements.define(tag, class extends HTMLElement {});
  }
}
vi.stubGlobal(
  "ResizeObserver",
  class {
    observe = vi.fn();

    unobserve = vi.fn();

    disconnect = vi.fn();
  }
);
vi.mock("../../../src/components/ha-button", () => ({}));
vi.mock("../../../src/components/ha-svg-icon", () => ({}));
vi.mock("../../../src/panels/lovelace/hui-root", () => ({}));
vi.mock("../../../src/data/frontend", async (importOriginal) => {
  const actual = await importOriginal<object>();
  return {
    ...actual,
    fetchFrontendSystemData: vi.fn(),
    saveFrontendSystemData: vi.fn(),
    subscribeFrontendSystemData: vi.fn(() => () => undefined),
  };
});
vi.mock("../../../src/panels/home/dialogs/show-dialog-new-overview", () => ({
  showNewOverviewDialog: vi.fn(),
}));

const STORED: HomeFrontendSystemData = {
  welcome_banner_dismissed: false,
  favorite_entities: ["light.kitchen", "climate.living"],
  shortcuts: [
    { type: "summary", key: "light" },
    { type: "custom", path: "/energy", label: "Energy" },
  ],
};

const openPanel = async (
  fetchImpl: (key?: string) => Promise<any>,
  waitForLoad = true
) => {
  const { fetchFrontendSystemData } =
    await import("../../../src/data/frontend");
  vi.mocked(fetchFrontendSystemData).mockImplementation(((_connection, key) =>
    fetchImpl(key)) as any);
  const host = document.createElement("div");
  // No legacy overview panel: the banner decision must rest on loaded config.
  const hass = provideHass(host, { panels: {} });
  document.body.append(host);
  const panel = document.createElement("ha-panel-home") as unknown as HomePanel;
  host.append(panel);
  panel.hass = hass;
  await panel.updateComplete;
  if (waitForLoad) {
    await panel._loadConfigPromise;
  }
  return panel;
};

afterEach(() => {
  document.body.replaceChildren();
  vi.clearAllMocks();
});

describe("home panel welcome banner", () => {
  it("stays hidden when the home config load is rejected", async () => {
    const { saveFrontendSystemData } =
      await import("../../../src/data/frontend");
    const { showNewOverviewDialog } =
      await import("../../../src/panels/home/dialogs/show-dialog-new-overview");
    let dismissed: Promise<void> | undefined;
    vi.mocked(showNewOverviewDialog).mockImplementation(((
      _el: any,
      opts: any
    ) => {
      dismissed = opts.dismiss();
    }) as any);
    const panel = await openPanel(async () => {
      throw new Error("no answer");
    });
    await panel.updateComplete;
    expect(panel._showBanner).toBe(false);
    // Defense in depth: even if invoked directly, dismiss must not persist
    // an unloaded (empty) config over stored favourites/shortcuts.
    (panel["_learnMore"] as () => void)();
    await dismissed;
    await panel.updateComplete;
    expect(vi.mocked(saveFrontendSystemData)).not.toHaveBeenCalled();
  });

  it("dismissing saves the loaded favourites, not an empty config", async () => {
    const { saveFrontendSystemData } =
      await import("../../../src/data/frontend");
    const { showNewOverviewDialog } =
      await import("../../../src/panels/home/dialogs/show-dialog-new-overview");
    let dismissed: Promise<void> | undefined;
    vi.mocked(showNewOverviewDialog).mockImplementation(((
      _el: any,
      opts: any
    ) => {
      dismissed = opts.dismiss();
    }) as any);
    const panel = await openPanel(async () => ({ ...STORED }));
    await panel.updateComplete;
    expect(panel._showBanner).toBe(true);
    (panel["_learnMore"] as () => void)();
    await dismissed;
    await panel.updateComplete;
    expect(vi.mocked(saveFrontendSystemData)).toHaveBeenCalledTimes(1);
    const saved = vi.mocked(saveFrontendSystemData).mock.calls[0][2] as any;
    expect(saved.favorite_entities).toEqual(STORED.favorite_entities);
    expect(saved.shortcuts).toEqual(STORED.shortcuts);
    expect(saved.welcome_banner_dismissed).toBe(true);
  });
});

describe("home panel system data subscription", () => {
  it("uses live home data when the initial fetch finishes after a subscription update", async () => {
    const { fetchFrontendSystemData, subscribeFrontendSystemData } =
      await import("../../../src/data/frontend");
    let resolveHomeFetch!: (value: HomeFrontendSystemData) => void;
    const panel = await openPanel(
      async (key) =>
        key === "home"
          ? new Promise<HomeFrontendSystemData>((resolve) => {
              resolveHomeFetch = resolve;
            })
          : {},
      false
    );

    const homeSubscription = vi
      .mocked(subscribeFrontendSystemData)
      .mock.calls.find(([, key]) => key === "home")?.[2];
    expect(homeSubscription).toBeDefined();
    expect(vi.mocked(fetchFrontendSystemData)).toHaveBeenCalledWith(
      expect.anything(),
      "home"
    );

    const liveConfig: HomeFrontendSystemData = {
      favorite_entities: ["vacuum.dacha"],
    };
    const setLovelace = vi.spyOn(panel as any, "_setLovelace");
    homeSubscription?.({ value: liveConfig });
    await panel.updateComplete;
    expect((panel as any)._config).toEqual(liveConfig);
    expect(setLovelace).toHaveBeenCalled();

    resolveHomeFetch({ favorite_entities: ["light.kitchen"] });
    await panel._loadConfigPromise;
    expect((panel as any)._config).toEqual(liveConfig);
  });
});
