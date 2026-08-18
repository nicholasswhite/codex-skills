#!/usr/bin/env python3
"""
Renderer module — converts the LLM's Markdown explanation into a standalone
HTML page with safely embedded Mermaid diagram support.
"""

import html
import re
from pathlib import Path

HTML_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "assets" / "report.html"


# ---------------------------------------------------------------------------
# Markdown → HTML conversion
# ---------------------------------------------------------------------------

def _load_html_template() -> str:
    with open(HTML_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _markdown_to_html_body(md: str) -> str:
    """Convert Markdown to HTML.

    Uses the ``markdown`` library when available (better output).
    Falls back to a lightweight built-in converter so the tool still works
    without the optional dependency.
    """
    try:
        import markdown as md_lib

        # Project content is evidence, not trusted HTML. Escaping before Markdown
        # conversion preserves Markdown syntax while rendering embedded HTML as
        # visible text instead of executable markup.
        md = html.escape(md, quote=False)

        # ---- pull mermaid blocks out before the markdown parser sees them ---
        mermaid_blocks: list[str] = []

        def _stash_mermaid(match):
            idx = len(mermaid_blocks)
            mermaid_blocks.append(match.group(1).strip())
            return f"MERMAID_PLACEHOLDER_{idx}"

        md_processed = re.sub(
            r"```mermaid\s*\n(.*?)\n```",
            _stash_mermaid,
            md,
            flags=re.DOTALL,
        )

        html = md_lib.markdown(
            md_processed,
            extensions=["fenced_code", "tables", "toc", "codehilite"],
            extension_configs={
                "codehilite": {"css_class": "highlight", "guess_lang": False}
            },
        )

        # ---- restore mermaid blocks as <pre class="mermaid"> ----
        for idx, block in enumerate(mermaid_blocks):
            placeholder = f"MERMAID_PLACEHOLDER_{idx}"
            mermaid_html = f'<pre class="mermaid">{block}</pre>'
            # The markdown parser may have wrapped the placeholder in <p> tags
            html = html.replace(f"<p>{placeholder}</p>", mermaid_html)
            html = html.replace(placeholder, mermaid_html)

        return html

    except ImportError:
        return _basic_markdown_to_html(md)


def _basic_markdown_to_html(md: str) -> str:
    """Bare-minimum Markdown → HTML converter (no external deps)."""
    md = html.escape(md, quote=False)
    lines = md.split("\n")
    html_lines: list[str] = []
    in_code = False
    in_mermaid = False
    buf: list[str] = []

    for line in lines:
        stripped = line.strip()

        # --- fenced code blocks ---
        if stripped.startswith("```mermaid") and not in_code and not in_mermaid:
            in_mermaid = True
            buf = []
            continue
        if stripped.startswith("```") and not in_code and not in_mermaid:
            in_code = True
            buf = []
            continue
        if stripped == "```" and (in_code or in_mermaid):
            content = "\n".join(buf)
            if in_mermaid:
                html_lines.append(f'<pre class="mermaid">{content}</pre>')
            else:
                html_lines.append(f"<pre><code>{content}</code></pre>")
            in_code = False
            in_mermaid = False
            buf = []
            continue
        if in_code or in_mermaid:
            buf.append(line)
            continue

        # --- headings ---
        if stripped.startswith("#### "):
            html_lines.append(f"<h4>{stripped[5:]}</h4>")
        elif stripped.startswith("### "):
            html_lines.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            html_lines.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            html_lines.append(f"<h1>{stripped[2:]}</h1>")
        elif stripped.startswith("- "):
            html_lines.append(f"<li>{stripped[2:]}</li>")
        elif stripped.startswith("> "):
            html_lines.append(f"<blockquote><p>{stripped[2:]}</p></blockquote>")
        elif stripped == "":
            html_lines.append("")
        else:
            html_lines.append(f"<p>{line}</p>")

    return "\n".join(html_lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render(
    explanation: str,
    output_dir: Path,
    filename: str = "HIW-project",
    project_name: str = "",
) -> Path:
    """Write the explanation to an HTML file.

    Returns the path to the generated HTML file.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError("filename must be a plain filename without path separators")

    # ---- HTML ----
    html_body = _markdown_to_html_body(explanation)
    template = _load_html_template()

    title = html.escape(project_name or "How It Works", quote=True)
    html_content = template.replace("{{TITLE}}", title).replace(
        "{{CONTENT}}", html_body
    )

    html_path = output_dir / f"{filename}.html"
    html_path.write_text(html_content, encoding="utf-8")

    return html_path
