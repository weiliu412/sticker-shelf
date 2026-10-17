FROM node:22-alpine

WORKDIR /app
RUN apk add --no-cache python3

COPY package.json pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile --prod

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8787

CMD ["node", "mcp-server.mjs"]
