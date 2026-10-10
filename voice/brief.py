"""Short-term memory: the brief the Voice eats each conversation.

The composed pulse payload: page snippets, dated todos for the next 7 days,
service health, brief freshness. Identical to what Claude Code sessions get
injected at session start, so the Voice and the terminals share one view of
"what's happening right now". One-way / read-only here: the Voice adds
snippets only through its pulse_note tool, never by editing the brief.
"""
import brain_pulse


def build_brief():
    """Return the composed pulse payload (short-term context snapshot)."""
    try:
        return brain_pulse.composed_payload()
    except Exception:
        return "(short-term memory unavailable)"
