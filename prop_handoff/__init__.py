# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
#  PropHandoff — prop transfers and two-handed grips
#  License: GPL-3.0-or-later
# ---------------------------------------------------------------------------
"""Add-on entry point.

This module only holds `bl_info` and the register/unregister orchestration.
The logic lives in three modules:

    utils.py      -> pure functions: matrices, F-Curves, keys, drivers,
                     serialization, cycle detection (no Blender class)
    operators.py  -> PropertyGroups + operators + core functions
                     (apply_handoff, attach_hand, release_hand, build_helpers)
    panel.py      -> UIList + panels of the "PropHandoff" tab (N panel)
"""

bl_info = {
    "name": "PropHandoff",
    "author": "PropHandoff",
    "version": (0, 3, 3),
    "blender": (3, 6, 0),
    "location": "3D View > Sidebar (N) > PropHandoff tab",
    "description": (
        "Prop transfers between hands/slots (assign, release, throw) without "
        "visual jumps, and two-handed grips (Grip Zone, attach, master hand) "
        "without dependency cycles."
    ),
    "warning": "",
    "doc_url": "",
    "tracker_url": "",
    "category": "Animation",
}

# ---------------------------------------------------------------------------
#  Import / hot reload
#  If `bpy` is already in the namespace the add-on was imported before: reload
#  the submodules (F3 > Reload Scripts, or running from the text editor)
#  instead of keeping the old versions in memory.
# ---------------------------------------------------------------------------
if "bpy" in locals():
    import importlib

    utils = importlib.reload(utils)          # noqa: F821
    operators = importlib.reload(operators)  # noqa: F821
    panel = importlib.reload(panel)          # noqa: F821
else:
    from . import utils
    from . import operators
    from . import panel

import bpy  # noqa: E402, F401  (deliberately after the reload block)


def register():
    """Register the classes: data model + operators, then UI."""
    operators.register()
    panel.register()


def unregister():
    """Unregister in the reverse order of `register()`."""
    panel.unregister()
    operators.unregister()


if __name__ == "__main__":
    register()
