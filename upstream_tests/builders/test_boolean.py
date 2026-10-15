"""Unit tests for Boolean builder."""

import pytest

from onshape_mcp.builders.boolean import BooleanType, BooleanBuilder


class TestBooleanType:
    """Test BooleanType enum."""

    def test_boolean_type_values(self):
        # member names keep the short spelling; values are Onshape's enum strings
        assert BooleanType.UNION.value == "UNION"
        assert BooleanType.SUBTRACT.value == "SUBTRACTION"
        assert BooleanType.INTERSECT.value == "INTERSECTION"


class TestBooleanBuilder:
    """Test BooleanBuilder functionality."""

    def test_initialization_with_defaults(self):
        b = BooleanBuilder()
        assert b.name == "Boolean"
        assert b.boolean_type == BooleanType.UNION
        assert b.tool_body_queries == []
        assert b.target_body_queries == []

    def test_initialization_with_custom_values(self):
        b = BooleanBuilder(name="MyBool", boolean_type=BooleanType.SUBTRACT)
        assert b.name == "MyBool"
        assert b.boolean_type == BooleanType.SUBTRACT

    def test_add_tool_body(self):
        b = BooleanBuilder()
        result = b.add_tool_body("body1")
        assert result is b
        assert b.tool_body_queries == ["body1"]

    def test_add_multiple_tool_bodies(self):
        b = BooleanBuilder()
        b.add_tool_body("b1").add_tool_body("b2")
        assert b.tool_body_queries == ["b1", "b2"]

    def test_add_target_body(self):
        b = BooleanBuilder()
        result = b.add_target_body("target1")
        assert result is b
        assert b.target_body_queries == ["target1"]

    def test_build_union_requires_two_bodies(self):
        b = BooleanBuilder(boolean_type=BooleanType.UNION)
        b.add_tool_body("only1")
        with pytest.raises(ValueError, match="needs at least two bodies"):
            b.build()

    def test_build_intersect_requires_two_bodies(self):
        b = BooleanBuilder(boolean_type=BooleanType.INTERSECT)
        b.add_tool_body("only1")
        with pytest.raises(ValueError, match="needs at least two bodies"):
            b.build()

    def test_build_subtract_requires_tool_and_target(self):
        b = BooleanBuilder(boolean_type=BooleanType.SUBTRACT)
        b.add_tool_body("tool1")
        with pytest.raises(ValueError, match="tool body and one target body"):
            b.build()

    def test_build_structure(self):
        b = BooleanBuilder(name="TestBool")
        b.add_tool_body("tool1").add_tool_body("tool2")
        result = b.build()

        assert result["btType"] == "BTFeatureDefinitionCall-1406"
        feature = result["feature"]
        assert feature["btType"] == "BTMFeature-134"
        assert feature["featureType"] == "booleanBodies"
        assert feature["name"] == "TestBool"

    def test_build_operation_type_parameter(self):
        cases = [
            (BooleanType.UNION, "UNION"),
            (BooleanType.SUBTRACT, "SUBTRACTION"),
            (BooleanType.INTERSECT, "INTERSECTION"),
        ]
        for bt, expected in cases:
            b = BooleanBuilder(boolean_type=bt)
            b.add_tool_body("tool1")
            if bt == BooleanType.SUBTRACT:
                b.add_target_body("target1")
            else:
                b.add_tool_body("tool2")
            params = b.build()["feature"]["parameters"]
            type_param = next(p for p in params if p["parameterId"] == "operationType")
            assert type_param["value"] == expected

    def test_build_union_merges_all_bodies_into_tools(self):
        b = BooleanBuilder(boolean_type=BooleanType.UNION)
        b.add_tool_body("t1").add_tool_body("t2")
        b.add_target_body("t3")
        params = b.build()["feature"]["parameters"]

        tools = next(p for p in params if p["parameterId"] == "tools")
        assert tools["queries"][0]["deterministicIds"] == ["t1", "t2", "t3"]
        assert not any(p["parameterId"] == "targets" for p in params)

    def test_build_subtract_keeps_tools_and_targets_separate(self):
        b = BooleanBuilder(boolean_type=BooleanType.SUBTRACT)
        b.add_tool_body("cut1")
        b.add_target_body("keep1").add_target_body("keep2")
        params = b.build()["feature"]["parameters"]

        tools = next(p for p in params if p["parameterId"] == "tools")
        targets = next(p for p in params if p["parameterId"] == "targets")
        assert tools["queries"][0]["deterministicIds"] == ["cut1"]
        assert targets["queries"][0]["deterministicIds"] == ["keep1", "keep2"]
        assert any(p["parameterId"] == "keepTools" for p in params)

    def test_method_chaining(self):
        b = (
            BooleanBuilder(name="Chained", boolean_type=BooleanType.SUBTRACT)
            .add_tool_body("t1")
            .add_target_body("tgt1")
        )
        assert b.name == "Chained"
        assert len(b.tool_body_queries) == 1
        assert len(b.target_body_queries) == 1
