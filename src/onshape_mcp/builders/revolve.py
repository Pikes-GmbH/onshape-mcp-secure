"""Revolve feature builder for Onshape."""

from enum import Enum
from typing import Any, Dict, Optional

from .axis_helper import normalize_axis


class RevolveType(Enum):
    """Revolve operation type."""

    NEW = "NEW"
    ADD = "ADD"
    REMOVE = "REMOVE"
    INTERSECT = "INTERSECT"


class RevolveBuilder:
    """Builder for creating Onshape revolve features.

    The revolve axis needs a real line. The MCP handler creates a construction
    line through the origin (see ``axis_helper.build_axis_sketch``) and passes
    its edge's deterministic id to :meth:`build`.
    """

    def __init__(
        self,
        name: str = "Revolve",
        sketch_feature_id: Optional[str] = None,
        axis: str = "Y",
        angle: float = 360.0,
        operation_type: RevolveType = RevolveType.NEW,
    ):
        self.name = name
        self.sketch_feature_id = sketch_feature_id
        self.axis = normalize_axis(axis)
        self.angle = angle
        self.angle_variable: Optional[str] = None
        self.operation_type = operation_type
        self.opposite_direction = False

    def set_sketch(self, sketch_feature_id: str) -> "RevolveBuilder":
        self.sketch_feature_id = sketch_feature_id
        return self

    def set_angle(self, angle: float, variable_name: Optional[str] = None) -> "RevolveBuilder":
        self.angle = angle
        self.angle_variable = variable_name
        return self

    def set_axis(self, axis: str) -> "RevolveBuilder":
        self.axis = normalize_axis(axis)
        return self

    def set_opposite_direction(self, opposite: bool = True) -> "RevolveBuilder":
        self.opposite_direction = opposite
        return self

    def build(self, axis_edge_id: Optional[str] = None) -> Dict[str, Any]:
        """Build the revolve feature JSON.

        Args:
            axis_edge_id: Deterministic id of the axis line (from the helper
                construction-line sketch created by the MCP handler).

        Raises:
            ValueError: If the sketch feature id or axis edge id is missing
        """
        if not self.sketch_feature_id:
            raise ValueError("Sketch feature ID must be set before building revolve")
        if not axis_edge_id:
            raise ValueError("axis_edge_id is required (create an axis construction line first)")

        # fullRevolve hides angle / oppositeDirection; only send them for a
        # partial revolve.
        full_revolve = self.angle_variable is None and self.angle >= 360.0

        parameters = [
            {
                "btType": "BTMParameterEnum-145",
                "namespace": "",
                "enumName": "ExtendedToolBodyType",
                "value": "SOLID",
                "parameterId": "bodyType",
                "parameterName": "",
            },
            {
                "btType": "BTMParameterEnum-145",
                "namespace": "",
                "enumName": "NewBodyOperationType",
                "value": self.operation_type.value,
                "parameterId": "operationType",
                "parameterName": "",
            },
            {
                "btType": "BTMParameterQueryList-148",
                "queries": [
                    {
                        "btType": "BTMIndividualSketchRegionQuery-140",
                        "featureId": self.sketch_feature_id,
                        "filterInnerLoops": True,
                    }
                ],
                "parameterId": "entities",
                "parameterName": "",
            },
            {
                "btType": "BTMParameterQueryList-148",
                "queries": [
                    {
                        "btType": "BTMIndividualQuery-138",
                        "deterministicIds": [axis_edge_id],
                    }
                ],
                "parameterId": "axis",
                "parameterName": "",
            },
            {
                "btType": "BTMParameterBoolean-144",
                "value": full_revolve,
                "parameterId": "fullRevolve",
                "parameterName": "",
            },
        ]

        if not full_revolve:
            angle_expression = (
                f"#{self.angle_variable}" if self.angle_variable else f"{self.angle} deg"
            )
            parameters += [
                {
                    "btType": "BTMParameterBoolean-144",
                    "value": self.opposite_direction,
                    "parameterId": "oppositeDirection",
                    "parameterName": "",
                },
                {
                    "btType": "BTMParameterQuantity-147",
                    "isInteger": False,
                    "value": self.angle,
                    "units": "",
                    "expression": angle_expression,
                    "parameterId": "angle",
                    "parameterName": "",
                },
            ]

        return {
            "btType": "BTFeatureDefinitionCall-1406",
            "feature": {
                "btType": "BTMFeature-134",
                "featureType": "revolve",
                "name": self.name,
                "suppressed": False,
                "namespace": "",
                "parameters": parameters,
            },
        }
