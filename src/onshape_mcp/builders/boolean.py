"""Boolean operation builder for Onshape."""

from enum import Enum
from typing import Any, Dict, List


class BooleanType(Enum):
    """Boolean operation type. ``value`` is the Onshape ``BooleanOperationType``
    enum string (member names keep the short spelling used by the MCP tool)."""

    UNION = "UNION"
    SUBTRACT = "SUBTRACTION"
    INTERSECT = "INTERSECTION"


class BooleanBuilder:
    """Builder for creating Onshape ``booleanBodies`` features.

    - UNION / INTERSECT combine every supplied body; put them all in as tool
      bodies (target bodies, if any were added, are merged into the same list).
    - SUBTRACT removes the tool bodies from the target bodies, so both lists
      are required and kept separate.
    """

    def __init__(
        self,
        name: str = "Boolean",
        boolean_type: BooleanType = BooleanType.UNION,
    ):
        self.name = name
        self.boolean_type = boolean_type
        self.tool_body_queries: List[str] = []
        self.target_body_queries: List[str] = []

    def add_tool_body(self, body_id: str) -> "BooleanBuilder":
        """Add a tool body by its deterministic ID.

        For SUBTRACT these are the bodies removed from the targets; for
        UNION / INTERSECT they are simply bodies to combine."""
        self.tool_body_queries.append(body_id)
        return self

    def add_target_body(self, body_id: str) -> "BooleanBuilder":
        """Add a target body by its deterministic ID.

        Required for SUBTRACT (the bodies kept / cut into). For UNION and
        INTERSECT it is folded into the combined body list."""
        self.target_body_queries.append(body_id)
        return self

    @staticmethod
    def _query_list(param_id: str, ids: List[str]) -> Dict[str, Any]:
        return {
            "btType": "BTMParameterQueryList-148",
            "queries": [{"btType": "BTMIndividualQuery-138", "deterministicIds": list(ids)}],
            "parameterId": param_id,
            "parameterName": "",
        }

    def build(self) -> Dict[str, Any]:
        """Build the boolean feature JSON.

        Raises:
            ValueError: If required bodies are missing
        """
        is_subtract = self.boolean_type == BooleanType.SUBTRACT

        if is_subtract:
            if not self.tool_body_queries or not self.target_body_queries:
                raise ValueError("SUBTRACT needs at least one tool body and one target body")
            tools = list(self.tool_body_queries)
        else:
            # UNION / INTERSECT: everything goes in as tools.
            tools = list(self.tool_body_queries) + list(self.target_body_queries)
            if len(tools) < 2:
                raise ValueError(f"{self.boolean_type.name} needs at least two bodies")

        parameters: List[Dict[str, Any]] = [
            {
                "btType": "BTMParameterEnum-145",
                "namespace": "",
                "enumName": "BooleanOperationType",
                "value": self.boolean_type.value,
                "parameterId": "operationType",
                "parameterName": "",
            },
            self._query_list("tools", tools),
        ]

        if is_subtract:
            parameters.append(self._query_list("targets", self.target_body_queries))
            parameters.append(
                {
                    "btType": "BTMParameterBoolean-144",
                    "value": False,
                    "parameterId": "keepTools",
                    "parameterName": "",
                }
            )

        return {
            "btType": "BTFeatureDefinitionCall-1406",
            "feature": {
                "btType": "BTMFeature-134",
                "featureType": "booleanBodies",
                "name": self.name,
                "suppressed": False,
                "namespace": "",
                "parameters": parameters,
            },
        }
