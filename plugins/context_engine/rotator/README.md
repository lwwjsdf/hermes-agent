# rotator — mechanical session rotation (family-ops#22 prototype)

0-LLM context engine: at 70% of the context window it mechanically rotates the
session instead of in-place LLM summarization. The child session opens with a
handoff header (anchor index + session_search pointer) plus a verbatim lean tail
(2.5% of the window, clamped 10k-25k tokens). Session forking, gateway remapping
and lease rebinding are the host's legacy rotating path.

## Enable (per profile — grey release, never default)

The engine is opt-in via config; activation IS the switch. On an isolated profile:

    hermes -p rotator-test config set context.engine rotator
    hermes -p rotator-test config set compression.in_place false

compression.in_place: false opens the rotating path; it only matters where the
rotator engine is active, because the engine owns the compression trigger. Cron
agents (platform="cron") are inert by construction, and fresh one-shot sessions
never reach the 70% threshold. Rollback to stock behavior: set
context.engine: compressor (in_place can stay false or revert to true).

## Safety nets (host-provided, no plugin code)

- Rotation publish failure rolls back to the live parent session and arms the
  engine's failure cooldown; the engine also latches a breaker after 3
  ineffective-rotation strikes.
- The gateway's 85% hygiene agent always forces in-place compaction
  (gateway/run_turn.py) — the final backstop if rotation keeps failing.
- The public compression_count stays <= 1, so the "Consider /new" warning never
  fires for rotator sessions; the real rotation count is in get_status().
