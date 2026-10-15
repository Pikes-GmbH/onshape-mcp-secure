"""Regression tests for the ``libraryRelationType`` feature-parameter bug.

Every feature builder used to hard-code ``"libraryRelationType": "NONE"`` on
each parameter it emitted. Onshape's ``/features`` endpoint (API v9) rejects
``"NONE"`` as an invalid enum value, responding with HTTP 400 and a
``BTWeirdStringValueException``. This made ``create_extrude``, ``create_revolve``,
``create_linear_pattern``, ``create_circular_pattern``, ``create_fillet``,
``create_chamfer`` and ``create_boolean`` all fail against a real document.

Omitting the field lets Onshape default it to ``"DEFAULT"``, which is accepted.
These tests assert that no builder emits ``libraryRelationType == "NONE"``
anywhere in the feature definition it produces. Thicken is included too: it never
emitted the field, and this keeps it that way.
"""

from typing import Any, Iterator

import pytest

from onshape_mcp.builders.boolean import BooleanBuilder
from onshape_mcp.builders.chamfer import ChamferBuilder
from onshape_mcp.builders.extrude import ExtrudeBuilder
from onshape_mcp.builders.fillet import FilletBuilder
from onshape_mcp.builders.pattern import CircularPatternBuilder, LinearPatternBuilder
from onshape_mcp.builders.revolve import RevolveBuilder
from onshape_mcp.builders.thicken import ThickenBuilder


def _library_relation_types(obj: Any) -> Iterator[Any]:
    """Yield every ``libraryRelationType`` value found anywhere in ``obj``."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "libraryRelationType":
                yield value
            else:
                yield from _library_relation_types(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _library_relation_types(item)


def _minimal_builders():
    """Return (id, built-feature-dict) for a minimal-but-valid build of each builder."""
    return [
        ("extrude", ExtrudeBuilder(sketch_feature_id="sketch1").build()),
        ("revolve", RevolveBuilder(sketch_feature_id="sketch1").build(axis_edge_id="edge1")),
        ("linear_pattern", LinearPatternBuilder().add_feature("feature1").build()),
        (
            "circular_pattern",
            CircularPatternBuilder().add_feature("feature1").build(axis_edge_id="edge1"),
        ),
        ("fillet", FilletBuilder().add_edge("edge1").build()),
        ("chamfer", ChamferBuilder().add_edge("edge1").build()),
        ("boolean", BooleanBuilder().add_tool_body("body1").add_tool_body("body2").build()),
        ("thicken", ThickenBuilder("Thicken", "sketch1").set_thickness(0.5).build()),
    ]


@pytest.mark.parametrize("builder_id, feature", _minimal_builders())
def test_builder_does_not_emit_library_relation_type_none(builder_id, feature):
    """No builder may emit ``libraryRelationType == "NONE"`` (Onshape rejects it)."""
    offending = [v for v in _library_relation_types(feature) if v == "NONE"]
    assert not offending, (
        f"{builder_id} builder emitted libraryRelationType={offending!r}; "
        'Onshape API v9 rejects "NONE" with a BTWeirdStringValueException. '
        "Omit the field so it defaults to DEFAULT."
    )
