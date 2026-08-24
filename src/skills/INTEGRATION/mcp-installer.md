---
name: mcp-installer
description: "Add and install MCP servers to ZervGen"
tags: [mcp, mcps, install, server, model-context-protocol]
---
# MCP Server Installer

MCP (Model Context Protocol) servers provide additional tools to ZervGen. Each server is a process that communicates via stdio.

## Check Current Servers

```python
result = await list_mcp_servers()
return await response(text=result)
```

## EXAMPLES

```python
result = await add_mcp_server(
    name="fetch",
    command="python",
    args='["-m", "mcp_server_fetch"]'
)
print(result)
```

```python
result = await add_mcp_server(
    name="brave-search",
    command="npx",
    args='["-y", "@modelcontextprotocol/server-brave-search"]',
    env='{"BRAVE_API_KEY": "YOUR_KEY_HERE"}'
    default=True # Add MCP as default
)
return await response(text=result)
```

## Install Dependencies First

```python
result = await shell("pip install mcp_server_fetch")
return await response(text=result)
```

## Call a tool (arguments = JSON object string)
```python
result = await mcp_execute(server="time", tool="get_current_time", arguments='{"timezone": "Europe/Minsk"}')
print(result)
```