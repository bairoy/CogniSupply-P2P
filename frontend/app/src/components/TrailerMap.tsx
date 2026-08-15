import { useEffect, useState } from "react";
import { CircleMarker, MapContainer, Polyline, Popup, TileLayer } from "react-leaflet";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Spinner, statusTone } from "./ui";

/**
 * Live inbound map.
 *
 * CircleMarker rather than the default Leaflet pin: the default marker pulls
 * PNG assets over a bundler-relative path and silently renders as a broken
 * image in a Vite build. Circles need no assets and colour-code cleanly.
 *
 * Tiles come from OpenStreetMap, so this panel needs internet. Everything else
 * in the app works offline -- if the venue network is down, only this panel
 * degrades (see BUILD_PLAN §9).
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

export default function TrailerMap({ height = 420 }: { height?: number }) {
  const [trailers, setTrailers] = useState<MapTrailer[]>([]);
  const [locations, setLocations] = useState<MapLocation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

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

  if (error)
    return (
      <div className="p-5 text-body-sm text-on-surface-variant">
        Map data unavailable: {error}
      </div>
    );
  if (!loaded) return <Spinner label="Loading map" />;

  const warehouse = locations.find((l) => l.type === "WAREHOUSE");
  const centre: [number, number] = warehouse
    ? [warehouse.latitude, warehouse.longitude]
    : [41.8781, -87.6298];

  return (
    <div style={{ height }} className="relative">
      <MapContainer center={centre} zoom={8} className="h-full w-full" scrollWheelZoom={false}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />

        {locations.map((l) => (
          <CircleMarker
            key={l.id}
            center={[l.latitude, l.longitude]}
            radius={l.type === "WAREHOUSE" ? 9 : 5}
            pathOptions={{
              color: l.type === "WAREHOUSE" ? "#065f46" : "#777587",
              fillColor: l.type === "WAREHOUSE" ? "#065f46" : "#c7c4d8",
              fillOpacity: 0.9,
              weight: 2,
            }}
          >
            <Popup>
              <strong>{l.name}</strong>
              <br />
              <span style={{ textTransform: "lowercase" }}>{l.type}</span>
            </Popup>
          </CircleMarker>
        ))}

        {trailers.map((t) => {
          if (t.latitude === null || t.longitude === null) return null;
          const colour = STATUS_COLOUR[t.status] ?? "#3525cd";
          const dest =
            t.destination.latitude !== null && t.destination.longitude !== null
              ? ([t.destination.latitude, t.destination.longitude] as [number, number])
              : null;
          return (
            <div key={t.id}>
              {dest && (
                <Polyline
                  positions={[[t.latitude, t.longitude], dest]}
                  pathOptions={{ color: colour, weight: 1.5, opacity: 0.35, dashArray: "4 6" }}
                />
              )}
              <CircleMarker
                center={[t.latitude, t.longitude]}
                radius={7}
                pathOptions={{ color: colour, fillColor: colour, fillOpacity: 0.85, weight: 2 }}
              >
                <Popup>
                  <div style={{ minWidth: 190 }}>
                    <strong>{t.id}</strong>
                    <br />
                    {t.carrier ?? "—"}
                    <br />
                    Status: {t.status}
                    {t.dock_id && (
                      <>
                        <br />
                        Dock: {t.dock_id}
                      </>
                    )}
                    {t.eta && (
                      <>
                        <br />
                        ETA: {new Date(t.eta).toLocaleString()}
                      </>
                    )}
                    <br />
                    <Link to={`/track/${t.id}`}>Track this trailer →</Link>
                  </div>
                </Popup>
              </CircleMarker>
            </div>
          );
        })}
      </MapContainer>

      <div className="pointer-events-none absolute right-3 top-3 z-[400] flex flex-wrap gap-2 rounded-lg bg-surface-container-lowest/95 p-2 shadow-overlay">
        {["EN_ROUTE", "ARRIVED", "DOCKED"].map((s) => (
          <Badge key={s} tone={statusTone(s)}>
            {s.replace("_", " ").toLowerCase()}
          </Badge>
        ))}
      </div>
    </div>
  );
}
