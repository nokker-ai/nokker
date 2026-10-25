# Editors: ACP and VS Code

Nokker Lokkal runs a coding session in a folder of your machine. Besides the `nokker` terminal client, two editor
integrations talk to the same local kernel, with the same local token, the same permission rules and the same approval
cards.

> **Rolling out.** The VS Code extension is not published on the Visual Studio Marketplace or Open VSX yet; build it
> locally (below).

## Agent Client Protocol: `nokker acp`

`nokker acp` speaks the [Agent Client Protocol](https://agentclientprotocol.com) (protocol version 1, JSON-RPC 2.0, one
JSON message per line on stdin/stdout). Editors that support ACP agents, such as Zed, can start it.

| ACP | Nokker |
|---|---|
| `session/new {cwd}` | a new local coding session for the editor's folder (the folder must be under a root declared by Lokkal) |
| `session/load`, `session/list` | resume a local session of that folder; its history is replayed |
| `session/prompt` | one turn, streamed; images are attached; embedded files become context; file links become `@path` |
| `tool_call` / `tool_call_update` | every tool call with its kind, locations and redacted input; edits carry a `diff` |
| `session/request_permission` | the approval card: *Once*, *For this session*, *Always* (the exact rule is shown) or *Reject* |
| strong card | never offered to the editor. The editor says where it waits: "waits for your phone" when the machine is linked and the card went to your account (**rolling out**, see [gateway.md](gateway.md#approving-from-your-phone)); otherwise "decide it in the terminal (`nokker`)" (`/approvals`) |
| `session/cancel` | stops the turn in the kernel |
| `/cost`, `/diff`, `/plan`, `/checkpoints` | answered without a model call |

A prompt ends only when its follow-ups are done: when you approve a card, the kernel runs the tool and continues the
turn. Each turn ends with a small cost line (tokens, and price when the model has one). MCP servers offered by the editor
are ignored: connectors live in the kernel.

### Zed

Open `agent: open settings` → External Agents → Add Custom Agent, or edit `settings.json`:

    {
      "agent_servers": {
        "Nokker": { "type": "custom", "command": "nokker", "args": ["acp"], "env": {} }
      }
    }

Use an absolute path (`~/.nokker/bin/nokker`) if `nokker` is not on Zed's `PATH`, and add
`"--server", "http://127.0.0.1:<port>"` to `args` if your kernel does not listen on the port written by the installer.
Start a thread with New Thread → Nokker. Logs: `dev: open acp logs` in Zed, `nokker logs --component cli` for Nokker.

## VS Code extension

A chat panel on the coding session of the open folder:

- live replies, tool calls with their effect, sub-agents;
- **patches in VS Code's native diff editor** ("View diff" under each edit);
- **approval cards** with the same choices as the terminal client; a strong card is not decided in VS Code: the
  panel says whether it waits for your phone or for the terminal (see above);
- a cost line under each turn;
- **Send selection** (editor context menu, `Ctrl+Alt+N` / `Cmd+Alt+N`): file and lines are added as context to your next
  message;
- `@image.png` in a message attaches that image from the folder; **Stop** cancels the turn.

The extension reads the kernel address and the local token exactly where `nokker` does (`~/.config/nokker/lokkal.json`,
written by the installer). The token is never stored in VS Code settings, which Settings Sync copies between machines,
and is only sent to a kernel on `127.0.0.1`. The only setting, `nokker.serverUrl`, is machine-scoped. The panel has a
strict Content Security Policy and loads nothing remote. It requires a trusted workspace.

Until the Marketplace release, build and install the package locally from the `nokker-apps` repository:

    pnpm install --frozen-lockfile
    pnpm --filter nokker-vscode package
    code --install-extension apps/vscode/nokker-vscode-0.1.0.vsix
