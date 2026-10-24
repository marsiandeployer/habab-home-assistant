import { beforeEach, describe, expect, it, vi } from "vitest";
import type { HassEntity } from "home-assistant-js-websocket";
import { computeFavoriteCardConfig } from "../../../../src/panels/lovelace/strategies/helpers/favorite-cards";
import { CommonControlsSectionStrategy } from "../../../../src/panels/lovelace/strategies/usage_prediction/common-controls-section-strategy";
import type { HomeAssistant } from "../../../../src/types";

const { getCommonControlsUsagePrediction } = vi.hoisted(() => ({
  getCommonControlsUsagePrediction: vi.fn(),
}));

vi.mock("../../../../src/data/usage_prediction", () => ({
  getCommonControlsUsagePrediction,
}));

const state = (entityId: string): HassEntity =>
  ({
    entity_id: entityId,
    state: "on",
    attributes: { supported_features: 0 },
    last_changed: "",
    last_updated: "",
    context: { id: "", parent_id: null, user_id: null },
  }) as HassEntity;

const hassWith = (
  components: string[],
  entityIds: string[],
  entities: HomeAssistant["entities"] = {}
): HomeAssistant =>
  ({
    config: { components },
    states: Object.fromEntries(
      entityIds.map((entityId) => [entityId, state(entityId)])
    ),
    entities,
    localize: (key: string) => key,
  }) as unknown as HomeAssistant;

describe("common-controls section strategy", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows pinned favorites when usage prediction is not loaded", async () => {
    const hass = hassWith([], ["vacuum.robot"]);

    const section = await CommonControlsSectionStrategy.generate(
      {
        type: "common-controls",
        include_entities: ["vacuum.robot"],
        hide_empty: true,
      },
      hass
    );

    expect(section.disabled).toBeUndefined();
    expect(section.cards).toEqual([computeFavoriteCardConfig("vacuum.robot")]);
    expect(getCommonControlsUsagePrediction).not.toHaveBeenCalled();
  });

  it("hides an empty section when there are no valid favorites and prediction is unavailable", async () => {
    const hass = hassWith([], []);

    const section = await CommonControlsSectionStrategy.generate(
      {
        type: "common-controls",
        include_entities: ["vacuum.missing"],
        hide_empty: true,
      },
      hass
    );

    expect(section.disabled).toBe(true);
    expect(section.cards).toEqual([
      {
        type: "markdown",
        content: "ui.panel.lovelace.strategy.common_controls.not_loaded",
      },
    ]);
    expect(getCommonControlsUsagePrediction).not.toHaveBeenCalled();
  });

  it("keeps predicted controls while retaining pinned favorites and exclusions", async () => {
    getCommonControlsUsagePrediction.mockResolvedValue({
      entities: [
        "light.predicted",
        "vacuum.robot",
        "light.hidden",
        "switch.excluded",
      ],
    });
    const hass = hassWith(
      ["usage_prediction"],
      ["vacuum.robot", "light.predicted", "light.hidden", "switch.excluded"],
      {
        "light.hidden": { entity_id: "light.hidden", labels: [], hidden: true },
      }
    );

    const section = await CommonControlsSectionStrategy.generate(
      {
        type: "common-controls",
        include_entities: ["vacuum.robot"],
        exclude_entities: ["switch.excluded"],
      },
      hass
    );

    expect(section.cards?.map((card) => card.entity)).toEqual([
      "vacuum.robot",
      "light.predicted",
    ]);
    expect(getCommonControlsUsagePrediction).toHaveBeenCalledOnce();
  });
});

describe("favorite vacuum tile", () => {
  it("uses the built-in robot icon and native start, stop and dock controls with a readable full-width label", () => {
    expect(computeFavoriteCardConfig("vacuum.robot")).toEqual({
      type: "tile",
      entity: "vacuum.robot",
      icon: "mdi:robot-vacuum",
      show_entity_picture: false,
      state_content: ["state"],
      features: [
        {
          type: "vacuum-commands",
          commands: ["start_pause", "stop", "return_home"],
        },
      ],
      features_position: "bottom",
      grid_options: { columns: 12, rows: 2 },
    });
  });
});
