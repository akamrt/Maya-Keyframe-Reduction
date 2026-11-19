"""Headless check of 'Target selected anim layers' with a fake maya.cmds.

Run from the repo root:  python tests/test_selected_layers.py
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "..", "animator_curve_reducer.py")

# --- fake maya package --------------------------------------------------------
LAYERS = {
    "BaseAnimation": dict(selected=False, lock=False, link=False,
                          curves={"tail.rotateZ": "tail_rotateZ"}),
    "wave": dict(selected=True, lock=False, link=False,
                 curves={"tail.rotateY": "tail_rotateY_wave",
                         "tail.translateX": "tail_translateX_wave",
                         "tail.visibility": "tail_visibility_wave"}),
    "locked": dict(selected=True, lock=True, link=False,
                   curves={"tail.rotateX": "tail_rotateX_locked"}),
    "detail": dict(selected=True, lock=False, link=True,
                   curves={"tail.rotateY": "tail_rotateY_detail"}),
    "other": dict(selected=False, lock=False, link=False,
                  curves={"tail.rotateX": "tail_rotateX_other"}),
}
CURVES = {c: plug for L in LAYERS.values() for plug, c in L["curves"].items()}
CURVES["ref:tail_rotateY_wave"] = "tail.rotateY"
OPTIONS = {}
KEY_SELECTION = ["tail_rotateZ"]
CHECKBOX = {"value": False}


def _fn(default=None):
    return lambda *a, **k: default


cmds = types.ModuleType("maya.cmds")
cmds.__getattr__ = lambda name: _fn()


def animLayer(layer=None, query=False, **k):
    L = LAYERS[layer]
    if k.get("selected"):
        return L["selected"]
    if k.get("lock"):
        return L["lock"]
    if k.get("attribute"):
        return list(L["curves"])
    if "findCurveForPlug" in k:
        return [L["curves"][k["findCurveForPlug"]]]


def objExists(name):
    node, _, attr = name.partition(".")
    if node in LAYERS:
        return not attr or (attr == "acrLinkedDetailTiming" and LAYERS[node]["link"])
    return node in CURVES


def nodeType(node):
    plug = CURVES[node]
    return "animCurveTA" if ".rotate" in plug else "animCurveTL" if ".translate" in plug else "animCurveTU"


def optionVar(**k):
    if "exists" in k:
        return k["exists"] in OPTIONS
    if "query" in k:
        return OPTIONS[k["query"]]
    for flag in ("intValue", "floatValue", "stringValue"):
        if flag in k:
            OPTIONS[k[flag][0]] = k[flag][1]


def keyframe(*a, **k):
    if k.get("selected") and k.get("name"):
        return list(KEY_SELECTION)
    if k.get("timeChange"):
        return [1.0, 2.0, 3.0, 4.0]
    if k.get("valueChange"):
        return [0.0, 1.0, 0.0, 1.0]
    return []


def checkBoxGrp(control, **k):
    if k.get("exists"):
        return True
    if k.get("query"):
        return CHECKBOX["value"]


cmds.animLayer = animLayer
cmds.objExists = objExists
cmds.nodeType = nodeType
cmds.optionVar = optionVar
cmds.keyframe = keyframe
cmds.checkBoxGrp = checkBoxGrp
cmds.ls = lambda *a, **k: list(LAYERS) if k.get("type") == "animLayer" else []
cmds.referenceQuery = lambda node, **k: node.startswith("ref:")
cmds.lockNode = lambda *a, **k: [False]
cmds.listConnections = lambda *a, **k: (
    [CURVES[a[0].split(".")[0]]] if a and a[0].endswith(".output") else []
)
cmds.getAttr = lambda plug, **k: "bool" if plug.endswith("visibility") else "doubleAngle"
cmds.getPanel = lambda *a, **k: []

maya = types.ModuleType("maya")
api = types.ModuleType("maya.api")
for name in ("maya.api.OpenMaya", "maya.api.OpenMayaAnim", "maya.OpenMayaUI"):
    sys.modules[name] = types.ModuleType(name)
maya.cmds = cmds
maya.api = api
sys.modules.update({"maya": maya, "maya.cmds": cmds, "maya.api": api})
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

g = {"__name__": "animator_curve_reducer"}
exec(compile(open(SOURCE).read(), "animator_curve_reducer.py", "exec"), g)
g["UI"]["target_selected_layers"] = "checkbox"

failures = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("  " + detail if detail else ""))
    if not ok:
        failures.append(label)


# Off: the old behaviour (selected keys win, no layer curves).
work, skipped, had_keys = g["_selected_key_work_items"]()
check("scope off: selected keys still take priority", had_keys)
curves, skipped, source = g["collect_target_curves"]()
check("scope off: layers are not targeted", curves == [], str(curves))

# On.
CHECKBOX["value"] = True
curves, skipped, source = g["collect_target_curves"]()
check("scope on: every reducible curve of the selected layer",
      curves == ["tail_rotateY_wave", "tail_translateX_wave"], str(curves))
reasons = dict(skipped)
check("locked layer skipped with a reason", "locked" in reasons and "locked" in reasons["locked"], str(skipped))
check("linked detail layer skipped with a reason", "linked detail" in reasons.get("detail", ""))
check("bool channel skipped", "discrete" in reasons.get("tail_visibility_wave", ""))
check("unselected layers and the base layer left out",
      not set(curves) & {"tail_rotateZ", "tail_rotateX_other"})
check("source names the layers", "selected anim layer" in source, source)
check("Graph Editor key selection is ignored in layer scope",
      g["_selected_key_work_items"]() == ([], [], False))
items, _skipped, src = g["_collect_action_work_items"]()
check("Reduce / Preview work items come from the layers",
      [i["curve"] for i in items] == curves and all(i["selected_times"] is None for i in items))
text = g["_target_summary_text"]()
check("target summary names the layers", "2 channel(s) on 3 selected anim layer(s)" in text, text)

LAYERS["BaseAnimation"]["selected"] = True
curves, _s, _src = g["collect_target_curves"]()
check("selecting the base layer includes its curves", "tail_rotateZ" in curves)

for L in LAYERS.values():
    L["selected"] = False
text = g["_target_summary_text"]()
check("nothing selected: summary asks for a layer", "Select one or more unlocked anim layers" in text, text)
try:
    g["_collect_action_work_items"]()
    check("nothing selected: reducing reports it", False)
except RuntimeError as exc:
    check("nothing selected: reducing reports it", "No reducible" in str(exc), str(exc))

g["_layer_scope_toggled"](True)
check("toggle is remembered", OPTIONS.get("animatorCurveReducer_targetSelectedLayers") == 1)
print("FAILURES:", len(failures))
sys.exit(1 if failures else 0)
