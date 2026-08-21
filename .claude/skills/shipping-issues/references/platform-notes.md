<!-- platform-annex -->
# Platform notes

## Tool mapping

- Independent issue work may be delegated in parallel when the environment
  supports it; otherwise the same work is performed sequentially.
- The `create-pr` skill is invoked by name when available. If cross-skill
  invocation is unavailable, its checklist is followed inline.
- GitHub operations continue to use the GitHub CLI.

## Codex constraints

Parallel worktree execution is best-effort. Sequential execution preserves the
dependency order and all review gates, but may take longer.
