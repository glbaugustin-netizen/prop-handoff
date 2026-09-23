# -*- coding: utf-8 -*-
"""PropHandoff data model, core functions and operators.

Layout:

  1. PropertyGroups (slot, object settings, scene settings)
  2. `apply_handoff()`: core shared by Assign / Release / Throw / Master hand
  3. Configuration operators (section 1)
  4. Animation operators (section 1)
  5. Two-Hand Grip: `build_helpers()`, `attach_hand()`, `release_hand()`
     and their operators (section 2)
  6. History navigation
  7. register / unregister

Direction of the dependencies, to keep in mind everywhere:
  section 1 = the prop follows the hand; section 2 = the hand follows the prop.
  Both on the same hand = dependency cycle.
"""

import math

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from mathutils import Euler, Matrix, Vector

from . import utils


# ===========================================================================
#  1. Data model
# ===========================================================================

def _slot_name_update(self, context):
    """Slot rename: cleaning, uniqueness, constraint and data_paths.

    The callback re-assigns itself once if the name had to be cleaned, then
    stabilizes (`cleaned == self.name`).
    """
    obj = self.id_data
    settings = utils.get_obj_settings(obj)

    cleaned = utils.sanitize_slot_name(self.name)
    if settings is not None:
        cleaned = utils.unique_slot_name(settings.slots, cleaned, exclude=self)
    if cleaned != self.name:
        self.name = cleaned
        return

    previous = self.stored_name
    if previous and previous != cleaned:
        utils.rename_slot(obj, previous, cleaned)
    self.stored_name = cleaned

    utils.ensure_constraints(obj)
    utils.store_slots_id_prop(obj)


def _slot_target_update(self, context):
    """Immediately reflect a target change on the constraint."""
    obj = self.id_data
    utils.ensure_constraints(obj)
    utils.store_slots_id_prop(obj)


class PH_SlotItem(bpy.types.PropertyGroup):
    """A slot = an attachment point (armature bone or object/empty)."""

    name: StringProperty(
        name="ID",
        description="Technical identifier of the slot (names the PH_<slot> constraint)",
        default="Slot",
        update=_slot_name_update,
    )
    stored_name: StringProperty(
        name="Previous name",
        description="Internal: detects a rename",
        default="",
        options={'HIDDEN'},
    )
    label: StringProperty(
        name="Label",
        description="Readable name shown in the interface (e.g. Left hand)",
        default="",
    )
    target: PointerProperty(
        name="Target",
        description="Armature or object/empty used as attachment point",
        type=bpy.types.Object,
        update=_slot_target_update,
    )
    subtarget: StringProperty(
        name="Bone",
        description="Bone name (only if the target is an armature)",
        default="",
        update=_slot_target_update,
    )


def _twohand_settings_update(self, context):
    """Sync the custom properties read by the drivers and the sphere."""
    prop = self.id_data
    utils.sync_twohand_id_props(prop)
    prop.update_tag()
    zone = utils.find_grip_zone(prop)
    if zone is not None:
        zone.empty_display_size = self.grip_zone_radius


class PH_ObjectSettings(bpy.types.PropertyGroup):
    """Settings carried by an object: `obj.prop_handoff`."""

    slots: CollectionProperty(type=PH_SlotItem)
    active_slot_index: IntProperty(name="Active slot", default=0, min=0)

    # --- Section 2: a hand's IK target is an object OR a bone -------------
    ik_object_l: PointerProperty(
        name="Left hand IK",
        description="Object carrying the left hand IK target "
                    "(the armature if the target is a bone)",
        type=bpy.types.Object,
        update=_twohand_settings_update,
    )
    ik_bone_l: StringProperty(
        name="Left bone",
        description="Left hand IK bone (empty if the target is an object)",
        default="",
        update=_twohand_settings_update,
    )
    ik_object_r: PointerProperty(
        name="Right hand IK",
        description="Object carrying the right hand IK target "
                    "(the armature if the target is a bone)",
        type=bpy.types.Object,
        update=_twohand_settings_update,
    )
    ik_bone_r: StringProperty(
        name="Right bone",
        description="Right hand IK bone (empty if the target is an object)",
        default="",
        update=_twohand_settings_update,
    )
    grip_zone_radius: FloatProperty(
        name="Radius",
        description="Radius of the proximity zone (Blender units). Beyond it the "
                    "hand is free; inside it blends towards the grip point",
        default=utils.DEFAULT_GRIP_RADIUS,
        min=0.1, max=2.0, soft_min=0.1, soft_max=2.0,
        subtype='DISTANCE',
        update=_twohand_settings_update,
    )
    reach_margin: FloatProperty(
        name="Margin",
        description="Fraction of the arm length kept in reserve: the prop stops "
                    "slightly before the arm is fully extended",
        default=utils.DEFAULT_REACH_MARGIN,
        min=0.0, max=0.5, soft_max=0.2,
        subtype='FACTOR',
        update=lambda self, context: update_reach_values(self.id_data),
    )


class PH_SceneSettings(bpy.types.PropertyGroup):
    """Scene settings: active prop and throw defaults."""

    prop_object: PointerProperty(
        name="Active prop",
        description="Object driven by PropHandoff (empty = active object of the view)",
        type=bpy.types.Object,
    )

    # Duplicated on PROPHANDOFF_OT_throw (copied in its invoke) so that the
    # F9 panel stays usable.
    flight_frames: IntProperty(
        name="Flight frames",
        description="Number of trajectory frames generated after the release",
        default=24, min=1, max=1000,
    )
    velocity_source: EnumProperty(
        name="Velocity",
        description="Origin of the initial velocity",
        items=[
            ('MEASURED', "From animation",
             "Measure the real motion of the prop just before the release"),
            ('MANUAL', "Direction + speed",
             "Use the direction and speed entered below"),
        ],
        default='MEASURED',
    )
    velocity_samples: IntProperty(
        name="Sample frames",
        description="Frames analysed backwards to estimate the velocity",
        default=2, min=1, max=20,
    )
    speed_scale: FloatProperty(
        name="Multiplier",
        description="Factor applied to the measured velocity",
        default=1.0, min=0.0, soft_max=5.0,
    )
    direction: FloatVectorProperty(
        name="Direction",
        description="Throw direction in world space (normalized)",
        default=(0.0, 1.0, 0.5), subtype='XYZ', size=3,
    )
    speed: FloatProperty(
        name="Speed",
        description="Initial speed in Blender units per second",
        default=5.0, min=0.0, soft_max=50.0,
    )
    gravity: FloatProperty(
        name="Gravity",
        description="Vertical acceleration in m/s² (negative = downwards)",
        default=-9.81, soft_min=-30.0, soft_max=0.0,
    )
    spin: FloatVectorProperty(
        name="Spin",
        description="Angular velocity (world axes), shown in degrees/s",
        default=(0.0, 0.0, 0.0), subtype='EULER', size=3,
    )


# ===========================================================================
#  2. Transfer core
# ===========================================================================

#: Tolerance (Blender units) under which a placement is considered exact.
SETTLE_TOLERANCE = 1e-5


def _matrices_close(a, b, tolerance=SETTLE_TOLERANCE):
    return all(abs(a[i][j] - b[i][j]) <= tolerance for i in range(4) for j in range(4))


def settle_owner(context, owner, world, constraint_matrix, frame, interpolation=None):
    """Place an owner (Object or PoseBone) and insert its LocRotScale keys so
    that its final result at the current frame is exactly `world`.

    Iteration 0: the spec's compensation, `basis = C⁻¹ @ world` (C = factor
    of the Child Of just activated, None otherwise). If other constraints
    still act on the owner (Auto-Rig Pro IK space-switch Child Of, the
    animator's own constraint…), the result differs: the residual is measured
    after evaluation and corrected, at most twice. For a constraint that
    pre-multiplies by a constant matrix one correction is exact; without a
    third-party constraint iteration 0 is enough and nothing changes. The
    keys are rewritten on every pass.
    """
    pre = world.copy()
    if constraint_matrix is not None:
        pre = constraint_matrix.inverted_safe() @ pre
    for _ in range(3):
        utils.set_owner_world_matrix(owner, pre, None)
        utils.insert_transform_keys(owner, frame, interpolation)
        context.view_layer.update()
        final = utils.owner_world_matrix(context.evaluated_depsgraph_get(), owner)
        if final is None or _matrices_close(final, world):
            break
        pre = pre @ final.inverted_safe() @ world


def constraint_active_frames(scene, owner, constraint, last_frame=None):
    """Sorted frames where a keyframed constraint acts (influence > threshold).

    Without F-Curve: the whole scene range if it is active, else nothing.
    With F-Curve: from the first key (or scene start) to the last key (or
    scene end, or `last_frame` — the current frame may lie beyond the scene
    range), where the evaluated influence exceeds the threshold.
    """
    if not utils.is_constraint_enabled(constraint):
        return []
    end = scene.frame_end if last_frame is None else max(scene.frame_end, int(last_frame))
    fcurve = utils.find_fcurve(owner.id_data, utils.constraint_influence_path(constraint))
    if fcurve is None or not len(fcurve.keyframe_points):
        if constraint.influence > utils.ACTIVE_THRESHOLD:
            return list(range(scene.frame_start, end + 1))
        return []
    keys = [keyframe.co.x for keyframe in fcurve.keyframe_points]
    low = int(min(scene.frame_start, math.floor(min(keys))))
    high = int(max(end, math.ceil(max(keys))))
    return [frame for frame in range(low, high + 1)
            if fcurve.evaluate(frame) > utils.ACTIVE_THRESHOLD]


def _evaluated_influence(context, owner, constraint):
    """Evaluated influence (driver included) of a constraint at the current frame."""
    depsgraph = context.evaluated_depsgraph_get()
    if isinstance(owner, bpy.types.PoseBone):
        evaluated = owner.id_data.evaluated_get(depsgraph).pose.bones.get(owner.name)
    else:
        evaluated = owner.evaluated_get(depsgraph)
    if evaluated is None:
        return 0.0
    evaluated_constraint = evaluated.constraints.get(constraint.name)
    return evaluated_constraint.influence if evaluated_constraint is not None else 0.0


def bake_and_remove_constraint(context, owner, constraint, last_frame=None):
    """Bake the owner's visual motion to keys on the frames where the
    constraint acts, then remove the constraint, its driver if any and its
    influence keys.

    This is the equivalent of "Bake Action ▸ Visual Keying ▸ Clear
    Constraints" limited to one constraint and to the frames where it
    matters: the motion is preserved identically, without the constraint —
    hence without a dependency relation. Two passes (read every matrix, then
    write) because writing keys changes the evaluation of the next frames.

    Keyframed influence (attach, slot): frames where it exceeds the
    threshold. Driven influence (Grip Zone, Lock): sampled frame by frame from
    the scene start to `last_frame` (usually the current frame: afterwards
    the hand is free or attached), kept as soon as it is non-zero.
    Returns ``(first, last)`` baked frame, or None if nothing was baked.
    """
    scene = context.scene
    original = scene.frame_current
    driven = utils.constraint_is_driven(owner, constraint)
    if driven:
        last = scene.frame_end if last_frame is None else int(last_frame)
        candidates = list(range(min(scene.frame_start, last), last + 1))
    else:
        candidates = constraint_active_frames(scene, owner, constraint, last_frame)
        if last_frame is not None:
            candidates = [frame for frame in candidates if frame <= last_frame]
    worlds = {}
    try:
        if utils.is_constraint_enabled(constraint):
            for frame in candidates:
                scene.frame_set(frame)
                if driven and _evaluated_influence(context, owner, constraint) <= 1e-3:
                    continue
                worlds[frame] = utils.owner_world_matrix(context.evaluated_depsgraph_get(), owner)
        utils.remove_driver(constraint, "influence")
        utils.remove_fcurves(owner.id_data, utils.constraint_influence_path(constraint))
        owner.constraints.remove(constraint)
        for frame in sorted(worlds):
            if worlds[frame] is None:
                continue
            scene.frame_set(frame)
            settle_owner(context, owner, worlds[frame], None, frame)
    finally:
        scene.frame_set(original)
    frames = sorted(frame for frame in worlds if worlds[frame] is not None)
    return (frames[0], frames[-1]) if frames else None


def bake_and_remove_grip_blend(context, prop, side, last_frame):
    """Remove one side's Grip Zone after baking its effect to keys (up to
    `last_frame` included), then remove the helper empties. Returns a note
    for the report, or None if nothing existed."""
    owner = utils.ik_handle_owner(prop, side)
    notes = []
    if owner is not None:
        for constraint in list(utils.iter_prefixed_constraints(owner, utils.PH_GRIPBLEND_PREFIX)):
            baked = bake_and_remove_constraint(context, owner, constraint, last_frame)
            notes.append(_bake_note("Grip Zone of the %s" % utils.side_label(side), baked))
    if utils.helpers_present(prop, side):
        utils.remove_helpers(prop, side)
        if not notes:
            notes.append("Helper empties of the %s removed." % utils.side_label(side))
    return " ".join(notes) if notes else None


def _bake_note(what, baked):
    if baked is None:
        return "%s removed (inactive)." % what
    return "%s baked to keys (frames %d–%d) then removed." % (what, baked[0], baked[1])


def detach_hand_from_prop(context, prop, side, frame):
    """No more `hand → prop` constraint on this hand (anti-cycle).

    Required before the prop follows this hand. A constraint keeps its
    dependency relations even at influence 0 or disabled: muting is not
    enough, it must be removed. To lose nothing:
      * attach active at the current frame → released first (hold keys, the
        hand stays where it is);
      * Grip Zone → removed (continuous effect, baked first);
      * keyframed attach → history baked to keys then constraint removed.
    Returns the list of notes for the report.
    """
    notes = []
    ik_object, ik_bone = utils.get_ik_handle(prop, side)
    owner = utils.resolve_ik_owner(ik_object, ik_bone)
    if owner is None:
        return notes

    # Grip Zone / Lock first: its effect is baked up to the current frame
    # (the hand keeps exactly its visual position), then the constraint goes
    # away with its relations.
    note = bake_and_remove_grip_blend(context, prop, side, frame)
    if note:
        notes.append(note)
        context.view_layer.update()

    if utils.is_hand_attached(prop, side):
        success, message = release_hand(context, prop, side, frame, keep_grip_zone=True)
        if success:
            notes.append(message)
            context.view_layer.update()

    for constraint in list(utils.hand_constraints_targeting_prop(prop, ik_object, ik_bone)):
        baked = bake_and_remove_constraint(context, owner, constraint)
        notes.append(_bake_note("Attach of the %s" % utils.side_label(side), baked))
    return notes


def release_prop_from_hand(context, prop, ik_object, ik_bone):
    """No more `prop → hand` slot constraint towards this IK handle, nor
    towards a bone or object depending on it (deform bone copying the
    controller, object parented to the hand…).

    Required before the hand follows the prop. The slot history (frames where
    the prop followed the hand) is baked to keys on the prop before the
    constraint is removed. Error if the slot is active at the current frame:
    the prop must be released first. Returns ``(success, message, notes)``.
    """
    notes = []
    for constraint in list(utils.prop_constraints_depending_on_handle(prop, ik_object, ik_bone)):
        if constraint.influence > utils.ACTIVE_THRESHOLD:
            return False, ("The prop follows this hand (slot \"%s\"): release it first."
                           % utils.slot_label_for(prop, utils.slot_name_from_constraint(constraint))), notes
        label = utils.slot_label_for(prop, utils.slot_name_from_constraint(constraint))
        baked = bake_and_remove_constraint(context, prop, constraint)
        notes.append(_bake_note("Prop slot \"%s\"" % label, baked))
    return True, "", notes


def _sides_for_handle(prop, target, subtarget):
    """Sides whose IK handle is (target, subtarget) or on which this target
    depends (`utils.target_depends_on_handle`)."""
    sides = []
    for side in utils.SIDES:
        ik_object, ik_bone = utils.get_ik_handle(prop, side)
        if ik_object is not None and utils.target_depends_on_handle(
                target, subtarget or "", ik_object, ik_bone):
            sides.append(side)
    return sides


def apply_handoff(context, obj, slot_name=None):
    """Transfer at the current frame; `slot_name=None` = release.

    The order is critical:
      0. the targeted hand must no longer follow the prop (anti-cycle, see
         `detach_hand_from_prop`); the slot constraint is (re)created;
      1. current VISUAL transform (constraints applied);
      2. previous state frozen at `frame - 1` as CONSTANT (transform + influences);
      3. every influence to 0 at the current frame;
      4. activation of the target slot;
      5. compensation: the final result must be exactly the matrix of 1.

    Returns ``(success, message)``. Nothing is written on a validation error.
    """
    scene = context.scene
    frame = scene.frame_current
    notes = []

    slot = utils.find_slot(obj, slot_name) if slot_name else None
    if slot_name:
        if slot is None and utils.get_slot_constraint(obj, slot_name) is None:
            return False, "Slot not found: %s" % slot_name
        if slot is not None:
            if slot.target is None:
                return False, ("Slot \"%s\" has no target."
                               % utils.slot_display(slot))
            if slot.subtarget and (slot.target.type != 'ARMATURE'
                                   or slot.subtarget not in slot.target.pose.bones):
                return False, "Bone \"%s\" does not exist in %s." % (slot.subtarget, slot.target.name)
            # --- 0. the targeted hand must no longer follow the prop ---------
            for side in _sides_for_handle(obj, slot.target, slot.subtarget):
                notes += detach_hand_from_prop(context, obj, side, frame)
            if utils.slot_is_suspended(obj, slot):
                # hand → prop constraints outside the declared IK handles
                # (settings changed after an attach): baked and removed.
                owner = utils.resolve_ik_owner(slot.target, slot.subtarget)
                for constraint in list(utils.hand_constraints_targeting_prop(
                        obj, slot.target, slot.subtarget)):
                    baked = bake_and_remove_constraint(context, owner, constraint)
                    notes.append(_bake_note("Constraint \"%s\"" % constraint.name, baked))
            utils.ensure_constraints(obj)

    constraints = list(utils.iter_ph_constraints(obj))
    if not constraints:
        return False, "The object is not set up: run \"Setup Prop\" first."

    target = None
    if slot_name:
        target = utils.get_slot_constraint(obj, slot_name)
        if target is None:
            return False, "Slot not found: %s" % slot_name
        if target.target is None:
            return False, ("Slot \"%s\" has no target."
                           % utils.slot_label_for(obj, slot_name))
        if target.subtarget and (target.target.type != 'ARMATURE'
                                 or target.subtarget not in target.target.pose.bones):
            return False, ("Bone \"%s\" does not exist in %s."
                           % (target.subtarget, target.target.name))

    # --- 1. current visual transform ---------------------------------------
    depsgraph = context.evaluated_depsgraph_get()
    world = utils.evaluated_matrix_world(depsgraph, obj)

    constraint_matrix = None
    if target is not None:
        constraint_matrix = utils.constraint_parent_matrix(depsgraph, target)
        if constraint_matrix is None:
            return False, "Unreadable target of slot \"%s\"." % slot_name

    # --- 2. previous state frozen at frame - 1 -----------------------------
    utils.hold_transform(obj, frame - 1)
    for constraint in constraints:
        utils.hold_influence(constraint, frame - 1)

    # --- 3. everything to zero at the current frame ------------------------
    for constraint in constraints:
        utils.insert_influence_key(constraint, 0.0, frame)

    # --- 4. activation of the requested slot -------------------------------
    if target is not None:
        # Never `set_inverse_pending = True`: the deferred computation would
        # happen after the operator and `inverse_matrix` is not animatable.
        if hasattr(target, "set_inverse_pending"):
            target.set_inverse_pending = False
        utils.insert_influence_key(target, 1.0, frame)

    # --- 5. gate of the reach limit ----------------------------------------
    # The reach Limit Location expresses the world position from the prop's
    # channels: it only makes sense when the prop follows no hand. Its
    # keyframed gate is 0 as soon as a slot is active — closed BEFORE the
    # compensation, otherwise the constraint still pins the prop during the
    # residual evaluation and the recorded channels are wrong (the prop "goes
    # anywhere"). On release it is reopened AFTER: the channels keep the exact
    # visual position, and the stop only applies afterwards (only if the prop
    # is out of reach).
    reach_gated = utils.reach_limit_present(obj)
    if reach_gated:
        utils.hold_id_prop(obj, utils.ID_PROP_REACH_GATE, frame - 1)
        if target is not None:
            utils.insert_id_prop_key(obj, utils.ID_PROP_REACH_GATE, 0.0, frame)

    # --- 6. compensation + LocRotScale keys --------------------------------
    settle_owner(context, obj, world, constraint_matrix, frame)

    if reach_gated and target is None:
        utils.insert_id_prop_key(obj, utils.ID_PROP_REACH_GATE, 1.0, frame)

    if slot_name:
        message = "Frame %d: %s assigned to %s" % (
            frame, obj.name, utils.slot_label_for(obj, slot_name))
    else:
        message = "Frame %d: %s released" % (frame, obj.name)
    if notes:
        message += " " + " ".join(notes)
    return True, message


def freeze_interactive_state(context, prop):
    """Freeze the unkeyed moves of the prop and of the hands (IK targets and
    slot targets) before any operation.

    Without auto-key, a move made with the mouse only exists until the next
    `frame_set` — which our operators call (bake, tag_update). Without this
    step the scene would "jump" to the keyed pose in the middle of the
    operation. Returns the number of owners frozen.
    """
    frame = context.scene.frame_current
    owners = [prop]
    for side in utils.SIDES:
        owners.append(utils.ik_handle_owner(prop, side))
    settings = utils.get_obj_settings(prop)
    if settings is not None:
        for slot in settings.slots:
            owners.append(utils.resolve_ik_owner(slot.target, slot.subtarget)
                          if slot.target is not None else None)
    frozen, seen = 0, set()
    for owner in owners:
        if owner is None:
            continue
        key = (owner.id_data.name, getattr(owner, "name", ""))
        if key in seen:
            continue
        seen.add(key)
        if utils.freeze_unkeyed_edit(owner, frame):
            frozen += 1
    return frozen


def _resolve_prop(context, operator):
    """Prop to process, or None after reporting the error. Also freezes the
    unkeyed moves on the way (see `freeze_interactive_state`)."""
    obj = utils.get_prop_object(context)
    if obj is None:
        operator.report({'ERROR'}, "No prop selected.")
        return None
    if obj.library is not None:
        operator.report({'ERROR'}, "\"%s\" is a linked object (library): not editable." % obj.name)
        return None
    if utils.get_obj_settings(obj) is not None:
        freeze_interactive_state(context, obj)
    return obj


class _PropPoll:
    """Shared poll: a non-linked prop is required."""

    @classmethod
    def poll(cls, context):
        obj = utils.get_prop_object(context)
        return obj is not None and obj.library is None


# ===========================================================================
#  3. Configuration (section 1)
# ===========================================================================

class PROPHANDOFF_OT_setup_prop(_PropPoll, bpy.types.Operator):
    """Prepare the prop: one Child Of constraint per slot, all at influence 0"""

    bl_idname = "prophandoff.setup_prop"
    bl_label = "Setup Prop"
    bl_options = {'REGISTER', 'UNDO'}

    create_default_slots: BoolProperty(
        name="Default slots",
        description="Create Hand_L and Hand_R if no slot is defined yet",
        default=True,
    )
    auto_target: BoolProperty(
        name="Target the armature automatically",
        description="Bind the default slots to the selected armature, "
                    "else to the only armature of the scene",
        default=True,
    )

    #: (identifier, label, candidate bones — case-insensitive, first found).
    #: The hand's deform bone (Auto-Rig Pro `hand.l`, Rigify `DEF-hand.L`)
    #: follows the hand in FK as in IK; if it depends on the IK controller
    #: declared in section 2, the slot is "suspended" while the hand follows
    #: the prop (`utils.slot_is_suspended`).
    DEFAULT_SLOTS = (
        ("Hand_L", "Left hand", ("hand.L", "DEF-hand.L", "Hand_L", "left_hand", "hand_l")),
        ("Hand_R", "Right hand", ("hand.R", "DEF-hand.R", "Hand_R", "right_hand", "hand_r")),
    )

    @staticmethod
    def _guess_bone(armature, candidates):
        by_lower = {}
        for pose_bone in armature.pose.bones:
            by_lower.setdefault(pose_bone.name.lower(), pose_bone.name)
        for candidate in candidates:
            name = by_lower.get(candidate.lower())
            if name is not None:
                return name
        return ""

    @staticmethod
    def _guess_armature(context, obj):
        for candidate in context.selected_objects:
            if candidate.type == 'ARMATURE' and candidate is not obj:
                return candidate
        armatures = [ob for ob in context.scene.objects if ob.type == 'ARMATURE']
        return armatures[0] if len(armatures) == 1 else None

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        settings = utils.get_obj_settings(obj)
        existing = {constraint.name for constraint in utils.iter_ph_constraints(obj)}

        # .blend opened without the add-on: the RNA collection may have been
        # lost while the custom property is still there.
        if not len(settings.slots) and utils.read_slots_id_prop(obj):
            utils.load_slots_id_prop(obj)

        if not len(settings.slots) and self.create_default_slots:
            armature = self._guess_armature(context, obj) if self.auto_target else None
            for name, label, candidates in self.DEFAULT_SLOTS:
                slot = settings.slots.add()
                slot.name = name
                slot.stored_name = name
                slot.label = label
                if armature is not None:
                    slot.target = armature
                    slot.subtarget = self._guess_bone(armature, candidates)

        if not len(settings.slots):
            self.report({'WARNING'}, "No slot defined: add at least one slot.")
            return {'CANCELLED'}

        # The slot callbacks may already have created constraints: count the
        # overall result, not just this last call.
        utils.ensure_constraints(obj)
        utils.store_slots_id_prop(obj)
        created = sum(1 for constraint in utils.iter_ph_constraints(obj)
                      if constraint.name not in existing)
        self.report({'INFO'}, "%s configured: %d slot(s), %d constraint(s) created."
                    % (obj.name, len(settings.slots), created))
        return {'FINISHED'}


class PROPHANDOFF_OT_clear_setup(_PropPoll, bpy.types.Operator):
    """Remove the PropHandoff constraints and their influence keys"""

    bl_idname = "prophandoff.clear_setup"
    bl_label = "Clear Setup"
    bl_options = {'REGISTER', 'UNDO'}

    keep_visual_transform: BoolProperty(
        name="Keep visual position",
        description="Freeze the current position before removing the constraints",
        default=True,
    )
    remove_slots: BoolProperty(
        name="Also remove the slots",
        description="Empty the slot list and the ph_slots custom property",
        default=False,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}

        world = utils.evaluated_matrix_world(context.evaluated_depsgraph_get(), obj)
        for constraint in list(utils.iter_ph_constraints(obj)):
            utils.remove_slot_data(obj, utils.slot_name_from_constraint(constraint))

        if self.keep_visual_transform:
            utils.set_world_matrix(obj, world)
            # Animated object: without a key, `frame_set` would overwrite the
            # matrix with the old keys (expressed in the bone's space).
            if len(utils.iter_fcurves(obj)):
                utils.insert_transform_keys(obj, context.scene.frame_current)

        if self.remove_slots:
            settings = utils.get_obj_settings(obj)
            settings.slots.clear()
            settings.active_slot_index = 0
            for key in (utils.ID_PROP_SLOTS, utils.ID_PROP_VERSION):
                if key in obj.keys():
                    del obj[key]
        else:
            utils.store_slots_id_prop(obj)

        utils.tag_update(context, obj)
        self.report({'INFO'}, "PropHandoff setup removed from %s." % obj.name)
        return {'FINISHED'}


class PROPHANDOFF_OT_slot_add(_PropPoll, bpy.types.Operator):
    """Add a slot (taken from the active bone if an armature is in Pose mode)"""

    bl_idname = "prophandoff.slot_add"
    bl_label = "Add Slot"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        settings = utils.get_obj_settings(obj)

        armature = context.active_object
        bone_name = ""
        if armature is not None and armature.type == 'ARMATURE' and armature is not obj:
            active_bone = armature.data.bones.active
            if active_bone is not None:
                bone_name = active_bone.name

        slot = settings.slots.add()
        slot.name = utils.unique_slot_name(
            settings.slots, utils.sanitize_slot_name(bone_name or "Slot"), exclude=slot)
        slot.stored_name = slot.name
        slot.label = slot.name
        if bone_name:
            slot.target = armature
            slot.subtarget = bone_name

        settings.active_slot_index = len(settings.slots) - 1
        utils.ensure_constraints(obj)
        utils.store_slots_id_prop(obj)
        self.report({'INFO'}, "Slot \"%s\" added." % slot.name)
        return {'FINISHED'}


class PROPHANDOFF_OT_slot_remove(_PropPoll, bpy.types.Operator):
    """Remove a slot, its constraint and its influence keys"""

    bl_idname = "prophandoff.slot_remove"
    bl_label = "Remove Slot"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(name="Index", default=-1, options={'HIDDEN'})

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        settings = utils.get_obj_settings(obj)
        index = self.index if self.index >= 0 else settings.active_slot_index
        if not (0 <= index < len(settings.slots)):
            self.report({'ERROR'}, "No slot to remove.")
            return {'CANCELLED'}

        slot_name = settings.slots[index].name
        utils.remove_slot_data(obj, slot_name)
        settings.slots.remove(index)
        settings.active_slot_index = max(0, min(index, len(settings.slots) - 1))
        utils.store_slots_id_prop(obj)
        self.report({'INFO'}, "Slot \"%s\" removed." % slot_name)
        return {'FINISHED'}


class PROPHANDOFF_OT_slot_move(_PropPoll, bpy.types.Operator):
    """Move the active slot in the list"""

    bl_idname = "prophandoff.slot_move"
    bl_label = "Move Slot"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        name="Direction",
        items=[('UP', "Up", "Move up"), ('DOWN', "Down", "Move down")],
        default='UP',
    )

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        settings = utils.get_obj_settings(obj)
        index = settings.active_slot_index
        target = index - 1 if self.direction == 'UP' else index + 1
        if not (0 <= index < len(settings.slots)) or not (0 <= target < len(settings.slots)):
            return {'CANCELLED'}
        settings.slots.move(index, target)
        settings.active_slot_index = target
        utils.store_slots_id_prop(obj)
        return {'FINISHED'}


class PROPHANDOFF_OT_slot_from_selection(_PropPoll, bpy.types.Operator):
    """Fill the active slot's target from the selection (active bone or active object)"""

    bl_idname = "prophandoff.slot_from_selection"
    bl_label = "From Selection"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        settings = utils.get_obj_settings(obj)
        if not (0 <= settings.active_slot_index < len(settings.slots)):
            self.report({'ERROR'}, "No active slot.")
            return {'CANCELLED'}

        active = context.active_object
        if active is None or active is obj:
            self.report({'ERROR'}, "Select an armature (active bone) or a target object.")
            return {'CANCELLED'}

        slot = settings.slots[settings.active_slot_index]
        slot.target = active
        if active.type == 'ARMATURE':
            active_bone = active.data.bones.active
            slot.subtarget = active_bone.name if active_bone is not None else ""
        else:
            slot.subtarget = ""

        utils.ensure_constraints(obj)
        utils.store_slots_id_prop(obj)
        self.report({'INFO'}, "Target of slot \"%s\" updated." % slot.name)
        return {'FINISHED'}


class PROPHANDOFF_OT_use_active_object(bpy.types.Operator):
    """Pin the active object as the current prop (eyedropper)"""

    bl_idname = "prophandoff.use_active_object"
    bl_label = "Use Active Object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.active_object is not None
                and utils.get_scene_settings(context.scene) is not None)

    def execute(self, context):
        utils.get_scene_settings(context.scene).prop_object = context.active_object
        return {'FINISHED'}


class PROPHANDOFF_OT_pick_prop(bpy.types.Operator):
    """Make this object the active prop (tabs of the panels, any mode)"""

    bl_idname = "prophandoff.pick_prop"
    bl_label = "Pick Prop"
    bl_options = {'REGISTER', 'UNDO'}

    object_name: StringProperty(name="Object", default="", options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return utils.get_scene_settings(context.scene) is not None

    def execute(self, context):
        obj = context.scene.objects.get(self.object_name)
        if obj is None:
            self.report({'ERROR'}, "Object \"%s\" not found in the scene." % self.object_name)
            return {'CANCELLED'}
        utils.get_scene_settings(context.scene).prop_object = obj
        return {'FINISHED'}


class PROPHANDOFF_OT_reload_slots(_PropPoll, bpy.types.Operator):
    """Reload the slot list from the ph_slots custom property"""

    bl_idname = "prophandoff.reload_slots"
    bl_label = "Reload from ph_slots"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        count = utils.load_slots_id_prop(obj)
        if not count:
            self.report({'WARNING'}, "No usable data in ph_slots.")
            return {'CANCELLED'}
        self.report({'INFO'}, "%d slot(s) reloaded." % count)
        return {'FINISHED'}


# ===========================================================================
#  4. Animation (section 1)
# ===========================================================================

class PROPHANDOFF_OT_assign(_PropPoll, bpy.types.Operator):
    """Assign the prop to a slot at the current frame, without moving it"""

    bl_idname = "prophandoff.assign"
    bl_label = "Assign to Slot"
    bl_options = {'REGISTER', 'UNDO'}

    slot_name: StringProperty(name="Slot", default="")

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}

        slot_name = self.slot_name
        if not slot_name:
            settings = utils.get_obj_settings(obj)
            if not (0 <= settings.active_slot_index < len(settings.slots)):
                self.report({'ERROR'}, "No active slot.")
                return {'CANCELLED'}
            slot_name = settings.slots[settings.active_slot_index].name

        success, message = apply_handoff(context, obj, slot_name)
        if not success:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        notes = sync_master_reach(context, obj, context.scene.frame_current)
        utils.tag_update(context, obj)
        self.report({'INFO'}, " ".join([message] + notes))
        return {'FINISHED'}


class PROPHANDOFF_OT_release(_PropPoll, bpy.types.Operator):
    """Release the prop: it stays in world space at its current position"""

    bl_idname = "prophandoff.release"
    bl_label = "Release"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        success, message = apply_handoff(context, obj, None)
        if not success:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        notes = sync_master_reach(context, obj, context.scene.frame_current)
        utils.tag_update(context, obj)
        self.report({'INFO'}, " ".join([message] + notes))
        return {'FINISHED'}


class PROPHANDOFF_OT_throw(_PropPoll, bpy.types.Operator):
    """Release the prop and generate a ballistic trajectory over N frames"""

    bl_idname = "prophandoff.throw"
    bl_label = "Throw"
    bl_options = {'REGISTER', 'UNDO'}

    # Mirror of PH_SceneSettings: `invoke()` copies the panel settings, which
    # makes the F9 panel fully functional.
    flight_frames: IntProperty(name="Flight frames", default=24, min=1, max=1000)
    velocity_source: EnumProperty(
        name="Velocity",
        items=[
            ('MEASURED', "From animation", "Measure the real motion before the release"),
            ('MANUAL', "Direction + speed", "Use the direction and speed entered"),
        ],
        default='MEASURED',
    )
    velocity_samples: IntProperty(name="Sample frames", default=2, min=1, max=20)
    speed_scale: FloatProperty(name="Multiplier", default=1.0, min=0.0, soft_max=5.0)
    direction: FloatVectorProperty(name="Direction", default=(0.0, 1.0, 0.5),
                                   subtype='XYZ', size=3)
    speed: FloatProperty(name="Speed", default=5.0, min=0.0, soft_max=50.0)
    gravity: FloatProperty(name="Gravity", default=-9.81, soft_min=-30.0, soft_max=0.0)
    spin: FloatVectorProperty(name="Spin", default=(0.0, 0.0, 0.0),
                              subtype='EULER', size=3)

    def invoke(self, context, event):
        settings = utils.get_scene_settings(context.scene)
        if settings is not None:
            self.flight_frames = settings.flight_frames
            self.velocity_source = settings.velocity_source
            self.velocity_samples = settings.velocity_samples
            self.speed_scale = settings.speed_scale
            self.direction = settings.direction
            self.speed = settings.speed
            self.gravity = settings.gravity
            self.spin = settings.spin
        return self.execute(context)

    def _initial_velocity(self, context, obj, frame):
        """Initial world velocity (Blender units / second)."""
        if self.velocity_source == 'MEASURED':
            return utils.measure_velocity(context, obj, frame, self.velocity_samples) * self.speed_scale
        direction = Vector(self.direction)
        if direction.length < 1e-6:
            direction = Vector((0.0, 0.0, 1.0))
        return direction.normalized() * self.speed

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        scene = context.scene
        frame = scene.frame_current

        # The measurement moves the playhead: BEFORE any key is written.
        velocity = self._initial_velocity(context, obj, frame)
        world = utils.evaluated_matrix_world(context.evaluated_depsgraph_get(), obj)

        success, message = apply_handoff(context, obj, None)
        if not success:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        sync_master_reach(context, obj, frame)

        # p(t) = p0 + v·t + ½·g·t², sampled at every frame.
        fps = utils.scene_fps(scene)
        gravity = Vector((0.0, 0.0, utils.gravity_in_blender_units(scene, self.gravity)))
        origin = world.translation.copy()
        basis = world.to_3x3()              # rotation + scale at the release
        spin = Vector(self.spin)            # radians/s (EULER subtype)
        spinning = spin.length > 1e-9

        for step in range(1, self.flight_frames + 1):
            time = step / fps
            position = origin + velocity * time + gravity * (0.5 * time * time)
            rotation = basis
            if spinning:
                rotation = Euler((spin.x * time, spin.y * time, spin.z * time), 'XYZ').to_matrix() @ basis
            obj.matrix_world = Matrix.Translation(position) @ rotation.to_4x4()
            # LINEAR: the parabola is already sampled, a bezier would only
            # overshoot between two frames.
            utils.insert_transform_keys(obj, frame + step, interpolation='LINEAR')

        utils.tag_update(context, obj)
        self.report({'INFO'}, "%s — %d-frame flight (v0 = %.2f u/s)."
                    % (message, self.flight_frames, velocity.length))
        return {'FINISHED'}


class PROPHANDOFF_OT_bake_visual_transform(_PropPoll, bpy.types.Operator):
    """Freeze the current visual transform (visual_transform_apply) and insert
    LocRotScale keys, without touching the constraints"""

    bl_idname = "prophandoff.bake_visual_transform"
    bl_label = "Bake Visual Transform"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = utils.get_prop_object(context)
        return obj is not None and obj.library is None and context.mode == 'OBJECT'

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        frame = context.scene.frame_current
        if not utils.visual_transform_apply_native(context, [obj]):
            self.report({'ERROR'}, "visual_transform_apply failed (Object mode required).")
            return {'CANCELLED'}
        utils.insert_transform_keys(obj, frame)
        utils.tag_update(context, obj)
        self.report({'INFO'}, "Visual transform baked at frame %d." % frame)
        return {'FINISHED'}


# ===========================================================================
#  5. Two-Hand Grip
# ===========================================================================

def _link_next_to(context, prop, new_object):
    """Link a new object into the prop's collections (fallback: the scene)."""
    collections = list(prop.users_collection) or [context.scene.collection]
    for collection in collections:
        collection.objects.link(new_object)


def _place_grip_empty(prop, empty, matrix_local):
    """"Clean" parenting: identity parent_inverse, the empty's basis **is** its
    local matrix relative to the prop (the one of `ph_grip_points`)."""
    empty.parent = prop
    empty.parent_type = 'OBJECT'
    empty.matrix_parent_inverse = Matrix.Identity(4)
    empty.matrix_basis = matrix_local


def _create_grip_empty(context, prop, side, matrix_local):
    """Grip point: ARROWS empty, direct child of the prop."""
    empty = bpy.data.objects.new(utils.grip_empty_basename(side), None)
    _link_next_to(context, prop, empty)
    empty.empty_display_type = 'ARROWS'
    empty.empty_display_size = 0.1
    empty.show_in_front = True
    _place_grip_empty(prop, empty, matrix_local)
    return empty


def _default_grip_matrix(prop, sign, fallback_offset):
    """Default grip **along the object**: on the longest local axis of the
    bounding box, at ±25 % of its length from the center (a spear modelled
    along Y or Z gets its grips on the shaft, not perpendicular to it)."""
    try:
        corners = [Vector(corner) for corner in prop.bound_box]
    except (AttributeError, TypeError):
        corners = []
    if len(corners) == 8:
        low = Vector((min(c.x for c in corners), min(c.y for c in corners),
                      min(c.z for c in corners)))
        high = Vector((max(c.x for c in corners), max(c.y for c in corners),
                       max(c.z for c in corners)))
        extents = high - low
        axis = max(range(3), key=lambda index: extents[index])
        if extents[axis] > 1e-6:
            direction = Vector((0.0, 0.0, 0.0))
            direction[axis] = 1.0
            center = (low + high) * 0.5
            return Matrix.Translation(center + direction * (sign * 0.25 * extents[axis]))
    return Matrix.Translation((sign * fallback_offset, 0.0, 0.0))


def _new_helper_empty(context, prop, name):
    """Helper empty: tiny, not selectable, never rendered — but never
    `hide_viewport` (an object excluded from the view may stop being
    evaluated, and the driver would read a frozen value)."""
    helper = bpy.data.objects.new(name, None)
    _link_next_to(context, prop, helper)
    helper.empty_display_type = 'PLAIN_AXES'
    helper.empty_display_size = 0.02
    helper.hide_select = True
    helper.hide_render = True
    return helper


def _reset_basis(obj):
    obj.rotation_mode = 'XYZ'
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)


def build_helpers(context, prop, side):
    """Create/update the two helper empties of the Grip Zone.

    * **frame** (`PH_Frame_<S>`): its world frame is the one in which the
      `location` channels of the IK target are expressed. It only depends on
      the bone's *parent*, never on the bone itself.
    * **grip local** (`PH_GripLocal_<S>`): child of the frame, Copy Location
      towards the grip point. Its local position (constraints included) is
      therefore `frame⁻¹ @ grip_world`, directly comparable to the bone's
      channels.

    For a bone B with parent P: `pose(B) = pose(P) @ rest(P)⁻¹ @ rest(B) @ T(loc)`,
    so `F = arm @ pose(P) @ rest(P)⁻¹ @ rest(B)`. A bone-parented object is
    placed at the **tail** of the bone: `world = arm @ pose(P) @ T(0, len_P, 0)
    @ parent_inverse @ basis`; with `parent_inverse = T(0, −len_P, 0) @
    rest(P)⁻¹ @ rest(B)` and an identity basis, the frame is exactly F.
    Without parent: `parent_type='OBJECT'`, `parent_inverse = rest(B)`.

    Bones without "Local Location" (`use_local_location` off — Rigify's IK
    controllers, which move in their parent's axes): Blender then applies
    `loc` at B's head but in P's orientation, not B's
    (`BKE_bone_parent_transform_calc_from_matrices`). The frame is the same
    up to B's rest rotation: only the translation of `rest(P)⁻¹ @ rest(B)` is
    kept (without parent: the translation of `rest(B)`). The other
    inheritance options (rotation, scale) do not change the location frame.
    Measured: zero error on `hand_ik.L`.

    Known limit: the controller's own constraints (Auto-Rig Pro IK
    space-switch Child Of…) are not reproduced.

    Returns ``(frame, grip_local)`` or None if the side is invalid.
    """
    ik_object, ik_bone = utils.get_ik_handle(prop, side)
    if utils.resolve_ik_owner(ik_object, ik_bone) is None:
        return None
    grip = utils.find_grip_empty(prop, side)
    if grip is None:
        return None

    # --- frame: the channel frame ------------------------------------------
    frame = utils.find_helper(prop, utils.ID_PROP_FRAME[side])
    if frame is None:
        frame = _new_helper_empty(context, prop, utils.frame_helper_basename(side))
        prop[utils.ID_PROP_FRAME[side]] = frame.name
    _reset_basis(frame)

    if ik_bone:
        bone = ik_object.data.bones[ik_bone]
        frame.parent = ik_object
        if bone.parent is not None:
            parent = bone.parent
            rest_offset = parent.matrix_local.inverted_safe() @ bone.matrix_local
            if not bone.use_local_location:
                rest_offset = Matrix.Translation(rest_offset.translation)
            frame.parent_type = 'BONE'
            frame.parent_bone = parent.name
            frame.matrix_parent_inverse = (
                Matrix.Translation((0.0, -parent.length, 0.0)) @ rest_offset)
        else:
            frame.parent_type = 'OBJECT'
            frame.parent_bone = ""
            frame.matrix_parent_inverse = (
                bone.matrix_local.copy() if bone.use_local_location
                else Matrix.Translation(bone.matrix_local.translation))
    else:
        # Object IK target: same parent, same type, same inverse as it.
        frame.parent = ik_object.parent
        frame.parent_type = ik_object.parent_type if ik_object.parent else 'OBJECT'
        frame.parent_bone = ik_object.parent_bone if ik_object.parent else ""
        frame.matrix_parent_inverse = ik_object.matrix_parent_inverse.copy()

    # --- grip local: the grip point seen from that frame -------------------
    grip_local = utils.find_helper(prop, utils.ID_PROP_GRIP_LOCAL[side])
    if grip_local is None:
        grip_local = _new_helper_empty(context, prop, utils.grip_local_helper_basename(side))
        prop[utils.ID_PROP_GRIP_LOCAL[side]] = grip_local.name
    grip_local.parent = frame
    grip_local.parent_type = 'OBJECT'
    grip_local.parent_bone = ""
    grip_local.matrix_parent_inverse = Matrix.Identity(4)
    _reset_basis(grip_local)

    follow = grip_local.constraints.get(utils.FOLLOW_GRIP_CONSTRAINT)
    if follow is None:
        follow = grip_local.constraints.new('COPY_LOCATION')
        follow.name = utils.FOLLOW_GRIP_CONSTRAINT
    follow.target = grip
    follow.subtarget = ""
    follow.use_offset = False
    follow.target_space = 'WORLD'
    follow.owner_space = 'WORLD'
    follow.influence = 1.0
    utils.set_constraint_enabled(follow, True)

    return frame, grip_local


def _validate_side(prop, side):
    """Error message if a side is unusable (invalid IK target or missing grip
    point), else None."""
    error = utils.ik_handle_error(prop, side)
    if error:
        return error
    if utils.find_grip_empty(prop, side) is None:
        return ("Missing grip point for the %s: run \"Setup Two-Hand Grip\"."
                % utils.side_label(side))
    return None


def _find_or_create_ik_slot(prop, side):
    """Section 1 slot targeting exactly one side's IK handle.

    Reused if it exists (`Hand_R` → `c_hand_ik.r`), else created.
    """
    settings = utils.get_obj_settings(prop)
    ik_object, ik_bone = utils.get_ik_handle(prop, side)
    for slot in settings.slots:
        if slot.target is ik_object and (slot.subtarget or "") == (ik_bone or ""):
            return slot.name

    slot = settings.slots.add()
    slot.name = utils.unique_slot_name(settings.slots, "Hand_" + utils.side_suffix(side),
                                       exclude=slot)
    slot.stored_name = slot.name
    slot.label = utils.side_label(side).capitalize()
    slot.target = ik_object
    slot.subtarget = ik_bone
    utils.ensure_constraints(prop)
    utils.store_slots_id_prop(prop)
    return slot.name


def attach_hand(context, prop, side, frame):
    """Attach a hand to its grip point at the given frame, without a jump.

    Same mechanics as `apply_handoff()` applied to the IK target (object or
    pose bone). Returns ``(success, message)``.
    """
    error = _validate_side(prop, side)
    if error:
        return False, error

    ik_object, ik_bone = utils.get_ik_handle(prop, side)
    owner = utils.resolve_ik_owner(ik_object, ik_bone)
    grip = utils.find_grip_empty(prop, side)

    # 0. the prop must no longer follow this hand (anti-cycle): the dormant
    #    slots targeting it are baked to keys then removed.
    success, message, notes = release_prop_from_hand(context, prop, ik_object, ik_bone)
    if not success:
        return False, message
    depsgraph = context.evaluated_depsgraph_get()

    # 1. current visual transform of the hand (blend included)
    world = utils.owner_world_matrix(depsgraph, owner)
    if world is None:
        return False, "Cannot read the position of the %s." % utils.side_label(side)

    # 2. the Grip Zone must not compete with the attach. Its influence is
    #    driven (not keyframable): its effect is baked to keys up to
    #    frame - 1 then the constraint is removed — muting would lose the
    #    whole past where the zone (or the Lock) held the hand on the prop.
    note = bake_and_remove_grip_blend(context, prop, side, frame - 1)
    if note:
        notes.append(note)
    depsgraph = context.evaluated_depsgraph_get()

    # 3. previous state frozen at frame - 1
    attaches = list(utils.iter_prefixed_constraints(owner, utils.PH_TWOHAND_PREFIX))
    utils.hold_transform(owner, frame - 1)
    for constraint in attaches:
        utils.hold_influence(constraint, frame - 1)

    # 4. every attach to zero at the frame
    for constraint in attaches:
        utils.insert_influence_key(constraint, 0.0, frame)

    # 5. the requested side's constraint
    name = utils.twohand_constraint_name(side)
    constraint = utils.get_constraint(owner, name)
    if constraint is None:
        constraint = owner.constraints.new('CHILD_OF')
        constraint.name = name
        constraint.influence = 0.0                  # a new constraint is born at 1
        constraint.inverse_matrix = Matrix.Identity(4)
        # Without this key, a single-key F-Curve would be 1 everywhere before
        # the attach frame.
        utils.insert_influence_key(constraint, 0.0, frame - 1)
    constraint.target = grip
    constraint.subtarget = ""
    if hasattr(constraint, "set_inverse_pending"):
        constraint.set_inverse_pending = False
    utils.set_constraint_enabled(constraint, True)
    utils.insert_influence_key(constraint, 1.0, frame)

    # 6. compensation: the hand does not move
    constraint_matrix = utils.constraint_parent_matrix(depsgraph, constraint)
    if constraint_matrix is None:
        return False, "Unreadable grip point of the %s." % utils.side_label(side)
    settle_owner(context, owner, world, constraint_matrix, frame)

    message = "%s attached at frame %d." % (utils.side_label(side).capitalize(), frame)
    if notes:
        message += " " + " ".join(notes)
    return True, message


def release_hand(context, prop, side, frame, keep_grip_zone=False):
    """Free a hand: back to normal IK control, without a jump.

    Covers the attach (influence keyed to 0) **and** the Grip Zone (cut, or
    removed if the prop follows this hand). Fails only if neither is active.
    The Grip Zone is never re-enabled here: a hand still inside the sphere
    would be re-grabbed in the same frame. Returns ``(success, message)``.
    """
    owner = utils.ik_handle_owner(prop, side)
    if owner is None:
        return False, utils.ik_handle_error(prop, side) or (
            "IK target of the %s is not set." % utils.side_label(side))

    attaches = list(utils.iter_prefixed_constraints(owner, utils.PH_TWOHAND_PREFIX))
    blends = list(utils.iter_prefixed_constraints(owner, utils.PH_GRIPBLEND_PREFIX))
    if not attaches and not any(utils.is_constraint_enabled(blend) for blend in blends):
        return False, "The %s does not follow the prop." % utils.side_label(side)

    depsgraph = context.evaluated_depsgraph_get()
    world = utils.owner_world_matrix(depsgraph, owner)
    if world is None:
        return False, "Cannot read the position of the %s." % utils.side_label(side)

    # Attaches: hold at frame - 1, then 0 at the frame.
    utils.hold_transform(owner, frame - 1)
    for constraint in attaches:
        utils.hold_influence(constraint, frame - 1)
    for constraint in attaches:
        utils.insert_influence_key(constraint, 0.0, frame)

    # Grip Zone: its effect is baked to keys up to frame - 1 then the
    # constraint is removed (muting would keep its relations and lose the
    # past). `keep_grip_zone` leaves it intact, unless the prop follows this
    # hand (cycle).
    ik_object, ik_bone = utils.get_ik_handle(prop, side)
    note = None
    if blends and (not keep_grip_zone or utils.prop_held_by_handle(prop, ik_object, ik_bone)):
        note = bake_and_remove_grip_blend(context, prop, side, frame - 1)

    # The hand stays where it was.
    settle_owner(context, owner, world, None, frame)

    message = "%s released at frame %d." % (utils.side_label(side).capitalize(), frame)
    if note:
        message += " " + note
    return True, message


def _remove_grip_blends(prop):
    """Remove the Grip Zone of both hands. Returns the number of sides cleaned."""
    return sum(1 for side in utils.SIDES if utils.remove_grip_blend(prop, side))


def enable_grip_zone_side(context, prop, side):
    """Create (or rebuild) one hand's Grip Zone. Raises RuntimeError.

    Refused for the hand the prop follows (cycle). A dormant slot targeting
    this hand would keep its dependency relation: baked to keys then removed.
    """
    if utils.holding_side(prop) == side:
        raise RuntimeError("the %s carries the prop (Master Hand) and cannot follow it"
                           % utils.side_label(side))
    error = _validate_side(prop, side)
    if error:
        raise RuntimeError(error)
    ik_object, ik_bone = utils.get_ik_handle(prop, side)
    owner = utils.resolve_ik_owner(ik_object, ik_bone)
    grip = utils.find_grip_empty(prop, side)

    success, message, notes = release_prop_from_hand(context, prop, ik_object, ik_bone)
    if not success:
        raise RuntimeError(message)
    helpers = build_helpers(context, prop, side)
    if helpers is None:
        raise RuntimeError("helper empties cannot be built")
    frame, grip_local = helpers

    name = utils.gripblend_constraint_name(side)
    constraint = utils.get_constraint(owner, name)
    if constraint is None:
        constraint = owner.constraints.new('COPY_TRANSFORMS')
        constraint.name = name
        constraint.influence = 0.0      # a new constraint is born at 1
    constraint.target = grip
    constraint.subtarget = ""
    utils.set_constraint_enabled(constraint, True)
    utils.add_grip_driver(constraint, ik_object, ik_bone, frame, grip_local, prop)
    return notes


def enable_grip_zone(context, prop):
    """Enable the Grip Zone on both hands (except the master hand) and store
    the intention (`ph_grip_zone_wanted`).

    Returns ``(success, message, level)`` where `level` is 'INFO', 'WARNING'
    or 'ERROR'. No half-done state: on failure both sides are cleaned.
    """
    errors = [m for m in (_validate_side(prop, side) for side in utils.SIDES) if m]
    if errors:
        return False, errors[0], 'ERROR'

    utils.sync_twohand_id_props(prop)
    holding = utils.holding_side(prop)      # anti-cycle: the master hand is skipped
    notes = []
    for side in utils.SIDES:
        if side == holding:
            continue
        try:
            notes += enable_grip_zone_side(context, prop, side)
        except (RuntimeError, KeyError, AttributeError, TypeError) as error:
            _remove_grip_blends(prop)           # no half-done state
            return False, "Grip Zone impossible on the %s: %s" % (utils.side_label(side), error), 'ERROR'
    utils.set_grip_zone_wanted(prop, True)

    suffix = (" " + " ".join(notes)) if notes else ""
    if holding is not None:
        return True, ("Grip Zone enabled on the %s only: the %s carries the prop "
                      "(Master Hand) and cannot follow it at the same time.%s"
                      % (utils.side_label(utils.other_side(holding)),
                         utils.side_label(holding), suffix)), 'WARNING'
    if tuple(round(value, 4) for value in prop.scale) != (1.0, 1.0, 1.0):
        # Copy Transforms also copies the grip point's scale.
        return True, ("Grip Zone enabled, but the prop has a non-unit scale: "
                      "apply it (Ctrl+A ▸ Scale) or the hand will be deformed.%s" % suffix), 'WARNING'
    return True, "Grip Zone enabled (radius %.2f).%s" % (utils.grip_zone_radius(prop), suffix), 'INFO'


def restore_grip_zone(context, prop, sides):
    """Restore the Grip Zone (if wanted) on the hands that do not already
    follow the prop. Returns the notes."""
    notes = []
    if not utils.grip_zone_wanted(prop):
        return notes
    for side in sides:
        if utils.hand_follows_prop(prop, side) or utils.holding_side(prop) == side:
            continue
        if _validate_side(prop, side) is not None:
            continue
        try:
            notes += enable_grip_zone_side(context, prop, side)
            notes.append("Grip Zone restored on the %s." % utils.side_label(side))
        except (RuntimeError, KeyError, AttributeError, TypeError) as error:
            notes.append("Grip Zone not restored on the %s: %s." % (utils.side_label(side), error))
    return notes


# ---------------------------------------------------------------------------
#  Lock: arm reach
# ---------------------------------------------------------------------------
#
#  Under Lock the hands stay on the grips whatever the distance (Grip Zone
#  driver: max(lock, proximity)). So that the prop stops when the arms are
#  extended, the prop carries a Limit Location `PH_Reach` (world space) whose
#  min = max bounds are driven:
#
#      position = prop channels − correction × (lock × gate)
#
#  Measured in Blender 5.2.1: a driver on `delta_location` reads back its own
#  output (Transform Space = loc + delta), hence inconsistent values; a
#  constraint, on the other hand, is not read back by the drivers of the same
#  object. The whole computation chain lives on the prop (driven custom
#  properties): an object can only read its own channels without a cycle from
#  its own drivers ("same ID" exemption). The only external inputs are the
#  shoulder helpers `PH_ReachHelper_*`, bone-parented to the parent bone of
#  the IK chain (the shoulder does not depend on the IK target).
#
#  Per hand: R·(S·g) (grip rotated by the prop rotation, read as Euler XYZ
#  whatever the mode), distance d = |P + R·(S·g) − shoulder|, factor
#  f = max(0, d − reach) / d; the correction kept is the one of the hand that
#  is furthest beyond its reach. min/max/sqrt/sin/cos functions and the
#  conditional only (simple evaluator, no autoexec).
# ---------------------------------------------------------------------------

REACH_AXES = ("x", "y", "z")
_ROTATED_OFFSET_EXPRESSIONS = (
    "cos(ry)*cos(rz)*({gx}*kx) + (sin(rx)*sin(ry)*cos(rz) - cos(rx)*sin(rz))*({gy}*ky)"
    " + (cos(rx)*sin(ry)*cos(rz) + sin(rx)*sin(rz))*({gz}*kz)",
    "cos(ry)*sin(rz)*({gx}*kx) + (sin(rx)*sin(ry)*sin(rz) + cos(rx)*cos(rz))*({gy}*ky)"
    " + (cos(rx)*sin(ry)*sin(rz) - sin(rx)*cos(rz))*({gz}*kz)",
    "-sin(ry)*({gx}*kx) + sin(rx)*cos(ry)*({gy}*ky) + cos(rx)*cos(ry)*({gz}*kz)",
)


def _own_transform_variables(driver, prop, location=True, rotation=False, scale=False):
    """Transform Channel variables of the prop on its own channels ("same ID"
    exemption: no relation, channel values without constraints)."""
    if location:
        for axis, kind in zip(REACH_AXES, ('LOC_X', 'LOC_Y', 'LOC_Z')):
            utils.driver_var_transform(driver, "p" + axis, prop, kind, 'TRANSFORM_SPACE')
    if rotation:
        for axis, kind in zip(REACH_AXES, ('ROT_X', 'ROT_Y', 'ROT_Z')):
            utils.driver_var_transform(driver, "r" + axis, prop, kind, 'TRANSFORM_SPACE',
                                       rotation_mode='XYZ')
    if scale:
        for axis, kind in zip(REACH_AXES, ('SCALE_X', 'SCALE_Y', 'SCALE_Z')):
            utils.driver_var_transform(driver, "k" + axis, prop, kind, 'TRANSFORM_SPACE')


def _set_prop_driver(prop, key, expression):
    """Scripted driver on a custom property of the prop. Returns the driver."""
    if key not in prop.keys():
        prop[key] = 0.0
    if len(expression) > 255:
        raise RuntimeError("driver expression too long (%d)" % len(expression))
    _fcurve, driver = utils.new_scripted_driver(prop, utils.id_prop_path(key))
    driver.expression = expression
    return driver


def _build_reach_side_drivers(prop, side):
    """Rotated grip offset of one side: `ph_rg_<S>x/y/z` = R·(S·g), with the
    grip's local offset g baked as constants of the expressions."""
    grip = utils.find_grip_empty(prop, side)
    if grip is None or utils.find_helper(prop, utils.ID_PROP_REACH_HELPER[side]) is None:
        raise RuntimeError("missing grip point or reach helper (%s)" % utils.side_label(side))
    offset = grip.matrix_basis.translation
    constants = {axis: "%.6f" % value for axis, value in zip(("gx", "gy", "gz"), offset)}
    for key, template in zip(utils.REACH_CHAIN_PROPS[side], _ROTATED_OFFSET_EXPRESSIONS):
        driver = _set_prop_driver(prop, key, template.format(**constants))
        _own_transform_variables(driver, prop, location=False, rotation=True, scale=True)


def _reach_pass_inputs(driver, prop, side, previous):
    """Variables of a projection pass: prop channels `p*`, rotated grip
    offset `a*`, shoulder helper `h*` (world) and the previous translation
    `t*` (none for the first pass)."""
    _own_transform_variables(driver, prop)
    helper = utils.find_helper(prop, utils.ID_PROP_REACH_HELPER[side])
    for axis, key, kind in zip(REACH_AXES, utils.REACH_CHAIN_PROPS[side], ('LOC_X', 'LOC_Y', 'LOC_Z')):
        utils.driver_var_prop(driver, "a" + axis, prop, utils.id_prop_path(key))
        utils.driver_var_transform(driver, "h" + axis, helper, kind, 'WORLD_SPACE')
        if previous is not None:
            utils.driver_var_prop(driver, "t" + axis, prop, utils.id_prop_path(previous[axis]))


def _build_reach_pass_drivers(prop, sides):
    """Translation that brings every grip back within reach, computed by
    alternating projections onto the reach spheres (`REACH_ROUNDS` rounds,
    one pass per side each). Each pass projects the translation of the
    previous pass onto one sphere: continuous in the prop's channels, so the
    stop never jumps — unlike picking the most extended hand, which switches
    abruptly when both hands are out of reach in different directions.

    Pass k (side S, previous translation t):
        d = |P + a_S + t − h_S|
        f = max(0, d − reach_S) / d
        t' = t − (P + a_S + t − h_S) · f
    The result `ph_rt_*` is the translation of the last pass. With a single
    side the first pass is already exact.
    """
    previous = None
    index = 0
    rounds = utils.REACH_ROUNDS if len(sides) > 1 else 1
    for _round in range(rounds):
        for side in sides:
            index += 1
            key_d, key_f, key_x, key_y, key_z = utils.reach_pass_props(index)
            axes = {"x": key_x, "y": key_y, "z": key_z}
            offsets = {axis: "(p%s+a%s-h%s)" % (axis, axis, axis) if previous is None
                       else "(p%s+a%s+t%s-h%s)" % (axis, axis, axis, axis) for axis in REACH_AXES}
            driver = _set_prop_driver(prop, key_d, "sqrt(%s*%s + %s*%s + %s*%s)" % (
                offsets["x"], offsets["x"], offsets["y"], offsets["y"], offsets["z"], offsets["z"]))
            _reach_pass_inputs(driver, prop, side, previous)
            driver = _set_prop_driver(prop, key_f, "max(0.0, d - reach) / max(d, 0.000001)")
            utils.driver_var_prop(driver, "d", prop, utils.id_prop_path(key_d))
            utils.driver_var_prop(driver, "reach", prop, utils.id_prop_path(utils.ID_PROP_REACH[side]))
            for axis in REACH_AXES:
                expression = ("-%s*f" % offsets[axis]) if previous is None else ("t%s - %s*f" % (axis, offsets[axis]))
                driver = _set_prop_driver(prop, axes[axis], expression)
                _reach_pass_inputs(driver, prop, side, previous)
                utils.driver_var_prop(driver, "f", prop, utils.id_prop_path(key_f))
            previous = axes
    for axis, key in zip(REACH_AXES, utils.REACH_TRANSLATION_PROPS):
        driver = _set_prop_driver(prop, key, "t")
        utils.driver_var_prop(driver, "t", prop, utils.id_prop_path(previous[axis]))


def _build_reach_constraint(prop):
    """Limit Location `PH_Reach`: min = max bounds = channels + translation,
    influence = lock × gate."""
    constraint = utils.reach_constraint(prop)
    if constraint is None:
        constraint = prop.constraints.new('LIMIT_LOCATION')
        constraint.name = utils.REACH_CONSTRAINT
    constraint.owner_space = 'WORLD'
    constraint.use_transform_limit = False
    for axis in REACH_AXES:
        setattr(constraint, "use_min_" + axis, True)
        setattr(constraint, "use_max_" + axis, True)
        kind = {"x": 'LOC_X', "y": 'LOC_Y', "z": 'LOC_Z'}[axis]
        key = utils.REACH_TRANSLATION_PROPS[REACH_AXES.index(axis)]
        for bound in ("min_", "max_"):
            utils.remove_driver(constraint, bound + axis)
            _fcurve, driver = utils.new_scripted_driver(constraint, bound + axis)
            driver.expression = "p + t"
            utils.driver_var_transform(driver, "p", prop, kind, 'TRANSFORM_SPACE')
            utils.driver_var_prop(driver, "t", prop, utils.id_prop_path(key))
    utils.remove_driver(constraint, "influence")
    _fcurve, driver = utils.new_scripted_driver(constraint, "influence")
    driver.expression = "lock * gate"
    utils.driver_var_prop(driver, "lock", prop, utils.id_prop_path(utils.ID_PROP_GRIP_LOCK))
    utils.driver_var_prop(driver, "gate", prop, utils.id_prop_path(utils.ID_PROP_REACH_GATE))
    utils.set_constraint_enabled(constraint, True)
    return constraint


def update_reach_values(prop):
    """Recompute `ph_reach_*` (margin, rig scale) for the installed sides and
    the distances of the master-hand constraints."""
    settings = utils.get_obj_settings(prop)
    if settings is None:
        return
    for side in utils.reach_sides(prop):
        ik_object, ik_bone = utils.get_ik_handle(prop, side)
        found = utils.find_ik_chain(ik_object, ik_bone)
        if found is not None:
            chain, _shoulder = found
            prop[utils.ID_PROP_REACH[side]] = utils.chain_reach(ik_object, chain, settings.reach_margin)
    for side in utils.SIDES:
        for constraint in utils.master_reach_constraints(utils.ik_handle_owner(prop, side)):
            reach_side = side if constraint.name.endswith("self") else utils.other_side(side)
            if utils.ID_PROP_REACH[reach_side] in prop.keys():
                constraint.distance = prop[utils.ID_PROP_REACH[reach_side]]
    prop.update_tag()


def _rebuild_reach_drivers(prop, sides):
    for key in [key for key in prop.keys() if str(key).startswith(utils.REACH_PASS_PREFIX)]:
        utils.remove_driver(prop, utils.id_prop_path(key))
        del prop[key]
    for side in sides:
        _build_reach_side_drivers(prop, side)
    _build_reach_pass_drivers(prop, sides)
    _build_reach_constraint(prop)


def refresh_reach_drivers(prop):
    """Regenerate the driver chain (the grip offsets are constants of the
    expressions): call it when a grip pose changes."""
    sides = utils.reach_sides(prop)
    if not sides or not utils.reach_limit_present(prop):
        return
    _rebuild_reach_drivers(prop, sides)
    prop.update_tag()


def build_reach_limit(context, prop):
    """Install (or rebuild) the reach limit. Returns the list of limited
    sides; raises RuntimeError with an explicit message otherwise."""
    if prop.parent is not None:
        raise RuntimeError("the prop must have no parent (reach measured in world "
                           "space): Alt+P ▸ Clear and Keep Transformation")
    settings = utils.get_obj_settings(prop)
    utils.sync_twohand_id_props(prop)
    sides = []
    for side in utils.SIDES:
        ik_object, ik_bone = utils.get_ik_handle(prop, side)
        if utils.find_grip_empty(prop, side) is None:
            continue
        found = utils.find_ik_chain(ik_object, ik_bone)
        if found is None:
            continue
        chain, shoulder = found
        root = chain[-1]

        helper = utils.find_helper(prop, utils.ID_PROP_REACH_HELPER[side])
        if helper is None:
            helper = _new_helper_empty(context, prop, "PH_ReachHelper_" + utils.side_suffix(side))
            prop[utils.ID_PROP_REACH_HELPER[side]] = helper.name
        _reset_basis(helper)
        # Root of the chain expressed in the shoulder bone's space; a
        # bone-parented object is placed at the bone's tail, hence the −len
        # offset.
        head_local = shoulder.bone.matrix_local.inverted_safe() @ root.bone.head_local
        helper.parent = ik_object
        helper.parent_type = 'BONE'
        helper.parent_bone = shoulder.name
        helper.matrix_parent_inverse = Matrix.Translation(
            head_local - Vector((0.0, shoulder.bone.length, 0.0)))
        prop[utils.ID_PROP_REACH[side]] = utils.chain_reach(ik_object, chain, settings.reach_margin)
        sides.append(side)

    if not sides:
        utils.remove_reach_limit(prop)
        raise RuntimeError("no IK chain driven by the hands was found: the reach "
                           "requires bone IK targets of an armature with an IK constraint")
    for side in utils.SIDES:
        if side not in sides:
            utils.remove_helper(prop, utils.ID_PROP_REACH_HELPER[side])
            for key in utils.REACH_CHAIN_PROPS[side] + (utils.ID_PROP_REACH[side],):
                if key in prop.keys():
                    utils.remove_driver(prop, utils.id_prop_path(key))
                    del prop[key]

    if utils.ID_PROP_REACH_GATE not in prop.keys():
        prop[utils.ID_PROP_REACH_GATE] = 0.0 if utils.holding_side(prop) is not None else 1.0
    _rebuild_reach_drivers(prop, sides)
    prop.update_tag()
    return sides


# ---------------------------------------------------------------------------
#  Master hand under Lock: the stop moves to the master controller
# ---------------------------------------------------------------------------
#
#  When the prop follows a hand (section 1), the prop's Limit Location is
#  gated off: clamping the prop would let the master controller run away
#  from it. The stop is applied to the master controller instead, with
#  driver-free Limit Distance constraints (inside a sphere, world space):
#
#    * `PH_ReachMaster_self`  → own shoulder helper, distance = own reach;
#    * `PH_ReachMaster_other` → helper `PH_ReachTarget_<other>` placed at
#      "other shoulder − a" (Copy Location of the other shoulder helper plus
#      the constant world offset −a, a = other grip − controller measured
#      when the master hand is set), distance = other reach: keeps the
#      other hand's grip within its reach while the controller drives.
#
#  Both are repeated once (sequential projections: a translation satisfying
#  both spheres). Their influence is keyframed with the Lock (a driver
#  reading the prop would link the controller to the object it drives).
#  The controllers never read the prop: no cycle. The offset `a` is refreshed
#  by "Set pose"; a rotation of the master hand changes it slightly (the stop
#  is then approximate), a translation does not.
# ---------------------------------------------------------------------------

def _master_reach_name(kind, round_index):
    return "%s_%s%s" % (utils.REACH_MASTER_PREFIX, kind, "" if round_index == 0 else str(round_index + 1))


def _bake_remove_master_reach(context, prop, side, last_frame):
    """Bake to keys then remove the master-hand constraints of one
    controller. Returns a note or None."""
    owner = utils.ik_handle_owner(prop, side)
    constraints = list(utils.master_reach_constraints(owner))
    if not constraints:
        utils.remove_helper(prop, utils.ID_PROP_REACH_TARGET[side])
        return None
    baked = None
    for constraint in constraints:
        result = bake_and_remove_constraint(context, owner, constraint, last_frame)
        if result is not None:
            baked = (min(result[0], baked[0]), max(result[1], baked[1])) if baked else result
    utils.remove_helper(prop, utils.ID_PROP_REACH_TARGET[side])
    return _bake_note("Arm reach of the %s (master hand)" % utils.side_label(side), baked)


def sync_master_reach(context, prop, frame):
    """Install the master-hand reach constraints on the current master
    controller and bake/remove them from any other controller. No-op
    without a reach limit. Returns the notes."""
    notes = []
    holder = utils.holding_side(prop) if utils.reach_limit_present(prop) else None
    sides = utils.reach_sides(prop) if holder is not None else []
    for side in utils.SIDES:
        if side != holder:
            note = _bake_remove_master_reach(context, prop, side, frame)
            if note:
                notes.append(note)
    if holder is None or not sides:
        return notes

    owner = utils.ik_handle_owner(prop, holder)
    other = utils.other_side(holder)
    depsgraph = context.evaluated_depsgraph_get()
    wanted = []
    if holder in sides:
        wanted.append(("self", utils.find_helper(prop, utils.ID_PROP_REACH_HELPER[holder]),
                       prop[utils.ID_PROP_REACH[holder]]))
    if other in sides and utils.find_grip_empty(prop, other) is not None:
        target = utils.find_helper(prop, utils.ID_PROP_REACH_TARGET[other])
        if target is None:
            target = _new_helper_empty(context, prop, "PH_ReachTarget_" + utils.side_suffix(other))
            prop[utils.ID_PROP_REACH_TARGET[other]] = target.name
        _reset_basis(target)
        target.parent = None
        follow = target.constraints.get(utils.FOLLOW_GRIP_CONSTRAINT)
        if follow is None:
            follow = target.constraints.new('COPY_LOCATION')
            follow.name = utils.FOLLOW_GRIP_CONSTRAINT
        follow.target = utils.find_helper(prop, utils.ID_PROP_REACH_HELPER[other])
        follow.subtarget = ""
        follow.use_offset = True
        follow.target_space = 'WORLD'
        follow.owner_space = 'WORLD'
        follow.influence = 1.0
        utils.set_constraint_enabled(follow, True)
        controller_world = utils.owner_world_matrix(depsgraph, owner)
        grip_world = utils.evaluated_matrix_world(depsgraph, utils.find_grip_empty(prop, other))
        if controller_world is not None:
            target.location = controller_world.translation - grip_world.translation   # = −a
        wanted.append(("other", target, prop[utils.ID_PROP_REACH[other]]))

    existing = {constraint.name: constraint for constraint in utils.master_reach_constraints(owner)}
    lock = 1.0 if utils.grip_lock_active(prop) else 0.0
    kept = set()
    for round_index in range(2):
        for kind, target, distance in wanted:
            name = _master_reach_name(kind, round_index)
            constraint = existing.get(name)
            created = constraint is None
            if created:
                constraint = owner.constraints.new('LIMIT_DISTANCE')
                constraint.name = name
                constraint.influence = 0.0
                utils.insert_influence_key(constraint, 0.0, frame - 1)
                utils.insert_influence_key(constraint, lock, frame)
            constraint.target = target
            constraint.subtarget = ""
            constraint.distance = distance
            constraint.limit_mode = 'LIMITDIST_INSIDE'
            constraint.use_transform_limit = False
            constraint.target_space = 'WORLD'
            constraint.owner_space = 'WORLD'
            utils.set_constraint_enabled(constraint, True)
            kept.add(name)
    for name, constraint in existing.items():
        if name not in kept:
            bake_and_remove_constraint(context, owner, constraint, frame)
    if kept and any(name not in existing for name in kept):
        notes.append("Arm reach applied to the %s (master hand)." % utils.side_label(holder))
    return notes


def key_master_reach_lock(prop, value, frame):
    """Key the influence of the master-hand reach constraints with the Lock."""
    for side in utils.SIDES:
        for constraint in utils.master_reach_constraints(utils.ik_handle_owner(prop, side)):
            utils.hold_influence(constraint, frame - 1)
            utils.insert_influence_key(constraint, value, frame)


class PROPHANDOFF_OT_setup_twohanded(_PropPoll, bpy.types.Operator):
    """Create the two grip points (on the hands if the IK targets are set)
    and the zone visualization sphere"""

    bl_idname = "prophandoff.setup_twohanded"
    bl_label = "Setup Two-Hand Grip"
    bl_options = {'REGISTER', 'UNDO'}

    offset: FloatProperty(
        name="Spacing (fallback)",
        description="Offset used if the object has no usable geometry",
        default=0.2, min=0.0, soft_max=2.0, subtype='DISTANCE',
    )
    create_zone: BoolProperty(
        name="Create the zone sphere",
        description="Wireframe sphere empty showing the grab radius",
        default=True,
    )
    snap_to_hands: BoolProperty(
        name="Place on the hands",
        description="If a side's IK target is set, place the grip point "
                    "directly on the hand (same as \"Set pose\")",
        default=True,
    )

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        settings = utils.get_obj_settings(prop)
        depsgraph = context.evaluated_depsgraph_get()
        prop_world = utils.evaluated_matrix_world(depsgraph, prop)
        created, snapped = [], []

        for side, sign in (('LEFT', -1.0), ('RIGHT', 1.0)):
            empty = utils.find_grip_empty(prop, side)
            if empty is not None:
                matrix_local = empty.matrix_basis.copy()   # setup re-run: pose kept
            else:
                matrix_local = None
                if self.snap_to_hands:
                    hand_world = utils.owner_world_matrix(
                        depsgraph, utils.ik_handle_owner(prop, side))
                    if hand_world is not None:
                        matrix_local = prop_world.inverted_safe() @ hand_world
                        snapped.append(utils.side_label(side))
                if matrix_local is None:
                    matrix_local = _default_grip_matrix(prop, sign, self.offset)
                empty = _create_grip_empty(context, prop, side, matrix_local)
                created.append(empty.name)
            utils.store_grip_point(prop, side, empty, matrix_local)

        if self.create_zone:
            zone = utils.find_grip_zone(prop)
            if zone is None:
                zone = bpy.data.objects.new(utils.grip_zone_basename(prop), None)
                _link_next_to(context, prop, zone)
                _place_grip_empty(prop, zone, Matrix.Identity(4))
                created.append(zone.name)
            zone.empty_display_type = 'SPHERE'
            zone.empty_display_size = settings.grip_zone_radius   # not `scale`
            zone.display_type = 'WIRE'
            zone.show_in_front = True
            zone.color = (1.0, 0.5, 0.0, 1.0)
            zone.hide_select = True

        utils.sync_twohand_id_props(prop)
        utils.tag_update(context, prop)

        if not created:
            self.report({'INFO'}, "Two-Hand Grip already in place: settings updated.")
        elif snapped:
            self.report({'INFO'}, "Two-Hand Grip configured, grip points placed on: %s."
                        % ", ".join(snapped))
        else:
            self.report({'INFO'}, "Two-Hand Grip configured (%s). Set the IK targets "
                                  "then \"Set pose\" to place the grips on the hands."
                        % ", ".join(created))
        return {'FINISHED'}


class PROPHANDOFF_OT_set_grip_pose(_PropPoll, bpy.types.Operator):
    """Store the current pose of the hand as its grab point"""

    bl_idname = "prophandoff.set_grip_pose"
    bl_label = "Set Grip Pose"
    bl_options = {'REGISTER', 'UNDO'}

    hand: EnumProperty(
        name="Hand",
        items=[('LEFT', "Left", "Left hand"), ('RIGHT', "Right", "Right hand")],
        default='LEFT',
    )

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        side = self.hand

        error = utils.ik_handle_error(prop, side)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}
        empty = utils.find_grip_empty(prop, side)
        if empty is None:
            self.report({'ERROR'}, "Run \"Setup Two-Hand Grip\" first.")
            return {'CANCELLED'}

        depsgraph = context.evaluated_depsgraph_get()
        hand_world = utils.owner_world_matrix(depsgraph, utils.ik_handle_owner(prop, side))
        prop_world = utils.evaluated_matrix_world(depsgraph, prop)
        if hand_world is None:
            self.report({'ERROR'}, "Unreadable hand position.")
            return {'CANCELLED'}

        # The hand expressed in the prop's frame: valid whatever the future
        # position of the prop.
        matrix_local = prop_world.inverted_safe() @ hand_world
        _place_grip_empty(prop, empty, matrix_local)
        utils.store_grip_point(prop, side, empty, matrix_local)
        utils.sync_twohand_id_props(prop)
        refresh_reach_drivers(prop)
        sync_master_reach(context, prop, context.scene.frame_current)
        utils.tag_update(context, prop)
        self.report({'INFO'}, "Grip pose stored for the %s." % utils.side_label(side))
        return {'FINISHED'}


class PROPHANDOFF_OT_enable_grip_zone(_PropPoll, bpy.types.Operator):
    """Enable the proximity blend: each hand snaps to its grip point when it
    enters the sphere (drivers, no dependency cycle)"""

    bl_idname = "prophandoff.enable_grip_zone"
    bl_label = "Enable Grip Zone"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        success, message, level = enable_grip_zone(context, prop)
        if not success:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        utils.tag_update(context, prop)
        self.report({level}, message)
        return {'FINISHED'}


class PROPHANDOFF_OT_grip_lock(_PropPoll, bpy.types.Operator):
    """Lock: the hands stay on the grips whatever the distance (keyed at the
    current frame); under Lock the reach limit acts"""

    bl_idname = "prophandoff.grip_lock"
    bl_label = "Lock"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=[('LOCK', "Lock", "Hands locked on the grips from this frame on"),
               ('UNLOCK', "Unlock", "Back to the proximity blend from this frame on")],
        default='LOCK',
    )

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        frame = context.scene.frame_current
        notes = []

        if self.action == 'LOCK':
            # The Lock goes through the Grip Zone driver: it is (re)created on
            # every lock — idempotent, and this updates the drivers of a scene
            # made with an earlier version of the add-on.
            already = all(utils.grip_zone_active(prop, side) for side in utils.SIDES
                          if side != utils.holding_side(prop))
            success, message, level = enable_grip_zone(context, prop)
            if not success:
                self.report({'ERROR'}, message)
                return {'CANCELLED'}
            if not already or level != 'INFO':
                notes.append(message)
            # The reach limit comes with the Lock as soon as an IK chain exists.
            if not utils.reach_limit_present(prop):
                try:
                    sides = build_reach_limit(context, prop)
                    notes.append("Arm reach installed (%s)."
                                 % ", ".join(utils.side_label(side) for side in sides))
                except RuntimeError as error:
                    notes.append("Arm reach not installed: %s." % error)
            notes += sync_master_reach(context, prop, frame)

        utils.sync_twohand_id_props(prop)
        utils.hold_id_prop(prop, utils.ID_PROP_GRIP_LOCK, frame - 1)
        utils.insert_id_prop_key(prop, utils.ID_PROP_GRIP_LOCK,
                                 1.0 if self.action == 'LOCK' else 0.0, frame)
        key_master_reach_lock(prop, 1.0 if self.action == 'LOCK' else 0.0, frame)
        utils.tag_update(context, prop)

        if self.action == 'LOCK':
            message = "Frame %d: lock on, the hands stay on %s." % (frame, prop.name)
        else:
            message = "Frame %d: lock off, back to the proximity blend." % frame
        if notes:
            message += " " + " ".join(notes)
        self.report({'INFO'}, message)
        return {'FINISHED'}


class PROPHANDOFF_OT_reach_limit(_PropPoll, bpy.types.Operator):
    """Reach limit: under Lock, the prop stops when an arm is fully extended
    (Limit Location driven from the shoulders, no cycle)"""

    bl_idname = "prophandoff.reach_limit"
    bl_label = "Arm Reach"
    bl_options = {'REGISTER', 'UNDO'}

    action: EnumProperty(
        name="Action",
        items=[('ENABLE', "Install", "Install or rebuild the reach limit"),
               ('DISABLE', "Remove", "Remove the constraint, its drivers and its helpers")],
        default='ENABLE',
    )

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        frame = context.scene.frame_current
        if self.action == 'DISABLE':
            for side in utils.SIDES:
                _bake_remove_master_reach(context, prop, side, frame)
            removed = utils.remove_reach_limit(prop)
            utils.tag_update(context, prop)
            self.report({'INFO'}, "Reach limit removed." if removed else "No reach limit.")
            return {'FINISHED'} if removed else {'CANCELLED'}
        try:
            sides = build_reach_limit(context, prop)
        except RuntimeError as error:
            self.report({'ERROR'}, "Reach impossible: %s" % error)
            return {'CANCELLED'}
        notes = sync_master_reach(context, prop, frame)
        utils.tag_update(context, prop)
        reach = ", ".join("%s %.2f" % (utils.side_label(side), prop[utils.ID_PROP_REACH[side]])
                          for side in sides)
        self.report({'INFO'}, " ".join(["Arm reach installed (%s): acts under Lock." % reach] + notes))
        return {'FINISHED'}


class PROPHANDOFF_OT_disable_grip_zone(_PropPoll, bpy.types.Operator):
    """Remove the blend constraints, their drivers and the helper empties"""

    bl_idname = "prophandoff.disable_grip_zone"
    bl_label = "Disable Grip Zone"
    bl_options = {'REGISTER', 'UNDO'}

    bake: BoolProperty(
        name="Bake the motion",
        description="First bake to keys the effect of the Grip Zone / Lock over the "
                    "whole scene (otherwise the hands go back to their raw channels)",
        default=True,
    )

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        if self.bake:
            removed = sum(1 for side in utils.SIDES
                          if bake_and_remove_grip_blend(context, prop, side, context.scene.frame_end))
            for side in utils.SIDES:
                _bake_remove_master_reach(context, prop, side, context.scene.frame_end)
        else:
            removed = _remove_grip_blends(prop)
        reach_removed = utils.remove_reach_limit(prop)
        utils.set_grip_zone_wanted(prop, False)
        utils.tag_update(context, prop)
        if not removed and not reach_removed:
            self.report({'INFO'}, "No Grip Zone constraint to remove.")
            return {'CANCELLED'}
        message = "Grip Zone disabled (%d hand(s), helper empties removed)." % removed
        if reach_removed:
            message += " Reach limit removed."
        self.report({'INFO'}, message)
        return {'FINISHED'}


class PROPHANDOFF_OT_attach_both_hands(_PropPoll, bpy.types.Operator):
    """Attach both hands to the prop at the current frame (the prop becomes the authority)"""

    bl_idname = "prophandoff.attach_both_hands"
    bl_label = "Attach Both Hands"
    bl_options = {'REGISTER', 'UNDO'}

    release_prop_first: BoolProperty(
        name="Release the prop first",
        description="If the prop already follows a hand (section 1), free it to "
                    "avoid a circular dependency",
        default=True,
    )

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        frame = context.scene.frame_current

        errors = [m for m in (_validate_side(prop, side) for side in utils.SIDES) if m]
        if errors:
            self.report({'ERROR'}, errors[0])
            return {'CANCELLED'}

        released = False
        holding = utils.holding_side(prop)
        if holding is not None:
            if not self.release_prop_first:
                self.report({'ERROR'},
                            "The prop follows the %s (section 1): release it first, or tick "
                            "\"Release the prop first\"." % utils.side_label(holding))
                return {'CANCELLED'}
            success, message = apply_handoff(context, prop, None)
            if not success:
                self.report({'ERROR'}, message)
                return {'CANCELLED'}
            released = True

        # The grip points follow the prop: up-to-date matrices before compensation.
        context.view_layer.update()
        notes = sync_master_reach(context, prop, frame)
        context.view_layer.update()
        utils.set_grip_zone_wanted(prop, False)    # the attach replaces the zone
        for side in utils.SIDES:
            success, message = attach_hand(context, prop, side, frame)
            if not success:
                self.report({'ERROR'}, message)
                return {'CANCELLED'}
            # Keep only the notes (bake / removal), not the base message.
            extra = message.split(".", 1)[1].strip() if "." in message else ""
            if extra:
                notes.append(extra)
            context.view_layer.update()

        utils.tag_update(context, prop)
        summary = "Both hands attached at frame %d." % frame
        if released:
            summary += " The prop was released beforehand (section 1)."
        if notes:
            summary += " " + " ".join(notes)
        self.report({'INFO'}, summary)
        return {'FINISHED'}


class PROPHANDOFF_OT_release_both_hands(_PropPoll, bpy.types.Operator):
    """Release both hands (attach and Grip Zone) and free the prop"""

    bl_idname = "prophandoff.release_both_hands"
    bl_label = "Release Both Hands"
    bl_options = {'REGISTER', 'UNDO'}

    keep_grip_zone: BoolProperty(
        name="Keep the Grip Zone",
        description="Leave the proximity blend enabled: a hand still inside the "
                    "sphere will be re-grabbed immediately",
        default=False,
    )
    release_prop: BoolProperty(
        name="Also release the prop",
        description="If the prop follows a hand (Master Hand / section 1), free it "
                    "too so that nothing stays linked",
        default=True,
    )

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        frame = context.scene.frame_current
        successes, failures = [], []

        for side in utils.SIDES:
            success, message = release_hand(context, prop, side, frame, self.keep_grip_zone)
            (successes if success else failures).append(message)
            if success:
                context.view_layer.update()

        prop_released = False
        if self.release_prop and utils.holding_side(prop) is not None:
            success, message = apply_handoff(context, prop, None)
            if success:
                prop_released = True
            else:
                failures.append(message)
        successes += sync_master_reach(context, prop, frame)

        # The hands are free: the Grip Zone is no longer wanted (nothing must
        # re-grab), and the Lock is lifted on the way (keyframed).
        if not self.keep_grip_zone:
            utils.set_grip_zone_wanted(prop, False)
        lock_lifted = False
        if utils.grip_lock_active(prop):
            utils.hold_id_prop(prop, utils.ID_PROP_GRIP_LOCK, frame - 1)
            utils.insert_id_prop_key(prop, utils.ID_PROP_GRIP_LOCK, 0.0, frame)
            lock_lifted = True

        if not successes and not prop_released and not lock_lifted:
            self.report({'WARNING'}, failures[0] if failures else "Nothing to release.")
            return {'CANCELLED'}

        utils.tag_update(context, prop)
        summary = "Frame %d: %d hand(s) released" % (frame, len(successes))
        if prop_released:
            summary += ", prop freed"
        if lock_lifted:
            summary += ", lock lifted"
        summary += "."
        if failures:
            # A skipped hand must be visible: WARNING with the cause.
            self.report({'WARNING'}, summary + " " + " ".join(failures))
        else:
            self.report({'INFO'}, summary)
        return {'FINISHED'}


class PROPHANDOFF_OT_switch_to_twohanded(_PropPoll, bpy.types.Operator):
    """Add the second hand without detaching the one already carrying the prop"""

    bl_idname = "prophandoff.switch_to_twohanded"
    bl_label = "Switch to Two-Handed"
    bl_options = {'REGISTER', 'UNDO'}

    free_hand: EnumProperty(
        name="Hand to add",
        description="Hand that comes onto the prop",
        items=[('AUTO', "Automatic", "Deduce from the active slot of section 1"),
               ('LEFT', "Left", "Add the left hand"),
               ('RIGHT', "Right", "Add the right hand")],
        default='AUTO',
    )

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        frame = context.scene.frame_current

        if self.free_hand == 'AUTO':
            holding = utils.holding_side(prop)
            if holding is None:
                self.report({'ERROR'},
                            "Cannot deduce which hand holds the prop: assign it first "
                            "through section 1, or pick the hand explicitly.")
                return {'CANCELLED'}
            side = utils.other_side(holding)
        else:
            side = self.free_hand
            holding = utils.other_side(side)

        error = _validate_side(prop, side)
        if error:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        # The added hand must not be the one carrying the prop.
        ik_object, ik_bone = utils.get_ik_handle(prop, side)
        if utils.prop_held_by_handle(prop, ik_object, ik_bone):
            self.report({'ERROR'}, "This hand already carries the prop (section 1): "
                                   "pick the other hand.")
            return {'CANCELLED'}

        # The carrying hand must hold no constraint towards the prop (Grip
        # Zone even disabled, attach even at 0): cycle otherwise.
        notes = detach_hand_from_prop(context, prop, holding, frame)
        context.view_layer.update()

        success, message = attach_hand(context, prop, side, frame)
        if not success:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}
        notes += sync_master_reach(context, prop, frame)

        utils.tag_update(context, prop)
        summary = "%s added on the prop at frame %d (the %s keeps carrying it)." % (
            utils.side_label(side).capitalize(), frame, utils.side_label(holding))
        for note in notes + [message]:
            if note and note not in summary:
                summary += " " + note
        self.report({'INFO'}, summary)
        return {'FINISHED'}


class PROPHANDOFF_OT_set_master_hand(_PropPoll, bpy.types.Operator):
    """Master hand: the prop follows this hand, the other hand follows the prop"""

    bl_idname = "prophandoff.set_master_hand"
    bl_label = "Master Hand"
    bl_options = {'REGISTER', 'UNDO'}

    hand: EnumProperty(
        name="Master hand",
        items=[('NONE', "None", "The prop is free, the hands follow it"),
               ('LEFT', "Left", "The prop follows the left hand"),
               ('RIGHT', "Right", "The prop follows the right hand")],
        default='NONE',
    )
    other_hand: EnumProperty(
        name="Other hand",
        description="What to do with the hand that is not driving",
        items=[('AUTO', "Automatic",
                "If it does not already follow the prop: restore its Grip Zone (if enabled), "
                "else attach it"),
               ('ATTACH', "Attach", "Attach it to the prop at the current frame"),
               ('KEEP', "Leave as is", "Keep its current state")],
        default='AUTO',
    )

    def _set_none(self, context, prop, frame):
        """Free the prop and restore the Grip Zone (if it was wanted) on the
        hands that no longer follow the prop: back to the state before Master
        Hand."""
        success, message = apply_handoff(context, prop, None)
        if not success:
            return False, message
        context.view_layer.update()
        notes = sync_master_reach(context, prop, frame)
        context.view_layer.update()
        notes += restore_grip_zone(context, prop, utils.SIDES)
        summary = "Master hand removed: the prop is free at frame %d." % frame
        if notes:
            summary += " " + " ".join(notes)
        elif not utils.grip_zone_wanted(prop):
            missing = [utils.side_label(side) for side in utils.SIDES
                       if not utils.hand_follows_prop(prop, side)]
            if missing:
                summary += " Free hands (%s): \"Enable\" or \"Lock\" to put them back." % ", ".join(missing)
        return True, summary

    def _set_side(self, context, prop, side, frame):
        """The prop follows `side`; the other hand follows the prop."""
        error = utils.ik_handle_error(prop, side)
        if error:
            return False, error
        other = utils.other_side(side)

        # 1. the master hand must no longer hold a constraint towards the
        #    prop (anti-cycle): attach released then baked/removed, Grip Zone
        #    removed (not muted).
        notes = detach_hand_from_prop(context, prop, side, frame)
        context.view_layer.update()

        # 2. the prop follows the master hand (section 1 mechanics)
        slot_name = _find_or_create_ik_slot(prop, side)
        context.view_layer.update()
        success, message = apply_handoff(context, prop, slot_name)
        if not success:
            return False, message
        context.view_layer.update()
        # Under Lock the stop moves to the master controller (own reach and
        # the other hand's reach through the grip offset).
        notes += sync_master_reach(context, prop, frame)
        context.view_layer.update()

        # 3. the other hand follows the prop: through its Grip Zone (restored
        #    if it was wanted), else through a keyframed attach.
        if self.other_hand == 'ATTACH':
            if _validate_side(prop, other) is None:
                success, message = attach_hand(context, prop, other, frame)
                if not success:
                    return False, message
                notes.append(message)
            else:
                notes.append("(%s not attached: missing IK target or grip point.)"
                             % utils.side_label(other).capitalize())
        elif self.other_hand == 'AUTO' and not utils.hand_follows_prop(prop, other):
            restored = restore_grip_zone(context, prop, (other,))
            notes += restored
            if not utils.hand_follows_prop(prop, other):
                if _validate_side(prop, other) is None:
                    success, message = attach_hand(context, prop, other, frame)
                    if not success:
                        return False, message
                    notes.append(message)
                else:
                    notes.append("(%s not attached: missing IK target or grip point.)"
                                 % utils.side_label(other).capitalize())

        summary = "The prop follows the %s at frame %d." % (utils.side_label(side), frame)
        if notes:
            summary += " " + " ".join(notes)
        return True, summary

    def execute(self, context):
        prop = _resolve_prop(context, self)
        if prop is None:
            return {'CANCELLED'}
        frame = context.scene.frame_current

        if self.hand == 'NONE':
            success, message = self._set_none(context, prop, frame)
        else:
            success, message = self._set_side(context, prop, self.hand, frame)
        if not success:
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        utils.tag_update(context, prop)
        self.report({'INFO'}, message)
        return {'FINISHED'}


# ===========================================================================
#  6. Navigation
# ===========================================================================

class PROPHANDOFF_OT_go_to_transfer(bpy.types.Operator):
    """Put the playhead on this transfer frame"""

    bl_idname = "prophandoff.go_to_transfer"
    bl_label = "Go to Transfer"
    bl_options = {'REGISTER', 'UNDO'}

    frame: IntProperty(name="Frame", default=1)

    def execute(self, context):
        context.scene.frame_set(self.frame)
        return {'FINISHED'}


class PROPHANDOFF_OT_jump_transfer(_PropPoll, bpy.types.Operator):
    """Jump to the previous or next transfer of the active prop"""

    bl_idname = "prophandoff.jump_transfer"
    bl_label = "Previous / Next Transfer"
    bl_options = {'REGISTER', 'UNDO'}

    direction: EnumProperty(
        name="Direction",
        items=[('PREV', "Previous", "Previous transfer"),
               ('NEXT', "Next", "Next transfer")],
        default='NEXT',
    )

    def execute(self, context):
        obj = _resolve_prop(context, self)
        if obj is None:
            return {'CANCELLED'}
        frames = [event["frame"] for event in utils.list_transfer_events(obj)]
        if not frames:
            self.report({'WARNING'}, "No transfer on this object.")
            return {'CANCELLED'}

        current = context.scene.frame_current
        if self.direction == 'NEXT':
            candidates = [f for f in frames if f > current]
            target = min(candidates) if candidates else None
        else:
            candidates = [f for f in frames if f < current]
            target = max(candidates) if candidates else None
        if target is None:
            self.report({'INFO'}, "No transfer in that direction.")
            return {'CANCELLED'}
        context.scene.frame_set(target)
        return {'FINISHED'}


# ===========================================================================
#  7. Registration
# ===========================================================================

classes = (
    # PropertyGroups first: the PointerProperty reference them.
    PH_SlotItem,
    PH_ObjectSettings,
    PH_SceneSettings,
    # Section 1 — configuration
    PROPHANDOFF_OT_setup_prop,
    PROPHANDOFF_OT_clear_setup,
    PROPHANDOFF_OT_slot_add,
    PROPHANDOFF_OT_slot_remove,
    PROPHANDOFF_OT_slot_move,
    PROPHANDOFF_OT_slot_from_selection,
    PROPHANDOFF_OT_use_active_object,
    PROPHANDOFF_OT_pick_prop,
    PROPHANDOFF_OT_reload_slots,
    # Section 1 — animation
    PROPHANDOFF_OT_assign,
    PROPHANDOFF_OT_release,
    PROPHANDOFF_OT_throw,
    PROPHANDOFF_OT_bake_visual_transform,
    # Section 2 — Two-Hand Grip
    PROPHANDOFF_OT_setup_twohanded,
    PROPHANDOFF_OT_set_grip_pose,
    PROPHANDOFF_OT_enable_grip_zone,
    PROPHANDOFF_OT_disable_grip_zone,
    PROPHANDOFF_OT_grip_lock,
    PROPHANDOFF_OT_reach_limit,
    PROPHANDOFF_OT_attach_both_hands,
    PROPHANDOFF_OT_release_both_hands,
    PROPHANDOFF_OT_switch_to_twohanded,
    PROPHANDOFF_OT_set_master_hand,
    # Navigation
    PROPHANDOFF_OT_go_to_transfer,
    PROPHANDOFF_OT_jump_transfer,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Object.prop_handoff = PointerProperty(
        type=PH_ObjectSettings, name="PropHandoff",
        description="Slots and Two-Hand Grip settings of this object")
    bpy.types.Scene.prop_handoff = PointerProperty(
        type=PH_SceneSettings, name="PropHandoff",
        description="Active prop and throw settings")


def unregister():
    # Pointers before the classes, in reverse order.
    for owner, attribute in ((bpy.types.Scene, "prop_handoff"),
                             (bpy.types.Object, "prop_handoff")):
        if hasattr(owner, attribute):
            delattr(owner, attribute)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
