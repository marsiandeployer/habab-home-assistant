import type { maplibreGL } from "@maplibre/maplibre-gl-leaflet";
import type { Map as LeafletMap, TileLayerOptions } from "leaflet";
import type { setWorkerUrl, StyleSpecification } from "maplibre-gl";
import { deepEqual } from "../util/deep-equal";
import type { LeafletModuleType } from "../dom/setup-leaflet-map";
import type { ResolvedMapStyle } from "./map-styles";
import type { MapBaseMapConfig, MapBaseMapError } from "./map-engine";
import {
  buildYandexRasterStyle,
  classifyYandexTileStatus,
  yandexTilesAttribution,
  yandexTilesUrl,
  YANDEX_TILE_SOURCE_ID,
  YANDEX_TILES_ORIGIN,
} from "./yandex-map";
import {
  MAP_TILES_PATH,
  mapTilesUrl,
  refreshMapTilesToken,
  subscribeMapTilesToken,
  withMapTilesToken,
} from "../../data/map_tiles";

// The default style, written out by build-scripts/gulp/map-assets.js; every
// other one is built in the browser. The attribution comes from the TileJSON,
// deliberately: it follows whoever serves the tiles.
const SHIPPED_STYLES = {
  light: "/static/map/light.json",
  dark: "/static/map/dark.json",
} as const;

// MapLibre needs WebGL2 even for raster, so the fallback stays a Leaflet layer.
// OSM serves no @2x variant.
const RASTER_TILE_URL = `${MAP_TILES_PATH}/raster/{z}/{x}/{y}.png?token={token}`;
// The demo has no proxy to go through. Upstream serves raster to a browser that
// identifies itself with a referrer, which the demo page's `same-origin` meta
// policy strips again unless the tiles ask for it back.
const DEMO_RASTER_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const OSM_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// Browsers keep about 16 live WebGL contexts and drop the oldest, which a
// dashboard full of map cards hits. A transient loss is restored, hence a grace.
export const CONTEXT_RESTORE_GRACE = 2000;
export const RECOVERY_THROTTLE = 30000;

// On the map, not the layer: marker clustering throws without a maximum. The
// floor is 1 because at Leaflet zoom 0 the adapter drives MapLibre to -1.
export const MAP_MIN_ZOOM = 1;
export const MAP_MAX_ZOOM = 20;
// OSM's raster stops at 19 and the proxy refuses higher, so Leaflet scales the
// last level up rather than asking for tiles that are not there.
const RASTER_MAX_NATIVE_ZOOM = 19;

// Leaflet substitutes any option into the URL template; its types do not.
type TokenTileLayerOptions = TileLayerOptions & { token?: string };

export interface MapBaseLayer {
  // A no-op for raster, which has no dark variant and is inverted in CSS.
  setMapStyle: (style: ResolvedMapStyle) => void;
  setBaseMap: (baseMap: MapBaseMapConfig) => void;
}

export const isYandexTilesUrl = (url: string): boolean => {
  try {
    const parsed = new URL(url, location.href);
    return (
      parsed.origin === YANDEX_TILES_ORIGIN &&
      parsed.pathname.startsWith("/v1/tiles/")
    );
  } catch {
    return false;
  }
};

let webGL2Supported: boolean | undefined;

// Rules out iOS below 15, older Android tablets and blocklisted drivers.
export const supportsWebGL2 = (): boolean => {
  if (webGL2Supported === undefined) {
    try {
      const context = document.createElement("canvas").getContext("webgl2");
      webGL2Supported = Boolean(context);
      // Contexts are scarce; the probe must not keep one.
      context?.getExtension("WEBGL_lose_context")?.loseContext();
    } catch {
      webGL2Supported = false;
    }
  }
  return webGL2Supported;
};

// The one gate for MapLibre, whether through the Leaflet adapter or the
// engine `ha-map` drives directly. Its worker is bundled per build target
// like the rest of it, so wherever the page runs, the worker runs too: the
// legacy floor (Chrome 67 / Safari 15) guarantees the BigInt literals MapLibre
// uses, which Babel cannot transpile.
export const supportsVectorMaps = (): boolean => supportsWebGL2();

// The demo has no core to proxy through. OSM sets CORS on its tiles but not on
// its glyphs, which is why those come from VersaTiles.
const DEMO_UPSTREAM = {
  tilejson: "https://vector.openstreetmap.org/shortbread_v1/tilejson.json",
  glyphs: "https://tiles.versatiles.org/assets/glyphs/{fontstack}/{range}.pbf",
};

const useDemoUpstream = (style: StyleSpecification): StyleSpecification => {
  style.glyphs = DEMO_UPSTREAM.glyphs;
  Object.values(style.sources).forEach((source) => {
    if ("url" in source) {
      source.url = DEMO_UPSTREAM.tilejson;
    }
  });
  return style;
};

// MapLibre rejects a relative sprite URL
const absoluteSprite = (url: string) => new URL(url, location.href).href;

// Shared only while in flight, or a style that failed once would never be
// fetched again. Text rather than a parsed style: MapLibre mutates what it
// is handed.
const pendingStyles = new Map<string, Promise<string>>();

const fetchStyle = (url: string): Promise<string> => {
  let pending = pendingStyles.get(url);
  if (!pending) {
    pending = fetch(url)
      .then((response) => response.text())
      .finally(() => pendingStyles.delete(url));
    pendingStyles.set(url, pending);
  }
  return pending;
};

export const loadStyle = async (
  mapStyle: ResolvedMapStyle,
  baseMap: MapBaseMapConfig = { provider: "osm" }
): Promise<StyleSpecification> => {
  if (baseMap.provider === "yandex" && !__DEMO__) {
    return buildYandexRasterStyle(baseMap);
  }
  const shipped = mapStyle.shipped && SHIPPED_STYLES[mapStyle.shipped];

  // Anything that does not ship is built here, from the same builder: its own
  // chunk, so a map on the default style never downloads it.
  const style: StyleSpecification = shipped
    ? JSON.parse(await fetchStyle(shipped))
    : await (
        await import("./build-map-style")
      ).buildMapStyle(
        mapStyle.palette,
        mapStyle.options ?? {},
        mapStyle.baseColors
      );

  if (typeof style.sprite === "string") {
    style.sprite = absoluteSprite(style.sprite);
  } else if (Array.isArray(style.sprite)) {
    style.sprite = style.sprite.map((sprite) => ({
      ...sprite,
      url: absoluteSprite(sprite.url),
    }));
  }
  return __DEMO__ ? useDemoUpstream(style) : style;
};

// Global to MapLibre; set once before the first map is created. Absolute,
// because on Cast the page is not served from the instance. The URL comes
// from the build: the worker is an entry of its own, next to the app.
let workerUrlSet = false;
export const ensureWorkerUrl = (setUrl: typeof setWorkerUrl) => {
  if (workerUrlSet) {
    return;
  }
  workerUrlSet = true;
  setUrl(new URL(__MAPLIBRE_WORKER_URL__, location.href).href);
};

const createVectorLayer = async (
  createLayer: typeof maplibreGL,
  leaflet: LeafletModuleType,
  map: LeafletMap,
  mapStyle: ResolvedMapStyle,
  token: string | undefined,
  baseMap: MapBaseMapConfig,
  onBaseMapError?: (error: MapBaseMapError) => void
): Promise<MapBaseLayer | undefined> => {
  let layer: ReturnType<typeof maplibreGL> | undefined;

  try {
    layer = createLayer({
      style: await loadStyle(mapStyle, baseMap),
      // Absolute, or the worker fetching tiles cannot resolve them.
      transformRequest: (url) => {
        const request = withMapTilesToken(url);
        return {
          ...request,
          referrerPolicy:
            isYandexTilesUrl(url) || __DEMO__ ? "origin" : undefined,
        };
      },
    });
    // The plugin builds the MapLibre map in `onAdd`, so a refused WebGL
    // context throws here. Keep it guarded or that loses the fallback. The
    // worker is spawned asynchronously and its failures never surface, which
    // is why the worker is built to run wherever the page does.
    layer.addTo(map);
  } catch {
    if (layer) {
      try {
        layer.remove();
      } catch {
        // May never have finished being added.
      }
    }
    return undefined;
  }

  // Tracked apart so a failed request rolls back to what is displayed, not to
  // whatever was asked for before it - which with several in flight differs.
  let appliedStyle = mapStyle;
  let requestedStyle = mapStyle;
  // Styles are fetched, so only the newest request may touch the map.
  let latestRequest = 0;
  let vector = true;
  let refused = false;
  // A fallback hands this to the raster layer, and a rotation that already
  // happened is not announced again.
  let currentToken = token;
  let currentBaseMap = baseMap;
  let rasterFallback: MapBaseLayer | undefined;

  const glMap = layer.getMaplibreMap();
  let fallbackTimeout: number | undefined;
  let contextLost = false;

  // Declared first, but only ever called once all three exist.
  const handleVisibilityChange = () => {
    if (contextLost) {
      scheduleSwap();
    }
  };

  const swapToRaster = () => {
    vector = false;
    document.removeEventListener("visibilitychange", handleVisibilityChange);
    try {
      layer.remove();
    } catch {
      // Nothing left to detach.
    }
    rasterFallback = createRasterLayer(
      leaflet,
      map,
      currentToken,
      currentBaseMap,
      onBaseMapError
    );
  };

  const scheduleSwap = () => {
    clearTimeout(fallbackTimeout);
    // Backgrounding drops it too, and there it comes back on return.
    if (!vector || document.hidden) {
      return;
    }
    fallbackTimeout = window.setTimeout(swapToRaster, CONTEXT_RESTORE_GRACE);
  };

  glMap.on("webglcontextlost", () => {
    contextLost = true;
    scheduleSwap();
  });
  glMap.on("webglcontextrestored", () => {
    contextLost = false;
    clearTimeout(fallbackTimeout);
  });
  document.addEventListener("visibilitychange", handleVisibilityChange);

  map.on("unload", () => {
    // Otherwise the timer revives a map that is already gone.
    clearTimeout(fallbackTimeout);
    document.removeEventListener("visibilitychange", handleVisibilityChange);
  });

  const applyStyle = (newStyle: ResolvedMapStyle) => {
    const request = ++latestRequest;

    loadStyle(newStyle, currentBaseMap)
      .then((style) => {
        if (request === latestRequest) {
          appliedStyle = newStyle;
          layer.getMaplibreMap()?.setStyle(style);
        }
      })
      .catch(() => {
        if (request === latestRequest) {
          requestedStyle = appliedStyle;
        }
      });
  };

  // A refused request leaves the source dead: the TileJSON is fetched once and
  // is never retried, so the style has to be applied again once there is a new
  // token. Throttled, or a proxy refusing for another reason loops.
  let lastRecovery = 0;
  glMap.on("error", (event) => {
    const sourceId = (event as { sourceId?: string }).sourceId;
    const status = (event.error as { status?: number } | undefined)?.status;
    if (currentBaseMap.provider === "yandex") {
      onBaseMapError?.(
        sourceId === YANDEX_TILE_SOURCE_ID
          ? classifyYandexTileStatus(status)
          : "unavailable"
      );
      return;
    }
    // 403 is a stale token, 404 the proxy not registered yet during a restart,
    // and no status at all a network failure. All three recover the same way,
    // and a token that comes back unchanged costs nothing.
    if (status !== undefined && status !== 403 && status !== 404) {
      return;
    }
    if (Date.now() - lastRecovery < RECOVERY_THROTTLE) {
      return;
    }
    lastRecovery = Date.now();
    refused = true;
    refreshMapTilesToken();
  });

  // Only a new token clears the refusal. A style change in between applies a
  // style that is refused just as the last one was, so it proves nothing.
  const unsubscribeToken = subscribeMapTilesToken((newToken) => {
    currentToken = newToken;
    if (currentBaseMap.provider === "osm" && vector && refused) {
      refused = false;
      applyStyle(requestedStyle);
    }
  });
  map.on("unload", unsubscribeToken);

  return {
    setMapStyle: (newStyle: ResolvedMapStyle) => {
      if (!vector || deepEqual(newStyle, requestedStyle)) {
        return;
      }
      requestedStyle = newStyle;
      if (currentBaseMap.provider === "yandex") {
        return;
      }
      applyStyle(newStyle);
    },
    setBaseMap: (newBaseMap: MapBaseMapConfig) => {
      if (deepEqual(newBaseMap, currentBaseMap)) {
        return;
      }
      currentBaseMap = newBaseMap;
      if (newBaseMap.provider === "yandex") {
        refused = false;
      }
      if (vector) {
        applyStyle(requestedStyle);
      } else {
        rasterFallback?.setBaseMap(newBaseMap);
      }
    },
  };
};

const createRasterLayer = (
  leaflet: LeafletModuleType,
  map: LeafletMap,
  token: string | undefined,
  baseMap: MapBaseMapConfig,
  onBaseMapError?: (error: MapBaseMapError) => void
): MapBaseLayer => {
  let currentBaseMap = baseMap;
  let refused = false;
  let lastRecovery = 0;
  let hasReportedYandexError = false;
  let layer: ReturnType<LeafletModuleType["tileLayer"]>;

  const createLayer = () => {
    const yandexConfig =
      currentBaseMap.provider === "yandex" && !__DEMO__
        ? currentBaseMap
        : undefined;
    const url = yandexConfig
      ? yandexTilesUrl(yandexConfig)
      : __DEMO__
        ? DEMO_RASTER_TILE_URL
        : mapTilesUrl(RASTER_TILE_URL);
    const attribution = yandexConfig
      ? yandexTilesAttribution(yandexConfig.language)
      : OSM_ATTRIBUTION;
    const created = leaflet
      .tileLayer(url, {
        attribution,
        maxZoom: MAP_MAX_ZOOM,
        maxNativeZoom: yandexConfig ? MAP_MAX_ZOOM : RASTER_MAX_NATIVE_ZOOM,
        referrerPolicy: yandexConfig || __DEMO__ ? "origin" : undefined,
        // Only the HA proxy URL has a token template.
        token: yandexConfig ? undefined : (token ?? ""),
      } as TokenTileLayerOptions)
      .addTo(map);

    created.on("tileerror", (event) => {
      if (currentBaseMap.provider === "yandex" && !__DEMO__) {
        if (!hasReportedYandexError) {
          const status = (event as { error?: { status?: number } }).error
            ?.status;
          hasReportedYandexError = true;
          onBaseMapError?.(classifyYandexTileStatus(status));
        }
        return;
      }
      refused = true;
      if (Date.now() - lastRecovery < RECOVERY_THROTTLE) {
        return;
      }
      lastRecovery = Date.now();
      refreshMapTilesToken();
    });
    created.on("tileload", () => {
      hasReportedYandexError = false;
    });
    return created;
  };
  layer = createLayer();

  // Substituted per request, so later HA proxy tiles use the rotated token.
  const unsubscribe = subscribeMapTilesToken((newToken) => {
    token = newToken;
    if (currentBaseMap.provider !== "osm") {
      return;
    }
    (layer.options as TokenTileLayerOptions).token = newToken;
    if (refused) {
      refused = false;
      layer.redraw();
    }
  });
  map.on("unload", unsubscribe);

  return {
    setMapStyle: () => undefined,
    setBaseMap: (newBaseMap) => {
      if (deepEqual(newBaseMap, currentBaseMap)) {
        return;
      }
      currentBaseMap = newBaseMap;
      hasReportedYandexError = false;
      refused = false;
      layer.remove();
      layer = createLayer();
    },
  };
};

export const createBaseLayer = async (
  leaflet: LeafletModuleType,
  map: LeafletMap,
  mapStyle: ResolvedMapStyle,
  token: string | undefined,
  // Skip the vector layer, e.g. after a permanent WebGL context loss
  rasterOnly = false,
  baseMap: MapBaseMapConfig = { provider: "osm" },
  onBaseMapError?: (error: MapBaseMapError) => void
): Promise<MapBaseLayer> => {
  if (!rasterOnly && supportsVectorMaps()) {
    let vectorLayer: MapBaseLayer | undefined;
    try {
      const [{ maplibreGL: createLayer }, maplibre] = await Promise.all([
        import("@maplibre/maplibre-gl-leaflet"),
        import("maplibre-gl"),
      ]);
      ensureWorkerUrl(maplibre.setWorkerUrl);
      vectorLayer = await createVectorLayer(
        createLayer,
        leaflet,
        map,
        mapStyle,
        token,
        baseMap,
        onBaseMapError
      );
    } catch {
      // No chunk, no vector map - but still a map.
    }
    if (vectorLayer) {
      return vectorLayer;
    }
  }
  return createRasterLayer(leaflet, map, token, baseMap, onBaseMapError);
};
