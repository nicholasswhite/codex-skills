# Pickup-prompt specification

Render a continuation prompt with:

```text
python <skill-root>/scripts/handoff_lint.py prompt --spec <spec.json>
```

Create the specification outside the project and delete it after rendering.

## Schema

```json
{
  "project": "Project name",
  "read_order": [
    {"path": "HANDOFF.md", "note": "current entry point"},
    {"path": "docs/IMPLEMENTATION-STATUS.md", "note": "verified status"}
  ],
  "state_lines": [
    "Last-known state from the durable handoff; re-derive it before acting.",
    "The workspace has no Git history; do not invent branch or commit facts."
  ],
  "accomplishments": [
    "Implemented the bounded feature and verified it with the recorded command."
  ],
  "constraints": [
    "Read and follow AGENTS.md before changing files.",
    "Do not commit or push unless explicitly requested and permitted."
  ],
  "threads": [
    {"title": "First option", "desc": "one-line next step"},
    {"title": "Second option", "desc": "alternative next step"}
  ],
  "verify_note": "Optional project-specific note appended to the fixed verification instructions.",
  "ask": "Optional replacement for the closing question."
}
```

`read_order` and `threads` entries may be strings or objects. State,
accomplishment, and constraint entries must be non-empty strings. Omit optional
fields to use safe defaults. `verify_note` supplements the fixed safety text;
it cannot replace the instruction to re-derive live state.

## Output shape

```markdown
# Continue: <project>

You're picking up an in-progress project. **Verify before you trust.**

## Read first (in order)
1. `HANDOFF.md` — current entry point

## Where things stand
> Reread project instructions, inspect live artifacts, and rerun checks. If Git
> exists, re-derive its branch, tip, comparison, and working-tree state.

- <last-known state>

## What the last session did
- <verified accomplishment>

## Hard constraints (do not violate)
- <constraint>

## Open threads — pick one
- (a) **<thread>** — <description>

<closing question>
```

The renderer rejects a prompt that pins a current Git tip hash. Historical
commit citations remain allowed when clearly written as historical commits.
