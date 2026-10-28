"""Render a parsed schema as a single self-contained HTML page.

Tables are absolutely positioned boxes with fixed header/row heights, so we
know exactly where every column sits and can draw the foreign-key edges as SVG
without measuring anything in a browser. The page works without JavaScript;
a small inline script adds dragging and a "Save canvas" button that writes the
page back out with the edited layout embedded. Re-rendering reads that layout
back (see `read_saved_layout`), so schema changes keep the user's canvas.

Edge routing is mirrored in `static/canvas.js` (`edgePath` / `num`) so edges redrawn while dragging match what we emit here.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from importlib.resources import files
from itertools import combinations, pairwise
from pathlib import Path
from typing import Any

from .parse import Class, ForeignKey, Schema

TABLE_W = 260
HEADER_H = 44
ROW_H = 24
BORDER = 1
COL_GAP = 140  # horizontal room between layers for edges + labels
ROW_GAP = 40
PAD = 48
LOOP = 36  # how far a same-side edge bulges out
INVERSE_SHIFT = 6  # how far below its counterpart's lane an inverse edge runs

Point = tuple[float, float]


@dataclass
class Box:
    x: float
    y: float
    h: float

    @property
    def right(self) -> float:
        return self.x + TABLE_W


@dataclass
class Layout:
    boxes: dict[str, Box]
    # Per foreign-key id: points where a long edge enters/leaves each column it
    # crosses (pairs, left-to-right in travel order), so it passes between
    # tables rather than under them.
    waypoints: dict[str, list[Point]] = field(default_factory=dict)


def column_x(col: int) -> float:
    return PAD + col * (TABLE_W + COL_GAP)


def table_height(c: Class) -> float:
    return 2 * BORDER + HEADER_H + max(1, len(c.slots)) * ROW_H


def _depths(names: list[str], fks: list[ForeignKey]) -> dict[str, int]:
    """A table's depth is 1 + the deepest table it references (ignoring cycles)."""
    targets: dict[str, set[str]] = {name: set() for name in names}
    for fk in fks:
        if fk.to_class != fk.from_class:
            targets[fk.from_class].add(fk.to_class)

    depth: dict[str, int] = {}

    def visit(name: str, stack: frozenset[str]) -> int:
        if name not in depth:
            deeper = [visit(t, stack | {name}) for t in targets[name] if t not in stack]
            depth[name] = 1 + max(deeper) if deeper else 0
        return depth[name]

    for name in names:
        visit(name, frozenset())
    return depth


def _isotonic(values: list[float]) -> list[float]:
    """The closest (least-squares) non-decreasing sequence: pool adjacent violators."""
    blocks: list[list[float]] = []  # [sum, count]
    for v in values:
        blocks.append([v, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, n = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += n
    return [s / n for s, n in blocks for _ in range(int(n))]


# Items in a layout column: ("t", class name, col) for tables, ("l", fk id, col) for lanes.
_Item = tuple[str, str, int]
SWEEPS = 24  # most barycenter + sift rounds per ordering tried
RESTARTS = 8  # orderings tried (see the loop below); shuffles are seeded, so output is stable
PLACE_PASSES = 8  # rounds of sliding tables towards their neighbours


def auto_layout(schema: Schema) -> Layout:
    """Place tables in columns by foreign-key depth (a Sugiyama-style layout).

    Referencing tables sit to the left of what they point at, so edges flow
    left-to-right, and edges that skip columns reserve a lane in every column
    they cross. Each column is ordered to minimise crossings -- barycenter
    sweeps, then each item moved to its best spot in its column (sifting),
    keeping the best order seen and counting where each edge really attaches
    (its row, not just its table) -- and then tables slide up or down towards
    the rows they connect to, so edges run as straight as that order allows.
    Inverse edges (see `parse._mark_inverses`) don't shape the layout; they
    follow the lanes of the edge they restate.
    """
    classes = {c.name: c for c in schema.classes}
    fks = [fk for fk in schema.foreign_keys if not fk.inverse]
    depth = _depths(list(classes), fks)
    max_depth = max(depth.values(), default=0)
    col_of = {name: max_depth - d for name, d in depth.items()}

    columns: list[list[_Item]] = [[] for _ in range(max_depth + 1)]
    for c in schema.classes:
        columns[col_of[c.name]].append(("t", c.name, col_of[c.name]))
    height = {item: table_height(classes[item[1]]) for col in columns for item in col}

    def port(name: str, slot: str | None) -> float:
        """Where an edge meets a table, as a fraction of the table's height."""
        c = classes[name]
        return (row_y(Box(0, 0, 0), _slot_index(c, slot))) / table_height(c)

    # Each edge piece between neighbouring columns: (left item, its port, right item, its port).
    segments: list[list[tuple[_Item, float, _Item, float]]] = [[] for _ in range(max_depth)]
    # Per item, its pieces: (side -1 left / +1 right, other item, other's port, own port).
    adj: dict[_Item, list[tuple[int, _Item, float, float]]] = {}
    for fk in fks:
        a, b = col_of[fk.from_class], col_of[fk.to_class]
        if a == b:
            continue
        step = 1 if b > a else -1
        chain = [(("t", fk.from_class, a), port(fk.from_class, fk.from_slot))]
        for col in range(a + step, b, step):
            lane: _Item = ("l", fk.id, col)
            columns[col].append(lane)
            height[lane] = ROW_H
            chain.append((lane, 0.5))
        chain.append((("t", fk.to_class, b), port(fk.to_class, fk.to_slot)))
        for (u, pu), (v, pv) in pairwise(chain):
            if u[2] > v[2]:
                (u, pu), (v, pv) = (v, pv), (u, pu)
            segments[u[2]].append((u, pu, v, pv))
            adj.setdefault(u, []).append((1, v, pv, pu))
            adj.setdefault(v, []).append((-1, u, pu, pv))

    # --- order each column to minimise crossings ---------------------------------
    pos: dict[_Item, int] = {}

    def reindex(col: int) -> None:
        pos.update((item, i) for i, item in enumerate(columns[col]))

    def crossings() -> int:
        total = 0
        for seg in segments:
            ends = [(pos[u] + pu, pos[v] + pv) for u, pu, v, pv in seg]
            total += sum((a1 - a2) * (b1 - b2) < 0 for (a1, b1), (a2, b2) in combinations(ends, 2))
        return total

    def sweep(order: range, side: int, rows: bool) -> None:
        """Sort columns by the mean position of their neighbours on one side (barycenter).

        With `rows`, positions include which row each edge attaches to; without, only
        which item -- coarser, but it sometimes settles in a better optimum.
        """
        for col in order:

            def key(item: _Item) -> float:
                hits = [pos[o] + (po - own) * rows for s, o, po, own in adj.get(item, ()) if s == side]
                return sum(hits) / len(hits) if hits else pos[item]

            columns[col].sort(key=key)
            reindex(col)

    def swap_gain(upper: _Item, lower: _Item) -> int:
        """How many fewer crossings there would be with `lower` moved above `upper`."""
        gain = 0
        for s, o, po, _ in adj.get(upper, ()):
            for t, q, pq, _ in adj.get(lower, ()):
                if s == t:
                    a, b = pos[o] + po, pos[q] + pq
                    gain += (a > b) - (a < b)
        return gain

    def sift() -> None:
        """Move each item to the spot in its column where it crosses the fewest edges."""
        for col in range(len(columns)):
            for item in list(columns[col]):
                others = [o for o in columns[col] if o != item]
                # Crossings saved by having `item` below each other item rather than above it.
                saved = [swap_gain(item, o) for o in others]
                best_at, best_cost, cost = 0, 0, 0
                for j, g in enumerate(saved, 1):
                    cost -= g
                    if cost < best_cost or (cost == best_cost and j == pos[item]):
                        best_at, best_cost = j, cost
                columns[col] = [*others[:best_at], item, *others[best_at:]]
                reindex(col)

    initial = [list(c) for c in columns]
    best: list[list[_Item]] = []
    fewest = -1
    for start in range(RESTARTS):
        # Sweeps settle in a local optimum, so try from the schema's order with and
        # without row positions, then from shuffles of the best order so far.
        columns = [list(c) for c in (initial if start < 2 else best)]
        if start >= 2:
            for items in columns:
                random.Random(start).shuffle(items)
        for col in range(len(columns)):
            reindex(col)
        n, stale = crossings(), 0
        if fewest < 0 or n < fewest:
            best, fewest = [list(c) for c in columns], n
        for _ in range(SWEEPS):
            if not fewest or stale >= 4:
                break
            sweep(range(1, len(columns)), -1, rows=start % 2 == 0)
            sweep(range(len(columns) - 2, -1, -1), +1, rows=start % 2 == 0)
            sift()
            n = crossings()
            stale = 0 if n < fewest else stale + 1
            if n < fewest:
                best, fewest = [list(c) for c in columns], n
        if not fewest:
            break
    columns = best

    # --- vertical placement: line edges up, keeping column order and gaps ---------
    def gap_after(item: _Item) -> float:
        return ROW_GAP if item[0] == "t" else ROW_GAP / 2

    top: dict[_Item, float] = {}
    for items in columns:
        y: float = 0
        for item in items:
            top[item] = y
            y += height[item] + gap_after(item)

    for p in range(PLACE_PASSES):
        for col in range(len(columns)) if p % 2 == 0 else range(len(columns) - 1, -1, -1):
            items = columns[col]
            want = []
            for item in items:
                pulls = [top[o] + po * height[o] - own * height[item] for _, o, po, own in adj.get(item, ())]
                want.append(sum(pulls) / len(pulls) if pulls else top[item])
            offsets = [0.0]
            for item in items[:-1]:
                offsets.append(offsets[-1] + height[item] + gap_after(item))
            slid = _isotonic([w - off for w, off in zip(want, offsets)])
            top.update((item, z + off) for item, z, off in zip(items, slid, offsets))

    shift = PAD - min(top.values(), default=PAD)
    boxes: dict[str, Box] = {}
    lane_y: dict[tuple[str, int], float] = {}
    for item, y in top.items():
        kind, ref, col = item
        y = round(y + shift)
        if kind == "t":
            boxes[ref] = Box(x=column_x(col), y=y, h=height[item])
        else:
            lane_y[(ref, col)] = y + ROW_H / 2

    waypoints: dict[str, list[Point]] = {}
    for fk in fks:
        a, b = col_of[fk.from_class], col_of[fk.to_class]
        forward = b > a
        points: list[Point] = []
        for col in range(a + (1 if forward else -1), b, 1 if forward else -1):
            left, y = column_x(col), lane_y[(fk.id, col)]
            points += [(left, y), (left + TABLE_W, y)] if forward else [(left + TABLE_W, y), (left, y)]
        if points:
            waypoints[fk.id] = points
    # An inverse edge runs back along the lanes of the edge it restates, just below it.
    for fk in schema.foreign_keys:
        if fk.inverse_of in waypoints:
            waypoints[fk.id] = [(x, y + INVERSE_SHIFT) for x, y in reversed(waypoints[fk.inverse_of])]
    return Layout(boxes=boxes, waypoints=waypoints)


def apply_saved_layout(schema: Schema, auto: Layout, saved: dict[str, Any]) -> Layout:
    """Pin tables where the user left them on the canvas.

    Tables the saved layout knows keep their position; tables new to the
    schema keep their auto-layout column but are stacked below the pinned
    ones, so they never land on top of them. Saved edge waypoints are reused
    only while both ends of the edge are pinned tables.
    """
    pins: dict[str, Box] = {}
    for c in schema.classes:
        pos = (saved.get("tables") or {}).get(c.name)
        if isinstance(pos, dict) and all(isinstance(pos.get(k), (int, float)) for k in ("x", "y")):
            pins[c.name] = Box(x=pos["x"], y=pos["y"], h=table_height(c))
    if not pins:
        return auto

    below = max(b.y + b.h for b in pins.values()) + ROW_GAP * 2
    next_y: dict[float, float] = {}
    boxes: dict[str, Box] = {}
    for c in schema.classes:
        if c.name in pins:
            boxes[c.name] = pins[c.name]
        else:
            x = auto.boxes[c.name].x
            y = next_y.get(x, below)
            boxes[c.name] = Box(x=x, y=y, h=table_height(c))
            next_y[x] = y + boxes[c.name].h + ROW_GAP

    waypoints: dict[str, list[Point]] = {}
    saved_edges = saved.get("edges") or {}
    for fk in schema.foreign_keys:
        pts = saved_edges.get(fk.id)
        if fk.from_class in pins and fk.to_class in pins and isinstance(pts, list):
            try:
                waypoints[fk.id] = [(float(x), float(y)) for x, y in pts]
            except (TypeError, ValueError):
                pass
    return Layout(boxes=boxes, waypoints=waypoints)


def read_saved_data(html_path: Path) -> dict[str, Any]:
    """The embedded data of a page previously written by linkml-herd (or saved from it), or {}."""
    try:
        text = html_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    # "bp-data": pages written before the rename to linkml-herd.
    m = re.search(r'<script[^>]*\bid="(?:herd|bp)-data"[^>]*>(.*?)</script>', text, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def read_saved_layout(html_path: Path) -> dict[str, Any]:
    """Pull the canvas layout out of a page previously written by linkml-herd (or saved from it)."""
    layout = read_saved_data(html_path).get("layout")
    return layout if isinstance(layout, dict) else {}


def read_saved_inverse(html_path: Path) -> bool:
    """Whether the page was saved with inverse relationships shown."""
    return read_saved_data(html_path).get("showInverse") is True


def read_saved_theme(html_path: Path) -> str | None:
    """The theme a previous `linkml-herd --theme` baked into the page, if it is still one we know."""
    theme = read_saved_data(html_path).get("theme")
    return theme if theme in THEMES else None


# --- edge routing (mirrored in static/canvas.js) ----------------------------


def num(v: float) -> str:
    """Round to 0.1 and print the way JS's String(number) does."""
    r = math.floor(v * 10 + 0.5) / 10
    return str(int(r)) if r == int(r) else repr(r)


def row_y(box: Box, index: int) -> float:
    """Vertical centre of the index-th column row, or of the header when index < 0."""
    if index < 0:
        return box.y + BORDER + HEADER_H / 2
    return box.y + BORDER + HEADER_H + index * ROW_H + ROW_H / 2


def edge_path(src: Box, sy: float, dst: Box, ty: float, waypoints: list[Point]) -> tuple[str, Point]:
    """SVG path between two rows, plus where to put the cardinality label.

    Curves leave/enter the facing sides of the two tables and cross any
    intermediate columns straight through their reserved lane (`waypoints`).
    """
    if not waypoints and dst.x < src.right and src.x < dst.right:
        # Tables overlap horizontally (incl. self-references): loop out to the right.
        sx, tx = src.right, dst.right
        bulge = LOOP + abs(ty - sy) / 6
        d = f"M{num(sx)},{num(sy)} C{num(sx + bulge)},{num(sy)} {num(tx + bulge)},{num(ty)} {num(tx)},{num(ty)}"
        return d, (max(sx, tx) + bulge * 0.75, (sy + ty) / 2)

    forward = dst.x > src.x
    sx, tx = (src.right, dst.x) if forward else (src.x, dst.right)
    anchors = [(sx, sy), *waypoints, (tx, ty)]
    d = f"M{num(sx)},{num(sy)}"
    for j in range(1, len(anchors)):
        (ax, ay), (bx, by) = anchors[j - 1], anchors[j]
        if j % 2:  # across a gap between columns
            mx = (ax + bx) / 2
            d += f" C{num(mx)},{num(ay)} {num(mx)},{num(by)} {num(bx)},{num(by)}"
        else:  # straight through a lane
            d += f" L{num(bx)},{num(by)}"
    (ax, ay), (bx, by) = anchors[0], anchors[1]
    return d, ((ax + bx) / 2, (ay + by) / 2)


# --- HTML --------------------------------------------------------------------


def _slot_index(c: Class, slot: str | None) -> int:
    return next((i for i, s in enumerate(c.slots) if s.name == slot), -1)


def _edge_geometry(fk: ForeignKey, classes: dict[str, Class], layout: Layout) -> tuple[str, Point]:
    src, dst = layout.boxes[fk.from_class], layout.boxes[fk.to_class]
    sy = row_y(src, _slot_index(classes[fk.from_class], fk.from_slot))
    ty = row_y(dst, _slot_index(classes[fk.to_class], fk.to_slot))
    return edge_path(src, sy, dst, ty, layout.waypoints.get(fk.id, []))


def _edge_svg(fk: ForeignKey, classes: dict[str, Class], layout: Layout) -> str:
    waypoints = layout.waypoints.get(fk.id, [])
    d, (lx, ly) = _edge_geometry(fk, classes, layout)

    attrs = {
        "data-id": fk.id,
        "data-from": fk.from_class,
        "data-from-slot": fk.from_slot,
        "data-to": fk.to_class,
        "data-to-slot": fk.to_slot or "",
    }
    if waypoints:
        attrs["data-waypoints"] = json.dumps([[x, y] for x, y in waypoints])
    title = f"{fk.from_class}.{fk.from_slot} → {fk.to_class}" + (f".{fk.to_slot}" if fk.to_slot else "")
    if fk.inverse_of:
        attrs["data-inverse-of"] = fk.inverse_of
        title += f" (inverse of {fk.inverse_of.split('->')[0]})"
    return (
        f'<g class="edge{" inverse" if fk.inverse else ""}"'
        + "".join(f' {k}="{escape(v)}"' for k, v in attrs.items())
        + ">"
        f"<title>{escape(title)}</title>"
        f'<path d="{d}" marker-end="url(#arrow)"/>'
        f'<text x="{num(lx)}" y="{num(ly)}">{escape(fk.cardinality)}</text></g>'
    )


def _table_html(c: Class, box: Box) -> str:
    rows = []
    for s in c.slots:
        kinds = [k for k, on in (("pk", s.pk), ("fk", s.ref_class), ("unresolved", s.unresolved)) if on]
        marker = "⚠️" if s.unresolved else "🔑" if s.pk else "🔗" if s.ref_class else ""
        tip = f'Unknown range "{s.range}"' if s.unresolved else (s.description or "")
        rows.append(
            f'<div class="row {" ".join(kinds)}" data-slot="{escape(s.name)}"'
            + (f' title="{escape(tip)}"' if tip else "")
            + f'><span class="marker">{marker}</span>'
            f'<span class="name">{escape(s.name)}{"<span class=req>*</span>" if s.required else ""}</span>'
            f'<span class="type">{escape(s.range)}{"[]" if s.multivalued else ""}</span></div>'
        )
    if not rows:
        rows.append('<div class="row empty"><span></span><span class="name">no columns</span></div>')
    desc = (
        f'<span class="desc" title="{escape(c.description)}">{escape(c.description)}</span>'
        if c.description
        else ""
    )
    return (
        f'<section class="table" id="{escape(c.name)}" data-name="{escape(c.name)}"'
        f' style="left:{num(box.x)}px;top:{num(box.y)}px">'
        f'<header><span class="title">{escape(c.name)}</span>{desc}</header>' + "".join(rows) + "</section>"
    )


def _issues_html(schema: Schema) -> str:
    n = len(schema.problems)
    if not n:
        return '<span class="stat"><strong>0</strong> issues</span>'
    items = "".join(
        f'<li title="{escape(p.message)}"><a href="#{escape(p.class_name)}" data-table="{escape(p.class_name)}">'
        f"<code>{escape(p.class_name)}.{escape(p.slot)}</code></a> — {escape(p.message)}</li>"
        for p in schema.problems
    )
    return (
        f'<details class="issues"><summary>⚠️ <strong>{n}</strong> issue{"s" if n != 1 else ""}</summary>'
        f'<ul class="problems">{items}</ul></details>'
    )


def _json_script(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _asset(name: str) -> str:
    return files("linkml_herd").joinpath("static", name).read_text(encoding="utf-8")


# "auto" follows the OS light/dark setting; the rest are the blocks in themes.css.
THEMES = ("auto", *re.findall(r':root\[data-theme="([\w-]+)"\]', _asset("themes.css")))
DEFAULT_THEME = "monokai"
REPO_URL = "https://github.com/ConradMearns/linkml-herd"


def _theme_menu(theme: str) -> str:
    options = "".join(
        f'<option value="{t}"{" selected" if t == theme else ""}>{t.capitalize()}</option>' for t in THEMES
    )
    return f'<select id="theme" title="Theme" aria-label="Theme">{options}</select>'


def _bounds(layout: Layout) -> tuple[float, float, float, float]:
    """Extent of everything drawn, in canvas coordinates (positions may be negative)."""
    xs = [b.x for b in layout.boxes.values()] + [b.right + 2 * LOOP for b in layout.boxes.values()]
    ys = [b.y for b in layout.boxes.values()] + [b.y + b.h for b in layout.boxes.values()]
    for pts in layout.waypoints.values():
        xs += [x for x, _ in pts]
        ys += [y + d for _, y in pts for d in (-ROW_H, ROW_H)]
    return (min(xs, default=0), min(ys, default=0), max(xs, default=0), max(ys, default=0))


# --- SVG image -----------------------------------------------------------------
# The same diagram drawn with plain SVG shapes and text (no HTML, no CSS), for
# `linkml-herd --svg` and the page's "Export SVG" button. Colours come from a `Color`
# lookup: literal theme values for a file, or var(--token) in the page's
# template, which the button fills in from the theme being shown.

Color = Callable[[str], str]
SANS = "ui-sans-serif, system-ui, -apple-system, sans-serif"
MONO = "ui-monospace, SFMono-Regular, monospace"


def css_var(token: str) -> str:
    return f"var(--{token})"


def theme_colors(theme: str) -> Color:
    """Look up colour tokens as the given theme sets them ("auto" draws as light)."""
    css = _asset("themes.css")
    tokens: dict[str, str] = {}
    for name in ("light", theme):
        m = re.search(r':root\[data-theme="' + re.escape(name) + r'"\]\s*\{([^}]*)\}', css)
        if m:
            tokens.update(re.findall(r"--([\w-]+):\s*([^;]+?)\s*;", m.group(1)))
    return tokens.__getitem__


def _fit(text: str, width: float, char_w: float) -> str:
    """Shorten text to roughly `width` px (SVG can't ellipsize), given an average glyph width."""
    if len(text) * char_w <= width:
        return text
    return text[: max(1, int(width / char_w) - 1)].rstrip() + "…"


def _svg_text(x: float, y: float, content: str, color: str, size: float, **attrs: str) -> str:
    """Text vertically centred on y."""
    attrs = {"font_family": SANS, **attrs}
    extra = "".join(f' {k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    return f'<text x="{num(x)}" y="{num(y)}" dy="0.35em" font-size="{size}" fill="{color}"{extra}>{content}</text>'


def _table_image(c: Class, box: Box, color: Color, clip_id: str) -> str:
    w, h = TABLE_W, table_height(c)
    inner_x, right = BORDER + 12, TABLE_W - BORDER - 12  # .row / header padding
    shapes = [
        f'<rect width="{w}" height="{num(h)}" fill="{color("card")}"/>',
        f'<rect width="{w}" height="{BORDER + HEADER_H}" fill="{color("head-bg")}"/>',
    ]
    texts = []
    if c.description:
        texts.append(
            _svg_text(inner_x, BORDER + 16, escape(c.name), color("head-text"), 12, font_weight="700")
        )
        desc = _fit(c.description, right - inner_x, 5.4)
        texts.append(_svg_text(inner_x, BORDER + 31, escape(desc), color("head-muted"), 10))
    else:
        texts.append(
            _svg_text(
                inner_x, BORDER + HEADER_H / 2, escape(c.name), color("head-text"), 12, font_weight="700"
            )
        )

    for i, s in enumerate(c.slots or [None]):
        top = BORDER + HEADER_H + i * ROW_H
        mid = top + ROW_H / 2
        if s is None:
            texts.append(_svg_text(inner_x + 26, mid, "no columns", color("muted"), 12, font_style="italic"))
        else:
            bg = "bad-row" if s.unresolved else "pk-row" if s.pk else "fk-row" if s.ref_class else None
            if bg:
                shapes.append(f'<rect y="{top}" width="{w}" height="{ROW_H}" fill="{color(bg)}"/>')
            if s.unresolved:
                shapes.append(
                    f'<rect x="{BORDER}" y="{top}" width="3" height="{ROW_H}" fill="{color("bad")}"/>'
                )
            marker = "⚠️" if s.unresolved else "🔑" if s.pk else "🔗" if s.ref_class else ""
            if marker:
                texts.append(_svg_text(inner_x + 9, mid, marker, color("text"), 10, text_anchor="middle"))
            typ = s.range + ("[]" if s.multivalued else "")
            name_c, type_c = (
                (color("bad-text"), color("bad-text")) if s.unresolved else (color("text"), color("type"))
            )
            room = right - (inner_x + 26) - len(typ) * 6.6 - 8 - (8 if s.required else 0)
            req = f'<tspan fill="{color("bad")}">*</tspan>' if s.required else ""
            texts.append(
                _svg_text(
                    inner_x + 26,
                    mid,
                    escape(_fit(s.name, room, 7)) + req,
                    name_c,
                    12,
                    font_weight="700" if s.pk else "500",
                )
            )
            texts.append(_svg_text(right, mid, escape(typ), type_c, 11, font_family=MONO, text_anchor="end"))
        shapes.append(f'<rect y="{top}" width="{w}" height="1" fill="{color("card-rule")}"/>')

    return (
        f'<g data-name="{escape(c.name)}" transform="translate({num(box.x)},{num(box.y)})">'
        f'<clipPath id="{clip_id}"><rect width="{w}" height="{num(h)}" rx="8"/></clipPath>'
        f'<g clip-path="url(#{clip_id})">{"".join(shapes)}</g>'
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{num(h - 1)}" rx="7.5" fill="none"'
        f' stroke="{color("card-border")}"/>' + "".join(texts) + "</g>"
    )


def _edge_image(fk: ForeignKey, classes: dict[str, Class], layout: Layout, color: Color) -> str:
    d, (lx, ly) = _edge_geometry(fk, classes, layout)
    inverse = ' data-inverse=""' if fk.inverse else ""
    dashes = ' stroke-dasharray="4 3"' if fk.inverse else ""
    return (
        f'<g data-id="{escape(fk.id)}"{inverse}><path d="{d}" fill="none" stroke="{color("edge")}"{dashes}'
        ' marker-end="url(#herd-arrow)"/>'
        + _svg_text(
            lx,
            ly,
            escape(fk.cardinality),
            color("edge-label"),
            10,
            text_anchor="middle",
            stroke=color("canvas-bg"),
            stroke_width="5",
            stroke_linejoin="round",
            paint_order="stroke",
        )
        + "</g>"
    )


def _svg_image(schema: Schema, layout: Layout, color: Color, show_inverse: bool = True) -> str:
    classes = {c.name: c for c in schema.classes}
    x0, y0, x1, y1 = _bounds(layout)
    x, y, w, h = (num(v) for v in (x0 - PAD, y0 - PAD, x1 - x0 + 2 * PAD, y1 - y0 + 2 * PAD))
    edges = "".join(
        _edge_image(fk, classes, layout, color)
        for fk in schema.foreign_keys
        if show_inverse or not fk.inverse
    )
    tables = "".join(
        _table_image(c, layout.boxes[c.name], color, f"herd-clip-{i}") for i, c in enumerate(schema.classes)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}" width="{w}" height="{h}">'
        f"<title>{escape(schema.title or schema.name)}</title>"
        '<defs><marker id="herd-arrow" viewBox="-10 -10 20 20" refX="0" refY="0" markerWidth="18"'
        ' markerHeight="18" markerUnits="userSpaceOnUse" orient="auto-start-reverse">'
        f'<polyline points="-5,-4 0,0 -5,4 -5,-4" fill="{color("edge")}" stroke="{color("edge")}"'
        ' stroke-linejoin="round"/></marker></defs>'
        f'<rect data-bg="" x="{x}" y="{y}" width="{w}" height="{h}" fill="{color("canvas-bg")}"/>'
        f"{edges}{tables}</svg>"
    )


def render_svg(
    schema: Schema,
    *,
    saved_layout: dict[str, Any] | None = None,
    theme: str = DEFAULT_THEME,
    show_inverse: bool = False,
) -> str:
    """Render the diagram as a standalone SVG image in `theme`'s colours ("auto" draws as light).

    Inverse relationships (see `parse._mark_inverses`) are left out unless `show_inverse`.
    """
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r} (choose from {', '.join(THEMES)})")
    layout = apply_saved_layout(schema, auto_layout(schema), saved_layout or {})
    image = _svg_image(schema, layout, theme_colors(theme), show_inverse)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + image + "\n"


def render_html(
    schema: Schema,
    *,
    saved_layout: dict[str, Any] | None = None,
    source: str | None = None,
    output_name: str | None = None,
    theme: str = DEFAULT_THEME,
    show_inverse: bool = False,
) -> str:
    """Render the schema as a complete, self-contained HTML document.

    `saved_layout` is the canvas layout read back from a previous render (see
    `read_saved_layout`); tables it mentions keep their position. `theme` is
    one of THEMES; viewers can still switch theme in the page. Inverse
    relationships are drawn but hidden unless `show_inverse`; the page has a
    toggle for them.
    """
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r} (choose from {', '.join(THEMES)})")
    auto = auto_layout(schema)
    layout = apply_saved_layout(schema, auto, saved_layout or {})
    pinned = layout is not auto
    classes = {c.name: c for c in schema.classes}

    # Without JS the page just scrolls, so shift everything into view; the
    # script replaces this with a pannable, zoomable view fitted to the content.
    x0, y0, x1, y1 = _bounds(layout)
    width, height = x1 - x0 + 2 * PAD, y1 - y0 + 2 * PAD

    edges = "".join(_edge_svg(fk, classes, layout) for fk in schema.foreign_keys)
    tables = "".join(_table_html(c, layout.boxes[c.name]) for c in schema.classes)
    heading = escape(schema.title or schema.name)

    data = {
        "file": output_name,
        "theme": theme,
        "showInverse": show_inverse,
        "geometry": {
            "tableW": TABLE_W,
            "headerH": HEADER_H,
            "rowH": ROW_H,
            "border": BORDER,
            "loop": LOOP,
            "pad": PAD,
        },
        # What "Save canvas" writes and the next `linkml-herd` run reads back.
        "layout": {
            "tables": {n: {"x": b.x, "y": b.y} for n, b in layout.boxes.items()},
            "edges": {k: [[x, y] for x, y in v] for k, v in layout.waypoints.items()},
        }
        if pinned
        else {},
        # Where "Reset layout" puts things.
        "auto": {
            "tables": {n: {"x": b.x, "y": b.y} for n, b in auto.boxes.items()},
            "edges": {k: [[x, y] for x, y in v] for k, v in auto.waypoints.items()},
        },
    }

    file_label = ""
    if source:
        target = f" → {escape(output_name)}" if output_name else ""
        file_label = f'<span class="file" title="{escape(source)}{target}">{escape(source)}{target}</span>'
    description = f' title="{escape(schema.description)}"' if schema.description else ""
    inverses = sum(fk.inverse for fk in schema.foreign_keys)
    inverse_toggle = (
        f'<label class="toggle" title="Also draw relationships that restate another from its other end,'
        f' like a list of children beside each child\'s reference back"><input type="checkbox" id="inverse"'
        f"{' checked' if show_inverse else ''}> inverses ({inverses})</label>"
        if inverses
        else ""
    )
    root_attrs = ("" if theme == "auto" else f' data-theme="{theme}"') + (
        ' class="show-inverse"' if show_inverse else ""
    )

    return f"""<!doctype html>
<html lang="en"{root_attrs}>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{heading}</title>
<script>try {{ const r = document.documentElement, t = localStorage.getItem("herd-theme:" + location.pathname + ":" + (r.dataset.theme || "auto")); if (t === "auto") delete r.dataset.theme; else if (t) r.dataset.theme = t; }} catch {{}}</script>
<style>
:root {{ --table-w: {TABLE_W}px; --header-h: {HEADER_H}px; --row-h: {ROW_H}px; }}
{_asset("themes.css")}
{_asset("style.css")}</style>
</head>
<body>
<header class="bar">
<a class="brand" href="{REPO_URL}" target="_blank" rel="noopener" title="linkml-herd on GitHub">linkml-herd</a>
<h1{description}>{heading}</h1>
{file_label}
<span class="stat"><strong>{len(schema.classes)}</strong> classes</span>
<span class="stat"><strong>{len(schema.foreign_keys) - inverses}</strong> relations</span>
{_issues_html(schema)}
<span class="save-state" id="save-state"></span>
<span class="actions" id="actions" hidden>
{inverse_toggle}
{_theme_menu(theme)}
<button id="export" title="Download the diagram as an SVG image">Export SVG</button>
<button id="reset" title="Discard manual positions and use the automatic layout">Reset layout</button>
<button id="save" class="primary" title="Write this page back out with the current canvas layout">Save canvas</button>
</span>
</header>
<main class="viewport" id="viewport">
<div class="sizer" id="sizer" style="width:{num(width)}px;height:{num(height)}px">
<div class="world" id="world" style="transform:translate({num(PAD - x0)}px,{num(PAD - y0)}px)">
<svg class="edges" id="edges" width="1" height="1" aria-hidden="true">
<defs><marker id="arrow" viewBox="-10 -10 20 20" refX="0" refY="0" markerWidth="18" markerHeight="18" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><polyline points="-5,-4 0,0 -5,4 -5,-4"/></marker></defs>
{edges}
</svg>
{tables}
</div>
</div>
<div class="legend"><span><span class="dot pk"></span> primary key</span><span><span class="dot fk"></span> foreign key</span></div>
<div class="controls" id="controls" hidden>
<button id="zoom-in" title="Zoom in">+</button>
<button id="zoom-out" title="Zoom out">−</button>
<button id="fit" title="Fit view">⛶</button>
<span id="zoom-level">100%</span>
</div>
</main>
<template id="herd-svg">{_svg_image(schema, layout, css_var)}</template>
<script type="application/json" id="herd-data">{_json_script(data)}</script>
<script>
{_asset("canvas.js")}</script>
</body>
</html>
"""
