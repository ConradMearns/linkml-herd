"""`linkml-herd` -- render a LinkML schema to an HTML ER diagram.

Re-running linkml-herd keeps the canvas layout saved in the existing output page (via
its "Save canvas" button), so schema edits never undo where you put things.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

import yaml

from .parse import parse_linkml_file
from .render import (
    DEFAULT_THEME,
    THEMES,
    read_saved_inverse,
    read_saved_layout,
    read_saved_theme,
    render_html,
    render_svg,
)
from .update_check import ENV_OPT_OUT, UpdateCheck, current_version


def output_path(schema_path: Path) -> Path:
    """mydb.yaml -> mydb.html, schema.linkml.yaml -> schema.linkml.html."""
    return schema_path.with_suffix(".html")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="linkml-herd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("schema", type=Path, help="LinkML schema (.yaml)")
    ap.add_argument("-o", "--output", help="output file, or '-' for stdout (default: <schema>.html)")
    ap.add_argument(
        "--layout",
        type=Path,
        help="take the saved canvas layout from this page instead of the output file "
        "(e.g. a copy the browser saved to Downloads)",
    )
    ap.add_argument(
        "--relayout", action="store_true", help="ignore any saved layout and lay out from scratch"
    )
    ap.add_argument(
        "--theme",
        choices=THEMES,
        help="colour theme baked into the page (default: the one the output page already has, "
        f"else {DEFAULT_THEME}; auto follows the OS light/dark setting)",
    )
    ap.add_argument(
        "--svg",
        action="store_true",
        help="write an SVG image instead of the page (default: <schema>.svg; implied by -o *.svg), "
        "laid out and themed like <schema>.html if it exists",
    )
    ap.add_argument(
        "--inverse",
        action=argparse.BooleanOptionalAction,
        help="draw inverse relationships -- a list of children beside each child's reference back "
        "(default: as the output page was saved, else off; the page has a toggle too)",
    )
    ap.add_argument("--open", action="store_true", help="open the result in a browser")
    ap.add_argument(
        "--no-update-check",
        action="store_true",
        help=f"don't check PyPI for a newer linkml-herd (or set {ENV_OPT_OUT}=1)",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {current_version() or 'unknown'}")
    args = ap.parse_args(argv)
    update = None if args.no_update_check else UpdateCheck()

    try:
        schema = parse_linkml_file(args.schema)
    except (OSError, yaml.YAMLError) as e:
        print(f"linkml-herd: {e}", file=sys.stderr)
        return 1

    for p in schema.problems:
        print(f"{p.level}: {p.class_name}.{p.slot}: {p.message}", file=sys.stderr)

    svg = args.svg or (args.output or "").lower().endswith(".svg")
    page = output_path(args.schema)  # where the canvas layout and theme live
    if args.output == "-":
        out = None
    elif args.output:
        out = Path(args.output)
    else:
        out = args.schema.with_suffix(".svg") if svg else page
    if not svg:
        page = out

    layout_from = args.layout or page
    saved = {} if args.relayout or layout_from is None else read_saved_layout(layout_from)
    if saved.get("tables"):
        print(f"keeping canvas layout saved in {layout_from}", file=sys.stderr)
    theme = args.theme or (read_saved_theme(layout_from) if layout_from else None) or DEFAULT_THEME
    show_inverse = args.inverse if args.inverse is not None else read_saved_inverse(layout_from)

    if svg:
        text = render_svg(schema, saved_layout=saved, theme=theme, show_inverse=show_inverse)
    else:
        text = render_html(
            schema,
            saved_layout=saved,
            source=args.schema.name,
            output_name=out.name if out else None,
            theme=theme,
            show_inverse=show_inverse,
        )
    if out is None:
        sys.stdout.write(text)
    else:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
        if args.open:
            webbrowser.open(out.resolve().as_uri())
    if update:
        update.report()
    return 0
