from linkml_herd.parse import parse_linkml

SCHEMA = """
name: shop
default_range: string
classes:
  Customer:
    attributes:
      id:
        identifier: true
        range: integer
      email:
        required: true
  Order:
    description: A purchase
    attributes:
      id:
        identifier: true
        range: integer
      customer:
        range: Customer
        required: true
      tags:
        range: string
        multivalued: true
"""


def cls(schema, name):
    return next(c for c in schema.classes if c.name == name)


def slot(c, name):
    return next(s for s in c.slots if s.name == name)


class TestParse:
    schema = parse_linkml(SCHEMA)

    def test_reads_schema_name(self):
        assert self.schema.name == "shop"

    def test_parses_all_classes(self):
        assert [c.name for c in self.schema.classes] == ["Customer", "Order"]

    def test_identifies_primary_key(self):
        customer = cls(self.schema, "Customer")
        assert customer.identifier_slot == "id"
        assert slot(customer, "id").identifier

    def test_applies_default_range(self):
        assert slot(cls(self.schema, "Customer"), "email").range == "string"

    def test_flags_required_and_multivalued(self):
        order = cls(self.schema, "Order")
        assert slot(order, "tags").multivalued
        assert slot(order, "customer").required

    def test_detects_foreign_key_when_range_is_a_class(self):
        [fk] = self.schema.foreign_keys
        assert (fk.from_class, fk.from_slot, fk.to_class, fk.to_slot) == (
            "Order",
            "customer",
            "Customer",
            "id",
        )

    def test_scalar_ranges_are_not_foreign_keys(self):
        assert slot(cls(self.schema, "Order"), "tags").ref_class is None


class TestValidation:
    # Order has been deleted, but OrderLine.order still points at it.
    schema = parse_linkml("""
name: shop
classes:
  OrderLine:
    attributes:
      id: { identifier: true, range: integer }
      order: { range: Order, required: true }
      note: { range: string }
enums:
  status:
    permissible_values:
      open:
""")

    def test_flags_unknown_range(self):
        assert slot(cls(self.schema, "OrderLine"), "order").unresolved

    def test_reports_broken_reference(self):
        [p] = self.schema.problems
        assert (p.level, p.class_name, p.slot) == ("error", "OrderLine", "order")

    def test_no_foreign_key_for_unresolved_range(self):
        assert self.schema.foreign_keys == []

    def test_builtins_and_enums_are_valid(self):
        line = cls(self.schema, "OrderLine")
        assert not slot(line, "note").unresolved
        assert not slot(line, "id").unresolved


def test_inherits_slots_from_is_a():
    schema = parse_linkml("""
name: inherit
classes:
  Base:
    attributes:
      id:
        identifier: true
        range: integer
  Derived:
    is_a: Base
    attributes:
      label:
        range: string
""")
    derived = cls(schema, "Derived")
    assert [s.name for s in derived.slots] == ["id", "label"]
    assert derived.identifier_slot == "id"


SHARED = """
slots:
  id:
    identifier: true
    range: integer
  owner:
    range: User
"""
MAIN = """
name: imported
imports:
  - shared
  - linkml:types
classes:
  User:
    slots: [id]
  Widget:
    slots: [id, owner]
"""


class TestImports:
    schema = parse_linkml(MAIN, lambda name: SHARED if name == "shared" else None)

    def test_merges_imported_slots(self):
        widget = cls(self.schema, "Widget")
        assert [s.name for s in widget.slots] == ["id", "owner"]
        assert widget.identifier_slot == "id"

    def test_detects_foreign_keys_through_imported_slots(self):
        [fk] = self.schema.foreign_keys
        assert (fk.from_class, fk.from_slot, fk.to_class, fk.to_slot) == ("Widget", "owner", "User", "id")

    def test_skips_curie_imports_without_problems(self):
        assert self.schema.problems == []

    def test_ignores_imports_without_resolver(self):
        assert parse_linkml(MAIN).foreign_keys == []


# --- inverse relationships -----------------------------------------------------------


def inverses(yaml_text: str) -> dict[str, str | None]:
    return {fk.id: fk.inverse_of for fk in parse_linkml(yaml_text).foreign_keys}


def test_list_of_children_is_the_inverse_of_each_childs_reference_back():
    found = inverses(
        SCHEMA.replace(
            "      email:\n",
            "      orders:\n        range: Order\n        multivalued: true\n      email:\n",
        )
    )
    assert found == {"Customer.orders->Order": "Order.customer->Customer", "Order.customer->Customer": None}


def test_declared_inverse_pairs_two_plain_references():
    found = inverses(
        """
classes:
  Person:
    attributes:
      passport: { range: Passport, inverse: holder }
  Passport:
    attributes:
      holder: { range: Person }
"""
    )
    assert found == {"Person.passport->Passport": "Passport.holder->Person", "Passport.holder->Person": None}


def test_two_plain_references_are_separate_relationships():
    found = inverses(
        """
classes:
  Person:
    attributes:
      employer: { range: Company }
  Company:
    attributes:
      ceo: { range: Person }
"""
    )
    assert set(found.values()) == {None}


def test_inverse_names_which_edge_it_pairs_with():
    # `managed` declares itself the inverse of `manager`, so `staff` stays a relationship of its own.
    found = inverses(
        """
classes:
  Team:
    attributes:
      staff: { range: Person, multivalued: true }
      managed: { range: Person, multivalued: true, inverse: manager }
  Person:
    attributes:
      manager: { range: Team }
      member_of: { range: Team, multivalued: true, inverse: staff }
"""
    )
    assert found["Team.managed->Person"] == "Person.manager->Team"
    assert found["Person.manager->Team"] is None
    # `member_of` names `staff`: of two lists, the one declaring `inverse:` is the inverse.
    assert found["Team.staff->Person"] is None
    assert found["Person.member_of->Team"] == "Team.staff->Person"


def test_self_references_are_never_inverses():
    found = inverses(
        "classes:\n  Node:\n    attributes:\n      children: { range: Node, multivalued: true }\n"
        "      parent: { range: Node }\n"
    )
    assert set(found.values()) == {None}
