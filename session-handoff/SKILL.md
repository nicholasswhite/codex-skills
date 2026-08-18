---
name: session-handoff
description: Create or refresh a durable HANDOFF.md and a copy-paste continuation prompt when a project session is ending, switching contexts, nearing a context limit, or the user asks to hand off, wrap up, prepare a transition, or write a pickup prompt. Works in Git and non-Git workspaces. Use for forward-looking continuation, not retrospectives, chat-history browsing, or forwarding a prompt to another tool.
---

# Session Handoff

Create a verifiable transition artifact that lets a new conversation resume
without trusting stale state or rediscovering project rules.

Produce two outputs:

1. A durable `HANDOFF.md`, updated in place or scaffolded when the project needs
   one.
2. A copy-paste continuation prompt for the next conversation.

## Non-negotiable rules

- Read applicable `AGENTS.md` files and project instructions before inspecting
  or changing the handoff.
- Verify claims from current files, commands, tests, and Git when available.
  Treat conversation memory only as a lead to verify.
- Never pin the current Git tip hash in durable prose. Historical commit
  citations are allowed when clearly labeled historical.
- Preserve existing hard constraints, read order, and gotchas unless current
  evidence proves they changed.
- Never initialize Git. Never stage, commit, push, publish, or create a pull
  request unless the user explicitly requests that separate action and project
  instructions permit it.
- Do not invent a repository-memory system. Update one only when the project
  already defines it and the requested handoff includes that file.
- Keep secrets, credentials, provider/user content, and transient callback or
  token data out of handoff artifacts.

## Resolve the skill root

Treat the directory containing this `SKILL.md` as `<skill-root>`. Invoke bundled
resources with paths under that directory. Do not assume a plugin environment
variable.

Read on demand:

- `references/handoff-template.md` when creating or updating `HANDOFF.md`.
- `references/pickup-prompt-template.md` when assembling the continuation
  prompt specification.

## Workflow

### 1. Establish scope and instructions

- Identify the active project root and the work being handed off.
- Read every applicable `AGENTS.md` from the project root to the active path,
  plus the project's current status, decisions, and release/verification docs
  when present.
- Locate an existing `HANDOFF.md` in the project root or owning subproject.
  Prefer the handoff nearest the work being transferred. If more than one is
  plausible, ask before choosing.
- For a trivial conversation with no durable project state, skip the file and
  produce only the pickup prompt; say why.

### 2. Capture live state without mutating it

Run:

```text
python <skill-root>/scripts/handoff_state.py capture --path <project-root> --markdown
```

The command detects Git automatically.

- In a Git worktree, also run `commits` with an explicit session-start ref when
  known. Otherwise use the discovered default branch only when that comparison
  accurately represents this session.
- In a long-lived or unrelated branch, do not present the full branch divergence
  as this session's accomplishments. Ask for a start ref or ground the summary
  from verified artifacts instead.
- In a non-Git workspace, do not initialize Git or fabricate branch/commit
  facts. Ground accomplishments in actual changed artifacts, current status
  docs, completed command output, and other durable evidence. Mark anything
  not independently confirmed as `Unverified`.

Useful commands:

```text
python <skill-root>/scripts/handoff_state.py capture --path <project-root> --json
python <skill-root>/scripts/handoff_state.py commits --path <project-root> --since <ref>
```

### 3. Build an evidence-backed accomplishment list

- Start with Git commits only when the selected ref represents the session.
- Cross-check every claimed outcome against the produced file, test output,
  package identity, screenshot, or other artifact.
- Label unfinished and uncommitted work accurately.
- Separate source implementation, automated verification, packaging, installed
  verification, and human/manual checks. Never promote one kind of evidence
  into another.

### 4. Update or scaffold `HANDOFF.md`

Follow `references/handoff-template.md`.

- Replace `Current state`, `What the last session did`, and `What's next`.
- Preserve and reconcile `Read these, in order`, `Hard constraints`, and
  `Gotchas / environment notes`.
- Record open work as a pick-one menu, not as a silently committed plan.
- Use last-known test results only when their exact commands actually ran; tell
  the next conversation to rerun them.
- Keep the document self-invalidating: describe how to re-derive live state
  instead of claiming a permanent tip, count, process, or test result.

### 5. Lint strictly

Run:

```text
python <skill-root>/scripts/handoff_lint.py check --file <handoff-path> --strict
```

When Git branch/default values are known, add `--branch` and `--default`.
Resolve every error and warning. Do not weaken or bypass the checks.

### 6. Render the pickup prompt

Create a temporary JSON specification outside the project using
`references/pickup-prompt-template.md`, then run:

```text
python <skill-root>/scripts/handoff_lint.py prompt --spec <temporary-spec.json>
```

Remove the temporary specification after rendering. Do not leave generated
scratch files in the project.

The prompt must:

- Point to durable files in reading order.
- Tell the next conversation to reread project instructions and re-derive live
  state before acting.
- Summarize verified accomplishments and honest evidence boundaries.
- Restate hard constraints.
- Offer open threads as a pick-one menu.
- Contain no pinned current tip hash or secrets.

### 7. Report without unauthorized follow-on actions

End with:

1. What durable handoff file was updated or why none was needed.
2. The pickup prompt in a fenced block ready to copy.
3. Important open threads or unverified items.
4. A statement that no commit or push was performed, unless the user separately
   authorized and the project permitted one.

Do not continue implementing open work after the handoff unless the user asks.
