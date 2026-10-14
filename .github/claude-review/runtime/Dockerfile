FROM node:24.21.0-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20
WORKDIR /opt/claude
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force
COPY entrypoint.cjs /opt/claude/entrypoint.cjs
USER 1000:1000
WORKDIR /workspace
ENTRYPOINT ["node", "/opt/claude/entrypoint.cjs"]
