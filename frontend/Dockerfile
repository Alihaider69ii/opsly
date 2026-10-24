# The dashboard. Build the chat widget first (it is a git-ignored artefact the "Website chat" page
# serves):  cd widget && npm ci && npm run build   (writes frontend/public/widget.js)
#
#   docker build \
#     --build-arg NEXT_PUBLIC_API_URL=https://api.example.com \
#     --build-arg NEXT_PUBLIC_APP_URL=https://app.example.com \
#     -t bizflow-frontend frontend
#
# NEXT_PUBLIC_* values are compiled into the pages for every visitor: nothing secret belongs in them.
# The server-side API_URL is read at run time.
FROM node:22-alpine

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

WORKDIR /app

COPY --chown=node:node package*.json ./
# devDependencies (TypeScript, Tailwind) are needed to build.
RUN NODE_ENV=development npm ci

COPY --chown=node:node . .

ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_APP_URL=http://localhost:3000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL

# Fail the build rather than ship a dashboard whose "Website chat" page serves a missing script.
RUN test -s public/widget.js || (echo "public/widget.js is missing: build the widget first (cd widget && npm ci && npm run build)" >&2; exit 1)

RUN npm run build && chown -R node:node /app/.next

USER node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD wget -q -O /dev/null --spider http://127.0.0.1:3000/login || exit 1

CMD ["npm", "start"]
