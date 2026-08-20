#!/usr/bin/env python3
"""Render readme.md into public/index.html.

The page is not a dumped README: the H1 and the prose above the first `##`
become a hero, and every `## ` section below it becomes a <section>. Title,
description and the primary link come from .claude-plugin/plugin.json, so the
manifest stays the single source of truth for what this plugin claims to be.

Output is deterministic — no timestamps, no build ids — so CI can commit the
result back only when it genuinely changed.

Usage:  python3 scripts/build_page.py [--check]
        --check exits 1 if public/index.html is stale instead of writing it.
"""

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "readme.md"
MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
OUT = ROOT / "public" / "index.html"

# Design tokens lifted from tuomashatakka.github.io/threejs-scene so the two
# pages read as one system. The source uses a lightningcss light-dark hack;
# this is the plain-CSS equivalent.
CSS = """
:root{
  color-scheme:light dark;
  --ink:#161616; --ink-soft:#3f3f3f; --ink-faint:#6b6b6b;
  --line:#e6e6e6; --wash:#f6f6f6; --paper:#fff; --accent:#2752e7;
  --font-sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --font-mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --text-xs:.75rem; --text-sm:.875rem; --text-md:1rem;
  --text-lg:1.175rem; --text-xl:1.35rem; --text-2x:2.35rem;
  --space-md:1.5rem; --space-lg:3rem; --space-xl:5.5rem;
  --measure:38em; --page-max:47.5rem; --snap:.2s cubic-bezier(.4,0,.2,1);
}
@media (prefers-color-scheme:dark){:root{
  --ink:#ededed; --ink-soft:#c9c9c9; --ink-faint:#9a9a9a;
  --line:#242424; --wash:#161616; --paper:#0e0e0e; --accent:#7aa2ff;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{background:var(--paper);color:var(--ink);
     font:400 var(--text-md)/1.65 var(--font-sans);
     -webkit-font-smoothing:antialiased;margin:0}
.wrap{max-width:var(--page-max);padding-inline:var(--space-md);margin-inline:auto}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code,pre{font-family:var(--font-mono);font-size:.875em}
code{background:var(--wash);border-radius:3px;padding:.1em .35em}
pre{background:var(--wash);border:1px solid var(--line);border-radius:6px;
    padding:1rem 1.125rem;line-height:1.5;overflow-x:auto}
pre code{background:0 0;padding:0}
header.hero{padding:var(--space-xl) 0 var(--space-lg);border-block-end:1px solid var(--line)}
header.hero .eyebrow{font:600 var(--text-xs)/1 var(--font-mono);letter-spacing:.14em;
    text-transform:uppercase;color:var(--ink-faint);margin-block-end:1.25rem}
header.hero h1{font-size:var(--text-2x);letter-spacing:-.02em;margin:0 0 1rem;
    font-weight:650;line-height:1.12}
header.hero .lede{font-size:var(--text-lg);color:var(--ink-faint);
    max-width:var(--measure);margin:0 0 1rem}
header.hero p{max-width:var(--measure);color:var(--ink-faint);margin:0 0 1rem}
header.hero .cta{flex-wrap:wrap;gap:.625rem;display:flex;margin-block-start:1.75rem}
a.btn{border:1px solid var(--line);color:var(--ink);
    transition:border-color var(--snap),background var(--snap),opacity var(--snap);
    border-radius:6px;padding:.5625rem 1rem;font-size:.95rem;font-weight:550;display:inline-block}
a.btn:hover{border-color:var(--ink);text-decoration:none}
a.btn.primary{background:var(--ink);color:var(--paper);border-color:var(--ink)}
a.btn.primary:hover{opacity:.88}
section{padding:var(--space-lg) 0;border-block-end:1px solid var(--line)}
section h2{font-size:var(--text-xl);letter-spacing:-.01em;margin:0 0 1.125rem;font-weight:600}
section h3{font-size:var(--text-md);margin:1.75rem 0 .375rem;font-weight:600}
section p{margin:0 0 1rem}
section ul,section ol{margin:0 0 1rem;padding-inline-start:1.25rem}
section li{margin-block-end:.35rem}
section blockquote{margin:0 0 1rem;padding-inline-start:1rem;
    border-inline-start:2px solid var(--line);color:var(--ink-faint)}
section:last-of-type{border-block-end:none}
.muted{color:var(--ink-faint)}
table{border-collapse:collapse;width:100%;margin:0 0 1rem;font-size:.95rem;display:block;overflow-x:auto}
th,td{text-align:start;padding:.7rem .9rem .7rem 0;border-block-end:1px solid var(--line);vertical-align:top}
th{font-weight:600;font-size:var(--text-xs);letter-spacing:.08em;text-transform:uppercase;
   color:var(--ink-faint);border-block-end-color:var(--ink-faint)}
td{color:var(--ink-faint)}
td:first-child{color:var(--ink);white-space:nowrap}
footer{padding:var(--space-lg) 0 4.5rem;color:var(--ink-faint);font-size:.9rem}
footer a{color:var(--ink-faint);text-decoration:underline}
footer a:hover{color:var(--ink)}
"""

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E"
    "%3Crect width='16' height='16' rx='3' fill='%232752e7'/%3E"
    "%3Cpath d='M4 5l3 3-3 3M8.5 11h4' stroke='white' stroke-width='1.6' "
    "fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"
)


def render_markdown(text: str) -> str:
    import markdown  # provided by CI; `pip install markdown` locally
    return markdown.markdown(
        text, extensions=["fenced_code", "tables", "sane_lists"], output_format="html5"
    )


def split_sections(md: str):
    """-> (h1, intro_markdown, [(heading, body_markdown), ...])"""
    lines = md.splitlines()
    h1 = ""
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            h1 = line[2:].strip()
            start = i + 1
            break

    intro, sections = [], []
    current = None
    for line in lines[start:]:
        if line.startswith("## "):
            if current:
                sections.append(current)
            current = (line[3:].strip(), [])
        elif current:
            current[1].append(line)
        else:
            intro.append(line)
    if current:
        sections.append(current)

    return h1, "\n".join(intro).strip(), [(t, "\n".join(b).strip()) for t, b in sections]


def build() -> str:
    manifest = json.loads(MANIFEST.read_text())
    name = manifest["name"]
    repo = manifest.get("repository", "")
    version = manifest.get("version", "")
    blurb = manifest.get("description", "")

    _h1, intro_md, sections = split_sections(README.read_text())

    # First paragraph of the intro carries the hero; the rest follows it.
    paragraphs = [p for p in intro_md.split("\n\n") if p.strip()]
    lede = render_markdown(paragraphs[0]) if paragraphs else ""
    lede = re.sub(r"^<p>(.*)</p>$", r"\1", lede.strip(), flags=re.S)
    rest = "\n".join(render_markdown(p) for p in paragraphs[1:])

    cta = []
    if repo:
        cta.append(f'<a class="btn primary" href="{repo}">View on GitHub</a>')
        cta.append(
            f'<a class="btn" href="{repo}/blob/main/skills/{name}/SKILL.md">Protocol reference</a>'
        )
    cta.append('<a class="btn" href="https://opencode.ai">opencode</a>')

    body = []
    for heading, md in sections:
        body.append(f"  <section>\n    <h2>{heading}</h2>\n{render_markdown(md)}\n  </section>")

    title = f"{name} — {blurb}" if blurb else name
    footer_link = repo.replace("https://", "") if repo else ""

    NL6 = "\n      "

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{blurb}">
<link rel="icon" href="{FAVICON}">
<style>{CSS}</style>
</head>
<body>
<main class="wrap">

  <header class="hero">
    <div class="eyebrow">{name}{f" &middot; v{version}" if version else ""}</div>
    <h1>{blurb}</h1>
    <p class="lede">{lede}</p>
{rest}
    <div class="cta">
      {NL6.join(cta)}
    </div>
  </header>

{chr(10).join(body)}

  <footer>
    <p><a href="{repo}">{footer_link}</a> &middot; MIT licensed</p>
  </footer>

</main>
</body>
</html>
"""


def main() -> int:
    html = build()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current == html:
            print("public/index.html is up to date")
            return 0
        print("public/index.html is stale — run scripts/build_page.py", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(html)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
