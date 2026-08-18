# Codex Skills

A public collection of reusable Codex skills that I use across projects.

Each folder is a complete installable skill. Each skill also has its own public
GitHub Gist for reading, sharing, and reconstructing that skill independently.

## Skills

| Skill | What it does | Public Gist |
| --- | --- | --- |
| [`daily-reflection`](./daily-reflection/) | Reviews recent same-project Codex work, saves durable lessons, and safely applies narrow improvements. | [Capsule](https://gist.github.com/nicholasswhite/49c3c874b0eb2fb1139ebf9683811d44) |
| [`i-am-not-a-smart-man`](./i-am-not-a-smart-man/) | Explains a project or file in plain language and creates a local HTML report with Mermaid diagrams. | [Capsule](https://gist.github.com/nicholasswhite/e747c6e4c3f42db17008f77c949f1dd9) |
| [`learn-from-source`](./learn-from-source/) | Evaluates one source for useful lessons and proposes evidence-backed improvements. | [Capsule](https://gist.github.com/nicholasswhite/95e12970070397d174cb99d965129339) |
| [`prepare-workos-objective`](./prepare-workos-objective/) | Turns project context into a consolidated Work OS Owner Objective. | [Capsule](https://gist.github.com/nicholasswhite/a193a5d42682f0cac7fa53d8afe4838c) |
| [`session-handoff`](./session-handoff/) | Creates a durable handoff document and continuation prompt for switching tasks or sessions. | [Capsule](https://gist.github.com/nicholasswhite/8d5879a02f1945af0fb008bb3fad534f) |
| [`transcribe-anything`](./transcribe-anything/) | Transcribes local or public audio and video through a CLI, browser app, or Codex skill. | [Capsule](https://gist.github.com/nicholasswhite/c0932b4943874669e8da34ff245abe3d) |

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
table above and in the machine-readable [`catalog.v1.json`](./catalog.v1.json).

To reconstruct a capsule after cloning its Gist, run:

```text
python -B UNPACK.py unpack --capsule-dir . --destination ..\skill-name
```

The unpacker refuses to overwrite an existing destination and verifies each file
hash plus the complete tree digest before writing the reconstructed skill.

## Validation and generated files

The skills are validated before publication. Local virtual environments, caches,
bytecode, credentials, and generated output are excluded from the repository and
Gist capsules.

## License

No open-source license has been selected for this collection yet. Public visibility
does not itself grant permission to copy, modify, or redistribute the contents.
