# Claude Code entry point

This file exists only because Claude Code auto-loads `CLAUDE.md`. The actual
orchestrator instructions are framework-agnostic and live in one place:

**Read `ORCHESTRATOR.md` FIRST and follow it exactly.** It is the single
source of truth for the candle-close pipeline, the setup/review agents, the
hard rules, the grounding protocol, and the persistence protocol.

Sub-agent definitions: full content is in `agents/<name>.md`. The files in
`.claude/agents/` are thin dispatch shims (front-matter only) that point
back there — when running as a sub-agent, read your `agents/<name>.md` file
before doing anything else.
