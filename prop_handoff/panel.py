# -*- coding: utf-8 -*-
"""User interface: the "PropHandoff" tab of the 3D View sidebar (N).

    PropHandoff                 active prop + prop tabs · Setup/Sync/Clear · frame state
     ├ Slots                    UIList + details of the active slot
     ├ Actions (current frame)  Assign → slot · Release · Throw… · Bake
     │  └ Throw Settings
     └ Transfers                Previous/Next · history + Go
    Two-Hand Grip               active prop + prop tabs · Setup/Resync · cycle warning
     ├ IK Targets               object + bone per hand · errors only
     ├ Grip Zone                radius · Enable/Disable · Lock/Unlock · Arm Reach
     ├ Grip Poses               Set left/right hand pose
     ├ Master Hand              None / Left / Right
     └ Attach (current frame)   frame state · Attach / Release · Switch to two-handed

Rules: no help text — the state is carried by the pressed buttons, the
button icons and the two "Frame N: …" boxes; red labels appear only on
errors. Every displayed state is derived from the constraints (influence,
presence, activation), never from a variable that could drift out of sync.
"""

import bpy

from . import utils

#: Maximum rows of the transfer history.
MAX_EVENT_ROWS = 20

#: Wrap width of long messages (the UI does not wrap by itself).
WRAP_WIDTH = 38


def _wrap(text, width=WRAP_WIDTH):
    """Split a message into short lines."""
    lines, current = [], ""
    for word in text.split():
        candidate = (current + " " + word).strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _alert_box(layout, message, icon='ERROR'):
    """Multi-line red box."""
    box = layout.box()
    box.alert = True
    for index, line in enumerate(_wrap(message)):
        box.label(text=line, icon=icon if index == 0 else 'BLANK1')


def _draw_prop_picker(layout, context):
    """"Active prop" field + eyedropper. Returns the prop, or None after
    showing why."""
    scene_settings = utils.get_scene_settings(context.scene)
    if scene_settings is None:
        return None

    column = layout.column(align=True)
    column.label(text="Active prop")
    row = column.row(align=True)
    row.prop(scene_settings, "prop_object", text="")
    row.operator("prophandoff.use_active_object", text="", icon='EYEDROPPER')

    obj = utils.get_prop_object(context)
    # One tab per object carrying PropHandoff data: switching props does not
    # depend on the selection nor on the mode (Object, Pose…).
    props = utils.iter_props(context.scene)
    if props:
        flow = column.grid_flow(row_major=True, columns=0, even_columns=True, align=True)
        for candidate in props:
            flow.operator("prophandoff.pick_prop", text=candidate.name,
                          depress=(candidate is obj)).object_name = candidate.name
    if obj is None:
        return None
    if obj.library is not None:
        layout.label(text="Linked object: read-only.", icon='ERROR')
        return None
    if utils.get_obj_settings(obj) is None:
        return None
    return obj


def _usable_prop(context):
    """Prop usable by the sub-panels (not linked, settings present)."""
    obj = utils.get_prop_object(context)
    if obj is None or obj.library is not None:
        return None
    if utils.get_obj_settings(obj) is None:
        return None
    return obj


# ---------------------------------------------------------------------------
#  Slot list
# ---------------------------------------------------------------------------

class PROPHANDOFF_UL_slots(bpy.types.UIList):
    """One row per slot: state dot, editable label, target, ×."""

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        obj = data.id_data
        constraint = utils.get_slot_constraint(obj, item.name)
        is_active = constraint is not None and constraint.influence > utils.ACTIVE_THRESHOLD

        row = layout.row(align=True)
        row.label(text="", icon='RADIOBUT_ON' if is_active else 'RADIOBUT_OFF')
        row.prop(item, "label", text="", emboss=False)

        info = row.row(align=True)
        info.alignment = 'RIGHT'
        if item.target is None:
            info.label(text="no target", icon='ERROR')
        elif constraint is None and utils.slot_is_suspended(obj, item):
            # The hand follows the prop (section 2): constraint suspended, see
            # `utils.ensure_constraints`. "Assign" lifts the suspension.
            info.label(text="%s › %s" % (item.target.name, item.subtarget)
                       if item.subtarget else item.target.name, icon='UNLINKED')
        elif item.subtarget:
            info.label(text="%s › %s" % (item.target.name, item.subtarget), icon='BONE_DATA')
        else:
            info.label(text=item.target.name, icon='OBJECT_DATA')
        info.operator("prophandoff.slot_remove", text="", icon='X', emboss=False).index = index


# ---------------------------------------------------------------------------
#  Section 1
# ---------------------------------------------------------------------------

class _PropHandoffPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PropHandoff"


class VIEW3D_PT_prophandoff_main(_PropHandoffPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_main"
    bl_label = "PropHandoff"

    def draw(self, context):
        layout = self.layout
        obj = _draw_prop_picker(layout, context)
        if obj is None:
            return

        layout.separator()
        if not utils.is_setup(obj):
            layout.operator("prophandoff.setup_prop", icon='CONSTRAINT')
            if utils.read_slots_id_prop(obj):
                layout.operator("prophandoff.reload_slots", icon='FILE_REFRESH')
            return

        row = layout.row(align=True)
        row.operator("prophandoff.setup_prop", text="Sync", icon='FILE_REFRESH')
        row.operator("prophandoff.clear_setup", text="", icon='TRASH')

        active_slot = utils.active_slot_name(obj)
        box = layout.box()
        frame = context.scene.frame_current
        if active_slot is None:
            box.label(text="Frame %d: released (world space)" % frame, icon='UNLINKED')
        else:
            box.label(text="Frame %d: %s" % (frame, utils.slot_label_for(obj, active_slot)),
                      icon='LINKED')


class VIEW3D_PT_prophandoff_slots(_PropHandoffPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_slots"
    bl_parent_id = "VIEW3D_PT_prophandoff_main"
    bl_label = "Slots"

    @classmethod
    def poll(cls, context):
        return _usable_prop(context) is not None

    def draw(self, context):
        layout = self.layout
        obj = _usable_prop(context)
        settings = utils.get_obj_settings(obj)

        row = layout.row()
        row.template_list("PROPHANDOFF_UL_slots", "", settings, "slots",
                          settings, "active_slot_index",
                          rows=max(2, min(len(settings.slots), 6)))
        side = row.column(align=True)
        side.operator("prophandoff.slot_add", text="", icon='ADD')
        side.operator("prophandoff.slot_remove", text="", icon='REMOVE').index = -1
        side.separator()
        side.operator("prophandoff.slot_move", text="", icon='TRIA_UP').direction = 'UP'
        side.operator("prophandoff.slot_move", text="", icon='TRIA_DOWN').direction = 'DOWN'

        if not (0 <= settings.active_slot_index < len(settings.slots)):
            return
        slot = settings.slots[settings.active_slot_index]
        box = layout.box()
        box.use_property_split = True
        box.use_property_decorate = False
        box.prop(slot, "name", text="ID")
        box.prop(slot, "label", text="Label")
        box.prop(slot, "target", text="Target")
        if slot.target is not None and slot.target.type == 'ARMATURE':
            box.prop_search(slot, "subtarget", slot.target.data, "bones", text="Bone")
        box.operator("prophandoff.slot_from_selection", icon='EYEDROPPER')


class VIEW3D_PT_prophandoff_actions(_PropHandoffPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_actions"
    bl_parent_id = "VIEW3D_PT_prophandoff_main"
    bl_label = "Actions (current frame)"

    @classmethod
    def poll(cls, context):
        obj = _usable_prop(context)
        return obj is not None and utils.is_setup(obj)

    def draw(self, context):
        layout = self.layout
        obj = _usable_prop(context)
        settings = utils.get_obj_settings(obj)
        active_slot = utils.active_slot_name(obj)

        column = layout.column(align=True)
        for slot in settings.slots:
            row = column.row(align=True)
            row.enabled = slot.target is not None       # no target: greyed out
            is_active = slot.name == active_slot
            operator = row.operator("prophandoff.assign",
                                    text="Assign → %s" % utils.slot_display(slot),
                                    icon='RADIOBUT_ON' if is_active else 'RADIOBUT_OFF',
                                    depress=is_active)
            operator.slot_name = slot.name

        layout.separator()
        column = layout.column(align=True)
        column.operator("prophandoff.release", text="Release", icon='UNLINKED')
        column.operator("prophandoff.throw", text="Throw…", icon='FORCE_WIND')
        layout.separator()
        layout.operator("prophandoff.bake_visual_transform", icon='KEYFRAME_HLT')


class VIEW3D_PT_prophandoff_throw(_PropHandoffPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_throw"
    bl_parent_id = "VIEW3D_PT_prophandoff_actions"
    bl_label = "Throw Settings"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        settings = utils.get_scene_settings(context.scene)
        if settings is None:
            return
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(settings, "flight_frames")
        layout.prop(settings, "velocity_source")
        if settings.velocity_source == 'MEASURED':
            layout.prop(settings, "velocity_samples")
            layout.prop(settings, "speed_scale")
        else:
            layout.prop(settings, "direction")
            layout.prop(settings, "speed")
        layout.separator()
        layout.prop(settings, "gravity")
        layout.prop(settings, "spin")


class VIEW3D_PT_prophandoff_events(_PropHandoffPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_events"
    bl_parent_id = "VIEW3D_PT_prophandoff_main"
    bl_label = "Transfers"

    @classmethod
    def poll(cls, context):
        obj = _usable_prop(context)
        return obj is not None and utils.is_setup(obj)

    def draw(self, context):
        layout = self.layout
        obj = _usable_prop(context)
        current = context.scene.frame_current

        row = layout.row(align=True)
        row.operator("prophandoff.jump_transfer", text="Previous",
                     icon='PREV_KEYFRAME').direction = 'PREV'
        row.operator("prophandoff.jump_transfer", text="Next",
                     icon='NEXT_KEYFRAME').direction = 'NEXT'

        events = utils.list_transfer_events(obj)
        if not events:
            return

        column = layout.column(align=True)
        for event in events[:MAX_EVENT_ROWS]:
            is_current = event["frame"] == current
            row = column.row(align=True)
            row.label(text="Frame %d → %s" % (event["frame"], event["label"]),
                      icon='KEYFRAME_HLT' if is_current else 'KEYFRAME')
            sub = row.row(align=True)
            sub.enabled = not is_current
            sub.operator("prophandoff.go_to_transfer", text="Go").frame = event["frame"]
        remaining = len(events) - MAX_EVENT_ROWS
        if remaining > 0:
            column.label(text="… and %d more." % remaining)


# ---------------------------------------------------------------------------
#  Section 2 — Two-Hand Grip
# ---------------------------------------------------------------------------

class VIEW3D_PT_prophandoff_twohand(_PropHandoffPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_twohand"
    bl_label = "Two-Hand Grip"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        obj = _draw_prop_picker(layout, context)    # same active prop as section 1
        if obj is None:
            return

        layout.separator()
        if not utils.is_twohand_setup(obj):
            layout.operator("prophandoff.setup_twohanded", icon='EMPTY_ARROWS')
            return

        layout.operator("prophandoff.setup_twohanded", text="Resync", icon='FILE_REFRESH')
        warning = utils.dependency_warning(obj)
        if warning:
            _alert_box(layout, warning)


def _draw_ik_target(layout, settings, side, scene):
    """Object + bone of one IK target, with the usual scene warnings."""
    suffix = "l" if side == 'LEFT' else "r"
    object_attribute = "ik_object_" + suffix
    bone_attribute = "ik_bone_" + suffix
    ik_object = getattr(settings, object_attribute)

    column = layout.column(align=True)
    column.prop(settings, object_attribute,
                text="Left hand" if side == 'LEFT' else "Right hand")
    if ik_object is None:
        return

    if ik_object.type == 'ARMATURE':
        column.prop_search(settings, bone_attribute, ik_object.data, "bones", text="Bone")
        if getattr(settings, bone_attribute) and not utils.armature_deforms_something(ik_object, scene):
            # Duplicate rig (Akaza_rig.001): right bones, no character follows it.
            column.label(text="This rig deforms no mesh (duplicate?)", icon='ERROR')
        return

    if ik_object.name.startswith("WGT-"):
        # Controller widget picked by mistake instead of the rig.
        column.label(text="This is a widget, not the rig", icon='ERROR')


class VIEW3D_PT_prophandoff_ik_targets(_PropHandoffPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_ik_targets"
    bl_parent_id = "VIEW3D_PT_prophandoff_twohand"
    bl_label = "IK Targets"

    @classmethod
    def poll(cls, context):
        return _usable_prop(context) is not None

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        obj = _usable_prop(context)
        settings = utils.get_obj_settings(obj)

        _draw_ik_target(layout, settings, 'LEFT', context.scene)
        layout.separator()
        _draw_ik_target(layout, settings, 'RIGHT', context.scene)

        missing = [utils.side_label(side) for side in utils.SIDES
                   if utils.ik_handle_owner(obj, side) is None]
        if missing:
            box = layout.box()
            box.alert = True
            box.label(text="Missing IK target: %s" % ", ".join(missing), icon='ERROR')


class _TwoHandSubPanel(_PropHandoffPanel):
    """Sub-panels shown once the grip points exist."""

    bl_parent_id = "VIEW3D_PT_prophandoff_twohand"

    @classmethod
    def poll(cls, context):
        obj = _usable_prop(context)
        return obj is not None and utils.is_twohand_setup(obj)


class VIEW3D_PT_prophandoff_grip_zone(_TwoHandSubPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_grip_zone"
    bl_label = "Grip Zone"

    def draw(self, context):
        layout = self.layout
        obj = _usable_prop(context)
        settings = utils.get_obj_settings(obj)

        row = layout.row()
        row.use_property_split = True
        row.prop(settings, "grip_zone_radius", text="Radius")

        active = [side for side in utils.SIDES if utils.grip_zone_active(obj, side)]
        row = layout.row(align=True)
        row.operator("prophandoff.enable_grip_zone", text="Enable",
                     icon='CHECKMARK', depress=bool(active))
        row.operator("prophandoff.disable_grip_zone", text="Disable", icon='X')

        # --- Lock: hands on the grips whatever the distance ---------------
        layout.separator()
        locked = utils.grip_lock_active(obj)
        row = layout.row(align=True)
        row.operator("prophandoff.grip_lock", text="Lock", icon='LOCKED',
                     depress=locked).action = 'LOCK'
        row.operator("prophandoff.grip_lock", text="Unlock", icon='UNLOCKED').action = 'UNLOCK'

        # --- Arm reach: the prop stops when an arm is fully extended -------
        reach_sides = utils.reach_sides(obj) if utils.reach_limit_present(obj) else []
        row = layout.row(align=True)
        row.operator("prophandoff.reach_limit", text="Arm Reach", icon='CON_LOCLIMIT',
                     depress=bool(reach_sides)).action = 'ENABLE'
        row.operator("prophandoff.reach_limit", text="", icon='X').action = 'DISABLE'
        sub = layout.row()
        sub.use_property_split = True
        sub.prop(settings, "reach_margin", text="Margin")


class VIEW3D_PT_prophandoff_grip_poses(_TwoHandSubPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_grip_poses"
    bl_label = "Grip Poses"

    def draw(self, context):
        layout = self.layout
        obj = _usable_prop(context)

        column = layout.column(align=True)
        for side in utils.SIDES:
            defined = utils.has_grip_pose(obj, side)
            row = column.row(align=True)
            row.enabled = utils.ik_handle_owner(obj, side) is not None
            row.operator("prophandoff.set_grip_pose",
                         text="Set %s pose" % utils.side_label(side),
                         icon='CHECKMARK' if defined else 'RADIOBUT_OFF').hand = side


class VIEW3D_PT_prophandoff_master(_TwoHandSubPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_master"
    bl_label = "Master Hand"

    def draw(self, context):
        layout = self.layout
        obj = _usable_prop(context)
        current = utils.holding_side(obj) or 'NONE'     # derived from the constraints

        row = layout.row(align=True)
        for value, text in (('NONE', "None"), ('LEFT', "Left"), ('RIGHT', "Right")):
            sub = row.row(align=True)
            if value != 'NONE':
                sub.enabled = utils.ik_handle_owner(obj, value) is not None
            sub.operator("prophandoff.set_master_hand", text=text,
                         depress=(current == value)).hand = value


class VIEW3D_PT_prophandoff_attach(_TwoHandSubPanel, bpy.types.Panel):
    bl_idname = "VIEW3D_PT_prophandoff_attach"
    bl_label = "Attach (current frame)"

    def draw(self, context):
        layout = self.layout
        obj = _usable_prop(context)
        frame = context.scene.frame_current

        attached = [side for side in utils.SIDES if utils.is_hand_attached(obj, side)]
        box = layout.box()
        if len(attached) == 2:
            box.label(text="Frame %d: both hands on the prop" % frame, icon='LINKED')
        elif attached:
            box.label(text="Frame %d: %s only" % (frame, utils.side_label(attached[0])),
                      icon='LINKED')
        else:
            box.label(text="Frame %d: hands free" % frame, icon='UNLINKED')

        column = layout.column(align=True)
        column.operator("prophandoff.attach_both_hands", text="Attach Both Hands",
                        icon='CON_CHILDOF')
        column.operator("prophandoff.release_both_hands", text="Release Both Hands",
                        icon='UNLINKED')
        layout.operator("prophandoff.switch_to_twohanded", text="→ Switch to Two-Handed",
                        icon='CON_TRANSLIKE')


# ---------------------------------------------------------------------------
#  Registration
# ---------------------------------------------------------------------------

classes = (
    PROPHANDOFF_UL_slots,
    # Section 1
    VIEW3D_PT_prophandoff_main,
    VIEW3D_PT_prophandoff_slots,
    VIEW3D_PT_prophandoff_actions,
    VIEW3D_PT_prophandoff_throw,
    VIEW3D_PT_prophandoff_events,
    # Section 2
    VIEW3D_PT_prophandoff_twohand,
    VIEW3D_PT_prophandoff_ik_targets,
    VIEW3D_PT_prophandoff_grip_zone,
    VIEW3D_PT_prophandoff_grip_poses,
    VIEW3D_PT_prophandoff_master,
    VIEW3D_PT_prophandoff_attach,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
