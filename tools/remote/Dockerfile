# A container that only runs `claude -p` for meeting-copilot (MEETING_COPILOT_REMOTE).
# The copilot reaches it with: ssh <server> docker exec -i copilot-ai claude
# Nothing listens on a port: each AI call is one `docker exec` started over ssh.
FROM node:22-slim
# Pin the Claude Code version you run on your Mac, so both answer the same way.
ARG CLAUDE_VERSION=latest
# ripgrep + python3: the fast fact search runs its text search and ranking here (MEETING_COPILOT_FACT_SEARCH=remote)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates ripgrep python3 \
 && rm -rf /var/lib/apt/lists/* \
 && npm i -g "@anthropic-ai/claude-code@${CLAUDE_VERSION}" && npm cache clean --force
COPY entry.sh /usr/local/bin/copilot-ai-entry
RUN chmod 755 /usr/local/bin/copilot-ai-entry && mkdir -p /home/node/work && chown -R node:node /home/node
USER node
ENV HOME=/home/node
# an empty folder: no project settings or CLAUDE.md for claude to load
WORKDIR /home/node/work
CMD ["copilot-ai-entry"]
