# -*- coding: utf-8 -*-
"""PropHandoff pure functions.

No Blender class here: only functions shared by `operators.py` and
`panel.py`. This module never imports the other two.

Contents:
  * naming conventions (`PH_<slot>`, `PH_TwoHand_*`, `PH_GripBlend_*`…)
  * F-Curve compatibility layer (3.6 → 5.x, "slotted" actions)
  * key insertion on an *owner* (Object **or** PoseBone) and on a constraint
    (of an object **or** a bone), with forced interpolation
  * matrices: visual transform capture and Child Of compensation
  * JSON serialization into the prop's custom properties
  * transfer history rebuilt from the F-Curves
  * Two-Hand Grip: IK handles, grip points, proximity drivers, helper
    empties, anti-cycle rules, static bone dependency analysis
"""

import json
import time

import bpy
from mathutils import Matrix, Vector

# ===========================================================================
#  Constants
# ===========================================================================

#: Prefix of everything the add-on creates (constraints, helper empties).
#: Whatever does not start with this prefix belongs to the animator: it is
#: never touched.
PH_PREFIX = "PH_"

#: Custom properties (ID props) of section 1.
ID_PROP_SLOTS = "ph_slots"
ID_PROP_VERSION = "ph_version"
DATA_VERSION = 1

#: Above this threshold an influence is considered "active".
ACTIVE_THRESHOLD = 0.5

# --- Section 2: Two-Hand Grip ----------------------------------------------

SIDES = ('LEFT', 'RIGHT')

#: Attach Child Of placed on the IK target (bone or object).
PH_TWOHAND_PREFIX = "PH_TwoHand_"
#: Grip Zone Copy Transforms, influence driven by a driver.
PH_GRIPBLEND_PREFIX = "PH_GripBlend_"
#: Copy Location of the "grip local" empty towards the grip point.
FOLLOW_GRIP_CONSTRAINT = "PH_FollowGrip"

#: Custom properties of section 2.
ID_PROP_GRIP_POINTS = "ph_grip_points"
ID_PROP_GRIP_RADIUS = "ph_grip_zone_radius"   # native float: read by the drivers
ID_PROP_IK_TARGET = {'LEFT': "ph_ik_target_L", 'RIGHT': "ph_ik_target_R"}
ID_PROP_IK_BONE = {'LEFT': "ph_ik_bone_L", 'RIGHT': "ph_ik_bone_R"}
ID_PROP_FRAME = {'LEFT': "ph_frame_L", 'RIGHT': "ph_frame_R"}
ID_PROP_GRIP_LOCAL = {'LEFT': "ph_griplocal_L", 'RIGHT': "ph_griplocal_R"}

#: Inner radius of the zone: below it the hand is fully on the grip.
GRIP_INNER_RADIUS = 0.05
#: Default zone radius (Blender units).
DEFAULT_GRIP_RADIUS = 0.5

#: Expression of the Grip Zone influence driver.
#:
#:   bx by bz : `location` channels of the IK target, read by the driver of
#:              the bone itself (depsgraph "same bone" exemption, see README);
#:   gx gy gz : the grip point expressed in the frame of those channels;
#:   s        : world scale of that frame (distance in Blender units);
#:   radius   : `ph_grip_zone_radius` of the prop;
#:   lock     : `ph_grip_lock` of the prop (Lock, keyframable): at 1 the hand
#:              stays on the grip whatever the distance.
#:
#: Only min/max/sqrt and arithmetic are used: Blender's simple expression
#: evaluator recognizes them, so the driver works without "Auto Run Python
#: Scripts". Squares are written `a*a` (`**` is not guaranteed). The
#: `max(…, 0.0001)` avoids a division by zero if the radius goes below the
#: inner radius.
GRIP_EXPRESSION = (
    "max(lock, max(0.0, min(1.0, 1.0 - (s * sqrt((bx-gx)*(bx-gx) + (by-gy)*(by-gy) "
    "+ (bz-gz)*(bz-gz)) - 0.05) / max(radius - 0.05, 0.0001))))"
)
assert len(GRIP_EXPRESSION) < 256, "driver expression too long"

# --- Lock and arm reach ------------------------------------------------------

#: User intention: 1 after Enable / Lock, 0 after Disable / Release / Attach.
#: Master hand and "None" use it to restore the Grip Zone of the hands that
#: are not driving.
ID_PROP_ZONE_WANTED = "ph_grip_zone_wanted"
#: Keyframable custom property of the prop: 1 = hands locked on the grips.
ID_PROP_GRIP_LOCK = "ph_grip_lock"
#: Keyframable gate of the reach limit: 1 = free prop (no active slot),
#: maintained by `apply_handoff()`. Influence of PH_Reach = lock × gate.
ID_PROP_REACH_GATE = "ph_reach_gate"
#: Effective reach (world units) of each arm, read by the drivers.
ID_PROP_REACH = {'LEFT': "ph_reach_L", 'RIGHT': "ph_reach_R"}
#: Helper empty at the root of the IK chain (tail of the shoulder).
ID_PROP_REACH_HELPER = {'LEFT': "ph_reachhelper_L", 'RIGHT': "ph_reachhelper_R"}
#: Limit Location constraint carried by the prop, driven bounds.
REACH_CONSTRAINT = "PH_Reach"
#: Rotated grip offsets R·(S·g) of each side, driven custom properties (on the prop).
REACH_CHAIN_PROPS = {
    'LEFT': ("ph_rg_Lx", "ph_rg_Ly", "ph_rg_Lz"),
    'RIGHT': ("ph_rg_Rx", "ph_rg_Ry", "ph_rg_Rz"),
}
#: Projection passes (alternating between the two reach spheres): prefix of
#: the per-pass driven properties `ph_rp<k>d`, `ph_rp<k>f`, `ph_rp<k>x/y/z`.
REACH_PASS_PREFIX = "ph_rp"
#: Number of rounds (one pass per side each) — continuous by construction.
REACH_ROUNDS = 3
#: Final translation applied by the Limit Location (bounds = channels + t).
REACH_TRANSLATION_PROPS = ("ph_rt_x", "ph_rt_y", "ph_rt_z")
#: Master hand: Limit Distance constraints carried by the master controller
#: (own sphere, then the other hand's sphere through the grip offset).
REACH_MASTER_PREFIX = "PH_ReachMaster"
#: Helper empty at "other shoulder − grip offset": Limit Distance target of
#: the master controller for the other hand's reach.
ID_PROP_REACH_TARGET = {'LEFT': "ph_reachtarget_L", 'RIGHT': "ph_reachtarget_R"}
#: Default safety margin on the reach (arms never fully extended).
DEFAULT_REACH_MARGIN = 0.02


# ===========================================================================
#  Names, slots, settings access
# ===========================================================================

def sanitize_slot_name(name):
    """Clean a slot identifier.

    Quotes and backslashes are removed: they would break the `data_path` of
    the influence F-Curves (`constraints["…"].influence`).
    """
    cleaned = (name or "").strip().replace('"', "").replace("\\", "")
    return cleaned or "Slot"


def unique_slot_name(slots, wanted, exclude=None):
    """`wanted`, suffixed `.001`, `.002`… if another slot already uses it."""
    taken = {slot.name for slot in slots if slot != exclude}
    if wanted not in taken:
        return wanted
    index = 1
    while "%s.%03d" % (wanted, index) in taken:
        index += 1
    return "%s.%03d" % (wanted, index)


def constraint_name(slot_name):
    """Name of a slot's Child Of constraint: `PH_<slot>`."""
    return PH_PREFIX + slot_name


def slot_name_from_constraint(constraint):
    """Inverse of `constraint_name()`."""
    return constraint.name[len(PH_PREFIX):]


def slot_display(slot):
    """Label of a slot (falls back to the identifier)."""
    return slot.label.strip() or slot.name


def get_obj_settings(obj):
    """`obj.prop_handoff` (or None: add-on being reloaded)."""
    return getattr(obj, "prop_handoff", None) if obj is not None else None


def get_scene_settings(scene):
    """`scene.prop_handoff` (or None)."""
    return getattr(scene, "prop_handoff", None) if scene is not None else None


def get_prop_object(context):
    """Object the add-on works on.

    The "Active prop" pinned with the eyedropper has priority: the user then
    selects the armature (Pose mode) to pick bones, and the prop must not
    change because of that. Fallback: the active object.
    """
    settings = get_scene_settings(context.scene)
    if settings is not None and settings.prop_object is not None:
        return settings.prop_object
    return context.active_object


def is_prop(obj):
    """True if the object carries any PropHandoff data: slots or IK targets,
    `PH_*` constraints, grip points, or the `ph_slots` mirror (file opened
    without the add-on). Linked objects are excluded (read-only)."""
    if obj is None or obj.library is not None:
        return False
    settings = get_obj_settings(obj)
    if settings is not None and (len(settings.slots)
                                 or settings.ik_object_l is not None
                                 or settings.ik_object_r is not None):
        return True
    if any(True for _ in iter_ph_constraints(obj)):
        return True
    return ID_PROP_SLOTS in obj.keys() or ID_PROP_GRIP_POINTS in obj.keys()


def iter_props(scene):
    """Objects of the scene that are props (`is_prop`), sorted by name: the
    tabs of the panels."""
    return sorted((obj for obj in scene.objects if is_prop(obj)),
                  key=lambda obj: obj.name.lower())


def find_slot(obj, slot_name):
    """Slot of an object by identifier (or None)."""
    settings = get_obj_settings(obj)
    if settings is None:
        return None
    for slot in settings.slots:
        if slot.name == slot_name:
            return slot
    return None


def slot_label_for(obj, slot_name):
    """Label of a slot, falling back to the raw identifier."""
    slot = find_slot(obj, slot_name)
    return slot_display(slot) if slot is not None else slot_name


# ===========================================================================
#  Slot constraints (section 1)
# ===========================================================================

def iter_ph_constraints(obj):
    """Slot constraints carried by the prop: Child Of `PH_<slot>` — not the
    section 2 constraints (`PH_TwoHand_*` attaches also live on object IK
    targets, `PH_GripBlend_*`), which are never slots."""
    if obj is None:
        return
    for constraint in obj.constraints:
        if (constraint.type == 'CHILD_OF' and constraint.name.startswith(PH_PREFIX)
                and not constraint.name.startswith((PH_TWOHAND_PREFIX, PH_GRIPBLEND_PREFIX))):
            yield constraint


def get_slot_constraint(obj, slot_name):
    """Constraint of a slot (or None if absent / of another type)."""
    if obj is None or not slot_name:
        return None
    constraint = obj.constraints.get(constraint_name(slot_name))
    if constraint is not None and constraint.type == 'CHILD_OF':
        return constraint
    return None


def is_setup(obj):
    """True if the prop carries at least one PropHandoff constraint — or a
    "suspended" slot (its constraint is absent because the hand follows the
    prop, see `ensure_constraints`)."""
    if any(True for _ in iter_ph_constraints(obj)):
        return True
    settings = get_obj_settings(obj)
    return settings is not None and any(slot_is_suspended(obj, slot) for slot in settings.slots)


def active_slot_name(obj):
    """Active slot at the current frame (influence > threshold), or None if released."""
    for constraint in iter_ph_constraints(obj):
        if constraint.influence > ACTIVE_THRESHOLD:
            return slot_name_from_constraint(constraint)
    return None


def constraint_is_driven(owner, constraint):
    """True if the constraint influence is driven by a driver."""
    anim_data = getattr(owner.id_data, "animation_data", None)
    if anim_data is None:
        return False
    path = constraint_influence_path(constraint)
    return any(fcurve.data_path == path for fcurve in anim_data.drivers)


def constraint_has_history(constraint):
    """True if the constraint acts somewhere in time: influence F-Curve with
    at least one key above the threshold, or current influence above the
    threshold without an F-Curve."""
    fcurve = find_fcurve(constraint.id_data, constraint_influence_path(constraint))
    if fcurve is None or not len(fcurve.keyframe_points):
        return constraint.influence > ACTIVE_THRESHOLD
    return any(keyframe.co.y > ACTIVE_THRESHOLD for keyframe in fcurve.keyframe_points)


def slot_is_suspended(prop, slot):
    """True if the constraint of this slot must stay absent: its target is a
    hand currently following the prop (attach or Grip Zone present), or
    depends on one (deform bone copying the IK controller, object parented
    to the hand…).

    A constraint creates its dependency relations as soon as it has a target,
    even at influence 0 or disabled (measured in Blender 5.2.1). A
    `prop → hand` slot therefore cannot coexist with a `hand → prop`
    constraint on the same hand without "Dependency cycle detected".
    """
    if slot.target is None:
        return False
    if hand_constraints_targeting_prop(prop, slot.target, slot.subtarget):
        return True
    for side in SIDES:
        ik_object, ik_bone = get_ik_handle(prop, side)
        if ik_object is None or not hand_constraints_targeting_prop(prop, ik_object, ik_bone):
            continue
        if target_depends_on_handle(slot.target, slot.subtarget or "", ik_object, ik_bone):
            return True
    return False


def ensure_constraints(obj):
    """One Child Of constraint per slot, created at influence 0. Idempotent.

    `inverse_matrix` stays identity and `set_inverse_pending` False: the
    compensation is done on the base transform (animatable), never on the
    inverse (a single value for the whole timeline). "Suspended" slots
    (`slot_is_suspended`) get no constraint: `apply_handoff()` lifts the
    suspension before assigning. Returns the number of constraints created.
    """
    settings = get_obj_settings(obj)
    if settings is None:
        return 0

    created = 0
    for slot in settings.slots:
        name = constraint_name(slot.name)
        constraint = obj.constraints.get(name)

        if constraint is not None and constraint.type != 'CHILD_OF':
            continue  # name already taken by one of the animator's constraints

        if slot_is_suspended(obj, slot):
            # A constraint created before the target was known (name callback
            # then target callback) and without history is removed; one with
            # history is left as is and reported by `dependency_warning()`
            # (the operators bake it before acting).
            if constraint is not None and not constraint_has_history(constraint):
                obj.constraints.remove(constraint)
            continue

        if constraint is None:
            constraint = obj.constraints.new('CHILD_OF')
            constraint.name = name
            constraint.influence = 0.0          # a new constraint is born at 1
            constraint.inverse_matrix = Matrix.Identity(4)
            if hasattr(constraint, "set_inverse_pending"):
                constraint.set_inverse_pending = False
            created += 1

        constraint.target = slot.target
        if slot.target is not None and slot.target.type == 'ARMATURE':
            constraint.subtarget = slot.subtarget
        else:
            constraint.subtarget = ""

    return created


def rename_slot(obj, old_name, new_name):
    """Rename a slot's constraint **and** repair its influence F-Curves.

    Blender does not update `fcurve.data_path` when a constraint is renamed:
    without this repair the influence animation would be orphaned.
    """
    constraint = obj.constraints.get(constraint_name(old_name))
    if constraint is None:
        return
    old_path = influence_path(constraint.name)
    constraint.name = constraint_name(new_name)
    new_path = influence_path(constraint.name)
    if old_path == new_path:
        return
    for fcurve in iter_fcurves(obj):
        if fcurve.data_path == old_path:
            fcurve.data_path = new_path


def remove_slot_data(obj, slot_name):
    """Remove a slot's constraint and its influence F-Curves."""
    constraint = obj.constraints.get(constraint_name(slot_name))
    if constraint is None:
        return
    remove_fcurves(obj, influence_path(constraint.name))
    obj.constraints.remove(constraint)


# ===========================================================================
#  F-Curves — compatibility layer 3.6 / 4.0-4.3 / 4.4+ (slots)
# ===========================================================================

def escape_identifier(name):
    """Escape a name for insertion into an RNA data_path."""
    escape = getattr(bpy.utils, "escape_identifier", None)
    if escape is not None:
        return escape(name)
    return name.replace("\\", "\\\\").replace('"', '\\"')


def influence_path(constraint_name_):
    """data_path of the influence of an **object** constraint."""
    return 'constraints["%s"].influence' % escape_identifier(constraint_name_)


def fcurve_collection(id_data):
    """F-Curve collection of the action of `id_data` (or None).

    Blender 4.4 introduced layered/slotted actions; in 5.x `action.fcurves`
    no longer exists at all. So we first look for the channelbag of the
    assigned slot, then fall back to the historical API. Every F-Curve
    access of the add-on goes through here.
    """
    anim_data = getattr(id_data, "animation_data", None)
    if anim_data is None or anim_data.action is None:
        return None
    action = anim_data.action

    slot = getattr(anim_data, "action_slot", None)
    if slot is not None and hasattr(action, "layers"):
        try:
            for layer in action.layers:
                for strip in layer.strips:
                    channelbag = strip.channelbag(slot)
                    if channelbag is not None:
                        return channelbag.fcurves
        except (AttributeError, TypeError, RuntimeError):
            pass

    return getattr(action, "fcurves", None)   # 3.6 → 4.3


def iter_fcurves(id_data):
    """Safe iterable over the F-Curves of `id_data` (empty if not animated)."""
    collection = fcurve_collection(id_data)
    return collection if collection is not None else ()


def find_fcurve(id_data, data_path, index=0):
    """F-Curve by data_path + component index (or None)."""
    for fcurve in iter_fcurves(id_data):
        if fcurve.data_path == data_path and fcurve.array_index == index:
            return fcurve
    return None


def evaluate_fcurve(id_data, data_path, index, frame, default=0.0):
    """Animated value at `frame`, or `default` if the channel is not animated."""
    fcurve = find_fcurve(id_data, data_path, index)
    return fcurve.evaluate(frame) if fcurve is not None else default


def remove_fcurves(id_data, data_path):
    """Remove every F-Curve (all components) of a data_path.

    The collection is re-read after each removal rather than iterating over a
    frozen list: references taken before a `remove()` are not guaranteed to
    stay valid.
    """
    collection = fcurve_collection(id_data)
    if collection is None:
        return 0
    removed = 0
    while True:
        match = next((fc for fc in collection if fc.data_path == data_path), None)
        if match is None:
            return removed
        try:
            collection.remove(match)
        except (RuntimeError, ReferenceError):
            return removed
        removed += 1


def owner_id(owner):
    """ID carrying the animation: the object itself, or the armature of a bone."""
    return owner.id_data


def owner_path(owner, property_path):
    """data_path of a property relative to the carrying ID.

    - Object   → ``location``
    - PoseBone → ``pose.bones["c_hand_ik.l"].location``
    """
    try:
        return owner.path_from_id(property_path)
    except (ValueError, AttributeError, TypeError):
        return property_path


def constraint_influence_path(constraint):
    """data_path of a constraint influence, object **or** bone constraint."""
    try:
        return constraint.path_from_id("influence")
    except (ValueError, AttributeError, TypeError):
        return influence_path(constraint.name)


def keying_group(owner):
    """Channel group (Dope Sheet): Blender convention."""
    if isinstance(owner, bpy.types.PoseBone):
        return owner.name
    return "Object Transforms"


def set_keys_interpolation(id_data, data_path, frame, interpolation='CONSTANT'):
    """Force the interpolation of the keys located at `frame` on **every**
    component of a data_path.

    The key is found by its `co.x`: `keyframe_points[-1]` is not necessarily
    the key just inserted. Since the user's "default interpolation"
    preference applies to RNA insertions, this explicit pass is essential.
    """
    for fcurve in iter_fcurves(id_data):
        if fcurve.data_path != data_path:
            continue
        touched = False
        for keyframe in fcurve.keyframe_points:
            if abs(keyframe.co.x - frame) < 1e-4:
                keyframe.interpolation = interpolation
                touched = True
        if touched:
            fcurve.update()


# ===========================================================================
#  Key insertion (owner = Object or PoseBone; object or bone constraint)
# ===========================================================================

def insert_influence_key(constraint, value, frame, interpolation='CONSTANT'):
    """Insert an influence key at `frame`, sharp transition by default."""
    constraint.influence = value
    constraint.keyframe_insert(data_path="influence", frame=frame)
    set_keys_interpolation(constraint.id_data, constraint_influence_path(constraint),
                           frame, interpolation)


def hold_influence(constraint, frame, interpolation='CONSTANT'):
    """Freeze at `frame` the influence value **already in place** (evaluated
    from the F-Curve, else the current value). Used on `frame - 1`."""
    path = constraint_influence_path(constraint)
    value = evaluate_fcurve(constraint.id_data, path, 0, frame,
                            default=constraint.influence)
    insert_influence_key(constraint, value, frame, interpolation)


def transform_channels(owner):
    """loc/rot/scale channels to animate according to the owner's rotation mode."""
    channels = [("location", 3)]
    mode = owner.rotation_mode
    if mode == 'QUATERNION':
        channels.append(("rotation_quaternion", 4))
    elif mode == 'AXIS_ANGLE':
        channels.append(("rotation_axis_angle", 4))
    else:
        channels.append(("rotation_euler", 3))
    channels.append(("scale", 3))
    return channels


def default_key_interpolation():
    """The user's "New Interpolation Type" preference (BEZIER by default)."""
    try:
        return bpy.context.preferences.edit.keyframe_new_interpolation_type
    except AttributeError:
        return 'BEZIER'


def insert_transform_keys(owner, frame, interpolation=None):
    """LocRotScale keys at `frame` on an object or a pose bone.

    `interpolation=None` applies the user's default interpolation
    **explicitly** (an animator working in stepped is not overruled): Blender
    copies the interpolation of the previous key onto every new key
    (measured in 5.2), so the CONSTANT hold key at `frame - 1` would
    otherwise propagate to the transfer key and then to every key the
    animator inserts afterwards — stepped motion after each transfer. The
    throw passes 'LINEAR'.
    """
    id_data = owner_id(owner)
    group = keying_group(owner)
    mode = interpolation or default_key_interpolation()
    for path, _size in transform_channels(owner):
        owner.keyframe_insert(data_path=path, frame=frame, group=group)
        set_keys_interpolation(id_data, owner_path(owner, path), frame, mode)


def hold_transform(owner, frame):
    """CONSTANT "hold" key on loc/rot/scale at `frame` (= `frame - 1`).

    The held value is the F-Curve **evaluated at that frame** if it exists,
    else the current value. Never the current frame's value: it would corrupt
    the previous motion. Without this hold, Blender would interpolate between
    two spaces (world → bone local) and the prop would drift before even
    being taken.
    """
    id_data = owner_id(owner)
    group = keying_group(owner)
    for path, size in transform_channels(owner):
        full_path = owner_path(owner, path)
        values = getattr(owner, path)
        for index in range(size):
            fcurve = find_fcurve(id_data, full_path, index)
            if fcurve is not None:
                values[index] = fcurve.evaluate(frame)
        owner.keyframe_insert(data_path=path, frame=frame, group=group)
        set_keys_interpolation(id_data, full_path, frame, 'CONSTANT')


# ===========================================================================
#  Matrices: visual transform, targets, Child Of compensation
# ===========================================================================

def evaluated_matrix_world(depsgraph, obj):
    """*Visible* world matrix (parents and constraints applied)."""
    try:
        return obj.evaluated_get(depsgraph).matrix_world.copy()
    except (RuntimeError, ReferenceError):
        return obj.matrix_world.copy()


def target_world_matrix(depsgraph, target, subtarget=""):
    """World matrix of a constraint target: object, or armature bone.

    A missing bone is an **error** (None): falling back to the armature's
    matrix would produce a huge jump at the transfer.
    """
    if target is None:
        return None
    evaluated = target.evaluated_get(depsgraph)
    if subtarget:
        if evaluated.type != 'ARMATURE':
            return None
        pose_bone = evaluated.pose.bones.get(subtarget)
        if pose_bone is None:
            return None
        # Child Of convention for a bone target: armature @ bone pose.
        return evaluated.matrix_world @ pose_bone.matrix
    return evaluated.matrix_world.copy()


def constraint_parent_matrix(depsgraph, constraint):
    """Factor applied by a Child Of at influence 1: `target @ inverse`.

    Child Of computes `final_world = target_world @ inverse_matrix @ basis`.
    Returns None if the target is invalid.
    """
    world = target_world_matrix(depsgraph, constraint.target, constraint.subtarget)
    if world is None:
        return None
    return world @ constraint.inverse_matrix


def set_world_matrix(obj, world, constraint_matrix=None):
    """Place the object so that its final result is exactly `world`.

    Deterministic equivalent of `visual_transform_apply`, extended to the
    Child Of just activated: `basis = C⁻¹ @ wanted_world`. The `matrix_world`
    setter handles parenting (parent + matrix_parent_inverse) but not
    constraints, hence the pre-multiplication.
    """
    matrix = world.copy()
    if constraint_matrix is not None:
        matrix = constraint_matrix.inverted_safe() @ matrix
    obj.matrix_world = matrix


def visual_transform_apply_native(context, objects):
    """The real `bpy.ops.object.visual_transform_apply()`.

    It acts on the whole selection and requires Object mode: it gets a
    restricted context override. Reserved to the "Bake visual transform"
    button; transfers go through `set_world_matrix()`.
    """
    objects = [ob for ob in objects if ob is not None]
    if not objects:
        return False
    override = {
        "active_object": objects[0],
        "object": objects[0],
        "selected_objects": objects,
        "selected_editable_objects": objects,
    }
    try:
        if hasattr(context, "temp_override"):      # Blender 3.2+
            with context.temp_override(**override):
                bpy.ops.object.visual_transform_apply()
        else:                                      # override dict (historical)
            bpy.ops.object.visual_transform_apply(override)
    except RuntimeError:
        return False
    return True


def tag_update(context, obj):
    """Re-run the evaluation: the displayed state becomes the keyed one again."""
    if obj is not None:
        obj.update_tag()
    context.scene.frame_set(context.scene.frame_current)


# ===========================================================================
#  Throw physics
# ===========================================================================

def scene_fps(scene):
    """Real frames per second (`fps / fps_base`)."""
    return scene.render.fps / max(scene.render.fps_base, 1e-6)


def gravity_in_blender_units(scene, gravity):
    """m/s² → Blender units/s² according to `Unit Scale`."""
    scale = getattr(scene.unit_settings, "scale_length", 1.0) or 1.0
    return gravity / scale


def measure_velocity(context, obj, frame, samples=2):
    """World velocity of the prop just before `frame` (units/second).

    Temporarily moves the playhead: call it **before** writing any key, and
    always restore the frame (finally).
    """
    scene = context.scene
    original = scene.frame_current
    samples = max(1, int(samples))
    try:
        scene.frame_set(frame - samples)
        first = evaluated_matrix_world(context.evaluated_depsgraph_get(), obj).translation.copy()
        scene.frame_set(frame)
        last = evaluated_matrix_world(context.evaluated_depsgraph_get(), obj).translation.copy()
    finally:
        scene.frame_set(original)
    delta_time = samples / scene_fps(scene)
    if delta_time <= 0.0:
        return Vector((0.0, 0.0, 0.0))
    return (last - first) / delta_time


# ===========================================================================
#  JSON serialization (ID props refuse lists of dicts and strings)
# ===========================================================================

def _read_json_list(owner, key):
    """Read a JSON ID prop → list of dicts (never None)."""
    raw = owner.get(key) if owner is not None else None
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
    else:
        try:
            data = list(raw)     # tolerance: real ID prop array
        except TypeError:
            return []
    return [entry for entry in data if isinstance(entry, dict)]


def store_slots_id_prop(obj):
    """Write `obj["ph_slots"]` (JSON mirror of the CollectionProperty)."""
    settings = get_obj_settings(obj)
    if settings is None:
        return
    payload = [
        {
            "name": slot.name,
            "label": slot.label,
            "target": slot.target.name if slot.target else "",
            "subtarget": slot.subtarget,
        }
        for slot in settings.slots
    ]
    obj[ID_PROP_SLOTS] = json.dumps(payload, ensure_ascii=False)
    obj[ID_PROP_VERSION] = DATA_VERSION


def read_slots_id_prop(obj):
    """Re-read `obj["ph_slots"]` → list of dicts (never None)."""
    return _read_json_list(obj, ID_PROP_SLOTS)


def load_slots_id_prop(obj):
    """Rebuild the slot collection from `obj["ph_slots"]`.

    Useful if the .blend was opened without the add-on. Returns the number of
    slots restored.
    """
    settings = get_obj_settings(obj)
    entries = read_slots_id_prop(obj)
    if settings is None or not entries:
        return 0

    settings.slots.clear()
    for entry in entries:
        slot = settings.slots.add()
        slot.name = sanitize_slot_name(str(entry.get("name", "Slot")))
        slot.stored_name = slot.name
        slot.label = str(entry.get("label", ""))
        target_name = str(entry.get("target", ""))
        slot.target = bpy.data.objects.get(target_name) if target_name else None
        slot.subtarget = str(entry.get("subtarget", ""))

    settings.active_slot_index = max(0, min(settings.active_slot_index, len(settings.slots) - 1))
    ensure_constraints(obj)
    store_slots_id_prop(obj)
    return len(entries)


# ===========================================================================
#  Transfer history
# ===========================================================================

def list_transfer_events(obj):
    """State changes read from the influence F-Curves.

    Returns ``[{"frame": int, "slots": tuple, "label": str}, …]``. Only the
    frames where the state really changes are kept: the hold keys at
    `frame - 1` reproduce the previous state and vanish by themselves.
    """
    curves = {}
    for constraint in iter_ph_constraints(obj):
        fcurve = find_fcurve(obj, influence_path(constraint.name), 0)
        if fcurve is not None and len(fcurve.keyframe_points):
            curves[slot_name_from_constraint(constraint)] = fcurve
    if not curves:
        return []

    frames = sorted({
        int(round(keyframe.co.x))
        for fcurve in curves.values()
        for keyframe in fcurve.keyframe_points
    })

    events, previous = [], frozenset()
    for frame in frames:
        state = frozenset(slot for slot, fcurve in curves.items()
                          if fcurve.evaluate(frame) > ACTIVE_THRESHOLD)
        if state == previous:
            continue
        if state:
            label = " + ".join(sorted(slot_label_for(obj, slot) for slot in state))
        else:
            label = "Released"
        events.append({"frame": frame, "slots": tuple(sorted(state)), "label": label})
        previous = state
    return events


# ===========================================================================
#  SECTION 2 — Two-Hand Grip
# ===========================================================================
#
#  Direction of the dependencies: section 1 = prop → hand; section 2 =
#  hand → prop. Both on the same hand = cycle. Every anti-cycle rule at the
#  end of the module follows from that.
# ---------------------------------------------------------------------------

def side_suffix(side):
    """'LEFT' → 'L', 'RIGHT' → 'R'."""
    return "L" if side == 'LEFT' else "R"


def side_label(side):
    """Readable label of a side."""
    return "left hand" if side == 'LEFT' else "right hand"


def other_side(side):
    """The other hand."""
    return 'RIGHT' if side == 'LEFT' else 'LEFT'


def grip_empty_basename(side):
    """Wanted name of the grip point (Blender may suffix it: the real one is stored)."""
    return "Grip_" + side_suffix(side)


def grip_zone_basename(prop):
    """Wanted name of the zone sphere."""
    return "GripZone_" + prop.name


def frame_helper_basename(side):
    """Wanted name of the "channel frame" empty."""
    return "PH_Frame_" + side_suffix(side)


def grip_local_helper_basename(side):
    """Wanted name of the "grip local" empty."""
    return "PH_GripLocal_" + side_suffix(side)


def twohand_constraint_name(side):
    """Attach Child Of: `PH_TwoHand_L` / `_R`."""
    return PH_TWOHAND_PREFIX + side_suffix(side)


def gripblend_constraint_name(side):
    """Grip Zone Copy Transforms: `PH_GripBlend_L` / `_R`."""
    return PH_GRIPBLEND_PREFIX + side_suffix(side)


# --- IK handles: (object, bone) ---------------------------------------------

def get_ik_handle(prop, side):
    """``(object, bone_name)`` pair of one side's IK target (empty bone = object)."""
    settings = get_obj_settings(prop)
    if settings is None:
        return None, ""
    if side == 'LEFT':
        return settings.ik_object_l, (settings.ik_bone_l or "")
    return settings.ik_object_r, (settings.ik_bone_r or "")


def resolve_ik_owner(ik_object, ik_bone):
    """Actual constraint owner: the PoseBone if a bone is given, else the
    object. None if the reference is invalid."""
    if ik_object is None:
        return None
    if ik_bone:
        if ik_object.type != 'ARMATURE' or ik_object.pose is None:
            return None
        return ik_object.pose.bones.get(ik_bone)
    return ik_object


def ik_handle_owner(prop, side):
    """Shortcut: constraint owner of one side's IK target."""
    ik_object, ik_bone = get_ik_handle(prop, side)
    return resolve_ik_owner(ik_object, ik_bone)


def ik_handle_label(prop, side):
    """Readable text describing the configured IK target."""
    ik_object, ik_bone = get_ik_handle(prop, side)
    if ik_object is None:
        return "undefined"
    return "%s › %s" % (ik_object.name, ik_bone) if ik_bone else ik_object.name


def ik_handle_error(prop, side):
    """Error message if one side's IK target is unusable, else None."""
    ik_object, ik_bone = get_ik_handle(prop, side)
    if ik_object is None:
        return "IK target of the %s is not set." % side_label(side)
    if ik_bone and resolve_ik_owner(ik_object, ik_bone) is None:
        return "Bone \"%s\" not found in %s." % (ik_bone, ik_object.name)
    return None


def owner_world_matrix(depsgraph, owner):
    """Evaluated world matrix of an owner (Object or PoseBone), or None."""
    if owner is None:
        return None
    if isinstance(owner, bpy.types.PoseBone):
        armature = owner.id_data.evaluated_get(depsgraph)
        pose_bone = armature.pose.bones.get(owner.name)
        if pose_bone is None:
            return None
        return armature.matrix_world @ pose_bone.matrix
    return evaluated_matrix_world(depsgraph, owner)


def set_owner_world_matrix(owner, world, constraint_matrix=None):
    """Place an owner so that its final result is exactly `world`.

    Same compensation as `set_world_matrix()`. `PoseBone.matrix` is expressed
    in armature space and its setter ignores constraints, exactly like
    `Object.matrix_world`.
    """
    matrix = world.copy()
    if constraint_matrix is not None:
        matrix = constraint_matrix.inverted_safe() @ matrix
    if isinstance(owner, bpy.types.PoseBone):
        owner.matrix = owner.id_data.matrix_world.inverted_safe() @ matrix
    else:
        owner.matrix_world = matrix


# --- Constraints: generic helpers -------------------------------------------

def iter_prefixed_constraints(owner, prefix):
    """Constraints of an owner whose name starts with `prefix`."""
    if owner is None:
        return
    for constraint in owner.constraints:
        if constraint.name.startswith(prefix):
            yield constraint


def get_constraint(owner, name):
    """Constraint of an owner by name (or None)."""
    if owner is None:
        return None
    return owner.constraints.get(name)


def set_constraint_enabled(constraint, state):
    """Enable/disable: `enabled` (4.x+) else inverted `mute` (3.6)."""
    if constraint is None:
        return
    if hasattr(constraint, "enabled"):
        constraint.enabled = bool(state)
    elif hasattr(constraint, "mute"):
        constraint.mute = not bool(state)


def is_constraint_enabled(constraint):
    """Activation state, 3.6 / 4.x+ compatible."""
    if constraint is None:
        return False
    if hasattr(constraint, "enabled"):
        return bool(constraint.enabled)
    if hasattr(constraint, "mute"):
        return not bool(constraint.mute)
    return True


# --- Drivers ---------------------------------------------------------------

def clear_rna_collection(collection):
    """Empty an RNA collection (keyframes, modifiers, variables…).

    Never ``for x in list(coll): coll.remove(x)``: each `remove()`
    reallocates the C array and the second reference is already stale
    ("Keyframe not in F-Curve"). Native `clear()` when it exists, else
    removal of the last element with an explicit upper bound.
    """
    clear = getattr(collection, "clear", None)
    if callable(clear):
        try:
            clear()
            return
        except (RuntimeError, TypeError):
            pass
    for _ in range(len(collection)):
        if not len(collection):
            break
        collection.remove(collection[-1])


def remove_driver(owner, data_path, index=-1):
    """Remove a driver if it exists (silent otherwise)."""
    try:
        if index >= 0:
            owner.driver_remove(data_path, index)
        else:
            owner.driver_remove(data_path)
    except (RuntimeError, TypeError, AttributeError):
        pass


def new_scripted_driver(owner, data_path, index=-1):
    """Blank driver F-Curve (no modifier, no key, no variable).

    A new driver F-Curve may carry keys or a modifier that would remap the
    computed value: start from a clean slate. Returns ``(fcurve, driver)``.
    """
    fcurve = owner.driver_add(data_path, index) if index >= 0 else owner.driver_add(data_path)
    clear_rna_collection(fcurve.modifiers)
    clear_rna_collection(fcurve.keyframe_points)
    driver = fcurve.driver
    driver.type = 'SCRIPTED'
    clear_rna_collection(driver.variables)
    return fcurve, driver


def driver_var_transform(driver, name, id_, transform_type, space, bone_target="", rotation_mode=None):
    """Transform Channel variable."""
    variable = driver.variables.new()
    variable.name = name
    variable.type = 'TRANSFORMS'
    target = variable.targets[0]
    target.id = id_
    if bone_target:
        target.bone_target = bone_target
    target.transform_type = transform_type
    target.transform_space = space
    if rotation_mode is not None:
        target.rotation_mode = rotation_mode
    return variable


def driver_var_prop(driver, name, id_, data_path):
    """Single Property variable on an object."""
    variable = driver.variables.new()
    variable.name = name
    variable.type = 'SINGLE_PROP'
    target = variable.targets[0]
    target.id_type = 'OBJECT'
    target.id = id_
    target.data_path = data_path
    return variable


def add_grip_driver(constraint, ik_object, ik_bone, frame, grip_local, prop):
    """Proximity driver on the influence of a Grip Zone constraint.

    The driver reads **itself** the `location` channels of its IK target
    (depsgraph "same bone" / "same ID" exemption: no relation created), and
    compares them with the position of the "grip local" empty expressed in
    the frame of those channels (`LOCAL_SPACE` of a child of the "frame").
    Neither the frame nor the grip local depends on the bone: no cycle.
    See README for the three architectures that failed.
    """
    remove_driver(constraint, "influence")
    fcurve, driver = new_scripted_driver(constraint, "influence")

    # Animated channels of the IK target, read by itself: Transform Channel
    # variable in Transform Space (= the loc channels, without constraints).
    #
    # ⚠️ No Single Property `pose.bones["B"].location[i]`: that variable is
    # linked to BONE_LOCAL(B), which the driver also feeds → two-node cycle,
    # measured in Blender 5.2.1 (see README). Only the BONE_DONE(B) →
    # BONE_LOCAL(B) relation of the same bone is exempted
    # (`is_same_bone_dependency`), and that is the one of a Transform Channel
    # variable targeting the bone. For an object the exemption is "same ID".
    for index, name in enumerate(("bx", "by", "bz")):
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'TRANSFORMS'
        target = variable.targets[0]
        target.id = ik_object
        if ik_bone:
            target.bone_target = ik_bone
        target.transform_type = ('LOC_X', 'LOC_Y', 'LOC_Z')[index]
        target.transform_space = 'TRANSFORM_SPACE'

    # Grip point in the channel frame: Local Space of the grip local
    # = (frame @ parent_inverse)⁻¹ @ world = frame⁻¹ @ grip_world.
    for index, name in enumerate(("gx", "gy", "gz")):
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'TRANSFORMS'
        target = variable.targets[0]
        target.id = grip_local
        target.transform_type = ('LOC_X', 'LOC_Y', 'LOC_Z')[index]
        target.transform_space = 'LOCAL_SPACE'

    # World scale of the frame (scaled rig or parent).
    scale = driver.variables.new()
    scale.name = "s"
    scale.type = 'TRANSFORMS'
    scale.targets[0].id = frame
    scale.targets[0].transform_type = 'SCALE_X'
    scale.targets[0].transform_space = 'WORLD_SPACE'

    # Zone radius and Lock, read on the prop.
    for name, key in (("radius", ID_PROP_GRIP_RADIUS), ("lock", ID_PROP_GRIP_LOCK)):
        variable = driver.variables.new()
        variable.name = name
        variable.type = 'SINGLE_PROP'
        variable.targets[0].id_type = 'OBJECT'
        variable.targets[0].id = prop
        variable.targets[0].data_path = '["%s"]' % key

    driver.expression = GRIP_EXPRESSION
    return fcurve


# --- Keyframable custom properties (Lock) -------------------------------------

def id_prop_path(key):
    """RNA data_path of a custom property: `["key"]`."""
    return '["%s"]' % escape_identifier(key)


def insert_id_prop_key(owner, key, value, frame, interpolation='CONSTANT'):
    """Insert a key on a (float) custom property with forced interpolation."""
    owner[key] = float(value)
    owner.keyframe_insert(data_path=id_prop_path(key), frame=frame)
    set_keys_interpolation(owner, id_prop_path(key), frame, interpolation)


def hold_id_prop(owner, key, frame, interpolation='CONSTANT'):
    """Freeze at `frame` the value already in place of a custom property
    (evaluated from its F-Curve, else the current value)."""
    default = float(owner.get(key, 0.0))
    value = evaluate_fcurve(owner, id_prop_path(key), 0, frame, default=default)
    insert_id_prop_key(owner, key, value, frame, interpolation)


def grip_lock_active(prop):
    """True if the Lock is 1 at the current frame (animated value of the ID prop)."""
    try:
        return float(prop.get(ID_PROP_GRIP_LOCK, 0.0)) > ACTIVE_THRESHOLD
    except (TypeError, ValueError):
        return False


def grip_zone_wanted(prop):
    """True if the user asked for the Grip Zone (Enable / Lock) without
    removing it since (Disable / Release / Attach)."""
    try:
        return bool(int(prop.get(ID_PROP_ZONE_WANTED, 0)))
    except (TypeError, ValueError):
        return False


def set_grip_zone_wanted(prop, wanted):
    prop[ID_PROP_ZONE_WANTED] = 1 if wanted else 0


# --- Unkeyed interactive edits ------------------------------------------------

def has_unkeyed_edit(owner, frame, tolerance=1e-6):
    """True if an animated channel of the owner differs from its keyed value
    at the frame: the user moved the object/bone without inserting a key
    (auto-key off). An internal `frame_set` (bake, tag_update) would erase
    that move; the operators freeze it first (`freeze_unkeyed_edit`)."""
    if owner is None:
        return False
    id_data = owner_id(owner)
    for path, size in transform_channels(owner):
        full_path = owner_path(owner, path)
        values = getattr(owner, path)
        for index in range(size):
            fcurve = find_fcurve(id_data, full_path, index)
            if fcurve is not None and abs(fcurve.evaluate(frame) - values[index]) > tolerance:
                return True
    return False


def freeze_unkeyed_edit(owner, frame):
    """Make an unkeyed move permanent: hold at `frame − 1` with the values of
    the existing animation, then key the current values at `frame`. Returns
    True if something was frozen."""
    if not has_unkeyed_edit(owner, frame):
        return False
    current = {path: list(getattr(owner, path)) for path, _size in transform_channels(owner)}
    hold_transform(owner, frame - 1)
    for path, values in current.items():
        target = getattr(owner, path)
        for index, value in enumerate(values):
            target[index] = value
    insert_transform_keys(owner, frame)
    return True


# --- Arm reach: IK chain, helpers, constraint ---------------------------------

def ik_chain_bones(pose_bone, constraint):
    """Bones of an IK chain, from the tip (constraint owner) to the root;
    `chain_count` 0 = up to the root bone."""
    chain, bone = [], pose_bone
    count = constraint.chain_count or 255
    while bone is not None and len(chain) < count:
        chain.append(bone)
        bone = bone.parent
    return chain


def find_ik_chain(ik_object, ik_bone):
    """IK chain driven by this bone: ``(chain_bones, shoulder)``.

    `chain_bones` goes from the tip (forearm) to the root (upper arm);
    `shoulder` is the first ancestor of the root, outside the chain, that
    depends neither on the controller nor on the IK target — the reach
    helpers follow it (the root itself depends on the IK target: reading it
    would create a cycle).

    The IK constraint may target the controller itself (Auto-Rig Pro:
    `c_hand_ik.l`) or a bone that depends on it (Rigify:
    `MCH-upper_arm_ik_target.L`, child of `hand_ik.L`). Likewise the shoulder
    is not always the direct parent of the root: Rigify inserts
    `MCH-upper_arm_ik_swing.L`, whose Damped Track targets the IK target; we
    then climb up to `MCH-upper_arm_parent.L`. Among several chains (Auto-Rig
    Pro has a "stretch" and a "nostr" one), the one targeting the controller
    directly is preferred, then the one whose root starts exactly at the tail
    of its parent. None if nothing is found.
    """
    if ik_object is None or ik_object.type != 'ARMATURE' or not ik_bone:
        return None
    if ik_object.pose is None or ik_bone not in ik_object.pose.bones:
        return None
    candidates = []
    for pose_bone in ik_object.pose.bones:
        for constraint in pose_bone.constraints:
            if constraint.type != 'IK' or constraint.target is not ik_object:
                continue
            subtarget = constraint.subtarget
            if not subtarget:
                continue
            direct = subtarget == ik_bone
            if not direct and not bone_depends_on(ik_object, subtarget, ik_bone):
                continue
            chain = ik_chain_bones(pose_bone, constraint)
            root = chain[-1]
            shoulder = root.parent
            while (shoulder is not None and shoulder not in chain
                   and (bone_depends_on(ik_object, shoulder.name, ik_bone)
                        or bone_depends_on(ik_object, shoulder.name, subtarget))):
                shoulder = shoulder.parent
            if shoulder is None or shoulder in chain:
                continue
            gap = (root.bone.head_local - root.parent.bone.tail_local).length
            candidates.append((0 if direct else 1, gap, len(candidates), chain, shoulder))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    _direct, _gap, _index, chain, shoulder = candidates[0]
    return chain, shoulder


def chain_reach(ik_object, chain, margin):
    """Reach of an IK chain in world units: sum of the rest lengths × world
    scale of the armature × (1 − margin)."""
    rest_length = sum(bone.bone.length for bone in chain)
    scale = ik_object.matrix_world.to_scale()
    world_scale = (abs(scale.x) + abs(scale.y) + abs(scale.z)) / 3.0
    return rest_length * world_scale * max(0.0, 1.0 - margin)


def reach_constraint(prop):
    """Reach Limit Location constraint of the prop (or None)."""
    if prop is None:
        return None
    constraint = prop.constraints.get(REACH_CONSTRAINT)
    return constraint if constraint is not None and constraint.type == 'LIMIT_LOCATION' else None


def reach_limit_present(prop):
    """True if the reach limit is installed on this prop."""
    return reach_constraint(prop) is not None


def reach_sides(prop):
    """Sides whose reach is installed (helper present)."""
    return [side for side in SIDES if find_helper(prop, ID_PROP_REACH_HELPER[side]) is not None]


def reach_pass_props(index):
    """Driven properties of projection pass `index` (1-based):
    ``(distance, factor, tx, ty, tz)``."""
    base = "%s%d" % (REACH_PASS_PREFIX, index)
    return (base + "d", base + "f", base + "x", base + "y", base + "z")


def master_reach_constraints(owner):
    """Limit Distance constraints of the master hand on a controller (or [])."""
    if owner is None:
        return []
    return [constraint for constraint in owner.constraints
            if constraint.name.startswith(REACH_MASTER_PREFIX)]


def remove_master_reach(prop, sides=SIDES):
    """Remove the master-hand reach constraints (and their influence keys)
    from the controllers of `sides`, plus the reach target helpers. No bake:
    the operators bake first when history matters. Returns True if something
    was removed."""
    removed = False
    for side in sides:
        owner = ik_handle_owner(prop, side)
        for constraint in list(master_reach_constraints(owner)):
            remove_fcurves(owner.id_data, constraint_influence_path(constraint))
            owner.constraints.remove(constraint)
            removed = True
        if find_helper(prop, ID_PROP_REACH_TARGET[side]) is not None:
            removed = True
        remove_helper(prop, ID_PROP_REACH_TARGET[side])
    return removed


def remove_reach_limit(prop):
    """Remove the reach limit: constraint, drivers, custom properties,
    helpers, master-hand constraints. Returns True if something was removed."""
    if prop is None:
        return False
    removed = remove_master_reach(prop)
    constraint = reach_constraint(prop)
    if constraint is not None:
        for attribute in ("min_x", "max_x", "min_y", "max_y", "min_z", "max_z", "influence"):
            remove_driver(constraint, attribute)
        prop.constraints.remove(constraint)
        removed = True
    keys = list(REACH_TRANSLATION_PROPS)
    for side in SIDES:
        keys.extend(REACH_CHAIN_PROPS[side])
        keys.append(ID_PROP_REACH[side])
    keys.extend(key for key in prop.keys() if str(key).startswith(REACH_PASS_PREFIX))
    for key in keys:
        if key in prop.keys():
            remove_driver(prop, id_prop_path(key))
            del prop[key]
            removed = True
    if ID_PROP_REACH_GATE in prop.keys():
        remove_fcurves(prop, id_prop_path(ID_PROP_REACH_GATE))
        del prop[ID_PROP_REACH_GATE]
    for side in SIDES:
        if find_helper(prop, ID_PROP_REACH_HELPER[side]) is not None:
            removed = True
        remove_helper(prop, ID_PROP_REACH_HELPER[side])
    return removed


# --- Grip points -----------------------------------------------------------

def matrix_to_list(matrix):
    """4x4 matrix → 16 floats, row by row."""
    return [float(value) for row in matrix for value in row]


def list_to_matrix(values):
    """16 floats → 4x4 matrix (None if the data is invalid)."""
    try:
        numbers = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if len(numbers) != 16:
        return None
    return Matrix([numbers[0:4], numbers[4:8], numbers[8:12], numbers[12:16]])


def read_grip_points(prop):
    """`prop["ph_grip_points"]` → list of dicts (never None)."""
    return _read_json_list(prop, ID_PROP_GRIP_POINTS)


def write_grip_points(prop, entries):
    """Write the grip point list as JSON."""
    prop[ID_PROP_GRIP_POINTS] = json.dumps(entries, ensure_ascii=False)


def get_grip_entry(prop, side):
    """`ph_grip_points` entry of one side (or None)."""
    for entry in read_grip_points(prop):
        if entry.get("hand") == side:
            return entry
    return None


def store_grip_point(prop, side, empty, matrix_local):
    """Update (or create) one side's entry: real name + local matrix."""
    entries = [entry for entry in read_grip_points(prop) if entry.get("hand") != side]
    entries.append({
        "name": empty.name,
        "hand": side,
        "matrix_local": matrix_to_list(matrix_local),
    })
    entries.sort(key=lambda entry: 0 if entry.get("hand") == 'LEFT' else 1)
    write_grip_points(prop, entries)


def find_grip_empty(prop, side):
    """Grip point of one side: by the stored name, else among the children."""
    if prop is None:
        return None
    entry = get_grip_entry(prop, side)
    if entry:
        candidate = bpy.data.objects.get(entry.get("name", ""))
        if candidate is not None and candidate.parent is prop:
            return candidate
    basename = grip_empty_basename(side)
    for child in prop.children:
        if child.type == 'EMPTY' and child.name.startswith(basename):
            return child
    return None


def find_grip_zone(prop):
    """Zone visualization sphere (or None)."""
    if prop is None:
        return None
    for child in prop.children:
        if child.type == 'EMPTY' and child.name.startswith("GripZone_"):
            return child
    return None


def has_grip_pose(prop, side):
    """True if a valid grip pose is stored for this side."""
    entry = get_grip_entry(prop, side)
    return bool(entry) and list_to_matrix(entry.get("matrix_local", [])) is not None


def is_twohand_setup(prop):
    """True if both grip points exist."""
    return all(find_grip_empty(prop, side) is not None for side in SIDES)


def grip_zone_radius(prop):
    """Current radius, read from the custom property (the one the drivers read)."""
    value = prop.get(ID_PROP_GRIP_RADIUS) if prop is not None else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_GRIP_RADIUS


def sync_twohand_id_props(prop):
    """Mirror the RNA settings into the prop's custom properties.

    `ph_grip_zone_radius` must be a native float: the drivers read it
    directly, the sync follows every change of the slider.
    """
    settings = get_obj_settings(prop)
    if settings is None:
        return
    prop[ID_PROP_GRIP_RADIUS] = float(settings.grip_zone_radius)
    if ID_PROP_GRIP_LOCK not in prop.keys():
        prop[ID_PROP_GRIP_LOCK] = 0.0          # read by the drivers: native float
    for side in SIDES:
        ik_object, ik_bone = get_ik_handle(prop, side)
        prop[ID_PROP_IK_TARGET[side]] = ik_object.name if ik_object else ""
        prop[ID_PROP_IK_BONE[side]] = ik_bone


# --- Grip Zone helper empties -------------------------------------------------

def find_helper(prop, key):
    """Helper empty found by the name stored under `key`."""
    if prop is None:
        return None
    name = prop.get(key, "")
    return bpy.data.objects.get(name) if isinstance(name, str) and name else None


def remove_helper(prop, key):
    """Remove a helper empty and forget its reference."""
    helper = find_helper(prop, key)
    if helper is not None:
        try:
            bpy.data.objects.remove(helper, do_unlink=True)
        except (RuntimeError, ReferenceError):
            pass
    if key in prop.keys():
        del prop[key]


def helpers_present(prop, side):
    """True if at least one of the two helper empties of a side exists."""
    return (find_helper(prop, ID_PROP_FRAME[side]) is not None
            or find_helper(prop, ID_PROP_GRIP_LOCAL[side]) is not None)


def remove_helpers(prop, side):
    """Remove both helper empties (grip local before frame: it is its child)."""
    remove_helper(prop, ID_PROP_GRIP_LOCAL[side])
    remove_helper(prop, ID_PROP_FRAME[side])


# --- Section 2 state ----------------------------------------------------------

def grip_blend_constraint(prop, side):
    """Grip Zone constraint of one side (enabled or not), or None."""
    return get_constraint(ik_handle_owner(prop, side), gripblend_constraint_name(side))


def grip_blend_present(prop, side):
    """True if the Grip Zone constraint exists, enabled or not."""
    return grip_blend_constraint(prop, side) is not None


def grip_zone_active(prop, side):
    """True if the Grip Zone is in place **and enabled**: the panel must
    reflect the real state, not mere presence."""
    return is_constraint_enabled(grip_blend_constraint(prop, side))


def attach_constraint(prop, side):
    """Attach Child Of of one side (or None)."""
    return get_constraint(ik_handle_owner(prop, side), twohand_constraint_name(side))


def is_hand_attached(prop, side):
    """True if the hand is attached to the prop at the current frame."""
    constraint = attach_constraint(prop, side)
    return constraint is not None and constraint.influence > ACTIVE_THRESHOLD


def hand_follows_prop(prop, side):
    """True if the hand follows the prop (active Grip Zone or attach)."""
    return grip_zone_active(prop, side) or is_hand_attached(prop, side)


def remove_grip_blend(prop, side):
    """Remove one side's Grip Zone: constraint, driver, helper empties.

    Use this — not "disable" — as soon as the prop follows that hand: a
    disabled constraint keeps its driver, hence its dependencies, and the
    hand → prop → hand cycle persists. Returns True if something was removed.
    """
    owner = ik_handle_owner(prop, side)
    removed = False
    if owner is not None:
        for constraint in list(iter_prefixed_constraints(owner, PH_GRIPBLEND_PREFIX)):
            remove_driver(constraint, "influence")
            owner.constraints.remove(constraint)
            removed = True
    if helpers_present(prop, side):
        remove_helpers(prop, side)
        removed = True
    return removed


def armature_deforms_something(armature, scene):
    """True if an Armature modifier of the scene points to this armature.

    Spots a duplicate rig (`Akaza_rig.001`): right bone names, but no
    character follows it.
    """
    if armature is None or armature.type != 'ARMATURE':
        return True
    for obj in scene.objects:
        for modifier in getattr(obj, "modifiers", ()):
            if modifier.type == 'ARMATURE' and modifier.object is armature:
                return True
    return False


# --- Cycle detection ----------------------------------------------------------
#
#  Measured fact (Blender 5.2.1, see README): a constraint creates its
#  dependency relations as soon as it has a target — influence 0, `enabled =
#  False` or a driver change nothing; only removal and the absence of a target
#  cut them. The "presence" functions below therefore reason on the existence
#  of the constraints, `holding_side()` / `is_hand_attached()` on their
#  influence (animated state, for the interface).

_DEPENDENCY_CACHE = {}
#: The signature of a Rigify rig (700 bones) costs ~2 ms: between two close
#: checks (panel redraws, operator loops) the map is reused without being
#: recomputed.
DEPENDENCY_CACHE_SECONDS = 0.5


def _bone_name_from_path(data_path):
    """`pose.bones["X"]…` → "X", else None."""
    prefix = 'pose.bones["'
    if not data_path or not data_path.startswith(prefix):
        return None
    end = data_path.find('"]', len(prefix))
    return data_path[len(prefix):end] if end > 0 else None


def _constraint_targets(constraint):
    """(object, bone) pairs targeted by a constraint (multiple targets of the
    Armature constraint, target and pole of the others)."""
    if constraint.type == 'ARMATURE':
        return [(target.target, target.subtarget or "") for target in constraint.targets
                if target.target is not None]
    pairs = []
    for attribute, bone_attribute in (("target", "subtarget"), ("pole_target", "pole_subtarget")):
        target = getattr(constraint, attribute, None)
        if target is not None:
            pairs.append((target, getattr(constraint, bone_attribute, "") or ""))
    return pairs


def _dependency_signature(armature):
    """Cheap cache key: parenting structure and constraint targets."""
    anim = armature.animation_data
    parents = tuple(pb.parent.name if pb.parent else "" for pb in armature.pose.bones)
    targets = tuple((constraint.type, target.name, subtarget)
                    for pb in armature.pose.bones for constraint in pb.constraints
                    for target, subtarget in _constraint_targets(constraint))
    return (len(parents), hash(parents), hash(targets), len(anim.drivers) if anim else 0)


def bone_dependency_map(armature):
    """Direct bone → {bones} dependencies of an armature, rebuilt statically
    (same armature only): parent, constraint targets (a whole IK chain
    depends on its target and its pole), bones read by the drivers.

    Conservative like the depsgraph: influence and activation do not count.
    Cached as long as the parenting, the targets and the number of drivers do
    not change (signature re-checked at most every `DEPENDENCY_CACHE_SECONDS`).
    """
    now = time.monotonic()
    cached = _DEPENDENCY_CACHE.get(armature.name)
    if cached is not None and now - cached[2] < DEPENDENCY_CACHE_SECONDS:
        return cached[1]
    signature = _dependency_signature(armature)
    if cached is not None and cached[0] == signature:
        _DEPENDENCY_CACHE[armature.name] = (signature, cached[1], now)
        return cached[1]

    deps = {pb.name: set() for pb in armature.pose.bones}
    for pb in armature.pose.bones:
        if pb.parent is not None:
            deps[pb.name].add(pb.parent.name)
        for constraint in pb.constraints:
            targets = {subtarget for target, subtarget in _constraint_targets(constraint)
                       if target is armature and subtarget}
            if not targets:
                continue
            owners = ik_chain_bones(pb, constraint) if constraint.type == 'IK' else (pb,)
            for owner in owners:
                deps[owner.name].update(targets)
    anim = armature.animation_data
    if anim is not None:
        for fcurve in anim.drivers:
            owner = _bone_name_from_path(fcurve.data_path)
            if owner not in deps or fcurve.driver is None:
                continue
            for variable in fcurve.driver.variables:
                for target in variable.targets:
                    if target.id is not armature:
                        continue
                    name = target.bone_target or _bone_name_from_path(target.data_path)
                    if name:
                        deps[owner].add(name)
    _DEPENDENCY_CACHE[armature.name] = (signature, deps, now)
    return deps


def bone_depends_on(armature, bone_name, other_name):
    """True if bone `bone_name` depends (transitively) on `other_name`, or is
    the same bone."""
    if bone_name == other_name:
        return True
    deps = bone_dependency_map(armature)
    seen, stack = set(), [bone_name]
    while stack:
        for dep in deps.get(stack.pop(), ()):
            if dep == other_name:
                return True
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return False


def object_depends_on_handle(obj, ik_object, ik_bone=""):
    """True if the transform of object `obj` depends on the IK handle:
    parenting (to the bone, or to an object depending on it), constraint or
    driver towards it."""
    seen, stack = set(), [obj]
    while stack:
        current = stack.pop()
        if current is None or current is ik_object or current.name in seen:
            continue
        seen.add(current.name)
        parent = current.parent
        if parent is not None:
            if parent is ik_object:
                if not ik_bone:
                    return True
                if (current.parent_type == 'BONE' and current.parent_bone
                        and bone_depends_on(ik_object, current.parent_bone, ik_bone)):
                    return True
            else:
                stack.append(parent)
        for constraint in current.constraints:
            for target, subtarget in _constraint_targets(constraint):
                if target is ik_object:
                    if not ik_bone or (subtarget and bone_depends_on(ik_object, subtarget, ik_bone)):
                        return True
                else:
                    stack.append(target)
        anim = current.animation_data
        if anim is not None:
            for fcurve in anim.drivers:
                if fcurve.driver is None:
                    continue
                for variable in fcurve.driver.variables:
                    for target in variable.targets:
                        if target.id is ik_object:
                            name = target.bone_target or _bone_name_from_path(target.data_path)
                            if not ik_bone or (name and bone_depends_on(ik_object, name, ik_bone)):
                                return True
                        elif isinstance(target.id, bpy.types.Object):
                            stack.append(target.id)
    return False


def target_depends_on_handle(target, subtarget, ik_object, ik_bone=""):
    """True if a slot target `(target, subtarget)` depends on the IK handle
    `(ik_object, ik_bone)`: same bone, bone depending on it (deform bone
    `hand.l` copying `c_hand_ik.l`, Rigify's `DEF-hand.L`…), or object whose
    transform depends on it. An identical target depends on itself."""
    if target is None or ik_object is None:
        return False
    if target is ik_object:
        if not ik_bone:
            return True                     # the whole armature follows the object
        if not subtarget or ik_object.pose is None or subtarget not in ik_object.pose.bones:
            return False                    # the armature object does not depend on its bones
        return bone_depends_on(ik_object, subtarget, ik_bone)
    return object_depends_on_handle(target, ik_object, ik_bone)


def prop_grip_empties(prop):
    """The grip points of this prop (set of objects)."""
    return {grip for grip in (find_grip_empty(prop, side) for side in SIDES) if grip is not None}


def hand_constraints_targeting_prop(prop, ik_object, ik_bone=""):
    """`hand → prop` constraints carried by this IK handle: PH_TwoHand_* and
    PH_GripBlend_* whose target is a grip point of this prop, whatever their
    influence or activation."""
    owner = resolve_ik_owner(ik_object, ik_bone)
    if owner is None or prop is None:
        return []
    grips = prop_grip_empties(prop)
    if not grips:
        return []
    return [constraint for constraint in owner.constraints
            if constraint.name.startswith((PH_TWOHAND_PREFIX, PH_GRIPBLEND_PREFIX))
            and getattr(constraint, "target", None) in grips]


def prop_constraints_targeting_handle(prop, ik_object, ik_bone=""):
    """`prop → hand` slot constraints (Child Of PH_*) targeting exactly this
    IK handle, whatever their influence."""
    if prop is None or ik_object is None:
        return []
    return [constraint for constraint in iter_ph_constraints(prop)
            if constraint.target is ik_object
            and (constraint.subtarget or "") == (ik_bone or "")]


def prop_constraints_depending_on_handle(prop, ik_object, ik_bone=""):
    """`prop → target` slot constraints (Child Of PH_*) whose target depends
    on this IK handle (`target_depends_on_handle`): those that would close a
    cycle if the hand followed the prop. Includes exact targets."""
    if prop is None or ik_object is None:
        return []
    return [constraint for constraint in iter_ph_constraints(prop)
            if target_depends_on_handle(constraint.target, constraint.subtarget or "",
                                        ik_object, ik_bone)]


def prop_held_by_handle(prop, ik_object, ik_bone=""):
    """Does the prop follow *exactly* this IK handle (object **and** bone)?

    The bone is compared, not just the armature: the depsgraph is granular
    per bone, two hands of the same rig can hold opposite relations with the
    prop without a cycle.
    """
    if prop is None or ik_object is None:
        return False
    for constraint in iter_ph_constraints(prop):
        if (constraint.influence > ACTIVE_THRESHOLD
                and constraint.target is ik_object
                and (constraint.subtarget or "") == (ik_bone or "")):
            return True
    return False


def holding_side(prop):
    """Side whose IK handle currently carries the prop (section 1), or None.
    Always derived from the constraints, never stored."""
    constraint = get_slot_constraint(prop, active_slot_name(prop))
    if constraint is None:
        return None
    for side in SIDES:
        ik_object, ik_bone = get_ik_handle(prop, side)
        if (ik_object is not None and constraint.target is ik_object
                and (constraint.subtarget or "") == (ik_bone or "")):
            return side
    return None


def dependency_warning(prop, side=None):
    """Message if the configuration contains a cycle, else None.

    Case: a slot constraint targets a hand or a bone / object depending on it
    (section 1) **and** that hand carries a constraint towards the prop
    (attach or Grip Zone, section 2) — presence is enough, influence and
    activation do not count. The operators maintain the invariant; this
    message should only appear after a manual edit of the constraints.
    """
    if prop is None:
        return None
    for current in (SIDES if side is None else (side,)):
        ik_object, ik_bone = get_ik_handle(prop, current)
        if not hand_constraints_targeting_prop(prop, ik_object, ik_bone):
            continue
        if prop_constraints_depending_on_handle(prop, ik_object, ik_bone):
            return ("The %s carries the prop (slot, section 1) and follows it at "
                    "the same time (attach / Grip Zone): circular dependency. "
                    "Run \"Assign\", \"Master Hand\" or \"Attach\" again: the "
                    "add-on removes the direction that became useless."
                    % side_label(current))
    return None
