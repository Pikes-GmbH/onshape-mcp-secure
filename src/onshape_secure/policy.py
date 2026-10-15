"""Security policy: tool classes, argument validation and document access rules."""

from __future__ import annotations

import copy
import math
import re
import time
from typing import Any, Awaitable, Callable

from .config import ONSHAPE_ID_RE, Settings

READ = "read"
WRITE = "write"
DELETE = "delete"
CREATE_DOC = "create_document"
FEATURESCRIPT = "featurescript"
META = "meta"
# cross-document listings (document lists, mapping table / part index): cannot be checked per
# document, so the whole class is hidden when a read scope is set (AD-14)
INDEX = "index"
SYSTEM = "system"  # server-internal maintenance (webhook registration), never reachable by tools

# Every upstream tool must be listed here. Unknown tools are never exposed (fail closed).
TOOL_CLASSES: dict[str, str] = {
    # read-only
    "get_variables": READ,
    "get_features": READ,
    "list_documents": INDEX,
    "search_documents": INDEX,
    "get_document": READ,
    "get_document_summary": READ,
    "find_part_studios": READ,
    "get_parts": READ,
    "get_elements": READ,
    "get_assembly": READ,
    "get_bounding_box": READ,
    "export_part_studio": READ,
    "export_assembly": READ,
    "capture_part_studio_screenshot": READ,
    "capture_assembly_screenshot": READ,
    "check_assembly_interference": READ,
    "get_assembly_positions": READ,
    "get_body_details": READ,
    "get_assembly_features": READ,
    # read-only FeatureScript evaluation (Onshape evaluates lambdas without changing the model)
    "eval_featurescript": FEATURESCRIPT,
    # new document
    "create_document": CREATE_DOC,
    # modifying tools
    "create_sketch_rectangle": WRITE,
    "create_extrude": WRITE,
    "create_thicken": WRITE,
    "create_variable_studio": WRITE,
    "set_variable": WRITE,
    "create_part_studio": WRITE,
    "create_assembly": WRITE,
    "add_assembly_instance": WRITE,
    "transform_instance": WRITE,
    "create_fastened_mate": WRITE,
    "create_revolute_mate": WRITE,
    "create_slider_mate": WRITE,
    "create_cylindrical_mate": WRITE,
    "create_mate_connector": WRITE,
    "create_sketch_circle": WRITE,
    "create_sketch_line": WRITE,
    "create_sketch_arc": WRITE,
    "create_fillet": WRITE,
    "create_chamfer": WRITE,
    "create_revolve": WRITE,
    "create_linear_pattern": WRITE,
    "create_circular_pattern": WRITE,
    "create_boolean": WRITE,
    "set_instance_position": WRITE,
    "align_instance_to_face": WRITE,
    # despite its name it inserts and deletes a temporary mate connector in the assembly
    "get_face_coordinate_system": WRITE,
    "delete_feature": DELETE,
    # tools added by this server (src/onshape_secure/extra_tools.py)
    "find_part_by_number": READ,
    "capture_part_screenshot": READ,
    "export_released_model": READ,
    "export_released_drawing": READ,
    "find_drawing_for_part": READ,
    "show_mapping_table": INDEX,
    "search_part_index": INDEX,
    "export_mapping_table": INDEX,
}

EXTRA_TOOL_NAMES = {
    "find_part_by_number", "capture_part_screenshot", "export_released_model", "export_released_drawing",
    "find_drawing_for_part", "show_mapping_table", "search_part_index", "export_mapping_table",
    "get_parts",  # replaces the upstream implementation (adds part numbers)
}

# Schema properties removed before tools are published to clients.
REMOVED_PROPERTIES: dict[str, set[str]] = {
    # would write files onto the server's disk at an arbitrary path
    "capture_part_studio_screenshot": {"outputPath"},
    "capture_assembly_screenshot": {"outputPath"},
    # upstream defaults to PUBLIC documents - always forced private here
    "create_document": {"isPublic"},
}

HEX_ID_KEYS = {"documentId", "workspaceId", "elementId", "partStudioElementId", "wvmId", "drawingElementId"}
# Onshape part/face/edge/feature/instance ids: base64-like, never path separators sequences
GENERIC_ID_RE = re.compile(r"^[A-Za-z0-9_+/=:.\-]{1,200}$")
PATH_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_+=\-]{1,200}$")
VIEW_MATRIX_RE = re.compile(r"^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){11}$")
NAMED_VIEWS = {"iso", "isometric", "top", "bottom", "front", "back", "left", "right"}
# default 3/4 view from front-right-top, deliberately off the iso diagonal so flat parts at
# 0/45/90 degrees are never shown edge-on
AUTO_VIEW_MATRIX = "0.8619,0.5070,0,0,-0.2635,0.4480,0.8543,0,0.4331,-0.7364,0.5198,0"

STRING_LIMITS = {"text": 64, "script": 20_000, "description": 2_000, "expression": 1_000, "query": 200, "partNumber": 64,
                 "drawingNumber": 64, "revision": 16}
DEFAULT_STRING_LIMIT = 500
MAX_ARRAY_ITEMS = 200
MAX_ABS_NUMBER = 1e7
MAX_IMAGE_PX = 1600


class PolicyDenied(Exception):
    """A tool call was refused by policy. The message is shown to the agent."""


# POST endpoints that Onshape documents as read-only (evaluation / export jobs)
READ_ONLY_POST_SUFFIXES = ("/featurescript", "/translations")
# own webhook calls (registration, weekly check); ids are version-independent
WEBHOOKS_PATH = "/api/v6/webhooks"
_WEBHOOK_ID_PATH_RE = re.compile(re.escape(WEBHOOKS_PATH) + r"/[0-9a-f]{24}")
_DOC_IN_PATH_RE = re.compile(r"/d/([0-9a-f]{24})(?:/|$)")


def check_onshape_request(method: str, path: str, cls: str | None, gated_document: str | None) -> None:
    """Last line of defence at the HTTP layer, independent of upstream code.

    Modifying requests (POST/DELETE) are only let through when the running tool
    is allowed to modify and the request targets exactly the gated document.
    """
    method = method.lower()
    if method == "get":
        return
    bare = path.split("?", 1)[0]
    if method == "post" and cls in (READ, FEATURESCRIPT) and bare.endswith(READ_ONLY_POST_SUFFIXES):
        return
    if method == "post" and cls == CREATE_DOC and bare == "/api/v10/documents":
        return
    if cls == SYSTEM and (
        (method == "post" and bare == WEBHOOKS_PATH)
        or (method in ("post", "delete") and _WEBHOOK_ID_PATH_RE.fullmatch(bare))
    ):
        return
    if cls in (WRITE, DELETE) and gated_document:
        targets = set(_DOC_IN_PATH_RE.findall(bare))
        if targets == {gated_document}:
            return
    raise PolicyDenied(f"Blocked a modifying Onshape request ({method.upper()}) not covered by the tool's permission.")


def tool_class(name: str) -> str | None:
    return TOOL_CLASSES.get(name)


def is_tool_enabled(name: str, settings: Settings) -> bool:
    cls = tool_class(name)
    if cls is None or name in settings.disabled_tools:
        return False
    if cls in (WRITE, CREATE_DOC, DELETE) and settings.write_mode == "off":
        return False
    if cls == DELETE and not settings.allow_delete:
        return False
    if cls == FEATURESCRIPT and not settings.allow_featurescript:
        return False
    if cls == INDEX and settings.read_restricted:
        return False
    return True


def sanitize_schema(name: str, schema: dict) -> dict:
    schema = copy.deepcopy(schema)
    props = schema.get("properties", {})
    for key in REMOVED_PROPERTIES.get(name, set()):
        props.pop(key, None)
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r in props]
    if "view" in props and name.endswith("_screenshot"):
        props["view"] = {
            "type": "string",
            "description": "auto (default 3/4 view), iso, top, bottom, front, back, left, right or a 12-number view matrix",
            "default": "auto",
        }
    return schema


def _check_string(key: str, value: str) -> str:
    if "\x00" in value:
        raise PolicyDenied(f"'{key}' contains a NUL character")
    if key in HEX_ID_KEYS:
        if not ONSHAPE_ID_RE.match(value):
            raise PolicyDenied(f"'{key}' must be a 24-character lowercase hex Onshape ID")
        return value
    if key.endswith("Id"):
        if not GENERIC_ID_RE.match(value) or ".." in value:
            raise PolicyDenied(f"'{key}' is not a valid Onshape ID")
        return value
    limit = STRING_LIMITS.get(key, DEFAULT_STRING_LIMIT)
    if len(value) > limit:
        raise PolicyDenied(f"'{key}' is longer than {limit} characters")
    return value


def _check_number(key: str, value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or abs(value) > MAX_ABS_NUMBER:
            raise PolicyDenied(f"'{key}' must be a finite number with |x| <= {MAX_ABS_NUMBER:g}")
    return value


def _check_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        return _check_string(key, value)
    if isinstance(value, list):
        if len(value) > MAX_ARRAY_ITEMS:
            raise PolicyDenied(f"'{key}' has more than {MAX_ARRAY_ITEMS} items")
        item_key = key[:-1] if key.endswith("Ids") else key  # edgeIds -> edgeId
        return [_check_value(item_key, v) for v in value]
    if isinstance(value, dict):
        raise PolicyDenied(f"'{key}' must not be an object")
    return _check_number(key, value)


def sanitize_arguments(name: str, arguments: dict | None, schema: dict, settings: Settings) -> dict:
    """Validate every argument and drop anything the published schema doesn't declare."""
    arguments = arguments or {}
    if not isinstance(arguments, dict):
        raise PolicyDenied("arguments must be an object")
    props = schema.get("properties", {})
    clean = {k: _check_value(k, v) for k, v in arguments.items() if k in props}

    if name == "create_document":
        clean["isPublic"] = False  # never create public (world-readable) documents
        prefix = settings.write_allowed_name_prefix
        doc_name = (clean.get("name") or "").strip() or "Untitled"
        if prefix and not doc_name.startswith(prefix):
            doc_name = f"{prefix}{doc_name}"
        clean["name"] = doc_name[:DEFAULT_STRING_LIMIT]

    if name in {"capture_part_studio_screenshot", "capture_assembly_screenshot", "capture_part_screenshot"}:
        for dim in ("outputWidth", "outputHeight"):
            if dim in clean:
                clean[dim] = max(16, min(int(clean[dim]), MAX_IMAGE_PX))
        view = clean.get("view")
        if view is None or view.lower() == "auto":
            clean["view"] = AUTO_VIEW_MATRIX
        elif view.lower() not in NAMED_VIEWS and not VIEW_MATRIX_RE.match(view):
            raise PolicyDenied("'view' must be auto, iso, top, bottom, front, back, left, right or a 12-number matrix")

    if name in {"list_documents", "search_documents"} and "limit" in clean:
        clean["limit"] = max(1, min(int(clean["limit"]), 50))

    if name == "delete_feature":
        element_type = clean.get("elementType", "PARTSTUDIO")
        if element_type not in {"PARTSTUDIO", "ASSEMBLY"}:
            raise PolicyDenied("'elementType' must be PARTSTUDIO or ASSEMBLY")
        # the upstream Part Studio delete puts featureId into the URL path unencoded
        if not PATH_SAFE_ID_RE.match(clean.get("featureId", "")):
            raise PolicyDenied("'featureId' is not a valid feature ID")

    return clean


class DocumentAccess:
    """Decides which documents may be read or modified."""

    def __init__(
        self,
        settings: Settings,
        fetch_name: Callable[[str], Awaitable[str]],
        cache_seconds: float = 10.0,
    ):
        self.settings = settings
        self._fetch_name = fetch_name
        self._cache: dict[str, tuple[str, float]] = {}
        self._cache_seconds = cache_seconds

    def remember_name(self, document_id: str, name: str) -> None:
        self._cache[document_id] = (name, time.monotonic() + self._cache_seconds)

    async def _name(self, document_id: str) -> str:
        cached = self._cache.get(document_id)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        name = await self._fetch_name(document_id)
        self.remember_name(document_id, name)
        return name

    async def _prefix_match(self, document_id: str) -> bool:
        prefix = self.settings.write_allowed_name_prefix
        if not prefix:
            return False
        try:
            name = await self._name(document_id)
        except Exception as exc:  # unknown/inaccessible document -> deny
            raise PolicyDenied("Could not verify the document (not found or no access).") from exc
        return name.startswith(prefix)

    async def check_write(self, document_id: str | None) -> None:
        s = self.settings
        if s.write_mode == "off":
            raise PolicyDenied("This server is configured read-only (WRITE_MODE=off).")
        if not document_id:
            raise PolicyDenied("documentId is required for modifying tools.")
        if s.write_mode == "all" or document_id in s.write_allowed_document_ids:
            return
        if await self._prefix_match(document_id):
            return
        hint = (
            f" Only documents whose name starts with '{s.write_allowed_name_prefix}' or that an admin"
            " allow-listed can be changed. Use create_document to start a new document."
            if s.write_allowed_name_prefix
            else " Only allow-listed documents can be changed."
        )
        raise PolicyDenied("Write access to this document is not allowed." + hint)

    async def check_read(self, document_id: str | None) -> None:
        s = self.settings
        if not s.read_restricted or not document_id:
            return
        if document_id in s.read_allowed_document_ids or document_id in s.write_allowed_document_ids:
            return
        if s.write_mode != "off" and await self._prefix_match(document_id):
            return
        raise PolicyDenied("Read access to this document is not allowed by the server policy.")
