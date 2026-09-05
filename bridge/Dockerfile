FROM node:20-slim

WORKDIR /app

# Install deps separately so layer caching works when only index.mjs changes.
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

COPY index.mjs ./

# Volume mount lands here; first boot writes api-creds.json (gitignored).
RUN mkdir -p /app/creds

ENV NODE_ENV=production
EXPOSE 3000

CMD ["node", "index.mjs"]
