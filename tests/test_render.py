import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from linkml_herd.cli import main, output_path
from linkml_herd.parse import parse_linkml, parse_linkml_file
from linkml_herd.render import (
    COL_GAP,
    TABLE_W,
    THEMES,
    apply_saved_layout,
    auto_layout,
    num,
    read_saved_layout,
    render_html,
    render_svg,
)

SVG = "{http://www.w3.org/2000/svg}"
EXAMPLE = Path(__file__).parent.parent / "examples" / "ecommerce.linkml.yaml"


def embedded_data(html: str) -> dict:
    m = re.search(r'<script type="application/json" id="herd-data">(.*?)</script>', html, re.DOTALL)
    assert m
    return json.loads(m.group(1))


def save_canvas(html_path: Path, tables: dict, edges: dict | None = None) -> None:
    """Do what the page's "Save canvas" button does to the embedded layout."""
    html = html_path.read_text()
    data = embedded_data(html)
    data["layout"] = {"tables": tables, "edges": edges or {}}
    payload = json.dumps(data).replace("</", "<\\/")
    html_path.write_text(
        re.sub(
            r'(id="herd-data">).*?(</script>)',
            lambda m: m.group(1) + payload + m.group(2),
            html,
            flags=re.DOTALL,
        )
    )


def table_positions(html: str) -> dict[str, tuple[float, float]]:
    found = re.findall(r'data-name="(\w+)" style="left:([\d.]+)px;top:([\d.]+)px', html)
    return {name: (float(x), float(y)) for name, x, y in found}


def table_positions_signed(html: str) -> dict[str, tuple[float, float]]:
    found = re.findall(r'data-name="(\w+)" style="left:(-?[\d.]+)px;top:(-?[\d.]+)px', html)
    return {name: (float(x), float(y)) for name, x, y in found}


def test_output_path_is_a_neighbour_html_file():
    assert output_path(Path("mydb.yaml")) == Path("mydb.html")
    assert output_path(Path("dir/schema.linkml.yaml")) == Path("dir/schema.linkml.html")
    assert output_path(Path("weird.yml")) == Path("weird.html")


def test_num_matches_js_number_formatting():
    assert [num(v) for v in (300.0, 312.5, 12.25, 0.04, 1234567.0)] == [
        "300",
        "312.5",
        "12.3",
        "0",
        "1234567",
    ]


# --- auto layout ---------------------------------------------------------------


def test_referencing_tables_sit_left_of_referenced_ones():
    schema = parse_linkml_file(EXAMPLE)
    boxes = auto_layout(schema).boxes
    for fk in schema.foreign_keys:
        if fk.from_class != fk.to_class:
            assert boxes[fk.from_class].x < boxes[fk.to_class].x, fk


def test_tables_do_not_overlap():
    boxes = list(auto_layout(parse_linkml_file(EXAMPLE)).boxes.values())
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert a.x + TABLE_W <= b.x or b.x + TABLE_W <= a.x or a.y + a.h <= b.y or b.y + b.h <= a.y


def test_long_edges_get_a_lane_per_skipped_column():
    schema = parse_linkml_file(EXAMPLE)
    layout = auto_layout(schema)
    for fk in schema.foreign_keys:
        span = abs(layout.boxes[fk.to_class].x - layout.boxes[fk.from_class].x) // (TABLE_W + COL_GAP)
        assert len(layout.waypoints.get(fk.id, [])) == 2 * max(0, span - 1), fk.id


# --- saved layout ----------------------------------------------------------------

TWO = "name: x\nclasses:\n  A:\n    attributes:\n      b: { range: B }\n  B:\n    attributes:\n      id: { identifier: true }\n"


def test_saved_positions_are_kept():
    schema = parse_linkml(TWO)
    layout = apply_saved_layout(
        schema, auto_layout(schema), {"tables": {"A": {"x": 500, "y": 20}, "B": {"x": 0, "y": 0}}}
    )
    assert (layout.boxes["A"].x, layout.boxes["A"].y) == (500, 20)
    assert (layout.boxes["B"].x, layout.boxes["B"].y) == (0, 0)


def test_new_tables_are_placed_below_pinned_ones():
    schema = parse_linkml(TWO + "  C:\n    attributes:\n      a: { range: A }\n  D: {}\n")
    saved = {"tables": {"A": {"x": 400, "y": 300}, "B": {"x": 0, "y": 0}, "Gone": {"x": 9, "y": 9}}}
    boxes = apply_saved_layout(schema, auto_layout(schema), saved).boxes
    pinned_bottom = max(boxes[n].y + boxes[n].h for n in ("A", "B"))
    assert boxes["C"].y > pinned_bottom and boxes["D"].y > pinned_bottom
    assert "Gone" not in boxes


def test_saved_edge_lanes_only_survive_between_pinned_tables():
    schema = parse_linkml(TWO + "  C:\n    attributes:\n      a: { range: A }\n")
    lane = [[10, 20], [30, 20]]
    saved = {
        "tables": {"A": {"x": 400, "y": 0}, "B": {"x": 900, "y": 0}},
        "edges": {"A.b->B": lane, "C.a->A": lane},
    }
    layout = apply_saved_layout(schema, auto_layout(schema), saved)
    assert layout.waypoints == {"A.b->B": [(10.0, 20.0), (30.0, 20.0)]}


def test_malformed_saved_layout_falls_back_to_auto():
    schema = parse_linkml(TWO)
    auto = auto_layout(schema)
    assert apply_saved_layout(schema, auto, {"tables": {"A": {"x": "nope"}}, "edges": 3}) is auto


# --- HTML ----------------------------------------------------------------------


def test_html_draws_everything_without_javascript():
    schema = parse_linkml_file(EXAMPLE)
    html = render_html(schema)
    assert html.startswith("<!doctype html>")
    assert html.count('class="table"') == len(schema.classes)
    assert html.count('<g class="edge"') == len(schema.foreign_keys)
    # Interactive controls stay hidden until the script reveals them.
    assert '<span class="actions" id="actions" hidden>' in html
    assert '<div class="controls" id="controls" hidden>' in html


def test_schema_source_is_not_embedded():
    html = render_html(parse_linkml_file(EXAMPLE), source="ecommerce.linkml.yaml")
    assert "prefixes:" not in html and "default_range" not in html


def test_negative_positions_are_shifted_into_view_without_js():
    schema = parse_linkml(TWO)
    html = render_html(schema, saved_layout={"tables": {"A": {"x": -500, "y": -40}, "B": {"x": 0, "y": 0}}})
    assert table_positions_signed(html)["A"] == (-500, -40)  # layout itself is untouched
    m = re.search(r'id="world" style="transform:translate\(([\d.]+)px,([\d.]+)px\)"', html)
    assert m and float(m.group(1)) >= 500 and float(m.group(2)) >= 40


def test_fresh_render_has_no_pinned_layout():
    data = embedded_data(render_html(parse_linkml_file(EXAMPLE)))
    assert data["layout"] == {}
    assert set(data["auto"]["tables"]) == {"Customer", "Address", "Category", "Product", "Order", "OrderLine"}


def test_html_escapes_schema_text():
    html = render_html(parse_linkml('name: x\nclasses:\n  A:\n    description: "<b>&</script>"\n'))
    assert "<b>&" not in html
    assert "&lt;b&gt;&amp;&lt;/script&gt;" in html
    assert html.count("</script>") == 3  # theme + data block + canvas script; nothing leaked


def test_problems_are_listed():
    html = render_html(parse_linkml("name: x\nclasses:\n  A:\n    attributes:\n      b: { range: Nope }\n"))
    assert "⚠️ <strong>1</strong> issue</summary>" in html
    assert "range &quot;Nope&quot; is not a known class" in html


# --- CLI -------------------------------------------------------------------------


def test_cli_writes_neighbour_file(tmp_path):
    schema = tmp_path / "shop.yaml"
    schema.write_text(EXAMPLE.read_text())
    assert main([str(schema)]) == 0
    assert "OrderLine" in (tmp_path / "shop.html").read_text()


def test_cli_keeps_canvas_edits_across_schema_changes(tmp_path):
    schema = tmp_path / "shop.yaml"
    schema.write_text(EXAMPLE.read_text())
    out = tmp_path / "shop.html"
    main([str(schema)])

    moved = {name: {"x": x + 7, "y": y + 300} for name, (x, y) in table_positions(out.read_text()).items()}
    save_canvas(out, moved)
    assert read_saved_layout(out)["tables"] == moved

    coupon = "classes:\n  Coupon:\n    attributes:\n      order: { range: Order }\n"
    schema.write_text(EXAMPLE.read_text().replace("classes:\n", coupon, 1))
    main([str(schema)])
    positions = table_positions(out.read_text())
    assert {n: positions[n] for n in moved} == {n: (p["x"], p["y"]) for n, p in moved.items()}
    assert positions["Coupon"][1] > max(p["y"] for p in moved.values())

    main([str(schema), "--relayout"])
    assert embedded_data(out.read_text())["layout"] == {}


def test_cli_reads_layout_from_another_file(tmp_path):
    schema = tmp_path / "shop.yaml"
    schema.write_text(TWO)
    downloaded = tmp_path / "downloaded.html"
    main([str(schema), "-o", str(downloaded)])
    save_canvas(downloaded, {"A": {"x": 111, "y": 222}, "B": {"x": 600, "y": 0}})

    main([str(schema), "--layout", str(downloaded)])
    assert table_positions((tmp_path / "shop.html").read_text())["A"] == (111, 222)


def test_themes_come_from_the_stylesheet():
    assert THEMES[:3] == ("auto", "light", "dark")
    assert {"monokai", "dracula", "nord", "solarized"} <= set(THEMES)


def test_theme_is_baked_into_the_page():
    schema = parse_linkml_file(EXAMPLE)
    monokai = render_html(schema)
    assert '<html lang="en" data-theme="monokai">' in monokai
    assert '<option value="monokai" selected>' in monokai
    assert embedded_data(monokai)["theme"] == "monokai"
    auto = render_html(schema, theme="auto")
    assert '<html lang="en">' in auto
    assert embedded_data(auto)["theme"] == "auto"


def test_unknown_theme_is_rejected():
    schema = parse_linkml_file(EXAMPLE)
    try:
        render_html(schema, theme="neon")
    except ValueError as e:
        assert "neon" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_cli_theme_sticks_until_changed(tmp_path):
    schema = tmp_path / "db.yaml"
    schema.write_text(EXAMPLE.read_text())
    out = tmp_path / "db.html"
    main([str(schema)])
    assert embedded_data(out.read_text())["theme"] == "monokai"
    main([str(schema), "--theme", "nord"])
    assert embedded_data(out.read_text())["theme"] == "nord"
    main([str(schema)])
    assert embedded_data(out.read_text())["theme"] == "nord"


def test_cli_rejects_unknown_theme(tmp_path, capsys):
    schema = tmp_path / "db.yaml"
    schema.write_text(EXAMPLE.read_text())
    try:
        main([str(schema), "--theme", "neon"])
    except SystemExit as e:
        assert e.code == 2
    assert "monokai" in capsys.readouterr().err


def svg_positions(svg: str) -> dict[str, tuple[float, float]]:
    root = ET.fromstring(svg.split("\n", 1)[1])
    return {
        g.get("data-name"): tuple(map(float, re.findall(r"-?[\d.]+", g.get("transform"))))
        for g in root.iter(f"{SVG}g")
        if g.get("data-name")
    }


def test_svg_draws_every_table_and_edge():
    schema = parse_linkml_file(EXAMPLE)
    svg = render_svg(schema)
    assert svg.startswith("<?xml")
    root = ET.fromstring(svg.split("\n", 1)[1])
    groups = list(root.iter(f"{SVG}g"))
    assert {g.get("data-name") for g in groups} - {None} == {c.name for c in schema.classes}
    assert {g.get("data-id") for g in groups} - {None} == {fk.id for fk in schema.foreign_keys}
    assert "var(" not in svg  # colours resolved, no CSS needed


def test_svg_uses_theme_colours():
    schema = parse_linkml_file(EXAMPLE)
    assert 'fill="#272822"' in render_svg(schema, theme="monokai")
    assert 'fill="#ffffff"' in render_svg(schema, theme="auto")  # auto draws as light
    assert 'fill="#2e3440"' in render_svg(schema, theme="nord")


def test_svg_follows_saved_layout():
    schema = parse_linkml_file(EXAMPLE)
    tables = {c.name: {"x": 0, "y": 0} for c in schema.classes} | {"Customer": {"x": 1234, "y": -50}}
    assert svg_positions(render_svg(schema, saved_layout={"tables": tables}))["Customer"] == (1234, -50)


def test_svg_escapes_schema_text():
    svg = render_svg(parse_linkml('name: x\nclasses:\n  A:\n    description: "<b>&</script>"\n'))
    ET.fromstring(svg.split("\n", 1)[1])
    assert "<b>" not in svg


def test_page_carries_svg_template_for_export():
    html = render_html(parse_linkml_file(EXAMPLE))
    m = re.search(r'<template id="herd-svg">(<svg .*?</svg>)</template>', html, re.DOTALL)
    assert m and "var(--card)" in m.group(1)
    assert 'id="export"' in html


def test_cli_svg_uses_neighbour_page_layout_and_theme(tmp_path):
    schema = tmp_path / "db.yaml"
    schema.write_text(EXAMPLE.read_text())
    page = tmp_path / "db.html"
    main([str(schema), "--theme", "nord"])
    tables = embedded_data(page.read_text())["auto"]["tables"] | {"Customer": {"x": 1234, "y": 777}}
    save_canvas(page, tables)
    before = page.read_text()

    assert main([str(schema), "--svg"]) == 0
    svg = (tmp_path / "db.svg").read_text()
    assert svg_positions(svg)["Customer"] == (1234, 777)
    assert 'fill="#2e3440"' in svg
    assert page.read_text() == before  # the page is only read

    main([str(schema), "-o", str(tmp_path / "out.svg"), "--theme", "solarized"])
    out = (tmp_path / "out.svg").read_text()
    assert svg_positions(out)["Customer"] == (1234, 777)
    assert 'fill="#fdf6e3"' in out


def test_layout_is_read_from_pages_written_before_the_rename(tmp_path):
    page = tmp_path / "old.html"
    layout = {"tables": {"Customer": {"x": 5, "y": 6}}, "edges": {}}
    page.write_text(f'<script type="application/json" id="bp-data">{json.dumps({"layout": layout})}</script>')
    assert read_saved_layout(page) == layout


def test_cli_reports_missing_file(tmp_path, capsys):
    assert main([str(tmp_path / "missing.yaml")]) == 1
    assert "linkml-herd:" in capsys.readouterr().err


# --- crossing minimisation ---------------------------------------------------------


def edge_crossings(schema, layout) -> int:
    """Crossings between edge pieces spanning the same gap between columns."""
    from itertools import combinations

    from linkml_herd.render import _slot_index, row_y

    classes = {c.name: c for c in schema.classes}
    pieces: dict[float, list[tuple[float, float]]] = {}
    for fk in schema.foreign_keys:
        src, dst = layout.boxes[fk.from_class], layout.boxes[fk.to_class]
        if fk.inverse or src.x == dst.x:
            continue
        sy = row_y(src, _slot_index(classes[fk.from_class], fk.from_slot))
        ty = row_y(dst, _slot_index(classes[fk.to_class], fk.to_slot))
        forward = dst.x > src.x
        anchors = [
            (src.right if forward else src.x, sy),
            *layout.waypoints.get(fk.id, []),
            (dst.x if forward else dst.right, ty),
        ]
        for (ax, ay), (bx, by) in zip(anchors[::2], anchors[1::2]):
            pieces.setdefault(min(ax, bx), []).append((ay, by) if ax < bx else (by, ay))
    return sum(
        (a1 - a2) * (b1 - b2) < 0 for ps in pieces.values() for (a1, b1), (a2, b2) in combinations(ps, 2)
    )


# Declared in an order that, stacked as written, crosses every edge.
CROSSED = """
name: crossed
classes:
  A1:
    attributes:
      to_b3: { range: B3 }
  A2:
    attributes:
      to_b2: { range: B2 }
  A3:
    attributes:
      to_b1: { range: B1 }
  B1:
    attributes:
      id: { identifier: true }
  B2:
    attributes:
      id: { identifier: true }
  B3:
    attributes:
      id: { identifier: true }
"""


def test_columns_are_ordered_to_untangle_edges():
    schema = parse_linkml(CROSSED)
    assert edge_crossings(schema, auto_layout(schema)) == 0


def test_rows_an_edge_attaches_to_count_towards_crossings():
    # Both of Hub's references could leave from either order of Left/Right; only one doesn't cross.
    schema = parse_linkml(
        "name: x\nclasses:\n"
        "  Hub:\n    attributes:\n      first: { range: Low }\n      second: { range: High }\n"
        "  High:\n    attributes:\n      id: { identifier: true }\n"
        "  Low:\n    attributes:\n      id: { identifier: true }\n"
    )
    layout = auto_layout(schema)
    assert edge_crossings(schema, layout) == 0
    assert layout.boxes["Low"].y < layout.boxes["High"].y


def test_layout_is_deterministic():
    schema = parse_linkml_file(EXAMPLE)
    assert auto_layout(schema) == auto_layout(parse_linkml_file(EXAMPLE))


def test_tables_line_up_with_what_they_reference():
    # A lone chain should draw as straight lines: each reference leaves level with the id it points at.
    schema = parse_linkml(
        "name: x\nclasses:\n"
        "  A:\n    attributes:\n      x: {}\n      y: {}\n      b: { range: B }\n"
        "  B:\n    attributes:\n      id: { identifier: true }\n"
    )
    from linkml_herd.render import _edge_geometry

    d, _ = _edge_geometry(schema.foreign_keys[0], {c.name: c for c in schema.classes}, auto_layout(schema))
    ys = {float(v) for v in re.findall(r",(-?[\d.]+)", d)}
    assert len(ys) == 1, d


# --- inverse relationships -----------------------------------------------------------

WITH_INVERSE = EXAMPLE.read_text().replace(
    "      full_name:\n",
    "      orders:\n        range: Order\n        multivalued: true\n      full_name:\n",
)


def test_inverse_edges_do_not_shape_the_layout():
    schema = parse_linkml(WITH_INVERSE)
    boxes = auto_layout(schema).boxes
    assert boxes["Order"].x < boxes["Customer"].x  # Customer.orders doesn't pull Customer leftwards
    assert auto_layout(parse_linkml_file(EXAMPLE)).boxes.keys() == boxes.keys()


def test_inverse_edges_run_back_along_their_counterparts_lanes():
    schema = parse_linkml(WITH_INVERSE)
    layout = auto_layout(schema)
    forward = layout.waypoints["Order.customer->Customer"]
    back = layout.waypoints["Customer.orders->Order"]
    assert [x for x, _ in back] == [x for x, _ in reversed(forward)]


def test_inverse_edges_are_drawn_but_hidden_by_default():
    html = render_html(parse_linkml(WITH_INVERSE))
    assert '<g class="edge inverse" data-id="Customer.orders-&gt;Order"' in html
    assert 'data-inverse-of="Order.customer-&gt;Customer"' in html
    assert '<html lang="en" data-theme="monokai">' in html
    assert '<input type="checkbox" id="inverse"> inverses (1)' in html
    assert "<strong>7</strong> relations" in html  # the inverse restates one, so it adds none
    assert embedded_data(html)["showInverse"] is False


def test_inverse_edges_can_be_shown():
    html = render_html(parse_linkml(WITH_INVERSE), show_inverse=True)
    assert 'class="show-inverse"' in html
    assert 'id="inverse" checked>' in html
    assert embedded_data(html)["showInverse"] is True


def test_no_inverse_toggle_without_inverses():
    assert 'id="inverse"' not in render_html(parse_linkml_file(EXAMPLE))


def test_svg_leaves_out_inverse_edges_unless_asked():
    schema = parse_linkml(WITH_INVERSE)

    def edge_ids(svg: str) -> set[str]:
        return {
            g.get("data-id") for g in ET.fromstring(svg.split("\n", 1)[1]).iter(f"{SVG}g") if g.get("data-id")
        }

    assert "Customer.orders->Order" not in edge_ids(render_svg(schema))
    assert "Customer.orders->Order" in edge_ids(render_svg(schema, show_inverse=True))


def test_cli_inverse_setting_sticks_until_changed(tmp_path):
    schema = tmp_path / "s.yaml"
    schema.write_text(WITH_INVERSE)
    page = tmp_path / "s.html"
    main([str(schema), "--inverse"])
    assert embedded_data(page.read_text())["showInverse"] is True
    main([str(schema)])
    assert embedded_data(page.read_text())["showInverse"] is True
    main([str(schema), "--no-inverse"])
    assert embedded_data(page.read_text())["showInverse"] is False


# --- page chrome -----------------------------------------------------------------------


def test_brand_links_to_the_repository():
    html = render_html(parse_linkml_file(EXAMPLE))
    assert '<a class="brand" href="https://github.com/ConradMearns/linkml-herd"' in html
