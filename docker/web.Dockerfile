# syntax=docker/dockerfile:1.7
# Build context: web/.   docker build -f docker/web.Dockerfile web
ARG NODE_VERSION=24

FROM node:${NODE_VERSION}-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

FROM node:${NODE_VERSION}-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --- runtime: Next.js standalone server, non-root -----------------------------------
FROM node:${NODE_VERSION}-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
# DAYLINE_API_URL is read by the server at runtime (BFF); it is never baked into the bundle.
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
USER node
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server.js"]

# --- dev: source bind-mounted at /app, node_modules in a named volume ------------------
FROM node:${NODE_VERSION}-slim AS dev
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1 WATCHPACK_POLLING=true
CMD ["sh", "-c", "npm ci --no-audit --no-fund && npm run dev -- -H 0.0.0.0 -p 3000"]
