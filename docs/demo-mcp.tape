# MCP demo for the README: Claude Code calling punk's tools. Record with https://github.com/charmbracelet/vhs.
#
# It uses a throwaway store and project, so nothing private is recorded:
#   mkdir -p /tmp/punk-demo/app /tmp/punk-demo/home && cd /tmp/punk-demo/app && git init -q
#   git remote add origin git@github.com:you/shop.git
#   printf '# shop\n- Run tests with `make test`.\n- Integration tests need Postgres 16 running in Docker (`make db`).\n- Deploy only from main, with `make ship`.\n' > CLAUDE.md
#   HOME=/tmp/punk-demo/home PUNK_HOME=/tmp/punk-demo/store punk init --no-global --machine demo
#   HOME=/tmp/punk-demo/home PUNK_HOME=/tmp/punk-demo/store punk track . && ... punk save
#   /tmp/punk-demo/mcp.json: {"mcpServers": {"punk-records": {"command": "punk", "args": ["mcp"],
#                             "env": {"PUNK_HOME": "/tmp/punk-demo/store", "HOME": "/tmp/punk-demo/home"}}}}
#   Start `claude` once in /tmp/punk-demo/app and answer "Yes, I trust this folder".
#   cd /tmp/punk-demo && vhs demo-mcp.tape
#   Crop the Claude Code header (version, model, plan) off before publishing:
#   ffmpeg -i demo-mcp.gif -vf "crop=1200:640:0:120,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse" docs/demo-mcp.gif
#
# Only the demo MCP server is loaded (--strict-mcp-config), user settings and hooks are skipped
# (--setting-sources project), and the two punk tools are pre-approved (--allowedTools).

Output demo-mcp.gif
Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 760
Set Padding 16
Set TypingSpeed 35ms

Hide
Type 'unset $(env | grep -oE "^(CLAUDE[A-Z_]*|ANTHROPIC[A-Z_]*)") && cd /tmp/punk-demo/app'
Enter
Type 'clear && claude --strict-mcp-config --mcp-config /tmp/punk-demo/mcp.json --setting-sources project --allowedTools mcp__punk-records__get_context mcp__punk-records__append_memory'
Enter
Sleep 8s
Show

Sleep 1s
Type 'Check punk records: what should I know before running the tests here? Keep it short.'
Sleep 500ms
Enter
Sleep 40s
Type 'Remember in punk records: the staging database resets every Monday at 06:00 UTC.'
Sleep 500ms
Enter
Sleep 35s
Sleep 2s

Hide
Ctrl+C
Sleep 1s
Ctrl+C
Sleep 2s
