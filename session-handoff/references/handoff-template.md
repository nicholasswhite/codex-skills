# Handoff document template

Use this skeleton when a project has no durable handoff document. Save it as
`HANDOFF.md` at the project root or inside the owning subproject. Adapt the
state block to Git or non-Git mode; do not invent unavailable facts.

```markdown
# <Project> — Continuation Handoff

**Point a new conversation at this file first.**
Last updated <YYYY-MM-DD>.

> <One or two sentences describing the project and current outcome.>

## Read these, in order

1. **This file** — current state, constraints, and open work.
2. `<path>` — <why it matters>.

## Current state (<YYYY-MM-DD>)

<!-- Git mode example; never include the current tip hash. -->
- **Git:** branch `<branch>` compared with `<default>`; <clean/dirty and count>.
  Re-derive with `git status --short --branch`, the appropriate `git rev-list`
  comparison, and the project checks before trusting this last-known state.

<!-- Non-Git mode example. -->
- **Workspace:** this project has no Git worktree, so branch, tip, and commit
  history are unavailable. Reread `AGENTS.md`, inspect the cited artifacts, and
  rerun the project checks before trusting this last-known state.

- **Verification:** <exact commands actually run and their last-known results>.
- **Evidence boundary:** <what was not run or not independently verified>.

## What the last session did

- <Verified accomplishment tied to an artifact or command>.
- <Unfinished or unverified work labeled accurately>.

## What's next (open)

1. **<Thread>** — <one-line description>.
2. **<Thread>** — <one-line description>.

## Hard constraints (do not violate)

- <Preserved project constraint>.
- Do not initialize Git, commit, push, publish, or open a pull request unless
  the user explicitly requests it and project instructions permit it.

## Gotchas / environment notes

- <Reusable fact that would otherwise cost time to rediscover>.
```

## Update contract

- Replace `Current state`, `What the last session did`, and `What's next`.
- Preserve and reconcile `Read these, in order`, `Hard constraints`, and
  `Gotchas / environment notes`; never silently drop a safety constraint.
- Restamp the update date.
- Never pin the current Git tip hash.
- Use last-known numbers only with a re-verification instruction.
- Run the strict linter after editing.
- Do not stage, commit, or push as part of the handoff unless separately and
  explicitly authorized.
