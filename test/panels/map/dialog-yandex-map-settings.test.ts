import { afterEach, describe, expect, it, vi } from "vitest";
import { provideHass } from "../../../src/fake_data/provide_hass";
import type { HomeAssistant } from "../../../src/types";
import "../../../src/panels/map/dialog-yandex-map-settings";

vi.mock("../../../src/components/ha-alert", () => ({}));
vi.mock("../../../src/components/ha-button", () => ({}));
vi.mock("../../../src/components/ha-dialog", () => ({}));
vi.mock("../../../src/components/ha-dialog-footer", () => ({}));
vi.mock("../../../src/components/input/ha-input", () => ({}));
vi.mock("../../../src/components/ha-select", () => ({}));

interface TestDialog extends HTMLElement {
  hass: HomeAssistant;
  showDialog: () => Promise<void>;
  updateComplete: Promise<boolean>;
  isDirtyState: boolean;
}

interface TestDialogInternals {
  _apiKey: string;
  _provider: "osm" | "yandex";
}

describe("dialog-yandex-map-settings draft lifecycle", () => {
  const roots: HTMLElement[] = [];
  const dialogs: TestDialog[] = [];

  const createDialog = async (saveFails = false) => {
    const root = document.createElement("div");
    document.body.append(root);
    roots.push(root);
    const hass = provideHass(
      root,
      {
        language: "en",
        selectedLanguage: "en",
        userData: { mapBasemap: { provider: "osm" } },
      },
      true
    );
    hass.connection.sendMessagePromise = vi.fn(async () => {
      if (saveFails) {
        throw new Error("fixture_save_failed");
      }
      return undefined;
    }) as typeof hass.connection.sendMessagePromise;

    const dialog = document.createElement(
      "dialog-yandex-map-settings"
    ) as TestDialog;
    dialog.hass = hass;
    root.append(dialog);
    dialogs.push(dialog);
    await dialog.showDialog();
    await dialog.updateComplete;
    return {
      dialog,
      internals: dialog as unknown as TestDialogInternals,
      hass,
    };
  };

  const invokeInternal = <Result>(
    dialog: TestDialog,
    methodName: string,
    ...args: unknown[]
  ): Result => {
    const method = (dialog as unknown as Record<string, unknown>)[methodName];
    if (typeof method !== "function") {
      throw new Error("Expected dialog test method");
    }
    return (method as (...methodArgs: unknown[]) => Result).apply(dialog, args);
  };

  const addUnsavedDraft = async (dialog: TestDialog) => {
    invokeInternal<undefined>(dialog, "_providerChanged", {
      detail: { value: "yandex" },
    });
    invokeInternal<undefined>(dialog, "_apiKeyChanged", {
      currentTarget: { value: "synthetic-only-key" },
    });
  };

  afterEach(async () => {
    roots.splice(0).forEach((root) => root.remove());
    dialogs.splice(0);
    await Promise.resolve();
  });

  it("keeps the secret draft in memory and blocks reload after a failed save", async () => {
    const { dialog, internals } = await createDialog(true);
    await addUnsavedDraft(dialog);
    await dialog.updateComplete;

    expect(dialog.isDirtyState).toBe(true);
    expect(window.isDirtyState).toBe(true);

    await invokeInternal<Promise<void>>(dialog, "_save");
    await dialog.updateComplete;

    expect(internals._apiKey.length).toBeGreaterThan(0);
    expect(dialog.isDirtyState).toBe(true);
    expect(window.isDirtyState).toBe(true);
    expect(JSON.stringify((dialog as any)._dirtyStateContext)).not.toContain(
      "synthetic-only-key"
    );
  });

  it("marks the draft clean and clears it only after a successful save", async () => {
    const { dialog, internals, hass } = await createDialog();
    await addUnsavedDraft(dialog);
    await dialog.updateComplete;
    expect(dialog.isDirtyState).toBe(true);

    await invokeInternal<Promise<void>>(dialog, "_save");
    await dialog.updateComplete;

    expect(hass.connection.sendMessagePromise).toHaveBeenCalledTimes(1);
    expect(internals._apiKey).toBe("");
    expect(dialog.isDirtyState).toBe(false);
    expect(window.isDirtyState).toBe(false);
  });

  it("clears the draft and dirty registration on explicit close", async () => {
    const { dialog, internals } = await createDialog();
    await addUnsavedDraft(dialog);
    await dialog.updateComplete;
    expect(window.isDirtyState).toBe(true);

    invokeInternal<undefined>(dialog, "_closed");
    await dialog.updateComplete;

    expect(internals._apiKey).toBe("");
    expect(internals._provider).toBe("osm");
    expect(dialog.isDirtyState).toBe(false);
    expect(window.isDirtyState).toBe(false);
  });

  it("clears the draft and unregisters dirty state on disconnect", async () => {
    const { dialog, internals } = await createDialog();
    await addUnsavedDraft(dialog);
    await dialog.updateComplete;
    expect(window.isDirtyState).toBe(true);

    dialog.remove();
    await dialog.updateComplete;

    expect(internals._apiKey).toBe("");
    expect(window.isDirtyState).toBe(false);
  });
});
