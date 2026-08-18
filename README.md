# Codex Skills

A public collection of reusable Codex skills that I use across projects.

Each folder is a complete installable skill. Each skill also has its own public
GitHub Gist for reading, sharing, and reconstructing that skill independently.

## Skills

| Skill | What it does |
| --- | --- |
| [`daily-reflection`](./daily-reflection/) | Reviews recent same-project Codex work, saves durable lessons, and safely applies narrow improvements. |
| [`i-am-not-a-smart-man`](./i-am-not-a-smart-man/) | Explains a project or file in plain language and creates a local HTML report with Mermaid diagrams. |
| [`learn-from-source`](./learn-from-source/) | Evaluates one source for useful lessons and proposes evidence-backed improvements. |
| [`prepare-workos-objective`](./prepare-workos-objective/) | Turns project context into a consolidated Work OS Owner Objective. |
| [`session-handoff`](./session-handoff/) | Creates a durable handoff document and continuation prompt for switching tasks or sessions. |
| [`transcribe-anything`](./transcribe-anything/) | Transcribes local or public audio and video through a CLI, browser app, or Codex skill. |

## Install a skill

Ask Codex to install the folder you want. For example:

```text
Install the daily-reflection skill from
https://github.com/nicholasswhite/codex-skills/tree/main/daily-reflection
```

The repository folders are the canonical installation source because Codex skills
can contain nested scripts, references, tests, and assets.

## Gist capsules

GitHub Gists have a flat file layout, while complete Codex skills often use nested
folders. The public Gists therefore use a versioned capsule format: a readable
`SKILL.md`, a manifest containing paths and SHA-256 hashes, and numbered payload
files that can reconstruct the exact repository tree. Gist links are listed in the
catalog after publication.

## Validation and generated files

The skills are validated before publication. Local virtual environments, caches,
bytecode, credentials, and generated output are excluded from the repository and
Gist capsules.

## License

No open-source license has been selected for this collection yet. Public visibility
does not itself grant permission to copy, modify, or redistribute the contents.
