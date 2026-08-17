# Two stages: build the bundle with node, serve it with nginx. The runtime
# image carries no node_modules and no toolchain -- just static files.

FROM node:20-alpine AS build
WORKDIR /app

# npm ci, not npm install: it installs exactly the lockfile and fails if
# package.json and package-lock.json disagree, so a container build can never
# silently resolve a different dependency tree than a developer's machine.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines env at BUILD time -- there is no runtime config for a static
# bundle, so the API origins have to be known here. These are the origins the
# BROWSER will call, not compose-internal service names: the browser runs on
# the host, so it reaches the services through their published host ports. The
# defaults therefore match src/api.ts and need no override for a local run.
# For a real deployment, pass the public URLs as build args.
ARG VITE_YARD_API=http://127.0.0.1:8001
ARG VITE_PROCUREMENT_API=http://127.0.0.1:8002
ARG VITE_GATEWAY_API=http://127.0.0.1:8003
ARG VITE_SIMULATOR_API=http://127.0.0.1:8004
# Unset is a supported state: Track.tsx renders a placeholder panel instead of
# the map and everything else still works.
ARG VITE_MAPBOX_TOKEN=
ARG VITE_MAPBOX_STYLE=
ENV VITE_YARD_API=$VITE_YARD_API \
    VITE_PROCUREMENT_API=$VITE_PROCUREMENT_API \
    VITE_GATEWAY_API=$VITE_GATEWAY_API \
    VITE_SIMULATOR_API=$VITE_SIMULATOR_API \
    VITE_MAPBOX_TOKEN=$VITE_MAPBOX_TOKEN \
    VITE_MAPBOX_STYLE=$VITE_MAPBOX_STYLE

# `npm run build` is `tsc --noEmit && vite build`, so a type error fails the
# image build rather than shipping a broken bundle.
RUN npm run build


FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

# 5173, matching the Vite dev server, so the demo URL, the README and the
# services' default CORS_ORIGINS all stay true whether the frontend is running
# in a container or under `npm run dev`.
EXPOSE 5173
