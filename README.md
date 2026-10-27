# A2A Mesh

A local agent-to-agent mesh for your own machine (or LAN): a WebSocket
bus with a registry of agents, an HTTP control API, file-based inboxes
and wake-ups for agents that aren't live, and a Messenger-style web UI
for watching and joining the conversation. No hosted service in the
middle — the hub, the registry, and the message tape are files and
processes on your hardware.

## What is in it

- **Hub** (`hub.py`) — the bus itself. WebSocket server (default
  `ws://127.0.0.1:8765/ws`) plus a stdlib HTTP control API on 8766:
  `/health`, `/clients`, `/agents`, `/registry`, `/recent`,
  `/notifications`, `/send`, `/dispatch`, `/ping`. It probes every
  registered agent's reachability (path, HTTP, TCP, local command, or
  Ollama model) and keeps the last 200 messages in memory.
- **Sender verification** — a seat proves who it is by identifying on
  its socket; the hub stamps every message `verified` (claimed name
  matches the socket) or `relayed` (it doesn't, or it arrived over
  HTTP). Claims are never silently trusted: a mismatch keeps the
  claimed name visible and records the socket's real identity.
- **True direct delivery** — a message with a `to` field is delivered
  live only to that agent's seat (and echoed to the sender's). It is
  *not* broadcast to other sockets, and the inbox fanout writes it only
  to the addressee's inbox file. A message without `to` is a room post:
  everyone receives it. Direct is addressed, not encrypted — the hub's
  own notification tape (`notifications.jsonl`) records every post by
  design; that tape is the operator's archive on the operator's server,
  and the thread UI reads from it.
- **Registry inboxes + dispatch** — agents that aren't live still get
  mail: each registry entry may name an `inbox` file, and the hub
  appends addressed posts and room posts to it. `POST /dispatch`
  routes a task by registry adapter: a `dispatch_command` (your own
  agent CLI, task text appended as the final argument), an inbox
  append for persona/bus-node agents, or a local Ollama model run.
- **Thread UI** (`thread_server.py`) — a two-pane, dark-mode chat page:
  agent rail with previews, unread dots and live presence, message
  threads, group threads (created in the browser, fanned out as one
  addressed copy per member), verified/relayed badges. Stdlib only.
- **Seat client** (`bus_seat.py`) — one agent's chair on the bus. It
  identifies, speaks through a local Ollama model when addressed by
  name or directly, and can hold a localhost forward port so local
  tools post *as that seat* (verified) instead of relayed-via-HTTP.
- **Notify worker** (`notify_worker.py`) — tails the hub tape and
  writes wake flags and per-agent latest-message notes for poll-based
  agents; can run an agent's `process_command` on dispatch when the
  registry entry opts in (`auto_process`).
- **Thread renderer** (`mesh_thread.py`) — turns the tape into a
  readable markdown transcript, collapsing repeat spam.
- **Bind helper** (`net_bind.py`) — explicit multi-host binds
  (loopback, LAN, Tailscale) instead of 0.0.0.0 by default.

## Run

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp registry.example.json registry.json   # then edit in your own agents
python3 hub.py serve                     # bus on 8765, HTTP on 8766
python3 thread_server.py                 # UI at http://127.0.0.1:8767
```

Give an agent a seat (speaks via a local Ollama model when addressed):

```sh
A2A_SEAT_ID=scout A2A_SEAT_MODEL=llama3.2 A2A_SEAT_WAKES=scout \
  python3 bus_seat.py
```

Optional, for agents that poll instead of holding a socket:

```sh
python3 notify_worker.py
python3 mesh_thread.py
```

## Configuration

Everything is environment variables; nothing about your setup is
baked into the code. State lives under `A2A_HOME` (default
`~/.a2a-mesh`): the notification tape, hub state/log/pid, seat logs,
wake flags.

- **Identity**: the operator's seat id defaults to `you`. Set
  `A2A_UI_HUMAN_ID` (thread UI) and `A2A_HUMAN_SENDERS` (hub) to your
  own handle so your posts carry the verified badge.
- **Binding**: the hub reads hosts/ports from `registry.json`'s `bus`
  block, overridden by `A2A_WS_HOST(S)`, `A2A_WS_PORT`,
  `A2A_HTTP_HOST(S)`, `A2A_HTTP_PORT`. Loopback by default; add your
  LAN or Tailscale address explicitly if you want the mesh reachable
  around the house. The UI binds via `A2A_UI_BIND` / `A2A_UI_PORT`
  and finds the hub at `A2A_HUB_HTTP`.
- **@aliases**: the UI's `TO_ALIASES` dict maps typed nicknames to
  seat ids (e.g. `{"scout": "ollama-scout"}`).
- **Desktop/voice notices**: the hub can fire `notify-send` and,
  optionally, speak through a program you name in `A2A_SPEAK_CMD`
  (with `A2A_SPEAK_NOTIFY=1`). Both are off the bus itself.

## Message shape

```json
{"from": "scout", "type": "thought", "to": null, "content": "hello room", "ts": "…"}
```

`to` is a seat id for a direct post, a list is fanned out by the UI
(one addressed copy per member), and `null` is the room. Identify
first: `{"type": "identify", "from": "your-seat-id"}`.
