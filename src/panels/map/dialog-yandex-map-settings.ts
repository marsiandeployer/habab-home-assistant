import type { CSSResultGroup } from "lit";
import { css, html, LitElement, nothing } from "lit";
import { customElement, property, state } from "lit/decorators";
import {
  saveFrontendUserData,
  withMapBasemapUserData,
} from "../../data/frontend";
import type { MapBasemapUserData } from "../../data/frontend";
import "../../components/ha-alert";
import "../../components/ha-button";
import "../../components/ha-dialog";
import "../../components/ha-dialog-footer";
import "../../components/input/ha-input";
import type { HaInput } from "../../components/input/ha-input";
import "../../components/ha-select";
import type {
  HaSelectOption,
  HaSelectSelectEvent,
} from "../../components/ha-select";
import { haStyleDialog } from "../../resources/styles";
import type { HomeAssistant } from "../../types";
import { DialogMixin } from "../../dialogs/dialog-mixin";
import { DirtyStateProviderMixin } from "../../mixins/dirty-state-provider-mixin";
import { yandexMapMessages } from "../../common/map/yandex-map";

type MapProvider = "osm" | "yandex";
interface MapSettingsDraftState {
  provider: MapProvider;
  hasApiKey: boolean;
}

@customElement("dialog-yandex-map-settings")
class DialogYandexMapSettings extends DirtyStateProviderMixin<MapSettingsDraftState>()(
  DialogMixin(LitElement)
) {
  @property({ attribute: false }) public hass!: HomeAssistant;

  @state() private _open = false;

  @state() private _provider: MapProvider = "osm";

  @state() private _apiKey = "";

  @state() private _keyConfigured = false;

  @state() private _error = "";

  @state() private _saving = false;

  public async showDialog(): Promise<void> {
    this._provider = this.hass.userData?.mapBasemap?.provider ?? "osm";
    this._keyConfigured = Boolean(this.hass.userData?.mapBasemap?.yandexApiKey);
    this._apiKey = "";
    this._error = "";
    this._saving = false;
    this._initDirtyTracking({ type: "shallow" }, this._draftState());
    this._open = true;
  }

  private _draftState(): MapSettingsDraftState {
    return {
      provider: this._provider,
      hasApiKey: this._apiKey.length > 0,
    };
  }

  private _updateDraftDirtyState(): void {
    this._updateDirtyState(this._draftState());
  }

  private get _messages() {
    return yandexMapMessages(this.hass.language);
  }

  private get _providerOptions(): HaSelectOption[] {
    return [
      { value: "osm", label: this._messages.openStreetMap },
      { value: "yandex", label: this._messages.yandexMaps },
    ];
  }

  private _providerChanged(ev: HaSelectSelectEvent): void {
    const value = ev.detail.value;
    if (value === "osm" || value === "yandex") {
      this._provider = value;
      this._error = "";
      this._updateDraftDirtyState();
    }
  }

  private _apiKeyChanged(ev: Event): void {
    this._apiKey = (ev.currentTarget as HaInput).value ?? "";
    this._error = "";
    this._updateDraftDirtyState();
  }

  private async _save(): Promise<void> {
    if (this._saving) {
      return;
    }
    const messages = this._messages;
    const enteredKey = this._apiKey.trim();
    const existingKey = this.hass.userData?.mapBasemap?.yandexApiKey;
    const apiKey = enteredKey || existingKey;
    let mapBasemap: MapBasemapUserData;
    if (this._provider === "yandex") {
      if (!apiKey) {
        this._error = messages.enterApiKey;
        return;
      }
      mapBasemap = { provider: "yandex" as const, yandexApiKey: apiKey };
    } else {
      mapBasemap = {
        provider: "osm" as const,
        ...(existingKey && { yandexApiKey: existingKey }),
      };
    }

    this._saving = true;
    this._error = "";
    try {
      await saveFrontendUserData(this.hass.connection, "core", {
        ...withMapBasemapUserData(this.hass.userData, mapBasemap),
      });
      this._provider = mapBasemap.provider;
      this._keyConfigured = Boolean(mapBasemap.yandexApiKey);
      this._apiKey = "";
      this._updateDraftDirtyState();
      this._markDirtyStateClean();
      this._saving = false;
      this._open = false;
    } catch {
      this._saving = false;
      this._error = messages.saveError;
    }
  }

  private async _removeKey(): Promise<void> {
    if (this._saving || !this._keyConfigured) {
      return;
    }
    this._saving = true;
    this._error = "";
    try {
      await saveFrontendUserData(this.hass.connection, "core", {
        ...withMapBasemapUserData(this.hass.userData, { provider: "osm" }),
      });
      this._keyConfigured = false;
      this._provider = "osm";
      this._apiKey = "";
      this._updateDraftDirtyState();
      this._markDirtyStateClean();
      this._saving = false;
      this._open = false;
    } catch {
      this._saving = false;
      this._error = this._messages.saveError;
    }
  }

  private _closed(): void {
    this._open = false;
    this._provider = this.hass.userData?.mapBasemap?.provider ?? "osm";
    this._keyConfigured = Boolean(this.hass.userData?.mapBasemap?.yandexApiKey);
    this._apiKey = "";
    this._error = "";
    this._updateDraftDirtyState();
    this._markDirtyStateClean();
  }

  public disconnectedCallback(): void {
    this._apiKey = "";
    this._error = "";
    this._updateDraftDirtyState();
    this._markDirtyStateClean();
    super.disconnectedCallback();
  }

  protected render() {
    const messages = this._messages;
    return html`
      <ha-dialog
        .open=${this._open}
        .preventScrimClose=${this.isDirtyState}
        .headerTitle=${messages.settingsTitle}
        @closed=${this._closed}
      >
        <div class="content">
          <ha-select
            .label=${messages.providerLabel}
            .options=${this._providerOptions}
            .value=${this._provider}
            @selected=${this._providerChanged}
          ></ha-select>
          ${
            this._provider === "yandex"
              ? html`
                  <ha-input
                    .label=${messages.apiKeyLabel}
                    .hint=${messages.apiKeyHelper}
                    .value=${this._apiKey}
                    type="password"
                    password-toggle
                    autocomplete="off"
                    name="yandex-maps-api-key"
                    @input=${this._apiKeyChanged}
                  ></ha-input>
                  ${
                    this._keyConfigured
                      ? html`<p class="key-configured">
                          ${messages.keyConfigured}
                        </p>`
                      : nothing
                  }
                `
              : nothing
          }
          ${
            this._error
              ? html`<ha-alert alert-type="error">${this._error}</ha-alert>`
              : nothing
          }
        </div>
        <ha-dialog-footer slot="footer">
          ${
            this._keyConfigured
              ? html`<ha-button
                  slot="secondaryAction"
                  appearance="plain"
                  .disabled=${this._saving}
                  @click=${this._removeKey}
                >
                  ${messages.removeKey}
                </ha-button>`
              : nothing
          }
          <ha-button
            slot="secondaryAction"
            appearance="plain"
            .disabled=${this._saving}
            @click=${this.closeDialog}
          >
            ${this.hass.localize("ui.common.cancel")}
          </ha-button>
          <ha-button
            slot="primaryAction"
            .disabled=${this._saving}
            @click=${this._save}
          >
            ${this.hass.localize("ui.common.save")}
          </ha-button>
        </ha-dialog-footer>
      </ha-dialog>
    `;
  }

  static get styles(): CSSResultGroup {
    return [
      haStyleDialog,
      css`
        .content {
          display: flex;
          flex-direction: column;
          gap: var(--ha-space-4);
          min-width: min(420px, 80vw);
        }

        .key-configured {
          color: var(--secondary-text-color);
          margin: 0;
        }
      `,
    ];
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "dialog-yandex-map-settings": DialogYandexMapSettings;
  }
}
