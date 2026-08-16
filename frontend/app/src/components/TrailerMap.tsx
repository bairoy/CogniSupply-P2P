import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Map, {
  AttributionControl,
  Layer,
  Marker,
  NavigationControl,
  Popup,
  Source,
  type MapRef,
} from "react-map-gl/mapbox";
import "mapbox-gl/dist/mapbox-gl.css";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Icon, Spinner, statusTone } from "./ui";

/**
 * Live shipment visibility map -- the control tower's fleet view.
 *
 * Same mapping stack as the customer tracker (Track.tsx): Mapbox GL JS via
 * react-map-gl, so the two maps in the product read as one map. A public pk.*
 * token is a client-side credential by design; it reaches the bundle only
 * because it is VITE_-prefixed.
 *
 * No token is a soft failure, not a crash: the panel says so and every other
 * part of the control tower still renders (see BUILD_PLAN §9). Tiles are
 * fetched from Mapbox, so this is the one panel that needs internet.
 */

interface MapTrailer {
  id: string;
  status: string;
  eta: string | null;
  priority: string;
  po_id: string | null;
  carrier: string | null;
  dock_id: string | null;
  latitude: number | null;
  longitude: number | null;
  origin: { latitude: number | null; longitude: number | null };
  destination: { latitude: number | null; longitude: number | null };
}

interface MapLocation {
  id: string;
  name: string;
  type: string;
  latitude: number;
  longitude: number;
}

const STATUS_COLOUR: Record<string, string> = {
  EN_ROUTE: "#3525cd",
  ARRIVED: "#065f46",
  DOCKED: "#4f46e5",
  DELAYED: "#ba1a1a",
};

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN as string | undefined;
// `||` not `??`: a key present but blank (VITE_MAPBOX_STYLE= as .env.example
// ships it) is "", which ?? would happily pass through to mapStyle.
const MAP_STYLE = import.meta.env.VITE_MAPBOX_STYLE || "mapbox://styles/mapbox/standard";

// Left/right clear the legend chips and the +/- control; top clears the
// warehouse pin's name chip, which is drawn above its anchor.
const FIT_PADDING = { top: 56, bottom: 32, left: 48, right: 48 };

// Fallback frame: the Bhiwandi DC, the network's convergence point. Zoom 4.2
// rather than 8 because the supplier network spans Jamshedpur to Hosur -- at
// city zoom the map opens on an empty Maharashtra with every trailer off-screen.
const FALLBACK_VIEW = { longitude: 73.0631, latitude: 19.2967, zoom: 4.2 };

type LngLat = [number, number];

export default function TrailerMap({ height = 420 }: { height?: number }) {
  const [trailers, setTrailers] = useState<MapTrailer[]>([]);
  const [locations, setLocations] = useState<MapLocation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [popup, setPopup] = useState<
    null | { kind: "trailer"; id: string } | { kind: "location"; id: string }
  >(null);
  const mapRef = useRef<MapRef>(null);
  const fitted = useRef(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .gateway<{ trailers: MapTrailer[]; locations: MapLocation[] }>("/map/trailers")
        .then((d) => {
          if (!alive) return;
          setTrailers(d.trailers);
          setLocations(d.locations);
          setLoaded(true);
        })
        .catch((e) => alive && setError(e.message));
    load();
    const timer = window.setInterval(load, 10000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const positioned = useMemo(
    () => trailers.filter((t) => t.latitude !== null && t.longitude !== null),
    [trailers],
  );

  /** Dashed trailer -> destination legs, one FeatureCollection, coloured per feature. */
  const legs = useMemo(
    () => ({
      type: "FeatureCollection" as const,
      features: positioned
        .filter((t) => t.destination.latitude !== null && t.destination.longitude !== null)
        .map((t) => ({
          type: "Feature" as const,
          properties: { colour: STATUS_COLOUR[t.status] ?? "#3525cd" },
          geometry: {
            type: "LineString" as const,
            coordinates: [
              [t.longitude as number, t.latitude as number],
              [t.destination.longitude as number, t.destination.latitude as number],
            ],
          },
        })),
    }),
    [positioned],
  );

  /**
   * Frame everything once, on the first fix that arrives -- not on every poll.
   * Positions move every 10s, and a map that re-fits under the operator's hands
   * while they are reading it is unusable.
   */
  const bounds = useMemo(() => {
    const pts: LngLat[] = [
      ...positioned.map((t) => [t.longitude as number, t.latitude as number] as LngLat),
      ...locations.map((l) => [l.longitude, l.latitude] as LngLat),
    ];
    if (!pts.length) return null;
    const lons = pts.map((p) => p[0]);
    const lats = pts.map((p) => p[1]);
    return [
      [Math.min(...lons), Math.min(...lats)],
      [Math.max(...lons), Math.max(...lats)],
    ] as [LngLat, LngLat];
  }, [positioned, locations]);

  useEffect(() => {
    if (!bounds || fitted.current) return;
    const gl = mapRef.current?.getMap();
    if (!gl) return;
    fitted.current = true;
    gl.fitBounds(bounds, { padding: FIT_PADDING, duration: 0, maxZoom: 7 });
  }, [bounds]);

  /**
   * Mapbox Standard is configured through the style, not through paint props.
   * Dropping POI/transit labels leaves the fleet as the only thing competing
   * for attention. Guarded: on a classic style these calls simply do not apply.
   */
  const onMapLoad = useCallback(() => {
    const gl = mapRef.current?.getMap();
    if (!gl?.setConfigProperty) return;
    try {
      gl.setConfigProperty("basemap", "lightPreset", "day");
      gl.setConfigProperty("basemap", "showPointOfInterestLabels", false);
      gl.setConfigProperty("basemap", "showTransitLabels", false);
    } catch {
      /* classic style (light-v11 etc.) has no basemap fragment -- fine */
    }
  }, []);

  if (error)
    return (
      <div className="p-5 text-body-sm text-on-surface-variant">
        Shipment visibility unavailable: {error}
      </div>
    );
  if (!loaded) return <Spinner label="Loading shipment positions" />;

  const legend = (
    <div className="pointer-events-none absolute right-3 top-3 z-[400] flex flex-wrap gap-2 rounded-lg bg-surface-container-lowest/95 p-2 shadow-overlay">
      {["EN_ROUTE", "ARRIVED", "DOCKED"].map((s) => (
        <Badge key={s} tone={statusTone(s)}>
          {s.replace("_", " ").toLowerCase()}
        </Badge>
      ))}
    </div>
  );

  if (!MAPBOX_TOKEN)
    return (
      <div
        style={{ height }}
        className="flex flex-col items-center justify-center gap-1 bg-surface-container-low text-center"
      >
        <Icon name="map" className="!text-[28px] text-outline" />
        <p className="text-body-sm text-on-surface-variant">
          Live map unavailable — no mapping token configured.
        </p>
        <p className="text-body-sm text-outline">
          Set <span className="mono">VITE_MAPBOX_TOKEN</span> to enable it.
        </p>
      </div>
    );

  const openTrailer =
    popup?.kind === "trailer" ? positioned.find((t) => t.id === popup.id) ?? null : null;
  const openLocation =
    popup?.kind === "location" ? locations.find((l) => l.id === popup.id) ?? null : null;

  return (
    <div style={{ height }} className="relative z-0">
      <Map
        ref={mapRef}
        mapboxAccessToken={MAPBOX_TOKEN}
        mapStyle={MAP_STYLE}
        initialViewState={
          bounds
            ? { bounds, fitBoundsOptions: { padding: FIT_PADDING, maxZoom: 7 } }
            : FALLBACK_VIEW
        }
        // Mercator, not Standard's default globe: fitBounds is exact in
        // mercator, and a curved horizon on a domestic fleet reads as
        // decoration rather than information.
        projection={{ name: "mercator" }}
        // The panel sits mid-page: grabbing the wheel would trap the operator's
        // scroll. Pan and the +/- control stay available.
        scrollZoom={false}
        dragRotate={false}
        touchPitch={false}
        attributionControl={false}
        onLoad={onMapLoad}
        reuseMaps
        style={{ width: "100%", height: "100%" }}
      >
        <NavigationControl position="top-left" showCompass={false} />
        {/* Mapbox terms require visible attribution. Compact keeps it to an (i)
            disc instead of a line of text across the map. */}
        <AttributionControl compact position="bottom-right" />

        <Source id="trailer-legs" type="geojson" data={legs}>
          <Layer
            id="trailer-legs"
            type="line"
            slot="middle"
            layout={{ "line-cap": "round" }}
            paint={{
              "line-color": ["get", "colour"],
              "line-width": 1.6,
              "line-opacity": 0.4,
              // dashed throughout: this is the straight bearing to the
              // destination, never a surveyed road route
              "line-dasharray": [2, 3],
            }}
          />
        </Source>

        {locations.map((l) => {
          const hub = l.type === "WAREHOUSE";
          return (
            <Marker
              key={l.id}
              longitude={l.longitude}
              latitude={l.latitude}
              anchor="center"
              onClick={() => setPopup({ kind: "location", id: l.id })}
            >
              {hub ? (
                <span className="flex cursor-pointer flex-col items-center gap-1">
                  <span className="grid h-8 w-8 place-items-center rounded-full border-[2.5px] border-success bg-white text-success shadow-[0_4px_12px_rgb(15_23_42/0.28)]">
                    <Icon name="warehouse" className="!text-[16px]" />
                  </span>
                  <span className="max-w-[140px] truncate rounded-full bg-white/95 px-2 py-0.5 text-[11px] font-semibold text-on-surface shadow-[0_2px_8px_rgb(15_23_42/0.18)] backdrop-blur">
                    {l.name}
                  </span>
                </span>
              ) : (
                <span className="block h-2.5 w-2.5 cursor-pointer rounded-full border-2 border-outline bg-surface-container-high shadow-[0_1px_4px_rgb(15_23_42/0.3)]" />
              )}
            </Marker>
          );
        })}

        {positioned.map((t) => {
          const colour = STATUS_COLOUR[t.status] ?? "#3525cd";
          return (
            <Marker
              key={t.id}
              longitude={t.longitude as number}
              latitude={t.latitude as number}
              anchor="center"
              onClick={() => setPopup({ kind: "trailer", id: t.id })}
            >
              <span
                className="grid h-[26px] w-[26px] cursor-pointer place-items-center rounded-full border-2 border-white text-white shadow-[0_3px_10px_rgb(15_23_42/0.35)]"
                style={{ backgroundColor: colour }}
              >
                <Icon name="local_shipping" className="!text-[14px]" />
              </span>
            </Marker>
          );
        })}

        {openLocation && (
          <Popup
            longitude={openLocation.longitude}
            latitude={openLocation.latitude}
            anchor="bottom"
            offset={18}
            closeButton={false}
            onClose={() => setPopup(null)}
          >
            <span className="text-body-sm font-semibold">{openLocation.name}</span>
            <span className="block text-body-sm lowercase text-on-surface-variant">
              {openLocation.type}
            </span>
          </Popup>
        )}

        {openTrailer && (
          <Popup
            longitude={openTrailer.longitude as number}
            latitude={openTrailer.latitude as number}
            anchor="bottom"
            offset={20}
            closeButton={false}
            onClose={() => setPopup(null)}
          >
            <div className="min-w-[180px]">
              <span className="text-body-sm font-semibold">{openTrailer.id}</span>
              <span className="block text-body-sm text-on-surface-variant">
                {openTrailer.carrier ?? "—"}
              </span>
              <span className="block text-body-sm text-on-surface-variant">
                Status: {openTrailer.status}
              </span>
              {openTrailer.dock_id && (
                <span className="block text-body-sm text-on-surface-variant">
                  Dock door: {openTrailer.dock_id}
                </span>
              )}
              {openTrailer.eta && (
                <span className="block text-body-sm text-on-surface-variant">
                  ETA: {new Date(openTrailer.eta).toLocaleString()}
                </span>
              )}
              <Link
                to={`/track/${openTrailer.id}`}
                className="mt-1 block text-body-sm font-medium text-primary"
              >
                Track this consignment →
              </Link>
            </div>
          </Popup>
        )}
      </Map>

      {legend}
    </div>
  );
}
