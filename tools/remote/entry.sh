#!/bin/sh
# Container start: writes the MCP config from its template (values like ${OPENWA_KEY} come from the
# container's environment, so no key is kept in a second file), then waits. The AI calls are `docker exec`s.
set -eu
if [ -f /config/mcp.template.json ]; then
  umask 077
  node -e '
    const fs = require("fs");
    const t = fs.readFileSync("/config/mcp.template.json", "utf8");
    const out = t.replace(/\$\{([A-Z0-9_]+)\}/g, (m, k) => {
      if (!(k in process.env)) { console.error("mcp template: " + k + " is not set"); process.exit(1); }
      return JSON.stringify(process.env[k]).slice(1, -1);
    });
    JSON.parse(out);
    fs.writeFileSync("/home/node/mcp.json", out, { mode: 0o600 });
  '
fi
exec sleep infinity
