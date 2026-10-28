"""Interpret the subset of LinkML we draw as an ER diagram.

LinkML is the source of truth for *what* to draw: one table per class, columns
from slots, and a foreign key wherever a slot's range is another class.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# LinkML builtin scalar types. A slot's range resolves to one of these, a
# schema-defined type/enum (a scalar column), or a class (a foreign key).
# Anything else is a broken reference and is reported as a problem.
# https://linkml.io/linkml-model/latest/docs/specification/03types/
BUILTIN_TYPES = frozenset(
    {
        "string",
        "integer",
        "boolean",
        "float",
        "double",
        "decimal",
        "time",
        "date",
        "datetime",
        "uriorcurie",
        "curie",
        "uri",
        "ncname",
        "objectidentifier",
        "nodeidentifier",
        "jsonpointer",
        "jsonpath",
        "sparqlpath",
    }
)

# Maps an `imports:` entry to the imported document's YAML text, or None to skip it.
ImportResolver = Callable[[str], "str | None"]


@dataclass
class Slot:
    name: str
    range: str
    required: bool = False
    multivalued: bool = False
    identifier: bool = False
    key: bool = False
    description: str | None = None
    inlined: bool = False
    # Name of the slot this one is declared the `inverse` of.
    inverse: str | None = None
    # Set when `range` resolves to another class in the schema (a foreign key).
    ref_class: str | None = None
    # True when `range` is not a known class, enum, type, or builtin.
    unresolved: bool = False

    @property
    def pk(self) -> bool:
        return self.identifier or self.key


@dataclass
class Class:
    name: str
    slots: list[Slot]
    description: str | None = None
    is_a: str | None = None
    identifier_slot: str | None = None


@dataclass
class ForeignKey:
    from_class: str
    from_slot: str
    to_class: str
    # Identifier slot of the target class, if it has one.
    to_slot: str | None
    multivalued: bool
    required: bool
    # Slot name from the source slot's `inverse:`, if it declares one.
    declared_inverse: str | None = None
    # Id of the edge this one restates from the other end (see `_mark_inverses`).
    inverse_of: str | None = None

    @property
    def inverse(self) -> bool:
        return self.inverse_of is not None

    @property
    def id(self) -> str:
        return f"{self.from_class}.{self.from_slot}->{self.to_class}"

    @property
    def cardinality(self) -> str:
        if self.multivalued:
            return "1..*"
        return "1" if self.required else "0..1"


@dataclass
class Problem:
    """A semantic problem found while interpreting the schema (not a YAML syntax error)."""

    level: str
    class_name: str
    slot: str
    message: str


@dataclass
class Schema:
    name: str
    classes: list[Class]
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    title: str | None = None
    description: str | None = None


def _merge_import(into: dict[str, Any], imported: dict[str, Any]) -> None:
    """Merge imported definitions under the importing doc, which wins on name conflicts."""
    for section in ("classes", "slots", "types", "enums"):
        into[section] = {**(imported.get(section) or {}), **(into.get(section) or {})}


def _load_with_imports(text: str, resolve: ImportResolver | None, seen: set[str]) -> dict[str, Any]:
    """Parse a doc and recursively fold its `imports` into one combined raw schema."""
    doc = yaml.safe_load(text) or {}
    imports = doc.get("imports")
    if resolve is None or not isinstance(imports, list):
        return doc
    for name in imports:
        if name in seen:
            continue
        seen.add(name)
        imported = resolve(name)
        if imported is not None:
            _merge_import(doc, _load_with_imports(imported, resolve, seen))
    return doc


def parse_linkml(text: str, resolve_import: ImportResolver | None = None) -> Schema:
    """Parse a LinkML YAML document into the ER model we render.

    Supports top-level `classes` with inline `attributes` and/or referenced
    `slots`, reusable top-level `slots`, `slot_usage` overrides, `is_a` /
    `mixins` inheritance, and `imports` (when a resolver is supplied). Slots and
    attributes are interchangeable, so every representation of the same
    database produces the same diagram.
    """
    doc = _load_with_imports(text, resolve_import, set())

    raw_classes: dict[str, Any] = doc.get("classes") or {}
    raw_slots: dict[str, Any] = doc.get("slots") or {}
    default_range = doc.get("default_range") or "string"

    class_names = set(raw_classes)
    scalar_ranges = BUILTIN_TYPES | set(doc.get("types") or {}) | set(doc.get("enums") or {})

    def resolve_slot(name: str, *layers: dict[str, Any] | None) -> Slot:
        """Merge slot layers (top-level slot -> local def -> slot_usage) into one resolved slot."""
        merged: dict[str, Any] = dict(raw_slots.get(name) or {})
        for layer in layers:
            merged.update(layer or {})
        rng = str(merged.get("range") or default_range)
        is_class_ref = rng in class_names
        return Slot(
            name=name,
            range=rng,
            required=bool(merged.get("required")),
            multivalued=bool(merged.get("multivalued")),
            identifier=bool(merged.get("identifier")),
            key=bool(merged.get("key")),
            description=merged.get("description"),
            inlined=bool(merged.get("inlined") or merged.get("inlined_as_list")),
            inverse=str(merged["inverse"]) if merged.get("inverse") else None,
            ref_class=rng if is_class_ref else None,
            unresolved=not is_class_ref and rng not in scalar_ranges,
        )

    def class_slots(class_name: str, seen: set[str]) -> list[Slot]:
        """Collect a class's slots, applying inheritance. Own slots override inherited ones."""
        if class_name in seen:
            return []
        seen.add(class_name)
        c = raw_classes.get(class_name)
        if not isinstance(c, dict):
            return []

        slots: dict[str, Slot] = {}
        if c.get("is_a"):
            slots.update((s.name, s) for s in class_slots(c["is_a"], seen))
        for mixin in c.get("mixins") or []:
            slots.update((s.name, s) for s in class_slots(mixin, seen))

        usage = c.get("slot_usage") or {}
        for sn in c.get("slots") or []:
            slots[sn] = resolve_slot(sn, raw_slots.get(sn), usage.get(sn))
        for sn, definition in (c.get("attributes") or {}).items():
            slots[sn] = resolve_slot(sn, definition, usage.get(sn))
        return list(slots.values())

    classes: list[Class] = []
    for name, raw in raw_classes.items():
        raw = raw if isinstance(raw, dict) else {}
        slots = class_slots(name, set())
        id_slot = next((s for s in slots if s.identifier), None) or next((s for s in slots if s.key), None)
        classes.append(
            Class(
                name=name,
                slots=slots,
                description=raw.get("description"),
                is_a=raw.get("is_a"),
                identifier_slot=id_slot.name if id_slot else None,
            )
        )

    by_name = {c.name: c for c in classes}
    foreign_keys: list[ForeignKey] = []
    problems: list[Problem] = []
    for c in classes:
        for s in c.slots:
            if s.unresolved:
                problems.append(
                    Problem(
                        level="error",
                        class_name=c.name,
                        slot=s.name,
                        message=f'range "{s.range}" is not a known class, type, or enum',
                    )
                )
            elif s.ref_class:
                foreign_keys.append(
                    ForeignKey(
                        from_class=c.name,
                        from_slot=s.name,
                        to_class=s.ref_class,
                        to_slot=by_name[s.ref_class].identifier_slot,
                        multivalued=s.multivalued,
                        required=s.required,
                        declared_inverse=s.inverse,
                    )
                )
    _mark_inverses(foreign_keys)

    return Schema(
        name=doc.get("name") or "schema",
        title=doc.get("title"),
        description=doc.get("description"),
        classes=classes,
        foreign_keys=foreign_keys,
        problems=problems,
    )


def _mark_inverses(foreign_keys: list[ForeignKey]) -> None:
    """Find edge pairs that draw one relationship twice, once from each end.

    Two edges between the same classes in opposite directions pair up when one
    names the other as its `inverse:`. Edges no `inverse:` speaks for also pair
    implicitly when at least one is a list (`Customer.orders: Order[]` beside
    `Order.customer`); two plain references (`Person.employer`, `Company.ceo`)
    are separate relationships. Of a pair, the list end is the inverse; then
    the end that declares `inverse:`; then the one declared later.
    """

    def opposing(e: ForeignKey, o: ForeignKey) -> bool:
        return (o.from_class, o.to_class) == (e.to_class, e.from_class)

    def names(e: ForeignKey, o: ForeignKey) -> bool:
        return opposing(e, o) and e.declared_inverse == o.from_slot

    spoken_for = {id(e) for e in foreign_keys if e.declared_inverse or any(names(o, e) for o in foreign_keys)}
    for i, e in enumerate(foreign_keys):
        if e.from_class == e.to_class:
            continue
        rank_e = (e.multivalued, e.declared_inverse is not None, i)
        for j, o in enumerate(foreign_keys):
            if not opposing(e, o):
                continue
            if id(e) in spoken_for or id(o) in spoken_for:
                paired = names(e, o) or names(o, e)
            else:
                paired = e.multivalued or o.multivalued
            if paired and rank_e > (o.multivalued, o.declared_inverse is not None, j):
                e.inverse_of = o.id
                break


def file_resolver(base_dir: Path) -> ImportResolver:
    """Resolve `imports:` entries to sibling files of the importing schema.

    CURIE-style imports (e.g. `linkml:types`) are skipped -- their types are
    builtins to us. The import name may omit the `.yaml`/`.yml` extension.
    """

    def resolve(name: str) -> str | None:
        if ":" in name:
            return None
        candidates = [name] if name.lower().endswith((".yaml", ".yml")) else [f"{name}.yaml", f"{name}.yml"]
        for candidate in candidates:
            path = base_dir / candidate
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return None

    return resolve


def parse_linkml_file(path: str | Path) -> Schema:
    """Parse a LinkML file from disk, resolving relative `imports` from its directory."""
    path = Path(path).resolve()
    return parse_linkml(path.read_text(encoding="utf-8"), file_resolver(path.parent))
