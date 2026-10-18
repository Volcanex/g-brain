# g_todos: owner commitments and machine work

This package belongs to the todos component together with `brain_todos.py` and
`outlets/`. It imports only shared config and the alert spine across components.

- `text.py` normalizes captured text and records requester, composer and source,
  defaulting missing provenance to `unknown`.
- `tickets.py` stores machine and agent work in `tickets/`, with open, done and
  dropped states. Stable `source_key` values deduplicate retries, including closed
  tickets. Bodies are one paragraph of at most 800 characters.
- `register_tools` adds create, list and close ticket tools to `brain_todos.mcp`.
  The host mounts that MCP and includes `create_tickets_router()` at `/api/tickets`.
  Tickets do not enter owner todos, calendars, reminders or pulse commitments.

Owner todos still live in `brain_todos.py`. Every writer uses its public boundary
for provenance, owner-action checks, source idempotency and normalized text.
Consumers use `get`, `iter_todos`, `serialize`, `create`, `update_todo` and `upsert`.
Reads derive expiry without writes. The CLI is a read-only compatibility report.
Set `BRAIN_ROOT` before imports and use invented temporary fixtures in tests.
