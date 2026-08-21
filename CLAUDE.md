<!-- agents-md-sync:begin -->
@AGENTS.md
<!-- agents-md-sync:end -->

# Claude Code specifics

Shared, tool-agnostic project instructions live in `AGENTS.md` (imported
above). This repo additionally ships host-specific configuration:

- `.claude/skills/` — the canonical skill definitions; the corresponding
  Codex-visible symlinks are generated under `.agents/skills/`
- `.claude/settings.json` — the permission allowlist and hook wiring for this
  host; the generated counterpart for the other supported host is
  `.codex/hooks.json`
- `.claude/settings.local.json` — personal preferences (model, output style,
  and extra permissions); never commit or modify this file

The shared hook scripts live in `.agents/hooks/` and are described in the
project-wide `Agent hooks` section of `AGENTS.md`.
