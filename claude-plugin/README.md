# Logic Pro MCP (Claude plugin)

Lets Claude drive Logic Pro on your Mac: transport, tracks, mixer levels, plug-in parameters, bounce and stems, delivery to your own cloud storage through your own rclone setup, level measuring and balancing. Every change is read back from Logic and reported as confirmed, uncertain or failed. The plugin adds one MCP server (12 tools, 7 resources) and one skill that teaches Claude how to use them. It runs locally on your Mac.

## Install

In Claude Code:

```
/plugin install logic-pro-mcp --marketplace rosasynthesiz/logic-pro-mcp
```

## Requirements

- macOS with Logic Pro (desktop or Creator Studio).
- [uv](https://docs.astral.sh/uv/getting-started/installation/) installed. See that page for install options.
- Python 3.12 already installed. The plugin starts with `--no-python-downloads`, so uv will not fetch a Python for you.
- A one-time setup in Logic Pro (control surface) and in System Settings > Privacy & Security (Accessibility and Automation > System Events for the app that runs Claude).

Claude can check the setup for you: ask it to run the `logic_system` health check. The standalone doctor command is described in the GitHub README.

## Links

- Product page: https://rosasynthesiz.com/products/logic-pro-mcp
- Setup guide and full tool list: https://github.com/rosasynthesiz/logic-pro-mcp#readme

MIT licensed.
