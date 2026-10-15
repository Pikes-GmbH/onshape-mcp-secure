"""Helpers for referencing the global X/Y/Z axes in Part Studio features.

Onshape has no queryable "origin axis" entity, so a revolve axis or a circular
pattern axis has to point at a real line. The robust, deterministic way to get
one is to drop a construction line through the origin on a default plane and
query that line's edge.

``build_axis_sketch`` returns a sketch feature payload with a single construction
line coincident with the requested global axis. After adding it, evaluate::

    qCreatedBy(makeId("<that sketch's featureId>"), EntityType.EDGE)

to get the axis edge's deterministic id, and pass that to the revolve / circular
pattern builder.

For a *linear* pattern the direction only needs an orientation, not a line, so a
planar default-plane face works directly - see ``FACE_QUERY_FOR_DIRECTION``.
"""

from typing import Any, Dict

from .sketch import SketchBuilder, SketchPlane

# Default-plane deterministic ids (see PartStudioManager.get_plane_id).
_PLANE_ID = {"Front": "JCC", "Top": "JDC", "Right": "JEC"}

# Global axis -> (default plane the line is drawn on, its plane id, line endpoints
# in that plane's local 2D coords). Each line passes through the origin and runs
# along the requested global axis. Default-plane local axes are: Top (x=X, y=Y,
# normal Z), Front (x=X, y=Z, normal -Y), Right (x=Y, y=Z, normal X) - so a world-Y
# line must be drawn on Top (or Right), never on Front.
_AXIS_LINE = {
    "X": ("Top", _PLANE_ID["Top"], (-1.0, 0.0), (1.0, 0.0)),
    "Y": ("Top", _PLANE_ID["Top"], (0.0, -1.0), (0.0, 1.0)),
    "Z": ("Right", _PLANE_ID["Right"], (0.0, -1.0), (0.0, 1.0)),
}

# Global axis -> default plane whose normal points along that axis. Used as a
# linear-pattern direction (the pattern uses the face normal).
FACE_QUERY_FOR_DIRECTION = {
    "X": 'query = qCreatedBy(makeId("Right"), EntityType.FACE);',
    "Y": 'query = qCreatedBy(makeId("Front"), EntityType.FACE);',
    "Z": 'query = qCreatedBy(makeId("Top"), EntityType.FACE);',
}


def normalize_axis(axis: str) -> str:
    a = (axis or "Z").strip().upper()
    if a not in ("X", "Y", "Z"):
        raise ValueError(f"axis must be one of X, Y, Z (got {axis!r})")
    return a


def build_axis_sketch(axis: str, name: str = "_Axis") -> Dict[str, Any]:
    """Sketch feature payload with one construction line along the global axis."""
    axis = normalize_axis(axis)
    plane_name, plane_id, p1, p2 = _AXIS_LINE[axis]
    sb = SketchBuilder(name=name, plane=SketchPlane[plane_name.upper()], plane_id=plane_id)
    sb.add_line(list(p1), list(p2), is_construction=True)
    return sb.build()
