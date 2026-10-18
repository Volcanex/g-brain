# g_pulse: the live layer (g-pulse)

What is true right now, kept beside the brain's long-term notes. The pulse page
(`.pulse/page.md` in the vault), inlet records, briefs and the pantry. `.pulse/` is
gitignored in the vault and rewritten all the time. Durable facts go to a corpus layer,
dated owner commitments go to todos and machine work goes to tickets.

## Files
- `pulse.py`: the page, the inlet registry and records, the composed brief, the pantry,
  the `/api/pulse/*` routes and the `pulse_*` MCP tools, plus a CLI
  (`python -m brain_pulse page`, `note`, `record`, `inlets`).
- The composed brief includes a block of `kind: fact` summaries from `about/`, with up
  to 40 entries and 1,600 characters of entries. The payload has a 4,000 character budget.
  Tickets stay out of the brief's owner commitments.
- `inlets/` at the repo root belongs to this component. Inlets bring outside data in
  (Gmail, recurring charges) and never write the other way.
- `pulse` at the repo root is the shell wrapper: `pulse inlets`, `pulse show <id>`,
  `pulse brief`, `pulse note`, `pulse record`. On h it is linked as `/usr/local/bin/pulse`
  (`ln -s /home/gabriel/brain-tools/pulse /usr/local/bin/pulse`): pulse cron agents call it by name.

## Rules
- Imports g-todos, g-spine and `g_config`. It reads the spine's `status/snapshot.json` as a
  file rather than importing the spine for it.
- Other components use three public functions: `append_page` (one line on the page),
  `record_state` (an inlet's latest state) and `composed_payload` (the brief voice and the
  MCP read). The corpus may use only `append_page` and `PAGE_PATH`.
- `pulse.py` and `inlets/subs.py` use the public todos API. The boundary test's private
  allowlist is empty. Todo expiry is derived by reads without rewriting stored notes.
- Callers outside the repo use the old name `brain_pulse`: the vault's `.pulse/crons`
  (`-m brain_pulse record|note`), g-env-linux's pulse-cron-dispatch and the `pulse` wrapper.
  Keep the alias until they move.
