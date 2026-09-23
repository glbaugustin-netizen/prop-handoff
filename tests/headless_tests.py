# -*- coding: utf-8 -*-
"""PropHandoff headless tests (Blender --background --factory-startup).

Replays the specification protocol (§14) on a synthetic scene: a small rig
(including a parentless hand bone, like Auto-Rig Pro's `c_hand_ik.*`, an IK
arm, bones without Local Location and a Rigify-like IK chain), a ball, a
spear, object IK targets. Numerically checks:

  * no visual jump on Assign / Release / Throw / Attach / Release both hands;
  * the hold keys at frame-1 do not alter the previous motion;
  * the history rebuilt from the F-Curves;
  * the "frame" of the Grip Zone (bones with and without parent, with and
    without Local Location, posed and scaled rig);
  * the Grip Zone: influence 1 on the grip, 0 far away, no one-frame lag;
  * the static bone dependency analysis and the anti-cycle rules;
  * **no "Dependency cycle detected"** in Blender's C output (the detection
    itself is verified by a deliberate cycle at the end of the script).

Run (Windows, Blender from the Store):
    powershell -File tests/run_headless.ps1
Or directly:
    blender --background --factory-startup --python tests/headless_tests.py
The report is written to tests/headless_tests.log (stdout/stderr included).
"""

import importlib
import math
import os
import random
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_PATH = os.path.join(HERE, "headless_tests.log")

# --- capture of stdout/stderr at the C level (depsgraph messages) ----------
_log_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
os.dup2(_log_fd, 1)
os.dup2(_log_fd, 2)
sys.stdout = os.fdopen(1, "w", buffering=1, encoding="utf-8", errors="replace")
sys.stderr = sys.stdout

import bpy  # noqa: E402
from mathutils import Euler, Matrix, Vector  # noqa: E402

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
prop_handoff = importlib.import_module("prop_handoff")
utils = prop_handoff.utils
operators = prop_handoff.operators
panel = prop_handoff.panel

RESULTS = []
CYCLE_MARKER = "Dependency cycle detected"


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    RESULTS.append((status, label))
    print("[%s] %s%s" % (status, label, (" -- " + str(detail)) if (detail and not condition) else ""))
    return bool(condition)


def approx(a, b, tol=1e-4):
    return abs(a - b) <= tol


def mat_close(a, b, tol=1e-4):
    return all(abs(a[i][j] - b[i][j]) <= tol for i in range(4) for j in range(4))


def vec_close(a, b, tol=1e-4):
    return (Vector(a) - Vector(b)).length <= tol


def mat_str(m):
    return "T=%s" % (tuple(round(v, 4) for v in m.translation),)


def ctx():
    return bpy.context


def dg():
    return bpy.context.evaluated_depsgraph_get()


def world_of(obj):
    return utils.evaluated_matrix_world(dg(), obj)


def bone_world(arm, bone_name):
    return utils.owner_world_matrix(dg(), arm.pose.bones[bone_name])


def goto(frame):
    bpy.context.scene.frame_set(frame)


def run_op(operator, **kwargs):
    """In background mode an operator that reports an ERROR raises
    RuntimeError: translate it into {'CANCELLED'}."""
    try:
        return operator(**kwargs)
    except RuntimeError:
        return {'CANCELLED'}


def flush_c_output():
    sys.stdout.flush()
    try:
        os.fsync(1)
    except OSError:
        pass


def cycle_count():
    """Number of "Dependency cycle detected" logged so far (CLOG messages are
    written immediately to the descriptor)."""
    flush_c_output()
    with open(LOG_PATH, encoding="utf-8", errors="replace") as handle:
        return handle.read().count(CYCLE_MARKER)


#: Cycles deliberately created by the tests (fabricated conflict, final control).
EXPECTED_CYCLES = [0]


def log_has_cycle():
    """True if an unexpected cycle was logged."""
    return cycle_count() > EXPECTED_CYCLES[0]


# ===========================================================================
#  Scene construction
# ===========================================================================

def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


def make_mesh_object(name, size=(0.2, 0.2, 0.2)):
    """Centered box of half-dimensions `size` (real geometry, scale 1)."""
    mesh = bpy.data.meshes.new(name)
    sx, sy, sz = size
    verts = [(x, y, z) for x in (-sx, sx) for y in (-sy, sy) for z in (-sz, sz)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    return link(bpy.data.objects.new(name, mesh))


def make_empty(name):
    empty = link(bpy.data.objects.new(name, None))
    empty.empty_display_size = 0.1
    return empty


def make_armature(name, bones):
    """`bones`: list of (name, head, tail, roll, parent_name|None)."""
    data = bpy.data.armatures.new(name)
    arm = link(bpy.data.objects.new(name, data))
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    for bone_name, head, tail, roll, parent in bones:
        edit_bone = data.edit_bones.new(bone_name)
        edit_bone.head = head
        edit_bone.tail = tail
        edit_bone.roll = roll
        if parent:
            edit_bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    return arm


def build_scene():
    scene = bpy.context.scene
    scene.frame_start = 1
    scene.frame_end = 500
    scene.frame_set(1)
    for obj in list(scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    random.seed(7)
    # Rig: root -> arm_L -> hand.L (bone with parent, non-trivial rest);
    #      hand.R without parent (Auto-Rig Pro's c_hand_ik.* case).
    arm = make_armature("Rig", [
        ("root", (0, 0, 0), (0, 0, 0.5), 0.0, None),
        ("arm_L", (0.2, 0.1, 1.2), (0.55, 0.05, 1.0), 0.3, "root"),
        ("hand.L", (0.55, 0.05, 1.0), (0.6, 0.15, 0.95), 0.7, "arm_L"),
        ("hand.R", (-0.5, 0.05, 1.0), (-0.6, 0.15, 0.95), -0.4, None),
        # Right IK arm: shoulder -> upper -> forearm, IK towards hand.R.
        ("shoulder.R", (-0.2, 0.1, 1.2), (-0.35, 0.08, 1.15), 0.0, "root"),
        ("upper.R", (-0.35, 0.08, 1.15), (-0.45, 0.06, 1.08), 0.0, "shoulder.R"),
        ("fore.R", (-0.45, 0.06, 1.08), (-0.5, 0.05, 1.0), 0.0, "upper.R"),
        # Deform bone copying the hand.R controller (ARP's `hand.l`, Rigify's
        # `DEF-hand.L`): depends on hand.R without being hand.R.
        ("def_hand.R", (-0.5, 0.05, 1.0), (-0.6, 0.15, 0.95), -0.4, "fore.R"),
        # Bones in "world location" (Rigify IK controllers): with and without parent.
        ("wl_parent", (0.2, -0.3, 1.3), (0.3, -0.35, 1.25), 0.5, "root"),
        ("wl_hand", (0.3, -0.35, 1.25), (0.35, -0.3, 1.2), -0.6, "wl_parent"),
        ("wl_free", (-0.3, -0.3, 1.3), (-0.35, -0.25, 1.25), 0.2, None),
        # Rigify-like IK chain: the IK target is a child of the controller, an
        # intermediate bone (swing, Damped Track towards the target) separates
        # the shoulder from the chain root.
        ("shoulder2.R", (-0.2, 0.3, 1.2), (-0.35, 0.28, 1.15), 0.0, "root"),
        ("swing.R", (-0.35, 0.28, 1.15), (-0.45, 0.26, 1.08), 0.0, "shoulder2.R"),
        ("up2.R", (-0.35, 0.28, 1.15), (-0.45, 0.26, 1.08), 0.0, "swing.R"),
        ("fore2.R", (-0.45, 0.26, 1.08), (-0.5, 0.25, 1.0), 0.0, "up2.R"),
        ("ctrl2.R", (-0.5, 0.25, 1.0), (-0.6, 0.35, 0.95), -0.4, None),
        ("ik_tgt.R", (-0.5, 0.25, 1.0), (-0.55, 0.3, 0.97), 0.0, "ctrl2.R"),
    ])
    ik = arm.pose.bones["fore.R"].constraints.new('IK')
    ik.target = arm
    ik.subtarget = "hand.R"
    ik.chain_count = 2
    copy = arm.pose.bones["def_hand.R"].constraints.new('COPY_TRANSFORMS')
    copy.target = arm
    copy.subtarget = "hand.R"
    for name in ("wl_hand", "wl_free"):
        arm.data.bones[name].use_local_location = False
    ik2 = arm.pose.bones["fore2.R"].constraints.new('IK')
    ik2.target = arm
    ik2.subtarget = "ik_tgt.R"
    ik2.chain_count = 2
    track = arm.pose.bones["swing.R"].constraints.new('DAMPED_TRACK')
    track.target = arm
    track.subtarget = "ik_tgt.R"
    arm.location = (0.3, -0.2, 0.1)
    arm.rotation_euler = (0.0, 0.0, 0.4)
    arm.scale = (1.25, 1.25, 1.25)

    ball = make_mesh_object("Ball", (0.1, 0.1, 0.1))
    ball.location = (1.0, 1.0, 1.0)
    spear = make_mesh_object("Spear", (0.03, 1.2, 0.03))   # long along Y
    spear.location = (0.0, 0.0, 1.0)
    spear.rotation_euler = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    return arm, ball, spear


# ===========================================================================
#  Tests — section 1
# ===========================================================================

def test_registration():
    print("\n== Registration ==")
    prop_handoff.register()
    check("Object.prop_handoff registered", hasattr(bpy.types.Object, "prop_handoff"))
    check("Scene.prop_handoff registered", hasattr(bpy.types.Scene, "prop_handoff"))
    ops = [c.bl_idname for c in operators.classes if hasattr(c, "bl_idname") and c.bl_idname.startswith("prophandoff.")]
    check("25 operators", len(ops) == 25, len(ops))
    panels = [c for c in panel.classes if issubclass(c, bpy.types.Panel)]
    check("11 panels + UIList", len(panels) == 11 and any(issubclass(c, bpy.types.UIList) for c in panel.classes), len(panels))
    check("driver expression < 256", len(utils.GRIP_EXPRESSION) < 256, len(utils.GRIP_EXPRESSION))
    # Icons: any invalid icon breaks the panel drawing.
    valid_icons = {item.identifier for item in bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"].enum_items}
    import re
    with open(os.path.join(ROOT, "prop_handoff", "panel.py"), encoding="utf-8") as handle:
        used = set(re.findall(r"icon='([A-Z0-9_]+)'", handle.read()))
    used |= {"ERROR", "BLANK1"}
    bad = sorted(icon for icon in used if icon not in valid_icons)
    check("panel icons all valid (%d)" % len(used), not bad, bad)


def test_section1(arm, ball):
    print("\n== Section 1: transfers ==")
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = ball
    settings = ball.prop_handoff

    # Setup Prop with default slots; the rig is the only armature.
    result = bpy.ops.prophandoff.setup_prop()
    check("setup_prop FINISHED", result == {'FINISHED'}, result)
    check("2 default slots", [s.name for s in settings.slots] == ["Hand_L", "Hand_R"], [s.name for s in settings.slots])
    check("bone hand.L detected", settings.slots[0].target is arm and settings.slots[0].subtarget == "hand.L",
          (settings.slots[0].target, settings.slots[0].subtarget))
    settings.slots[1].target = arm
    settings.slots[1].subtarget = "hand.R"
    constraints = list(utils.iter_ph_constraints(ball))
    check("2 Child Of PH_* constraints", [c.name for c in constraints] == ["PH_Hand_L", "PH_Hand_R"], [c.name for c in constraints])
    check("influence 0 at creation", all(c.influence == 0.0 for c in constraints))
    check("identity inverse", all(c.inverse_matrix == Matrix.Identity(4) for c in constraints))
    check("set_inverse_pending False", all(not c.set_inverse_pending for c in constraints))
    check("ph_slots JSON", isinstance(ball.get("ph_slots"), str) and "Hand_L" in ball["ph_slots"])

    # Rig animation: the left hand moves between 1 and 60.
    pb_l = arm.pose.bones["hand.L"]
    pb_l.location = (0.0, 0.0, 0.0)
    pb_l.keyframe_insert("location", frame=1)
    pb_l.location = (0.3, 0.2, -0.1)
    pb_l.keyframe_insert("location", frame=60)
    pb_r = arm.pose.bones["hand.R"]
    pb_r.location = (0.0, 0.0, 0.0)
    pb_r.keyframe_insert("location", frame=1)
    pb_r.location = (-0.2, 0.3, 0.2)
    pb_r.keyframe_insert("location", frame=60)

    # --- frame 1: Assign -> Hand_L, no jump ---------------------------------
    goto(1)
    before = world_of(ball)
    result = bpy.ops.prophandoff.assign(slot_name="Hand_L")
    check("assign Hand_L FINISHED", result == {'FINISHED'}, result)
    after = world_of(ball)
    check("assign Hand_L: no jump", mat_close(before, after, 1e-4), "%s vs %s" % (mat_str(before), mat_str(after)))
    check("state: Hand_L active", utils.active_slot_name(ball) == "Hand_L", utils.active_slot_name(ball))
    fc = utils.find_fcurve(ball, utils.influence_path("PH_Hand_L"))
    keys = {int(round(k.co.x)): (round(k.co.y, 3), k.interpolation) for k in fc.keyframe_points}
    check("Hand_L influence keys {0: 0 CONSTANT, 1: 1 CONSTANT}", keys == {0: (0.0, 'CONSTANT'), 1: (1.0, 'CONSTANT')}, keys)
    # Blender copies the previous key's interpolation onto a new key: the
    # transfer key must carry the user's default explicitly, or every key the
    # animator inserts afterwards would inherit the CONSTANT hold.
    fc_loc = utils.find_fcurve(ball, "location", 0)
    interp = {int(round(k.co.x)): k.interpolation for k in fc_loc.keyframe_points}
    check("transfer key at 1: default interpolation (hold at 0 stays CONSTANT)",
          interp.get(0) == 'CONSTANT' and interp.get(1) == utils.default_key_interpolation(), interp)
    ball.keyframe_insert("location", frame=10)
    interp = {int(round(k.co.x)): k.interpolation for k in fc_loc.keyframe_points}
    check("animator key after the transfer is not stepped", interp.get(10) == utils.default_key_interpolation(), interp)
    fc_loc.keyframe_points.remove(next(k for k in fc_loc.keyframe_points if abs(k.co.x - 10) < 1e-4))
    events = utils.list_transfer_events(ball)
    check("history: Frame 1 -> Left hand", [(e["frame"], e["label"]) for e in events] == [(1, "Left hand")], events)

    # The ball follows the hand: constant offset in the bone's frame.
    goto(1)
    offset_1 = bone_world(arm, "hand.L").inverted() @ world_of(ball)
    goto(30)
    offset_30 = bone_world(arm, "hand.L").inverted() @ world_of(ball)
    check("the ball follows hand.L (constant offset)", mat_close(offset_1, offset_30, 1e-4))
    goto(1)
    p1 = world_of(ball).translation.copy()
    goto(30)
    p30 = world_of(ball).translation.copy()
    check("the ball moves with the hand", (p1 - p30).length > 0.05, (p1 - p30).length)

    # --- frame 22: Assign -> Hand_R, no jump and no drift before -----------
    history = {}
    for frame in range(1, 22):
        goto(frame)
        history[frame] = world_of(ball)
    goto(22)
    before = world_of(ball)
    result = bpy.ops.prophandoff.assign(slot_name="Hand_R")
    after = world_of(ball)
    check("assign Hand_R: no jump", mat_close(before, after, 1e-4), "%s vs %s" % (mat_str(before), mat_str(after)))
    drift = []
    for frame in range(1, 22):
        goto(frame)
        if not mat_close(history[frame], world_of(ball), 1e-4):
            drift.append(frame)
    check("no drift on frames 1..21 after the transfer", not drift, drift)
    goto(22)
    check("Hand_L at 0, Hand_R at 1 at frame 22",
          approx(ball.constraints["PH_Hand_L"].influence, 0.0) and approx(ball.constraints["PH_Hand_R"].influence, 1.0))
    goto(21)
    check("Hand_L still at 1 at frame 21 (hold)", approx(ball.constraints["PH_Hand_L"].influence, 1.0))
    goto(40)
    offset_a = bone_world(arm, "hand.R").inverted() @ world_of(ball)
    goto(55)
    offset_b = bone_world(arm, "hand.R").inverted() @ world_of(ball)
    check("the ball follows hand.R after 22", mat_close(offset_a, offset_b, 1e-4))

    # --- frame 40: Throw (manual velocity) ---------------------------------
    goto(40)
    before = world_of(ball)
    result = bpy.ops.prophandoff.throw(flight_frames=12, velocity_source='MANUAL',
                                       direction=(0.0, 1.0, 0.5), speed=4.0, gravity=-9.81,
                                       spin=(0.0, 0.0, math.radians(90.0)))
    check("throw FINISHED", result == {'FINISHED'}, result)
    goto(40)
    after = world_of(ball)
    check("throw: no jump at the release frame", mat_close(before, after, 1e-4), "%s vs %s" % (mat_str(before), mat_str(after)))
    check("every influence at 0 after the release", all(c.influence == 0.0 for c in utils.iter_ph_constraints(ball)))
    fps = utils.scene_fps(scene)
    v0 = Vector((0.0, 1.0, 0.5)).normalized() * 4.0
    errors = []
    for step in (1, 6, 12):
        goto(40 + step)
        t = step / fps
        expected = before.translation + v0 * t + Vector((0, 0, -9.81)) * (0.5 * t * t)
        if not vec_close(world_of(ball).translation, expected, 1e-3):
            errors.append((step, tuple(world_of(ball).translation), tuple(expected)))
    check("exact parabolic trajectory at frames 41/46/52", not errors, errors)
    goto(52)
    spun = world_of(ball).to_3x3()
    expected_rot = Euler((0, 0, math.radians(90.0) * (12 / fps)), 'XYZ').to_matrix() @ before.to_3x3()
    check("in-flight rotation (spin) applied", all(abs(spun[i][j] - expected_rot[i][j]) < 1e-3 for i in range(3) for j in range(3)))
    fc_loc = utils.find_fcurve(ball, "location", 0)
    interp = {int(round(k.co.x)): k.interpolation for k in fc_loc.keyframe_points}
    check("LINEAR flight keys", all(interp.get(40 + s) == 'LINEAR' for s in range(1, 13)), interp)
    check("CONSTANT hold keys at 0, 21, 39", all(interp.get(f) == 'CONSTANT' for f in (0, 21, 39)), interp)

    events = [(e["frame"], e["label"]) for e in utils.list_transfer_events(ball)]
    check("full history", events == [(1, "Left hand"), (22, "Right hand"), (40, "Released")], events)

    # --- navigation ----------------------------------------------------------
    goto(30)
    bpy.ops.prophandoff.jump_transfer(direction='NEXT')
    check("jump NEXT from 30 -> 40", scene.frame_current == 40, scene.frame_current)
    bpy.ops.prophandoff.jump_transfer(direction='PREV')
    check("jump PREV from 40 -> 22", scene.frame_current == 22, scene.frame_current)

    # --- slot rename: constraint + data_path -----------------------------------
    settings.slots[0].name = 'Left "H"\\'
    check("cleaned name (quotes/backslash)", settings.slots[0].name == "Left H", settings.slots[0].name)
    check("constraint renamed", "PH_Left H" in ball.constraints and "PH_Hand_L" not in ball.constraints)
    check("data_path repaired", utils.find_fcurve(ball, utils.influence_path("PH_Left H")) is not None
          and utils.find_fcurve(ball, utils.influence_path("PH_Hand_L")) is None)
    events = [(e["frame"], e["label"]) for e in utils.list_transfer_events(ball)]
    check("history intact after rename", events[0] == (1, "Left hand"), events)
    settings.slots[1].name = "Left H"
    check("name uniqueness", settings.slots[1].name == "Left H.001", settings.slots[1].name)
    settings.slots[1].name = "Hand_R"
    settings.slots[0].name = "Hand_L"

    # --- "Only Insert Needed" must not drop the hold keys --------------------
    prefs = bpy.context.preferences.edit
    had = getattr(prefs, "use_keyframe_insert_needed", None)
    if had is not None:
        prefs.use_keyframe_insert_needed = True
        goto(80)
        before = world_of(ball)
        bpy.ops.prophandoff.assign(slot_name="Hand_L")
        after = world_of(ball)
        check("assign with Only Insert Needed: no jump", mat_close(before, after, 1e-4))
        fc = utils.find_fcurve(ball, utils.influence_path("PH_Hand_L"))
        frames = {int(round(k.co.x)) for k in fc.keyframe_points}
        check("hold keys present at 79 despite Only Insert Needed", 79 in frames and 80 in frames, sorted(frames))
        fc_loc = utils.find_fcurve(ball, "location", 0)
        frames = {int(round(k.co.x)) for k in fc_loc.keyframe_points}
        check("loc hold key at 79", 79 in frames and 80 in frames)
        prefs.use_keyframe_insert_needed = had

    # --- slot removal ------------------------------------------------------------
    count = len(settings.slots)
    settings.active_slot_index = 1
    bpy.ops.prophandoff.slot_remove(index=1)
    check("slot removed", len(settings.slots) == count - 1 and "PH_Hand_R" not in ball.constraints)
    check("influence F-Curves removed", utils.find_fcurve(ball, utils.influence_path("PH_Hand_R")) is None)

    # --- reload from ph_slots ------------------------------------------------------
    settings.slots.clear()
    ball["ph_slots"] = '[{"name": "Hand_L", "label": "Left hand", "target": "Rig", "subtarget": "hand.L"}, {"name": "Table", "label": "Table", "target": "", "subtarget": ""}]'
    restored = utils.load_slots_id_prop(ball)
    check("reload ph_slots: 2 slots", restored == 2 and [s.name for s in settings.slots] == ["Hand_L", "Table"])
    check("reload: targets resolved", settings.slots[0].target is arm and settings.slots[0].subtarget == "hand.L" and settings.slots[1].target is None)

    # --- clear_setup keeps the visual position ---------------------------------------
    goto(30)
    before = world_of(ball)
    bpy.ops.prophandoff.clear_setup(keep_visual_transform=True, remove_slots=False)
    after = world_of(ball)
    check("clear_setup: visual position kept at the current frame", mat_close(before, after, 1e-4))
    check("clear_setup: no PH_ constraint left", not utils.is_setup(ball))


def test_event_listing_spec_case():
    """Test case of the specification §6.5."""
    print("\n== History: specification case ==")
    obj = make_mesh_object("EventsProbe")
    bpy.context.scene.prop_handoff.prop_object = obj
    s = obj.prop_handoff.slots.add(); s.name = "L"; s.stored_name = "L"
    s = obj.prop_handoff.slots.add(); s.name = "R"; s.stored_name = "R"
    utils.ensure_constraints(obj)
    cl, cr = obj.constraints["PH_L"], obj.constraints["PH_R"]
    for frame, value in ((11, 0), (12, 1), (41, 1), (42, 0), (66, 0), (67, 0)):
        utils.insert_influence_key(cl, float(value), frame)
    for frame, value in ((11, 0), (12, 0), (41, 0), (42, 1), (66, 1), (67, 0)):
        utils.insert_influence_key(cr, float(value), frame)
    events = [(e["frame"], e["slots"]) for e in utils.list_transfer_events(obj)]
    check("events [12->L, 42->R, 67->released]", events == [(12, ("L",)), (42, ("R",)), (67, ())], events)
    bpy.data.objects.remove(obj, do_unlink=True)


# ===========================================================================
#  Tests — section 2
# ===========================================================================

def frame_expected_world(arm, bone_name):
    """Expected channel frame: arm @ pose(P) @ rest(P)^-1 @ rest(B), or
    arm @ rest(B) without parent; without Local Location only the translation
    of the rest offset counts (the rotation is the parent's)."""
    arm_eval = arm.evaluated_get(dg())
    bone = arm.data.bones[bone_name]
    if bone.parent is None:
        rest = bone.matrix_local
        if not bone.use_local_location:
            rest = Matrix.Translation(rest.translation)
        return arm_eval.matrix_world @ rest
    parent_pose = arm_eval.pose.bones[bone.parent.name].matrix
    offset = bone.parent.matrix_local.inverted() @ bone.matrix_local
    if not bone.use_local_location:
        offset = Matrix.Translation(offset.translation)
    return arm_eval.matrix_world @ parent_pose @ offset


def hand_head_world(arm, bone_name):
    return bone_world(arm, bone_name).translation.copy()


def evaluated_influence(owner_obj, bone_name, constraint_name):
    ev = owner_obj.evaluated_get(dg())
    owner = ev.pose.bones[bone_name] if bone_name else ev
    return owner.constraints[constraint_name].influence


def _frame_trials(arm, spear, pairs, posed, seed):
    """Random trials: the frame must coincide with the bone's head and the
    driver distance (s * |loc_B - g|) with the world distance hand <-> grip.
    The second check does not depend on the frame formula: it reads the
    actually evaluated head."""
    random.seed(seed)
    failures = []
    bones = [bone_name for _side, bone_name in pairs]
    for trial in range(5):
        # Random pose of the parents, the root and the armature; hand channels at zero.
        for name in posed:
            pb = arm.pose.bones[name]
            pb.rotation_mode = 'XYZ'
            pb.location = Vector([random.uniform(-0.2, 0.2) for _ in range(3)])
            pb.rotation_euler = Euler([random.uniform(-1.0, 1.0) for _ in range(3)], 'XYZ')
        for name in bones:
            pb = arm.pose.bones[name]
            pb.location = (0, 0, 0)
            pb.rotation_euler = (0, 0, 0)
        arm.location = Vector([random.uniform(-1, 1) for _ in range(3)])
        arm.rotation_euler = Euler([random.uniform(-1, 1) for _ in range(3)], 'XYZ')
        s = random.uniform(0.5, 2.0)
        arm.scale = (s, s, s)
        bpy.context.view_layer.update()
        for side, bone_name in pairs:
            helpers = operators.build_helpers(bpy.context, spear, side)
            if helpers is None:
                failures.append((trial, side, "build_helpers None"))
                continue
            frame, grip_local = helpers
            bpy.context.view_layer.update()
            frame_world = world_of(frame)
            expected = frame_expected_world(arm, bone_name)
            head = hand_head_world(arm, bone_name)
            if not mat_close(frame_world, expected, 1e-5):
                failures.append((trial, side, "matrix", mat_str(frame_world), mat_str(expected)))
            if not vec_close(frame_world.translation, head, 1e-5):
                failures.append((trial, side, "head", tuple(frame_world.translation), tuple(head)))
            # Distance: s * |loc_B - g| == world distance hand <-> grip.
            grip = utils.find_grip_empty(spear, side)
            pb = arm.pose.bones[bone_name]
            pb.location = Vector([random.uniform(-0.3, 0.3) for _ in range(3)])
            bpy.context.view_layer.update()
            g = frame_world.inverted() @ world_of(grip).translation
            scale_world = world_of(frame).to_scale()[0]
            driver_dist = scale_world * (Vector(pb.location) - g).length
            world_dist = (hand_head_world(arm, bone_name) - world_of(grip).translation).length
            if not approx(driver_dist, world_dist, 1e-4):
                failures.append((trial, side, "distance", driver_dist, world_dist))
            # Check of the grip local's Local Space (what the driver reads).
            local = world_of(grip_local)
            local_pos = (world_of(frame) @ grip_local.matrix_parent_inverse).inverted() @ local.translation
            if not vec_close(local_pos, g, 1e-4):
                failures.append((trial, side, "grip local", tuple(local_pos), tuple(g)))
            pb.location = (0, 0, 0)
    return failures


def test_frame_helper_math(arm):
    """§9.2: frame position == bone head on random configurations."""
    print("\n== Grip Zone: channel frame ==")
    spear = bpy.data.objects["Spear"]
    prop_settings = spear.prop_handoff
    prop_settings.ik_object_l = arm
    prop_settings.ik_bone_l = "hand.L"
    prop_settings.ik_object_r = arm
    prop_settings.ik_bone_r = "hand.R"
    bpy.context.scene.prop_handoff.prop_object = spear
    bpy.ops.prophandoff.setup_twohanded()

    failures = _frame_trials(arm, spear, (('LEFT', "hand.L"), ('RIGHT', "hand.R")), ("root", "arm_L"), 3)
    check("frame == bone head, 5 configurations x 2 branches (1e-5)", not failures, failures[:3])

    # Bones without "Local Location" (Rigify IK controllers): with a rotated parent, and without parent.
    prop_settings.ik_bone_l = "wl_hand"
    prop_settings.ik_bone_r = "wl_free"
    for side in utils.SIDES:
        utils.remove_helpers(spear, side)
    failures = _frame_trials(arm, spear, (('LEFT', "wl_hand"), ('RIGHT', "wl_free")), ("root", "wl_parent"), 5)
    check("frame == bone head without Local Location (wl_hand / wl_free), 5 configurations (1e-5)",
          not failures, failures[:3])
    prop_settings.ik_bone_l = "hand.L"
    prop_settings.ik_bone_r = "hand.R"
    for side in utils.SIDES:
        utils.remove_helpers(spear, side)

    # Reset the rig for what follows.
    for name in ("root", "arm_L", "hand.L", "hand.R", "wl_parent", "wl_hand", "wl_free"):
        pb = arm.pose.bones[name]
        pb.location = (0, 0, 0)
        pb.rotation_euler = (0, 0, 0)
    arm.location = (0.3, -0.2, 0.1)
    arm.rotation_euler = (0.0, 0.0, 0.4)
    arm.scale = (1.25, 1.25, 1.25)
    for side in utils.SIDES:
        utils.remove_grip_blend(spear, side)
    bpy.context.view_layer.update()


def test_section2(arm, spear):
    print("\n== Section 2: Two-Hand Grip ==")
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = spear
    settings = spear.prop_handoff

    # The hands are animated (keys inserted in section 1): put them back on
    # the spear at every frame, then place the grips on them.
    for bone_name, action_path in (("hand.L", None), ("hand.R", None)):
        pb = arm.pose.bones[bone_name]
        utils.remove_fcurves(arm, pb.path_from_id("location"))
        utils.remove_fcurves(arm, pb.path_from_id("rotation_euler"))
        utils.remove_fcurves(arm, pb.path_from_id("scale"))
        pb.location = (0, 0, 0)
        pb.rotation_euler = (0, 0, 0)
        pb.scale = (1, 1, 1)
    goto(1)
    bpy.context.view_layer.update()
    # Put the spear between the two hands, place the grips on them.
    spear.location = (0.0, 0.0, 1.2)
    bpy.context.view_layer.update()
    for side in utils.SIDES:
        result = bpy.ops.prophandoff.set_grip_pose(hand=side)
        check("set_grip_pose %s FINISHED" % side, result == {'FINISHED'}, result)
    for side, bone_name in (('LEFT', "hand.L"), ('RIGHT', "hand.R")):
        grip = utils.find_grip_empty(spear, side)
        check("grip %s on the hand" % side, mat_close(world_of(grip), bone_world(arm, bone_name), 1e-4))
        check("pose %s set" % side, utils.has_grip_pose(spear, side))
    check("zone sphere created", utils.find_grip_zone(spear) is not None and utils.find_grip_zone(spear).empty_display_type == 'SPHERE')
    check("ph_grip_zone_radius float", isinstance(spear.get("ph_grip_zone_radius"), float))
    settings.grip_zone_radius = 0.4
    check("radius synced into the ID prop and the sphere",
          approx(spear["ph_grip_zone_radius"], 0.4) and approx(utils.find_grip_zone(spear).empty_display_size, 0.4))

    # --- Grip Zone ---------------------------------------------------------
    result = bpy.ops.prophandoff.enable_grip_zone()
    check("enable_grip_zone FINISHED", result == {'FINISHED'}, result)
    for side, bone_name in (('LEFT', "hand.L"), ('RIGHT', "hand.R")):
        pb = arm.pose.bones[bone_name]
        c = pb.constraints.get(utils.gripblend_constraint_name(side))
        check("PH_GripBlend_%s present (Copy Transforms)" % utils.side_suffix(side), c is not None and c.type == 'COPY_TRANSFORMS')
        drv = next((d for d in arm.animation_data.drivers if d.data_path == c.path_from_id("influence")), None)
        check("driver %s: 9 variables, expression" % side,
              drv is not None and len(drv.driver.variables) == 9 and drv.driver.expression == utils.GRIP_EXPRESSION
              and len(drv.keyframe_points) == 0 and len(drv.modifiers) == 0)
        check("helpers %s present" % side, utils.helpers_present(spear, side))
        check("grip_zone_active %s" % side, utils.grip_zone_active(spear, side))
    goto(1)
    bpy.context.view_layer.update()
    check("no cycle after enable_grip_zone", not log_has_cycle())
    for side, bone_name in (('LEFT', "hand.L"), ('RIGHT', "hand.R")):
        inf = evaluated_influence(arm, bone_name, utils.gripblend_constraint_name(side))
        check("influence %s = 1 on the grip (evaluated driver)" % side, approx(inf, 1.0, 1e-4), inf)
        drv = next(d for d in arm.animation_data.drivers if d.data_path == arm.pose.bones[bone_name].constraints[utils.gripblend_constraint_name(side)].path_from_id("influence"))
        check("driver %s valid (simple evaluator, no autoexec)" % side, drv.driver.is_valid and not drv.driver.is_simple_expression is None)

    # Move the spear slightly (under the 0.05 inner radius): the hands follow
    # without lag (same frame). The Grip Zone is a proximity blend: a big move
    # of the spear would make the hands let go (the attach is what locks).
    spear.location = (0.0, 0.0, 1.2)
    spear.rotation_euler = (0.0, 0.0, 0.0)
    spear.keyframe_insert("location", frame=1)
    spear.keyframe_insert("rotation_euler", frame=1)
    spear.location = (0.02, 0.015, 1.22)
    spear.rotation_euler = (0.01, 0.0, 0.02)
    spear.keyframe_insert("location", frame=30)
    spear.keyframe_insert("rotation_euler", frame=30)
    lag = []
    for frame in (5, 15, 30):
        goto(frame)
        for side, bone_name in (('LEFT', "hand.L"), ('RIGHT', "hand.R")):
            grip = utils.find_grip_empty(spear, side)
            if not vec_close(hand_head_world(arm, bone_name), world_of(grip).translation, 1e-4):
                lag.append((frame, side, tuple(hand_head_world(arm, bone_name)), tuple(world_of(grip).translation)))
    check("hands on the grips at every frame (no lag)", not lag, lag[:2])
    check("no cycle while following", not log_has_cycle())

    # Move the right hand away: lets go outside the sphere, grabs again on return.
    goto(1)
    pb_r = arm.pose.bones["hand.R"]
    pb_r.location = (0.0, 0.0, 1.0)   # ~1.25 u in world: outside the zone
    bpy.context.view_layer.update()
    inf = evaluated_influence(arm, "hand.R", "PH_GripBlend_R")
    check("right hand far: influence 0", approx(inf, 0.0, 1e-4), inf)
    free_head = hand_head_world(arm, "hand.R")
    check("right hand free (does not stick to the grip)", (free_head - world_of(utils.find_grip_empty(spear, 'RIGHT')).translation).length > 0.5)
    pb_r.location = (0.0, 0.0, 0.12)  # 0.15 u world (s = 1.25): inside the zone
    bpy.context.view_layer.update()
    inf = evaluated_influence(arm, "hand.R", "PH_GripBlend_R")
    expected = 1.0 - (0.12 * 1.25 - 0.05) / (0.4 - 0.05)
    check("right hand inside the zone: linear influence (%.3f)" % expected, approx(inf, expected, 2e-3), inf)
    partial_head = hand_head_world(arm, "hand.R")
    grip_pos = world_of(utils.find_grip_empty(spear, 'RIGHT')).translation
    check("right hand partially blended (between channels and grip)",
          0.02 < (partial_head - grip_pos).length < 0.15, (partial_head - grip_pos).length)
    pb_r.location = (0.0, 0.0, 0.0)
    bpy.context.view_layer.update()
    inf = evaluated_influence(arm, "hand.R", "PH_GripBlend_R")
    check("right hand back: influence 1", approx(inf, 1.0, 1e-4), inf)
    check("no cycle after the round trip", not log_has_cycle())

    # --- Attach both hands ------------------------------------------------
    # The frames where the Grip Zone held the hands (1..9) must survive the
    # attach: the zone is baked to keys, not muted.
    zone_history = {}
    for frame in range(1, 10):
        goto(frame)
        zone_history[frame] = {b: bone_world(arm, b) for b in ("hand.L", "hand.R")}
    goto(10)
    before = {side: bone_world(arm, b) for side, b in (('LEFT', "hand.L"), ('RIGHT', "hand.R"))}
    result = bpy.ops.prophandoff.attach_both_hands()
    check("attach_both_hands FINISHED", result == {'FINISHED'}, result)
    goto(10)
    for side, bone_name in (('LEFT', "hand.L"), ('RIGHT', "hand.R")):
        check("attach %s: no jump" % side, mat_close(before[side], bone_world(arm, bone_name), 1e-4),
              "%s vs %s" % (mat_str(before[side]), mat_str(bone_world(arm, bone_name))))
        check("hand %s attached" % side, utils.is_hand_attached(spear, side))
        check("Grip Zone %s baked then removed at the attach" % side, not utils.grip_zone_active(spear, side) and not utils.grip_blend_present(spear, side))
        c = arm.pose.bones[bone_name].constraints[utils.twohand_constraint_name(side)]
        fc = utils.find_fcurve(arm, c.path_from_id("influence"))
        keys = {int(round(k.co.x)): round(k.co.y, 3) for k in fc.keyframe_points}
        check("attach keys %s {9: 0, 10: 1}" % side, keys.get(9) == 0.0 and keys.get(10) == 1.0, keys)
        fc_loc = utils.find_fcurve(arm, arm.pose.bones[bone_name].path_from_id("location"), 0)
        interp = {int(round(k.co.x)): k.interpolation for k in fc_loc.keyframe_points}
        check("attach %s: hand key at 10 uses the default interpolation" % side,
              interp.get(9) == 'CONSTANT' and interp.get(10) == utils.default_key_interpolation(), interp)
    goto(5)
    check("before the attach (frame 5): influence 0", all(not utils.is_hand_attached(spear, s) for s in utils.SIDES))
    drift = []
    for frame in range(1, 10):
        goto(frame)
        for b in ("hand.L", "hand.R"):
            if not mat_close(zone_history[frame][b], bone_world(arm, b), 1e-4):
                drift.append((frame, b))
    check("frames 1..9: the hands stay where the Grip Zone held them (bake)", not drift, drift[:4])
    # The attached hands follow the spear: constant hand/grip offset (the attach
    # does not snap the hand onto the grip, it locks it where it is).
    goto(10)
    offsets = {side: world_of(utils.find_grip_empty(spear, side)).inverted() @ bone_world(arm, b)
               for side, b in (('LEFT', "hand.L"), ('RIGHT', "hand.R"))}
    spear.location = (0.3, 0.2, 1.4)
    spear.keyframe_insert("location", frame=30)
    goto(30)
    follow = []
    for side, bone_name in (('LEFT', "hand.L"), ('RIGHT', "hand.R")):
        grip = utils.find_grip_empty(spear, side)
        if not mat_close(world_of(grip).inverted() @ bone_world(arm, bone_name), offsets[side], 1e-4):
            follow.append(side)
    check("attached hands follow the spear at frame 30 (constant offset)", not follow, follow)
    check("no cycle after the attach", not log_has_cycle())

    # --- Release both hands ---------------------------------------------------
    goto(30)
    before = {side: bone_world(arm, b) for side, b in (('LEFT', "hand.L"), ('RIGHT', "hand.R"))}
    result = bpy.ops.prophandoff.release_both_hands()
    check("release_both_hands FINISHED", result == {'FINISHED'}, result)
    goto(30)
    for side, bone_name in (('LEFT', "hand.L"), ('RIGHT', "hand.R")):
        check("release %s: no jump" % side, mat_close(before[side], bone_world(arm, bone_name), 1e-4))
        check("hand %s free" % side, not utils.hand_follows_prop(spear, side))
    goto(29)
    check("frame 29: still attached (hold)", all(utils.is_hand_attached(spear, s) for s in utils.SIDES))
    # Move the spear after the release: the hands no longer move.
    goto(30)
    heads = {b: hand_head_world(arm, b) for b in ("hand.L", "hand.R")}
    spear.location = (0.6, 0.4, 1.5)
    spear.keyframe_insert("location", frame=30)
    spear.location = (-0.5, -0.5, 1.2)
    spear.keyframe_insert("location", frame=60)
    goto(60)
    check("hands still after the release (nothing re-grabs)",
          all(vec_close(heads[b], hand_head_world(arm, b), 1e-4) for b in heads))

    # --- Grip Zone re-enabled, then right master hand ------------------------
    goto(60)
    # Put the hands back on the spear through the grip poses (Attach / Release
    # left the hands where the spear was at frame 30).
    for side in utils.SIDES:
        bpy.ops.prophandoff.set_grip_pose(hand=side)
    result = bpy.ops.prophandoff.enable_grip_zone()
    check("enable_grip_zone (re-enable) FINISHED", result == {'FINISHED'}, result)
    check("Grip Zone active on both hands", all(utils.grip_zone_active(spear, s) for s in utils.SIDES))

    # The right attach (frames 10-29) still exists at influence 0: its
    # dependency relation alone would loop with the future PH_Hand_R. Master
    # hand must bake it to keys then remove it, without changing the motion.
    hand_r_history = {}
    for frame in range(8, 33):
        goto(frame)
        hand_r_history[frame] = bone_world(arm, "hand.R")
    goto(60)
    before_prop = world_of(spear)
    before_l = bone_world(arm, "hand.L")
    result = bpy.ops.prophandoff.set_master_hand(hand='RIGHT')
    check("set_master_hand RIGHT FINISHED", result == {'FINISHED'}, result)
    goto(60)
    check("right attach removed (relation cut)", utils.attach_constraint(spear, 'RIGHT') is None)
    drift = [f for f in range(8, 33) if not mat_close(hand_r_history[f], (goto(f) or bone_world(arm, "hand.R")), 1e-4)]
    check("right hand motion preserved after bake (frames 8..32)", not drift, drift)
    fc = utils.find_fcurve(arm, arm.pose.bones["hand.R"].path_from_id("location"), 0)
    baked_frames = {int(round(k.co.x)) for k in fc.keyframe_points}
    check("bake keys present on 10..29", all(f in baked_frames for f in range(10, 30)), sorted(baked_frames)[:12])
    goto(60)
    # Move the right hand (a little): the spear follows, the left one stays on
    # it through its Grip Zone; a big move would make it let go (proximity
    # blend) — for a firm hold, "Other hand: Attach".
    pb_r = arm.pose.bones["hand.R"]
    pb_r.keyframe_insert("location", frame=60)
    pb_r.location = Vector(pb_r.location) + Vector((0.02, -0.01, 0.02))
    pb_r.keyframe_insert("location", frame=90)
    goto(90)
    moved = (world_of(spear).translation - before_prop.translation).length
    check("the spear follows the right hand (move ~0.037)", 0.03 < moved < 0.045, moved)
    grip_r = utils.find_grip_empty(spear, 'RIGHT')
    check("the right grip stays on the right hand", vec_close(world_of(grip_r).translation, hand_head_world(arm, "hand.R"), 1e-4))
    grip_l = utils.find_grip_empty(spear, 'LEFT')
    check("the left hand stays on the spear", vec_close(world_of(grip_l).translation, hand_head_world(arm, "hand.L"), 1e-3),
          (tuple(world_of(grip_l).translation), tuple(hand_head_world(arm, "hand.L"))))
    check("no cycle in master hand mode", not log_has_cycle())

    # --- Master hand: None -------------------------------------------------------
    goto(90)
    before_prop = world_of(spear)
    result = bpy.ops.prophandoff.set_master_hand(hand='NONE')
    check("set_master_hand NONE FINISHED", result == {'FINISHED'}, result)
    goto(90)
    check("None: the spear does not jump", mat_close(before_prop, world_of(spear), 1e-4))
    check("holding_side None", utils.holding_side(spear) is None)
    check("None: right Grip Zone restored (it was wanted)", utils.grip_zone_active(spear, 'RIGHT'))
    check("None: right hand back on its grip",
          vec_close(hand_head_world(arm, "hand.R"), world_of(utils.find_grip_empty(spear, 'RIGHT')).translation, 1e-4))

    # --- Release everything --------------------------------------------------------
    result = bpy.ops.prophandoff.release_both_hands()
    check("release_both_hands (end) FINISHED", result == {'FINISHED'}, result)
    check("everything free, Grip Zone inactive", all(not utils.hand_follows_prop(spear, s) for s in utils.SIDES) and utils.holding_side(spear) is None)
    result = bpy.ops.prophandoff.disable_grip_zone()
    check("disable_grip_zone: constraints, drivers and helpers removed",
          all(not utils.grip_blend_present(spear, s) and not utils.helpers_present(spear, s) for s in utils.SIDES)
          and not any("PH_GripBlend" in d.data_path for d in arm.animation_data.drivers))
    check("no cycle (end of section 2)", not log_has_cycle())


def test_object_ik_targets():
    """Object IK targets (empties), one of them bone-parented: TRANSFORMS branch."""
    print("\n== Section 2: object IK targets ==")
    arm = bpy.data.objects["Rig"]
    bat = make_mesh_object("Bat", (0.04, 0.04, 0.6))  # long along Z
    bat.location = (2.0, 0.0, 1.0)
    ik_l = make_empty("IK_L")
    ik_l.location = (2.0, 0.0, 1.3)
    ik_r = make_empty("IK_R")
    ik_r.parent = arm
    ik_r.parent_type = 'BONE'
    ik_r.parent_bone = "root"
    bpy.context.view_layer.update()
    ik_r.matrix_world = Matrix.Translation((2.0, 0.0, 0.7))
    bpy.context.view_layer.update()

    scene = bpy.context.scene
    scene.prop_handoff.prop_object = bat
    settings = bat.prop_handoff
    settings.ik_object_l = ik_l
    settings.ik_object_r = ik_r
    result = bpy.ops.prophandoff.setup_twohanded()
    check("setup_twohanded (objects) FINISHED", result == {'FINISHED'}, result)
    for side, ik in (('LEFT', ik_l), ('RIGHT', ik_r)):
        grip = utils.find_grip_empty(bat, side)
        check("grip %s placed on the IK object" % side, mat_close(world_of(grip), world_of(ik), 1e-4))
    result = bpy.ops.prophandoff.enable_grip_zone()
    check("enable_grip_zone (objects) FINISHED", result == {'FINISHED'}, result)
    bpy.context.view_layer.update()
    for side, ik in (('LEFT', ik_l), ('RIGHT', ik_r)):
        c = ik.constraints.get(utils.gripblend_constraint_name(side))
        check("constraint %s on the IK object" % side, c is not None)
        inf = ik.evaluated_get(dg()).constraints[c.name].influence
        check("influence %s = 1 on the grip (object)" % side, approx(inf, 1.0, 1e-4), inf)
        frame = utils.find_helper(bat, utils.ID_PROP_FRAME[side])
        check("frame %s: same parenting as the IK object" % side,
              frame.parent is ik.parent and frame.parent_type == ik.parent_type and frame.parent_bone == ik.parent_bone)
    # The bat moves (a little): the hands follow; move IK_L away: it lets go.
    bat.location = (2.02, 0.01, 1.01)
    bpy.context.view_layer.update()
    for side, ik in (('LEFT', ik_l), ('RIGHT', ik_r)):
        grip = utils.find_grip_empty(bat, side)
        check("IK object %s follows the bat" % side, vec_close(world_of(ik).translation, world_of(grip).translation, 1e-4),
              (tuple(world_of(ik).translation), tuple(world_of(grip).translation)))
    ik_l.location = (4.0, 0.0, 1.3)
    bpy.context.view_layer.update()
    inf = ik_l.evaluated_get(dg()).constraints["PH_GripBlend_L"].influence
    check("IK_L moved away: influence 0", approx(inf, 0.0, 1e-4), inf)
    check("no cycle (object IK targets)", not log_has_cycle())
    # Attach / release on objects.
    goto(20)
    before = world_of(ik_r)
    result = bpy.ops.prophandoff.attach_both_hands()
    check("attach_both_hands (objects)", result == {'FINISHED'}, result)
    goto(20)
    check("attach IK_R (bone-parented): no jump", mat_close(before, world_of(ik_r), 1e-4), "%s vs %s" % (mat_str(before), mat_str(world_of(ik_r))))
    result = bpy.ops.prophandoff.release_both_hands()
    check("release_both_hands (objects)", result == {'FINISHED'}, result)
    bpy.ops.prophandoff.disable_grip_zone()


def test_switch_to_twohanded(arm):
    """→ Switch to two-handed: keeps the carrying hand, attaches the other."""
    print("\n== Switch to two-handed ==")
    spear = bpy.data.objects["Spear"]
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = spear
    goto(100)
    for side in utils.SIDES:
        bpy.ops.prophandoff.set_grip_pose(hand=side)
    # The prop follows the left hand (section 1): Assign lifts the slot suspension.
    slot = operators._find_or_create_ik_slot(spear, 'LEFT')
    result = bpy.ops.prophandoff.assign(slot_name=slot)
    check("assign to the left hand", result == {'FINISHED'}, result)
    check("holding_side == LEFT", utils.holding_side(spear) == 'LEFT')
    before_r = bone_world(arm, "hand.R")
    before_prop = world_of(spear)
    # Conflict fabricated by hand (risky case): Copy Transforms left hand →
    # Grip_L, disabled. No depsgraph evaluation before the operator removes it
    # (otherwise the — real — cycle would be logged).
    fake = arm.pose.bones["hand.L"].constraints.new('COPY_TRANSFORMS')
    fake.name = "PH_GripBlend_L"
    fake.target = utils.find_grip_empty(spear, 'LEFT')
    fake.influence = 0.0
    fake.enabled = False
    check("dependency_warning detects the conflict (presence, even disabled)",
          utils.dependency_warning(spear) is not None)
    # `bpy.ops` evaluates the depsgraph before `execute`: the fabricated
    # conflict is logged once (expected behaviour), then the operator removes it.
    before_cycles = cycle_count()
    result = bpy.ops.prophandoff.switch_to_twohanded(free_hand='AUTO')
    check("switch_to_twohanded FINISHED", result == {'FINISHED'}, result)
    fabricated = cycle_count() - before_cycles
    EXPECTED_CYCLES[0] += fabricated
    check("the fabricated conflict was logged before the cleanup", fabricated > 0, fabricated)
    goto(100)
    goto(101)
    check("no cycle left after the operator's cleanup", not log_has_cycle())
    check("right hand attached", utils.is_hand_attached(spear, 'RIGHT'))
    check("right hand: no jump", mat_close(before_r, bone_world(arm, "hand.R"), 1e-4),
          "%s vs %s" % (mat_str(before_r), mat_str(bone_world(arm, "hand.R"))))
    check("the prop does not jump", mat_close(before_prop, world_of(spear), 1e-4))
    check("the prop still follows the left hand", utils.holding_side(spear) == 'LEFT')
    check("fabricated left constraint removed (anti-cycle)", not utils.grip_blend_present(spear, 'LEFT'))
    check("slot Hand_R suspended (right hand follows the prop)",
          utils.get_slot_constraint(spear, "Hand_R") is None
          and any(utils.slot_is_suspended(spear, sl) for sl in spear.prop_handoff.slots))
    check("is_setup despite the suspension", utils.is_setup(spear))
    check("no warning left", utils.dependency_warning(spear) is None, utils.dependency_warning(spear))
    result = run_op(bpy.ops.prophandoff.switch_to_twohanded, free_hand='LEFT')
    check("adding the carrying hand refused", result == {'CANCELLED'}, result)
    # Move the left hand: the prop and the right hand follow.
    pb_l = arm.pose.bones["hand.L"]
    pb_l.keyframe_insert("location", frame=100)
    pb_l.location = (0.2, 0.3, -0.2)
    pb_l.keyframe_insert("location", frame=120)
    goto(120)
    grip_r = utils.find_grip_empty(spear, 'RIGHT')
    check("the right hand follows the prop that follows the left hand",
          vec_close(world_of(grip_r).translation, hand_head_world(arm, "hand.R"), 1e-4))
    check("no cycle (switch to two-handed)", not log_has_cycle())
    bpy.ops.prophandoff.release_both_hands()
    bpy.ops.prophandoff.disable_grip_zone()


def test_master_then_attach(arm):
    """Master hand → None → Attach both hands: the prop's dormant slot (with
    history) is baked to keys then removed; no cycle."""
    print("\n== Master hand then attach both hands ==")
    spear = bpy.data.objects["Spear"]
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = spear
    goto(130)
    for side in utils.SIDES:
        bpy.ops.prophandoff.set_grip_pose(hand=side)
    result = bpy.ops.prophandoff.set_master_hand(hand='RIGHT', other_hand='KEEP')
    check("master RIGHT", result == {'FINISHED'}, result)
    pb_r = arm.pose.bones["hand.R"]
    pb_r.keyframe_insert("location", frame=130)
    pb_r.location = (-0.3, 0.1, 0.5)
    pb_r.keyframe_insert("location", frame=150)
    goto(150)
    result = bpy.ops.prophandoff.set_master_hand(hand='NONE')
    check("master NONE", result == {'FINISHED'}, result)
    prop_history = {}
    for frame in range(128, 153):
        goto(frame)
        prop_history[frame] = world_of(spear)
    goto(160)
    before = {side: bone_world(arm, b) for side, b in (('LEFT', "hand.L"), ('RIGHT', "hand.R"))}
    result = bpy.ops.prophandoff.attach_both_hands()
    check("attach_both_hands after master hand", result == {'FINISHED'}, result)
    goto(160)
    check("hands: no jump", all(mat_close(before[s], bone_world(arm, b), 1e-4)
                                for s, b in (('LEFT', "hand.L"), ('RIGHT', "hand.R"))))
    check("slot Hand_R (dormant, with history) removed from the prop", utils.get_slot_constraint(spear, "Hand_R") is None)
    drift = []
    for frame in range(128, 153):
        goto(frame)
        if not mat_close(prop_history[frame], world_of(spear), 1e-4):
            drift.append(frame)
    check("prop motion preserved after bake (frames 128..152)", not drift, drift)
    goto(160)
    offsets = {side: world_of(utils.find_grip_empty(spear, side)).inverted() @ bone_world(arm, b)
               for side, b in (('LEFT', "hand.L"), ('RIGHT', "hand.R"))}
    spear.keyframe_insert("location", frame=160)
    spear.location = (0.5, 0.5, 1.8)
    spear.keyframe_insert("location", frame=180)
    goto(180)
    follow = [s for s, b in (('LEFT', "hand.L"), ('RIGHT', "hand.R"))
              if not mat_close(world_of(utils.find_grip_empty(spear, s)).inverted() @ bone_world(arm, b),
                               offsets[s], 1e-4)]
    check("both hands follow the prop (constant offsets)", not follow, follow)
    check("no cycle (master hand then attach)", not log_has_cycle())
    bpy.ops.prophandoff.release_both_hands()


def test_rig_constraint_robustness(arm):
    """Rig's own constraint on the hand bone (Child Of → root, non-identity
    effect, like Auto-Rig Pro's Child Of → c_traj): attach, release and master
    hand stay jump-free thanks to the residual correction."""
    print("\n== Robustness: third-party constraint on the hand bone ==")
    spear = bpy.data.objects["Spear"]
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = spear
    goto(200)
    pb_r = arm.pose.bones["hand.R"]
    rig_con = pb_r.constraints.new('CHILD_OF')
    rig_con.name = "IK space (rig)"
    rig_con.target = arm
    rig_con.subtarget = "root"
    rig_con.inverse_matrix = Matrix.Identity(4)
    rig_con.set_inverse_pending = False
    root = arm.pose.bones["root"]
    root.location = (0.15, -0.1, 0.2)
    root.rotation_euler = (0.0, 0.0, 0.6)
    root.keyframe_insert("location", frame=200)
    root.keyframe_insert("rotation_euler", frame=200)
    bpy.context.view_layer.update()
    for side in utils.SIDES:
        bpy.ops.prophandoff.set_grip_pose(hand=side)
    before = bone_world(arm, "hand.R")
    result = bpy.ops.prophandoff.attach_both_hands()
    check("attach with third-party constraint FINISHED", result == {'FINISHED'}, result)
    goto(200)
    check("attach: no jump despite the rig's Child Of", mat_close(before, bone_world(arm, "hand.R"), 1e-4),
          "%s vs %s" % (mat_str(before), mat_str(bone_world(arm, "hand.R"))))
    goto(215)
    before = bone_world(arm, "hand.R")
    result = bpy.ops.prophandoff.release_both_hands()
    goto(215)
    check("release: no jump despite the rig's Child Of", mat_close(before, bone_world(arm, "hand.R"), 1e-4))
    goto(230)
    before_prop = world_of(spear)
    result = bpy.ops.prophandoff.set_master_hand(hand='RIGHT', other_hand='KEEP')
    check("master hand with third-party constraint FINISHED", result == {'FINISHED'}, result)
    goto(230)
    check("master hand: the prop does not jump", mat_close(before_prop, world_of(spear), 1e-4))
    check("no cycle (third-party constraint)", not log_has_cycle())
    bpy.ops.prophandoff.set_master_hand(hand='NONE')
    pb_r.constraints.remove(rig_con)
    root.location = (0, 0, 0)
    root.rotation_euler = (0, 0, 0)


class _DummyLayout:
    """Fake layout: accepts every call and attribute, and returns itself (an
    `operator()` returns an object whose properties are set)."""

    def __getattr__(self, name):
        return self

    def __setattr__(self, name, value):
        pass

    def __call__(self, *args, **kwargs):
        return self


class _FakePanel:
    def __init__(self):
        self.layout = _DummyLayout()


def test_prop_tabs():
    """Objects carrying PropHandoff data are listed as tabs; the pick operator
    switches the active prop without touching the selection, in any mode."""
    print("\n== Prop tabs ==")
    scene = bpy.context.scene
    spear, ball = bpy.data.objects["Spear"], bpy.data.objects["Ball"]
    fresh = make_mesh_object("NotAProp", (0.1, 0.1, 0.1))
    names = [obj.name for obj in utils.iter_props(scene)]
    check("props listed, sorted, without untouched objects nor object IK targets", names == ["Ball", "Bat", "Spear"], names)
    arm = bpy.data.objects["Rig"]
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='POSE')
    check("pick Spear from Pose mode", run_op(bpy.ops.prophandoff.pick_prop, object_name="Spear") == {'FINISHED'}
          and scene.prop_handoff.prop_object is spear and bpy.context.mode == 'POSE')
    check("pick Ball", run_op(bpy.ops.prophandoff.pick_prop, object_name="Ball") == {'FINISHED'}
          and scene.prop_handoff.prop_object is ball and utils.get_prop_object(bpy.context) is ball)
    check("unknown object refused", run_op(bpy.ops.prophandoff.pick_prop, object_name="Nope") == {'CANCELLED'}
          and scene.prop_handoff.prop_object is ball)
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.data.objects.remove(fresh, do_unlink=True)


def test_panel_draw_smoke():
    """Run every `draw()` / `poll()` / `draw_item()` with a fake layout, in
    several states (prop without setup, section 1 prop, section 2 prop, no
    prop): catches the UI runtime errors that headless cannot see otherwise."""
    print("\n== Panels: running draw() ==")
    scene = bpy.context.scene
    spear = bpy.data.objects["Spear"]
    ball = bpy.data.objects["Ball"]
    fresh = make_mesh_object("FreshProp", (0.1, 0.1, 0.1))
    errors = []
    panels = [c for c in panel.classes if issubclass(c, bpy.types.Panel)]
    for label, prop in (("spear (section 2)", spear), ("ball (section 1)", ball),
                        ("blank object", fresh), ("no prop", None)):
        scene.prop_handoff.prop_object = prop
        bpy.context.view_layer.objects.active = prop
        for cls in panels:
            try:
                if hasattr(cls, "poll") and not cls.poll(bpy.context):
                    continue
                cls.draw(_FakePanel(), bpy.context)
            except Exception as error:   # noqa: BLE001
                errors.append((label, cls.__name__, "%s: %s" % (type(error).__name__, error)))
        if prop is not None and len(prop.prop_handoff.slots):
            settings = prop.prop_handoff
            for index, item in enumerate(settings.slots):
                try:
                    panel.PROPHANDOFF_UL_slots.draw_item(
                        _FakePanel(), bpy.context, _DummyLayout(), settings, item, 0,
                        settings, "active_slot_index", index)
                except Exception as error:   # noqa: BLE001
                    errors.append((label, "UL_slots", "%s: %s" % (type(error).__name__, error)))
    check("draw()/poll()/draw_item() without exception (4 states)", not errors, errors[:3])
    bpy.data.objects.remove(fresh, do_unlink=True)


def test_lock_and_reach(arm, spear):
    """Lock: hands on the grips whatever the distance; arm reach: the spear
    stops when the right arm (IK chain) is extended; keyframed gate in Master
    Hand; no cycle; complete removal."""
    print("\n== Lock and arm reach ==")
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = spear
    settings = spear.prop_handoff
    goto(300)
    # The previous test left keys on `root` (moved shoulder): remove them to
    # start from a rig at rest.
    root = arm.pose.bones["root"]
    for path in ("location", "rotation_euler", "scale"):
        utils.remove_fcurves(arm, root.path_from_id(path))
    root.location = (0, 0, 0)
    root.rotation_euler = (0, 0, 0)
    root.scale = (1, 1, 1)
    # Hands put back near the body, spear between them, grips on the hands.
    pb_r = arm.pose.bones["hand.R"]
    pb_l = arm.pose.bones["hand.L"]
    # At rest the synthetic right arm is already extended (hand at the end of
    # the chain): fold the hand a bit towards the shoulder to start within reach.
    toward_shoulder = Vector((0.15, 0.03, 0.15)) * 0.4          # armature space
    pb_r.location = pb_r.bone.matrix_local.to_3x3().inverted() @ toward_shoulder
    for pb in (pb_l, pb_r):
        pb.keyframe_insert("location", frame=300)
    spear.location = (0.0, 0.0, 1.2)
    spear.rotation_euler = (0.0, 0.0, 0.0)
    spear.keyframe_insert("location", frame=300)
    spear.keyframe_insert("rotation_euler", frame=300)
    goto(300)
    for side in utils.SIDES:
        bpy.ops.prophandoff.set_grip_pose(hand=side)

    n0 = cycle_count()
    result = run_op(bpy.ops.prophandoff.grip_lock, action='LOCK')
    check("Lock FINISHED", result == {'FINISHED'}, result)
    goto(300)
    check("Grip Zone enabled by the Lock", all(utils.grip_zone_active(spear, s) for s in utils.SIDES))
    check("ph_grip_lock = 1 keyed (hold 0 at 299)",
          utils.grip_lock_active(spear)
          and utils.evaluate_fcurve(spear, utils.id_prop_path(utils.ID_PROP_GRIP_LOCK), 0, 299, 9.0) == 0.0)
    check("reach installed on the right only (IK chain)", utils.reach_sides(spear) == ['RIGHT'], utils.reach_sides(spear))
    reach = spear[utils.ID_PROP_REACH['RIGHT']]
    chain, shoulder = utils.find_ik_chain(arm, "hand.R")
    expected_reach = sum(b.bone.length for b in chain) * 1.25 * (1.0 - settings.reach_margin)
    check("reach = (upper arm + forearm) x scale x (1 - margin)", approx(reach, expected_reach, 1e-5), (reach, expected_reach))
    helper = utils.find_helper(spear, utils.ID_PROP_REACH_HELPER['RIGHT'])
    root_head = arm.matrix_world @ arm.pose.bones["shoulder.R"].matrix @ arm.data.bones["shoulder.R"].matrix_local.inverted() @ arm.data.bones["upper.R"].head_local
    check("reach helper at the chain root (head of upper.R)", vec_close(world_of(helper).translation, root_head, 1e-4),
          (tuple(world_of(helper).translation), tuple(root_head)))
    for attr in ("min_x", "max_x", "min_y", "max_y", "min_z", "max_z", "influence"):
        drv = next((d for d in spear.animation_data.drivers if d.data_path == 'constraints["PH_Reach"].' + attr), None)
        if drv is None or not drv.driver.is_simple_expression:
            check("simple PH_Reach drivers (%s)" % attr, False, drv)
            break
    else:
        check("PH_Reach drivers present and simple", True)

    # Grip rotated by the prop rotation: ph_rg_R* == R·(S·g), quaternion included.
    spear.rotation_mode = 'QUATERNION'
    spear.rotation_quaternion = Euler((0.3, -0.4, 0.9), 'XYZ').to_quaternion()
    spear.scale = (1.0, 1.3, 1.0)
    bpy.context.view_layer.update()
    ev = spear.evaluated_get(dg())
    g = utils.find_grip_empty(spear, 'RIGHT').matrix_basis.translation
    expected = spear.rotation_quaternion.to_matrix() @ Vector((g.x * 1.0, g.y * 1.3, g.z * 1.0))
    got = Vector((ev["ph_rg_Rx"], ev["ph_rg_Ry"], ev["ph_rg_Rz"]))
    check("rotated grip offset (quaternion + scale)", vec_close(got, expected, 1e-4), (tuple(got), tuple(expected)))
    spear.rotation_mode = 'XYZ'
    spear.rotation_euler = (0.0, 0.0, 0.0)
    spear.scale = (1.0, 1.0, 1.0)
    spear.keyframe_insert("rotation_euler", frame=300)

    # Within reach: no correction, the spear is exactly at its channels.
    goto(300)
    grip_r = utils.find_grip_empty(spear, 'RIGHT')
    check("within reach: no correction", vec_close(world_of(spear).translation, Vector(spear.location), 1e-5))
    grip_r = utils.find_grip_empty(spear, 'RIGHT')
    check("right hand on the grip (Lock)", vec_close(hand_head_world(arm, "hand.R"), world_of(grip_r).translation, 1e-4))

    # Spear pushed far ahead (1.5 u): it stops, the hand stays on it.
    spear.keyframe_insert("location", frame=300)
    spear.location = (1.5, 0.0, 1.2)
    spear.keyframe_insert("location", frame=330)
    lag, over = [], []
    for frame in range(300, 331, 3):
        goto(frame)
        dist = (world_of(grip_r).translation - world_of(helper).translation).length
        raw = (Vector(spear.location) + (world_of(spear).to_3x3() @ grip_r.matrix_basis.translation) - world_of(helper).translation).length
        if dist > reach + 1e-3:
            over.append((frame, dist))
        if abs(dist - min(raw, reach)) > 1e-3:
            lag.append((frame, round(dist, 4), round(min(raw, reach), 4)))
        if not vec_close(hand_head_world(arm, "hand.R"), world_of(grip_r).translation, 1e-4):
            lag.append((frame, "hand detached"))
    check("the right grip never exceeds the reach", not over, over[:3])
    check("distance = min(raw, reach) at every frame (no lag)", not lag, lag[:3])
    goto(330)
    check("the spear did stop (move < requested)", world_of(spear).translation.x < 1.4, world_of(spear).translation.x)
    check("left hand locked on its grip too",
          vec_close(hand_head_world(arm, "hand.L"), world_of(utils.find_grip_empty(spear, 'LEFT')).translation, 1e-4))
    check("no cycle (Lock + reach)", cycle_count() == n0)

    # Right master hand: gate at 0 (the Limit Location makes no sense when the
    # prop follows a hand); None: gate at 1.
    goto(340)
    before_prop = world_of(spear)          # stopped spear (non-zero correction)
    before_l = bone_world(arm, "hand.L")
    before_r = bone_world(arm, "hand.R")
    lock_history = {}
    for frame in range(300, 340, 4):
        goto(frame)
        lock_history[frame] = bone_world(arm, "hand.R")
    goto(340)
    result = run_op(bpy.ops.prophandoff.set_master_hand, hand='RIGHT', other_hand='KEEP')
    check("Master hand under Lock FINISHED", result == {'FINISHED'}, result)
    goto(340)
    check("Master hand under Lock: the stopped spear does not jump", mat_close(before_prop, world_of(spear), 1e-4),
          "%s vs %s" % (mat_str(before_prop), mat_str(world_of(spear))))
    check("Master hand under Lock: the left hand does not jump", mat_close(before_l, bone_world(arm, "hand.L"), 1e-4))
    check("Master hand under Lock: the right hand does not jump (zone baked)", mat_close(before_r, bone_world(arm, "hand.R"), 1e-4),
          "%s vs %s" % (mat_str(before_r), mat_str(bone_world(arm, "hand.R"))))
    drift = [f for f in lock_history if not (goto(f) or mat_close(lock_history[f], bone_world(arm, "hand.R"), 1e-4))]
    check("frames 300..339: the right hand stays where the Lock held it (bake)", not drift, drift[:4])
    goto(340)
    pb_r.keyframe_insert("location", frame=340)
    pb_r.location = Vector(pb_r.location) + Vector((0.02, 0.0, -0.03))
    pb_r.keyframe_insert("location", frame=348)
    goto(348)
    grip_r_now = utils.find_grip_empty(spear, 'RIGHT')
    check("Master hand under Lock: the spear follows the right hand (grip on the hand)",
          vec_close(world_of(grip_r_now).translation, hand_head_world(arm, "hand.R"), 1e-4))
    goto(345)
    inf = spear.evaluated_get(dg()).constraints["PH_Reach"].influence
    check("gate closed in Master hand (PH_Reach influence = 0)", approx(inf, 0.0, 1e-6), inf)
    check("Lock still active (left hand on the spear)", utils.grip_lock_active(spear)
          and vec_close(hand_head_world(arm, "hand.L"), world_of(utils.find_grip_empty(spear, 'LEFT')).translation, 1e-4))
    goto(350)
    run_op(bpy.ops.prophandoff.set_master_hand, hand='NONE')
    goto(355)
    inf = spear.evaluated_get(dg()).constraints["PH_Reach"].influence
    check("gate reopened after None (influence = 1)", approx(inf, 1.0, 1e-6), inf)
    goto(345)
    inf = spear.evaluated_get(dg()).constraints["PH_Reach"].influence
    check("history: gate still closed at frame 345", approx(inf, 0.0, 1e-6), inf)

    # Unlock: back to the proximity blend, reach inactive.
    goto(360)
    result = run_op(bpy.ops.prophandoff.grip_lock, action='UNLOCK')
    check("Unlock FINISHED", result == {'FINISHED'}, result)
    goto(365)
    check("Lock inactive", not utils.grip_lock_active(spear))
    inf = spear.evaluated_get(dg()).constraints["PH_Reach"].influence
    check("reach inactive without Lock", approx(inf, 0.0, 1e-6), inf)
    goto(320)
    check("history: Lock still active at frame 320", utils.grip_lock_active(spear))
    check("no cycle (end of Lock)", cycle_count() == n0)

    result = run_op(bpy.ops.prophandoff.reach_limit, action='DISABLE')
    check("reach removal", result == {'FINISHED'} and not utils.reach_limit_present(spear)
          and not utils.reach_sides(spear) and "ph_rt_x" not in spear.keys()
          and not any(str(k).startswith("ph_rp") for k in spear.keys())
          and not any("PH_Reach" in d.data_path or "ph_r" in d.data_path for d in spear.animation_data.drivers)
          and not any(utils.master_reach_constraints(arm.pose.bones[b]) for b in ("hand.L", "hand.R")))
    run_op(bpy.ops.prophandoff.release_both_hands)
    run_op(bpy.ops.prophandoff.disable_grip_zone)


def test_master_switch_and_freeze(arm, spear):
    """Left → Right → None: the hand that stops driving gets its Grip Zone
    back, "None" restores both; an unkeyed move made before a click is frozen
    instead of being lost."""
    print("\n== Master hand: L→R→None switch and unkeyed edits ==")
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = spear
    goto(380)
    root = arm.pose.bones["root"]
    for path in ("location", "rotation_euler", "scale"):
        utils.remove_fcurves(arm, root.path_from_id(path))
    root.location = (0, 0, 0)
    root.rotation_euler = (0, 0, 0)
    for side in utils.SIDES:
        run_op(bpy.ops.prophandoff.set_grip_pose, hand=side)
    check("Enable", run_op(bpy.ops.prophandoff.enable_grip_zone) == {'FINISHED'})
    check("intention stored", utils.grip_zone_wanted(spear))
    n0 = cycle_count()

    goto(380)
    check("Left", run_op(bpy.ops.prophandoff.set_master_hand, hand='LEFT') == {'FINISHED'})
    goto(380)
    check("Left: left zone removed, right zone kept",
          not utils.grip_blend_present(spear, 'LEFT') and utils.grip_zone_active(spear, 'RIGHT'))
    before_prop = world_of(spear)
    before_l = bone_world(arm, "hand.L")
    before_r = bone_world(arm, "hand.R")
    check("Right (from Left)", run_op(bpy.ops.prophandoff.set_master_hand, hand='RIGHT') == {'FINISHED'})
    goto(380)
    check("Right: nothing jumps", mat_close(before_prop, world_of(spear), 1e-4)
          and mat_close(before_l, bone_world(arm, "hand.L"), 1e-4) and mat_close(before_r, bone_world(arm, "hand.R"), 1e-4))
    check("Right: right zone removed, left zone restored (no attach)",
          not utils.grip_blend_present(spear, 'RIGHT') and utils.grip_zone_active(spear, 'LEFT')
          and utils.attach_constraint(spear, 'LEFT') is None)
    check("Right: the prop follows the right hand", utils.holding_side(spear) == 'RIGHT')
    pb_r = arm.pose.bones["hand.R"]
    pb_r.keyframe_insert("location", frame=380)
    pb_r.location = Vector(pb_r.location) + Vector((0.02, -0.01, 0.015))
    pb_r.keyframe_insert("location", frame=390)
    goto(390)
    check("Right: the spear follows the right hand, the left one stays on it (zone)",
          vec_close(world_of(utils.find_grip_empty(spear, 'RIGHT')).translation, hand_head_world(arm, "hand.R"), 1e-4)
          and vec_close(world_of(utils.find_grip_empty(spear, 'LEFT')).translation, hand_head_world(arm, "hand.L"), 1e-3))
    before_prop = world_of(spear)
    check("None", run_op(bpy.ops.prophandoff.set_master_hand, hand='NONE') == {'FINISHED'})
    goto(390)
    check("None: the prop does not jump", mat_close(before_prop, world_of(spear), 1e-4))
    check("None: both Grip Zones are there", all(utils.grip_zone_active(spear, side) for side in utils.SIDES))
    check("None: no active slot, no attach", utils.holding_side(spear) is None
          and all(utils.attach_constraint(spear, side) is None for side in utils.SIDES))
    check("no cycle (switch)", cycle_count() == n0)

    # --- unkeyed edit before a click --------------------------------------------
    goto(399)
    pb_l = arm.pose.bones["hand.L"]
    world_399 = bone_world(arm, "hand.L")
    goto(400)
    keyed = Vector(pb_l.location)
    pb_l.location = keyed + Vector((0.03, 0.0, 0.02))       # with the mouse, no key
    bpy.context.view_layer.update()
    moved_world = bone_world(arm, "hand.L")
    prop_world = world_of(spear)
    check("unkeyed edit detected", utils.has_unkeyed_edit(pb_l, 400))
    check("Left with an unkeyed edit", run_op(bpy.ops.prophandoff.set_master_hand, hand='LEFT') == {'FINISHED'})
    goto(400)
    check("the unkeyed edit is kept (left hand frozen where it was)",
          mat_close(moved_world, bone_world(arm, "hand.L"), 1e-4),
          "%s vs %s" % (mat_str(moved_world), mat_str(bone_world(arm, "hand.L"))))
    check("the prop does not jump", mat_close(prop_world, world_of(spear), 1e-4),
          "%s vs %s" % (mat_str(prop_world), mat_str(world_of(spear))))
    goto(399)
    check("frame 399: the left hand is visually where it was (hold + bake)",
          mat_close(world_399, bone_world(arm, "hand.L"), 1e-4),
          "%s vs %s" % (mat_str(world_399), mat_str(bone_world(arm, "hand.L"))))
    goto(400)
    run_op(bpy.ops.prophandoff.set_master_hand, hand='NONE')
    run_op(bpy.ops.prophandoff.release_both_hands)
    run_op(bpy.ops.prophandoff.disable_grip_zone)


def test_dependencies_and_rigify_chain(arm, spear):
    """Static dependency analysis (`utils.bone_depends_on`), IK chain whose
    target is a child of the controller (Rigify), slot targeting a deform bone
    that depends on the hand: suspended, assignable, no cycle."""
    print("\n== Static dependencies, Rigify-like IK chain, dependent slot ==")
    check("fore.R depends on hand.R (IK)", utils.bone_depends_on(arm, "fore.R", "hand.R"))
    check("upper.R depends on hand.R (whole IK chain)", utils.bone_depends_on(arm, "upper.R", "hand.R"))
    check("shoulder.R does not depend on hand.R", not utils.bone_depends_on(arm, "shoulder.R", "hand.R"))
    check("hand.L does not depend on hand.R", not utils.bone_depends_on(arm, "hand.L", "hand.R"))
    check("def_hand.R depends on hand.R (Copy Transforms)", utils.bone_depends_on(arm, "def_hand.R", "hand.R"))
    check("ik_tgt.R depends on ctrl2.R (parenting)", utils.bone_depends_on(arm, "ik_tgt.R", "ctrl2.R"))
    check("swing.R depends on ctrl2.R (Damped Track → ik_tgt.R)", utils.bone_depends_on(arm, "swing.R", "ctrl2.R"))
    check("shoulder2.R does not depend on ctrl2.R", not utils.bone_depends_on(arm, "shoulder2.R", "ctrl2.R"))
    check("a bone depends on itself", utils.bone_depends_on(arm, "hand.R", "hand.R"))

    empty = make_empty("PH_Test_BoneChild")
    empty.parent = arm
    empty.parent_type = 'BONE'
    empty.parent_bone = "def_hand.R"
    check("object parented to def_hand.R: depends on hand.R", utils.target_depends_on_handle(empty, "", arm, "hand.R"))
    check("… but not on hand.L", not utils.target_depends_on_handle(empty, "", arm, "hand.L"))
    check("the armature object does not depend on its bones", not utils.target_depends_on_handle(arm, "", arm, "hand.R"))
    check("identical target = dependent", utils.target_depends_on_handle(arm, "hand.R", arm, "hand.R"))
    check("object IK handle: its children depend on it", utils.target_depends_on_handle(empty, "", arm, ""))
    bpy.data.objects.remove(empty, do_unlink=True)

    found = utils.find_ik_chain(arm, "hand.R")
    check("find_ik_chain(hand.R): fore.R→upper.R, shoulder shoulder.R",
          found is not None and [b.name for b in found[0]] == ["fore.R", "upper.R"] and found[1].name == "shoulder.R",
          found and ([b.name for b in found[0]], found[1].name))
    found = utils.find_ik_chain(arm, "ctrl2.R")
    check("find_ik_chain(ctrl2.R) through ik_tgt.R: fore2.R→up2.R, shoulder shoulder2.R (swing.R skipped)",
          found is not None and [b.name for b in found[0]] == ["fore2.R", "up2.R"] and found[1].name == "shoulder2.R",
          found and ([b.name for b in found[0]], found[1].name))
    check("find_ik_chain(hand.L): no chain", utils.find_ik_chain(arm, "hand.L") is None)

    # --- slot targeting the deform bone def_hand.R --------------------------
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = spear
    settings = spear.prop_handoff
    settings.ik_object_l = arm
    settings.ik_bone_l = "hand.L"
    settings.ik_object_r = arm
    settings.ik_bone_r = "hand.R"
    goto(420)
    for side in utils.SIDES:
        run_op(bpy.ops.prophandoff.set_grip_pose, hand=side)
    slot = settings.slots.add()
    slot.name = "Deform_R"
    slot.stored_name = "Deform_R"
    slot.label = "Right hand (deform)"
    slot.target = arm
    slot.subtarget = "def_hand.R"
    utils.ensure_constraints(spear)
    utils.store_slots_id_prop(spear)
    check("slot Deform_R: constraint created (no hand follows the prop)",
          utils.get_slot_constraint(spear, "Deform_R") is not None)
    n0 = cycle_count()
    check("Enable with a dormant dependent slot", run_op(bpy.ops.prophandoff.enable_grip_zone) == {'FINISHED'})
    goto(420)
    goto(421)
    check("dependent slot suspended (constraint removed)",
          utils.get_slot_constraint(spear, "Deform_R") is None and utils.slot_is_suspended(spear, slot))
    check("no dependency warning", utils.dependency_warning(spear) is None)
    check("right hand on its grip (Grip Zone)",
          vec_close(hand_head_world(arm, "hand.R"), world_of(utils.find_grip_empty(spear, 'RIGHT')).translation, 1e-4))
    check("no cycle (dependent slot + Grip Zone)", cycle_count() == n0)

    before = world_of(spear)
    check("Assign Deform_R", run_op(bpy.ops.prophandoff.assign, slot_name="Deform_R") == {'FINISHED'})
    goto(421)
    check("Assign: the prop does not jump", mat_close(before, world_of(spear), 1e-4),
          "%s vs %s" % (mat_str(before), mat_str(world_of(spear))))
    check("right Grip Zone removed (anti-cycle), left one kept",
          not utils.grip_blend_present(spear, 'RIGHT') and utils.grip_zone_active(spear, 'LEFT'))
    check("the prop follows def_hand.R", utils.active_slot_name(spear) == "Deform_R")
    pb_r = arm.pose.bones["hand.R"]
    pb_r.keyframe_insert("location", frame=421)
    pb_r.location = Vector(pb_r.location) + Vector((0.03, 0.02, -0.02))
    pb_r.keyframe_insert("location", frame=428)
    goto(421)
    offset = bone_world(arm, "def_hand.R").inverted() @ world_of(spear)
    goto(428)
    check("constant offset prop ↔ def_hand.R", mat_close(offset, bone_world(arm, "def_hand.R").inverted() @ world_of(spear), 1e-4))
    check("no cycle (Assign dependent slot)", cycle_count() == n0)

    success, message, _level = operators.enable_grip_zone(bpy.context, spear)
    check("Enable refused while the prop follows the dependent bone", not success and "release it first" in message, message)
    check("no cycle (refusal)", cycle_count() == n0)

    goto(430)
    check("Release", run_op(bpy.ops.prophandoff.release) == {'FINISHED'})
    check("Enable after Release", run_op(bpy.ops.prophandoff.enable_grip_zone) == {'FINISHED'})
    goto(430)
    goto(431)
    check("dependent slot suspended again (history baked)", utils.get_slot_constraint(spear, "Deform_R") is None)
    check("both Grip Zones active", all(utils.grip_zone_active(spear, side) for side in utils.SIDES))
    check("no cycle (end)", cycle_count() == n0)
    run_op(bpy.ops.prophandoff.disable_grip_zone)
    index = [i for i, item in enumerate(settings.slots) if item.name == "Deform_R"][0]
    settings.slots.remove(index)
    utils.remove_slot_data(spear, "Deform_R")
    utils.store_slots_id_prop(spear)




def test_master_reach(arm, spear):
    """Master hand under Lock: the stop moves to the master controller. Right
    master (own IK chain): the controller is clamped to its reach. Left master
    (no chain on the left): the controller is clamped so that the RIGHT grip
    stays within the right arm's reach. Unlock frees, Lock clamps again, None
    bakes and removes the constraints."""
    print("\n== Master hand under Lock: reach on the controller ==")
    scene = bpy.context.scene
    scene.prop_handoff.prop_object = spear
    goto(450)
    root = arm.pose.bones["root"]
    for path in ("location", "rotation_euler", "scale"):
        utils.remove_fcurves(arm, root.path_from_id(path))
    root.location = (0, 0, 0)
    root.rotation_euler = (0, 0, 0)
    pb_r, pb_l = arm.pose.bones["hand.R"], arm.pose.bones["hand.L"]
    toward_shoulder = Vector((0.15, 0.03, 0.15)) * 0.4
    pb_r.location = pb_r.bone.matrix_local.to_3x3().inverted() @ toward_shoulder
    pb_l.location = (0, 0, 0)
    for pb in (pb_l, pb_r):
        pb.keyframe_insert("location", frame=450)
    spear.location = (0.0, 0.0, 1.2)
    spear.rotation_euler = (0.0, 0.0, 0.0)
    spear.keyframe_insert("location", frame=450)
    spear.keyframe_insert("rotation_euler", frame=450)
    goto(450)
    for side in utils.SIDES:
        run_op(bpy.ops.prophandoff.set_grip_pose, hand=side)
    n0 = cycle_count()
    check("Lock", run_op(bpy.ops.prophandoff.grip_lock, action='LOCK') == {'FINISHED'})
    goto(450)
    reach_r = spear[utils.ID_PROP_REACH['RIGHT']]
    helper_r = utils.find_helper(spear, utils.ID_PROP_REACH_HELPER['RIGHT'])
    grip_r, grip_l = utils.find_grip_empty(spear, 'RIGHT'), utils.find_grip_empty(spear, 'LEFT')

    # --- Right master: own chain -> PH_ReachMaster_self on hand.R ----------
    check("Right master under Lock", run_op(bpy.ops.prophandoff.set_master_hand, hand='RIGHT') == {'FINISHED'})
    goto(450)
    names = [c.name for c in utils.master_reach_constraints(pb_r)]
    check("hand.R: self constraint only (left has no chain)", names == ["PH_ReachMaster_self", "PH_ReachMaster_self2"], names)
    check("self constraint: Limit Distance inside, keyed influence 1",
          all(c.type == 'LIMIT_DISTANCE' and c.limit_mode == 'LIMITDIST_INSIDE' and approx(c.influence, 1.0)
              and c.target is helper_r and approx(c.distance, reach_r, 1e-6) for c in utils.master_reach_constraints(pb_r)))
    pb_r.keyframe_insert("location", frame=450)
    frame_r = frame_expected_world(arm, "hand.R")
    far = world_of(helper_r).translation + (bone_world(arm, "hand.R").translation - world_of(helper_r).translation).normalized() * 2.0
    pb_r.location = frame_r.inverted() @ far
    pb_r.keyframe_insert("location", frame=460)
    goto(460)
    dist = (bone_world(arm, "hand.R").translation - world_of(helper_r).translation).length
    check("hand.R pushed 2 u away: clamped at its reach (%.3f)" % reach_r, approx(dist, reach_r, 2e-3), dist)
    check("the spear follows the clamped controller (grip R on hand.R)",
          vec_close(world_of(grip_r).translation, hand_head_world(arm, "hand.R"), 1e-4))
    check("left hand locked on its grip", vec_close(hand_head_world(arm, "hand.L"), world_of(grip_l).translation, 1e-4))
    check("no cycle (right master under Lock)", cycle_count() == n0)
    before = world_of(spear)
    check("None", run_op(bpy.ops.prophandoff.set_master_hand, hand='NONE') == {'FINISHED'})
    goto(460)
    check("None: the spear does not jump", mat_close(before, world_of(spear), 1e-4), "%s vs %s" % (mat_str(before), mat_str(world_of(spear))))
    check("None: constraints removed from hand.R", not utils.master_reach_constraints(pb_r))
    goto(455)
    dist = (bone_world(arm, "hand.R").translation - world_of(helper_r).translation).length
    check("None: history baked (hand.R within reach at frame 455)", dist <= reach_r + 2e-3, dist)

    # --- Left master: no chain on the left -> PH_ReachMaster_other on hand.L
    goto(470)
    check("Left master under Lock", run_op(bpy.ops.prophandoff.set_master_hand, hand='LEFT') == {'FINISHED'})
    goto(470)
    names = [c.name for c in utils.master_reach_constraints(pb_l)]
    target = utils.find_helper(spear, utils.ID_PROP_REACH_TARGET['RIGHT'])
    check("hand.L: other constraint only, target = shoulder R - grip offset",
          names == ["PH_ReachMaster_other", "PH_ReachMaster_other2"] and target is not None
          and all(c.target is target for c in utils.master_reach_constraints(pb_l)), names)
    offset = world_of(grip_r).translation - bone_world(arm, "hand.L").translation
    check("reach target = helper R - (grip R - controller L)",
          vec_close(world_of(target).translation, world_of(helper_r).translation - offset, 1e-4))
    pb_l.keyframe_insert("location", frame=470)
    frame_l = frame_expected_world(arm, "hand.L")
    far = world_of(helper_r).translation + (world_of(grip_r).translation - world_of(helper_r).translation).normalized() * 2.0 - offset
    pb_l.location = frame_l.inverted() @ far
    pb_l.keyframe_insert("location", frame=480)
    goto(480)
    dist = (world_of(grip_r).translation - world_of(helper_r).translation).length
    check("hand.L pushed 2 u away: grip R clamped at the right reach (%.3f)" % reach_r, approx(dist, reach_r, 2e-3), dist)
    check("grip L on hand.L, hand.R locked on grip R",
          vec_close(world_of(grip_l).translation, hand_head_world(arm, "hand.L"), 1e-4)
          and vec_close(hand_head_world(arm, "hand.R"), world_of(grip_r).translation, 1e-4))
    goto(485)
    check("Unlock during master", run_op(bpy.ops.prophandoff.grip_lock, action='UNLOCK') == {'FINISHED'})
    goto(486)
    check("unlocked: constraints keyed to 0, controller free", all(approx(c.influence, 0.0) for c in utils.master_reach_constraints(pb_l))
          and (world_of(grip_r).translation - world_of(helper_r).translation).length > reach_r + 0.1)
    goto(490)
    check("Lock again during master", run_op(bpy.ops.prophandoff.grip_lock, action='LOCK') == {'FINISHED'})
    goto(491)
    dist = (world_of(grip_r).translation - world_of(helper_r).translation).length
    check("locked again: grip R clamped", approx(dist, reach_r, 2e-3), dist)
    check("no cycle (left master under Lock)", cycle_count() == n0)
    goto(495)
    run_op(bpy.ops.prophandoff.set_master_hand, hand='NONE')
    check("None: constraints and target helper removed", not utils.master_reach_constraints(pb_l)
          and utils.find_helper(spear, utils.ID_PROP_REACH_TARGET['RIGHT']) is None)
    run_op(bpy.ops.prophandoff.reach_limit, action='DISABLE')
    run_op(bpy.ops.prophandoff.release_both_hands)
    run_op(bpy.ops.prophandoff.disable_grip_zone)
    check("no cycle (end of master reach)", cycle_count() == n0)


def test_cycle_detection_is_observable():
    """A deliberate cycle must show up in the log: otherwise the "no cycle"
    tests would prove nothing."""
    print("\n== Control: cycle detection observable ==")
    a = make_empty("CycleA")
    b = make_empty("CycleB")
    ca = a.constraints.new('COPY_LOCATION'); ca.target = b
    cb = b.constraints.new('COPY_LOCATION'); cb.target = a
    before_cycles = cycle_count()
    bpy.context.view_layer.update()
    goto(bpy.context.scene.frame_current + 1)
    check("deliberate cycle detected in the output", cycle_count() > before_cycles)
    EXPECTED_CYCLES[0] = cycle_count()
    bpy.data.objects.remove(a, do_unlink=True)
    bpy.data.objects.remove(b, do_unlink=True)


def main():
    print("Blender", bpy.app.version_string, "| autoexec:", bpy.context.preferences.filepaths.use_scripts_auto_execute)
    try:
        test_registration()
        arm, ball, spear = build_scene()
        test_section1(arm, ball)
        test_event_listing_spec_case()
        test_frame_helper_math(arm)
        test_section2(arm, spear)
        test_object_ik_targets()
        test_switch_to_twohanded(arm)
        test_master_then_attach(arm)
        test_rig_constraint_robustness(arm)
        test_lock_and_reach(arm, spear)
        test_master_switch_and_freeze(arm, spear)
        test_dependencies_and_rigify_chain(arm, spear)
        test_master_reach(arm, spear)
        test_prop_tabs()
        test_panel_draw_smoke()
        check("no cycle over the whole session", not log_has_cycle())
        test_cycle_detection_is_observable()
        prop_handoff.unregister()
        check("clean unregister", not hasattr(bpy.types.Object, "prop_handoff"))
    except Exception:
        traceback.print_exc()
        RESULTS.append(("FAIL", "uncaught exception"))
    failed = [label for status, label in RESULTS if status == "FAIL"]
    print("\n==== %d tests, %d failure(s) ====" % (len(RESULTS), len(failed)))
    for label in failed:
        print("  FAIL:", label)
    print("RESULT:", "OK" if not failed else "FAILED")
    flush_c_output()


main()
