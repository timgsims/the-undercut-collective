# node:24-alpine is already on Sentinel (no pull over its broken IPv6). No
# dependencies: HTTP is node:http, SQLite is node:sqlite.
FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production DATA_DIR=/data PORT=3000 TZ=Pacific/Auckland
COPY package.json ./
COPY src ./src
# uid 1000 = "node" here and sentinel-admin on the host, so ./data is writable
USER node
EXPOSE 3000
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:3000/healthz || exit 1
CMD ["node", "src/server.js"]
