# plangraph MCP server for L1/L2 sessions (D-708 A, PG-TOKENS)

Keeper: `127.0.0.1:8787` on coding.vm (localhost only; workstation/Proxmox sessions cannot reach it yet).
Each principal has its own token file `~/.config/plangraph/<name>.token` (mode 600; never print it).

| principal | role | projects |
|---|---|---|
| server-L1-main | l1 | server |
| autoos-L1-main | l1 | autoos |
| server-L1-routing | l1 | routing |
| invest-L1-main | l1 | invest |
| greatwiki-L1-main | l1 | great-wiki |
| chaptarr-L1 | l1 | server |
| l2-coding | l1 | autoos, server, plangraph, great-wiki, invest, routing |
| plangraph-L1-main | l1 | plangraph |
| routing-00 | router | all |

Add to the session's MCP config (`mcpServers`), replacing `<name>`:

```json
"plangraph": {
  "type": "stdio",
  "command": "python3",
  "args": ["-m", "plangraph.keeper.mcp"],
  "env": {
    "PYTHONPATH": "/home/s/code/plangraph/.claude/worktrees/proposal/src",
    "PG_URL": "http://127.0.0.1:8787",
    "PG_TOKEN_FILE": "/home/s/.config/plangraph/<name>.token"
  }
}
```

Check: the `whoami` tool returns your name and projects. Write tools `create` / `edit` arrive with PG-MCP-WRITE
(l1: own projects, open items only; router: all). Until then L1s send batch lines to plangraph-L1.
A `spawner` principal follows with PG-MODELS (new role).
