#!/usr/bin/env python3
# hzmetrics docs — static site builder.
#
# Reads:
#   gh-pages/site.json    — site metadata + group definitions (pages listed
#                           per group, mapped to docs/<page>.md)
#   gh-pages/templates/   — home.html, group.html, doc.html
#   gh-pages/assets/      — site.css
#   docs/<page>.md        — markdown content (flat directory)
#   docs/README.md        — rendered into the homepage "About these docs"
#                           section
#
# Writes (clobbering on each run):
#   gh-pages/public/      — built static site, suitable for GitHub Pages
#     index.html
#     <group>/index.html
#     <group>/<page>/index.html
#     assets/...
#     .nojekyll
#
# Pattern lifted from rappture-monorepo's gh-pages/build_site.py:
#   - markdown-it-py for CommonMark + tables + strikethrough
#   - Each page gets its own directory (slug/index.html) so URLs are
#     pretty (/overview/summary/) rather than /overview/summary.html.
#   - .nojekyll prevents GitHub Pages from running Jekyll.
#   - Sidebar nav is grouped: top-level group titles, expanded for the
#     current group; collapsed for others.
#
# Difference from rappture: docs/ here is flat (no per-group subdirs), so
# group → page mapping is declared explicitly in site.json. The link
# rewriter consults a global page → group lookup so intra-doc links like
# [foo](bar.md) work whether bar is in the same group or another.

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
from html import escape
from pathlib import Path

try:
    from markdown_it import MarkdownIt
except ImportError as exc:
    raise SystemExit(
        "markdown-it-py is not installed. Install it with:\n"
        "    pip install -r gh-pages/requirements.txt"
    ) from exc


ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT / "gh-pages"
DOCS_SRC = ROOT / "docs"
OUTPUT_DIR = ROOT / "gh-pages" / "public"


# --- helpers ---------------------------------------------------------------

def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug or "section"


def render_template(path: Path, context: dict[str, str]) -> str:
    text = path.read_text(encoding="utf-8")
    for key, value in context.items():
        text = text.replace(f"{{{{ {key} }}}}", value)
    return text


def relative_href(from_file: Path, to_file: Path) -> str:
    return os.path.relpath(to_file, from_file.parent).replace(os.sep, "/")


def ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def copy_tree(source: Path, destination: Path) -> None:
    if source.exists():
        shutil.copytree(source, destination, dirs_exist_ok=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# --- markdown rendering ----------------------------------------------------

class MarkdownRenderer:
    def __init__(self) -> None:
        self.md = MarkdownIt("commonmark", {"html": True, "typographer": True})
        self.md.enable("table")
        self.md.enable("strikethrough")

    def render(self, text: str, *, link_rewrite=None) -> dict[str, object]:
        """Render markdown to HTML, extract title + TOC.

        link_rewrite: optional callable(href) -> rewritten href, applied to
        relative links (e.g. to convert bar.md → ../bar/).
        """
        tokens = self.md.parse(text)
        slug_counts: dict[str, int] = {}
        toc: list[dict[str, object]] = []
        title = None
        first_h1_index = None

        for index, token in enumerate(tokens):
            if token.type == "heading_open":
                level = int(token.tag[1])
                if index + 1 >= len(tokens):
                    continue
                inline = tokens[index + 1]
                if inline.type != "inline":
                    continue
                heading_text = inline.content.strip()
                if not heading_text:
                    continue
                base_slug = slugify(heading_text)
                count = slug_counts.get(base_slug, 0)
                slug_counts[base_slug] = count + 1
                anchor = base_slug if count == 0 else f"{base_slug}-{count + 1}"
                token.attrSet("id", anchor)
                if level == 1 and title is None:
                    title = heading_text
                    first_h1_index = index
                elif level in (2, 3, 4):
                    toc.append({"level": level, "anchor": anchor,
                                "text": heading_text})
            elif token.type == "inline" and link_rewrite is not None:
                if token.children:
                    for child in token.children:
                        if child.type == "link_open":
                            href = child.attrGet("href") or ""
                            new = link_rewrite(href)
                            if new != href:
                                child.attrSet("href", new)

        if first_h1_index is not None:
            del tokens[first_h1_index:first_h1_index + 3]

        html = self.md.renderer.render(tokens, self.md.options, {})
        return {"title": title, "toc": toc, "html": html}


# --- discovery -------------------------------------------------------------

def discover_pages(docs_src: Path, groups: list[dict]) -> dict[str, list[dict]]:
    """Walk site.json groups and resolve each declared page to docs/<page>.md.

    Returns a mapping group_slug → list of page dicts. Each page dict has:
        slug:        page slug (basename without .md)
        source:      Path to docs/<slug>.md
        output_path: relative Path under gh-pages/public/
                     (e.g. overview/summary/index.html)
    """
    by_group: dict[str, list[dict]] = {}
    for group in groups:
        pages = []
        for page_slug in group.get("pages", []):
            source = docs_src / f"{page_slug}.md"
            if not source.exists():
                print(f"warning: {source} declared in site.json but missing",
                      file=sys.stderr)
                continue
            pages.append({
                "slug": page_slug,
                "source": source,
                "output_path": Path(group["slug"]) / page_slug / "index.html",
            })
        by_group[group["slug"]] = pages

    # Warn about docs/*.md that aren't claimed by any group (excluding README).
    claimed = {p["slug"] for pages in by_group.values() for p in pages}
    for md_path in sorted(docs_src.glob("*.md")):
        if md_path.stem == "README":
            continue
        if md_path.stem not in claimed:
            print(f"warning: {md_path} exists but is not in any site.json group",
                  file=sys.stderr)

    return by_group


# --- link rewriting --------------------------------------------------------

def make_link_rewriter(current_group: str, page_to_group: dict[str, str]):
    """Return a function that rewrites .md links in markdown content.

    Source links use flat names (the docs/ dir has no subdirs):
        bar.md          → page bar in some group
        bar.md#anchor   → same, with anchor
        ../tests/...    → repo-relative (kept as-is; broken in the
                          rendered site, but the same string still
                          points at the right thing on GitHub when the
                          source file is viewed there)

    Output URLs are pretty:
        same group:    ../bar/
        cross group:   ../../<group>/bar/
    """
    def rewrite(href: str) -> str:
        if not href or href.startswith(("http://", "https://", "#", "mailto:")):
            return href
        anchor = ""
        path = href
        if "#" in path:
            path, anchor = path.split("#", 1)
            anchor = "#" + anchor
        if not path.endswith(".md"):
            return href
        # Only rewrite if it's a bare filename (no slashes). Anything with
        # a slash (e.g. ../tests/legacy/README.md) is a repo path we can't
        # meaningfully resolve here — leave it alone.
        if "/" in path:
            return href
        tgt_slug = path[:-3]
        tgt_group = page_to_group.get(tgt_slug)
        if tgt_group is None:
            return href
        if tgt_group == current_group:
            return f"../{tgt_slug}/{anchor}"
        return f"../../{tgt_group}/{tgt_slug}/{anchor}"
    return rewrite


# --- nav / sidebar ---------------------------------------------------------

def build_docs_nav(groups: list[dict], pages_by_group: dict, page_titles: dict,
                   current_output: Path, output_dir: Path,
                   current_group: str | None, current_slug: str | None) -> str:
    chunks = []

    # Lead with a synthetic "Homepage" section that links back to the
    # site root, so the sidebar makes it obvious there's a landing page
    # distinct from the section groups below. .is-active when we ARE on
    # the homepage (current_group and current_slug both None).
    home_href = relative_href(current_output, output_dir / "index.html")
    on_home = (current_group is None and current_slug is None)
    home_title_classes = "docs-nav__group-title" + (" is-active" if on_home else "")
    home_item_classes = "docs-nav__item" + (" is-active" if on_home else "")
    chunks.append(
        f'<div class="docs-nav__group">\n'
        f'  <a class="{home_title_classes}" href="{escape(home_href)}">Homepage</a>\n'
        f'  <ul class="docs-nav__list">\n'
        f'    <li><a class="{home_item_classes}" href="{escape(home_href)}">Home</a></li>\n'
        f'  </ul>\n'
        f'</div>'
    )

    for group in groups:
        g_slug = group["slug"]
        is_current_group = (g_slug == current_group)
        title_classes = "docs-nav__group-title"
        if is_current_group:
            title_classes += " is-active"
        group_index = output_dir / g_slug / "index.html"
        href = relative_href(current_output, group_index)
        items = []
        for page in pages_by_group.get(g_slug, []):
            page_output = output_dir / page["output_path"]
            page_href = relative_href(current_output, page_output)
            classes = "docs-nav__item"
            if is_current_group and page["slug"] == current_slug:
                classes += " is-active"
            title = page_titles.get((g_slug, page["slug"]), page["slug"])
            items.append(
                f'    <li><a class="{classes}" href="{escape(page_href)}">'
                f'{escape(title)}</a></li>'
            )
        # Always render the full page list — keeps the table of contents
        # complete and visible on every page; the active page (if any)
        # gets `.is-active` set above.
        if items:
            list_html = (
                '\n  <ul class="docs-nav__list">\n'
                + "\n".join(items)
                + "\n  </ul>"
            )
        else:
            list_html = ""
        chunks.append(
            f'<div class="docs-nav__group">\n'
            f'  <a class="{title_classes}" href="{escape(href)}">'
            f'{escape(group["title"])}</a>'
            f'{list_html}\n'
            f'</div>'
        )
    return "\n".join(chunks)


def build_toc(entries: list[dict[str, object]]) -> str:
    if not entries:
        return ""
    items = []
    for entry in entries:
        level = int(entry["level"])
        classes = f"toc__item toc__item--level-{level}"
        items.append(
            f'<li class="{classes}"><a href="#{escape(str(entry["anchor"]))}">'
            f"{escape(str(entry['text']))}</a></li>"
        )
    return (
        '<nav class="toc" aria-label="On this page">\n'
        '  <p class="toc__heading">On this page</p>\n'
        '  <ul class="toc__list">\n'
        + "\n".join(items)
        + "\n  </ul>\n"
        "</nav>"
    )


# --- build -----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Build the hzmetrics docs site.")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help="Output directory")
    args = parser.parse_args()

    output_dir = Path(args.output).resolve()

    config = json.loads((SOURCE_DIR / "site.json").read_text(encoding="utf-8"))
    site_name = config["site_name"]
    site_tagline = config["site_tagline"]
    site_description = config["site_description"]
    github_href = config.get("github_href", "#")
    groups = config["groups"]

    pages_by_group = discover_pages(DOCS_SRC, groups)

    # Build global page → group lookup for the link rewriter.
    page_to_group: dict[str, str] = {}
    for group in groups:
        for page in pages_by_group.get(group["slug"], []):
            page_to_group[page["slug"]] = group["slug"]

    renderer = MarkdownRenderer()
    page_titles: dict[tuple[str, str], str] = {}
    rendered_pages: dict[tuple[str, str], dict] = {}

    for group in groups:
        for page in pages_by_group.get(group["slug"], []):
            rewriter = make_link_rewriter(group["slug"], page_to_group)
            md_text = page["source"].read_text(encoding="utf-8")
            rendered = renderer.render(md_text, link_rewrite=rewriter)
            title = rendered["title"] or page["slug"].replace("_", " ")
            page_titles[(group["slug"], page["slug"])] = str(title)
            rendered_pages[(group["slug"], page["slug"])] = rendered

    # Render docs/README.md for the homepage "About these docs" section.
    readme_path = DOCS_SRC / "README.md"
    if readme_path.exists():
        def readme_rewrite(href: str) -> str:
            if not href or href.startswith(("http://", "https://", "#", "mailto:")):
                return href
            anchor = ""
            path = href
            if "#" in path:
                path, anchor = path.split("#", 1)
                anchor = "#" + anchor
            # Same flat-name rewrite as the docs themselves: bar.md → group/bar/.
            if path.endswith(".md") and "/" not in path:
                tgt_slug = path[:-3]
                tgt_group = page_to_group.get(tgt_slug)
                if tgt_group is not None:
                    return f"{tgt_group}/{tgt_slug}/{anchor}"
            return href
        readme_text = readme_path.read_text(encoding="utf-8")
        readme_rendered = renderer.render(readme_text, link_rewrite=readme_rewrite)
        readme_html = (
            '<div class="prose">\n'
            + str(readme_rendered["html"])
            + '\n</div>'
        )
    else:
        readme_html = ""

    ensure_clean_dir(output_dir)
    copy_tree(SOURCE_DIR / "assets", output_dir / "assets")
    write_text(output_dir / ".nojekyll", "")

    # --- homepage ---
    # Doc-page-like layout: a fully-expanded section sidebar on the left
    # (built by build_docs_nav with current_group=None) and the rendered
    # docs/README.md as the article on the right.
    home_output = output_dir / "index.html"
    home_nav_html = build_docs_nav(
        groups, pages_by_group, page_titles,
        home_output, output_dir,
        None, None,
    )
    home_html = render_template(
        SOURCE_DIR / "templates" / "home.html",
        {
            "site_name": escape(site_name),
            "site_tagline": escape(site_tagline),
            "site_description": escape(site_description),
            "github_href": escape(github_href),
            "assets_href": "assets/site.css",
            "docs_nav": home_nav_html,
            "readme_html": readme_html,
        },
    )
    write_text(home_output, home_html)

    # --- group landing pages ---
    for group in groups:
        g_slug = group["slug"]
        group_output = output_dir / g_slug / "index.html"
        page_items = []
        for page in pages_by_group.get(g_slug, []):
            ppath = output_dir / page["output_path"]
            href = relative_href(group_output, ppath)
            title = page_titles[(g_slug, page["slug"])]
            page_items.append(
                '<li>\n'
                '  <a href="{href}">\n'
                '    <span class="group-pages__title">{title}</span>\n'
                '    <span class="group-pages__slug">{slug}</span>\n'
                '  </a>\n'
                '</li>'.format(
                    href=escape(href), title=escape(title),
                    slug=escape(page["slug"]),
                )
            )
        group_html = render_template(
            SOURCE_DIR / "templates" / "group.html",
            {
                "site_name": escape(site_name),
                "group_title": escape(group["title"]),
                "group_summary": escape(group["summary"]),
                "github_href": escape(github_href),
                "assets_href": relative_href(group_output, output_dir / "assets" / "site.css"),
                "home_href": relative_href(group_output, output_dir / "index.html"),
                "page_count": str(len(pages_by_group.get(g_slug, []))),
                "page_list": "\n".join(page_items),
            },
        )
        write_text(group_output, group_html)

    # --- per-page docs ---
    for group in groups:
        g_slug = group["slug"]
        for page in pages_by_group.get(g_slug, []):
            page_output = output_dir / page["output_path"]
            rendered = rendered_pages[(g_slug, page["slug"])]
            title = page_titles[(g_slug, page["slug"])]
            nav_html = build_docs_nav(
                groups, pages_by_group, page_titles,
                page_output, output_dir,
                g_slug, page["slug"],
            )
            toc_html = build_toc(rendered["toc"])  # type: ignore[arg-type]
            assets_href = relative_href(page_output, output_dir / "assets" / "site.css")
            home_href = relative_href(page_output, output_dir / "index.html")
            group_href = relative_href(page_output, output_dir / g_slug / "index.html")
            source_path = f"docs/{page['slug']}.md"
            doc_html = render_template(
                SOURCE_DIR / "templates" / "doc.html",
                {
                    "site_name": escape(site_name),
                    "page_title": escape(title),
                    "page_summary": escape(group["summary"]),
                    "group_title": escape(group["title"]),
                    "group_href": escape(group_href),
                    "github_href": escape(github_href),
                    "assets_href": escape(assets_href),
                    "home_href": escape(home_href),
                    "source_path": escape(source_path),
                    "docs_nav": nav_html,
                    "toc": toc_html,
                    "content": str(rendered["html"]),
                },
            )
            write_text(page_output, doc_html)

    total_pages = sum(len(p) for p in pages_by_group.values())
    print(f"Built {total_pages} pages across {len(groups)} groups → {output_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
