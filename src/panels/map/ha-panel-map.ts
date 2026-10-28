import { mdiCogOutline, mdiPencil } from "@mdi/js";
import type { CSSResultGroup, PropertyValues } from "lit";
import { css, html, LitElement } from "lit";
import { customElement, property } from "lit/decorators";
import { fireEvent } from "../../common/dom/fire_event";
import { computeStateDomain } from "../../common/entity/compute_state_domain";
import { getEntityLocation } from "../../common/entity/get_entity_location";
import { navigate } from "../../common/navigate";
import "../../components/ha-icon-button";
import "../../components/ha-top-app-bar-fixed";
import "../../components/map/ha-map";
import { haStyle } from "../../resources/styles";
import type { HomeAssistant } from "../../types";
import { yandexMapMessages } from "../../common/map/yandex-map";

@customElement("ha-panel-map")
class HaPanelMap extends LitElement {
  @property({ attribute: false }) public hass!: HomeAssistant;

  @property({ type: Boolean }) public narrow = false;

  private _entities: string[] = [];

  protected render() {
    return html`
      <ha-top-app-bar-fixed .narrow=${this.narrow}>
        <div slot="title">${this.hass.localize("panel.map")}</div>
        ${
          !__DEMO__
            ? html`
                <ha-icon-button
                  slot="actionItems"
                  .label=${yandexMapMessages(this.hass.language).settingsTitle}
                  .path=${mdiCogOutline}
                  @click=${this._openMapSettings}
                ></ha-icon-button>
                ${
                  this.hass.user?.is_admin
                    ? html`<ha-icon-button
                        slot="actionItems"
                        .label=${this.hass.localize("ui.panel.map.edit_zones")}
                        .path=${mdiPencil}
                        @click=${this._openZonesEditor}
                      ></ha-icon-button>`
                    : ""
                }
              `
            : ""
        }
        <ha-map .entities=${this._entities} auto-fit interactive-zones></ha-map>
      </ha-top-app-bar-fixed>
    `;
  }

  private _openZonesEditor() {
    navigate("/config/zone?historyBack=1");
  }

  private _openMapSettings() {
    fireEvent(this, "show-dialog", {
      dialogTag: "dialog-yandex-map-settings",
      dialogImport: () => import("./dialog-yandex-map-settings"),
    });
  }

  public willUpdate(changedProps: PropertyValues<this>) {
    super.willUpdate(changedProps);
    if (!changedProps.has("hass")) {
      return;
    }
    const oldHass = changedProps.get("hass") as HomeAssistant | undefined;
    this._getStates(oldHass);
  }

  private _getStates(oldHass?: HomeAssistant) {
    let changed = false;
    const personSources = new Set<string>();
    const locationEntities: string[] = [];
    Object.values(this.hass!.states).forEach((entity) => {
      if (entity.state === "home") {
        return;
      }
      if (!getEntityLocation(entity, this.hass!.states)) {
        return;
      }
      locationEntities.push(entity.entity_id);
      if (computeStateDomain(entity) === "person" && entity.attributes.source) {
        personSources.add(entity.attributes.source);
      }
      if (oldHass?.states[entity.entity_id] !== entity) {
        changed = true;
      }
    });

    if (changed) {
      this._entities = locationEntities.filter(
        (entity) => !personSources.has(entity)
      );
    }
  }

  static get styles(): CSSResultGroup {
    return [
      haStyle,
      css`
        ha-map {
          height: calc(100vh - var(--header-height));
        }
      `,
    ];
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "ha-panel-map": HaPanelMap;
  }
}
