"""
Animator Curve Reducer
======================

Paste this entire file into Maya's Python Script Editor and run it.

The tool reduces time-based Maya animCurves with a recursive cubic Hermite fit.
It starts with endpoints and meaningful extrema, solves smooth tangent slopes,
then adds the worst-fitting sample until the requested error is satisfied.

An optional output mode bakes the original evaluated motion to a muted
additive animation layer, leaving the reduced curves as the editable base.
Base-aligned additive anchors keep the intervening baked detail as Maya
breakdown keys that can be synchronized after animator retiming.

No third-party Python packages are required.
"""

from __future__ import division, print_function

import json
import math
import time
import traceback

import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.api.OpenMayaAnim as oma
import maya.OpenMayaUI as omui

try:
    from PySide6 import QtCore, QtWidgets
    from shiboken6 import wrapInstance
except ImportError:
    try:
        from PySide2 import QtCore, QtWidgets
        from shiboken2 import wrapInstance
    except ImportError:
        from PySide import QtCore, QtGui
        from shiboken import wrapInstance
        QtWidgets = QtGui


TOOL_NAME = "Animator Curve Reducer"
WINDOW_NAME = "animatorCurveReducerWindow"
VERSION = "0.10.1"
MAX_SAMPLES_PER_CURVE = 20000
VERIFY_CORRECTION_LIMIT = 32
FALLBACK_VERIFY_CORRECTION_LIMIT = 96
PREVIEW_UPDATE_INTERVAL_MS = 90
PREVIEW_WARNING_CURVE_COUNT = 50
PREVIEW_SAMPLE_CURVE_COUNT = 25
BATCH_STEPS_PER_CURVE = 5
LINK_DATA_ATTR = "acrLinkedDetailTiming"
LINK_DATA_VERSION = 1
AUTO_SYNC_INTERVAL_MS = 140
AUTO_SYNC_WATCH_MIN_MS = 300
AUTO_SYNC_WATCH_MAX_MS = 1500

OPTION_PREFIX = "animatorCurveReducer_"

DEFAULT_SETTINGS = {
    "preset": "Balanced",
    "error_mode": "Percent of curve range",
    "maximum_error": 0.5,
    "sample_step": 1.0,
    "preserve_extrema": True,
    "extrema_prominence": 0.25,
    "extrema_window": 4.0,
    "maximum_keys": 0,
    "create_buffer": True,
    "create_additive_layer": False,
    "auto_sync_linked_timing": True,
}

FIT_PRESETS = {
    "Light": {
        "error_mode": "Percent of curve range",
        "maximum_error": 0.25,
        "sample_step": 1.0,
        "preserve_extrema": True,
        "extrema_prominence": 0.15,
        "extrema_window": 3.0,
        "maximum_keys": 0,
    },
    "Balanced": {
        "error_mode": "Percent of curve range",
        "maximum_error": 0.5,
        "sample_step": 1.0,
        "preserve_extrema": True,
        "extrema_prominence": 0.25,
        "extrema_window": 4.0,
        "maximum_keys": 0,
    },
    "Aggressive": {
        "error_mode": "Percent of curve range",
        "maximum_error": 1.0,
        "sample_step": 1.0,
        "preserve_extrema": True,
        "extrema_prominence": 0.4,
        "extrema_window": 5.0,
        "maximum_keys": 0,
    },
}

# UI design selection applied from the Codex visual mock-up: Alternative 1
# purple accent, hidden help, uppercase headings, 10 px density and 6 px radius.
UI_DENSITY = 10
UI_CORNER_RADIUS = 6

UI = {}
LAST_TARGETS = []
TARGET_OVERRIDE = {"curves": None, "items": None, "source": ""}
if "TARGET_WATCH_TIMER" not in globals():
    TARGET_WATCH_TIMER = None
if "SCRIPT_JOBS" not in globals():
    SCRIPT_JOBS = []
if "LINK_AUTO_SYNC" not in globals():
    LINK_AUTO_SYNC = {
        "callback_ids": [],
        "timer": None,
        "watch_timer": None,
        "busy": False,
        "enabled": False,
        "base_curves": set(),
        "timing_signatures": {},
    }
for _name, _default in (
        ("callback_ids", []), ("timer", None), ("watch_timer", None),
        ("busy", False), ("enabled", False), ("base_curves", set()),
        ("timing_signatures", {})):
    LINK_AUTO_SYNC.setdefault(_name, _default)
BATCH_DIAGNOSTICS = {
    "start_time": None,
    "action": "",
    "lines": [],
}


# Toolcraft-inspired design tokens translated to Maya/Qt.  The stylesheet is
# installed only on this tool's window, so it cannot leak into Maya's UI.
TOOLCRAFT_QSS = r"""
QWidget {
    color: #e9ebef;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 12px;
    selection-background-color: #7c3fd0;
    selection-color: #ffffff;
}
QWidget[acrRole="window"] {
    background-color: #15171a;
}
QWidget[acrRole="header"] {
    background-color: #101114;
    border: 1px solid #2d3036;
    border-radius: __ACR_RADIUS__px;
}
QWidget[acrRole="panel"] {
    background-color: #202226;
    border: 1px solid #32363e;
    border-radius: __ACR_RADIUS__px;
}
QWidget[acrRole="status"] {
    background-color: #241b30;
    border: 1px solid #553779;
    border-radius: __ACR_RADIUS__px;
}
QLabel {
    background-color: transparent;
    border: none;
}
QLabel[acrRole="title"] {
    color: #ffffff;
    font-size: 16px;
    font-weight: 600;
}
QLabel[acrRole="eyebrow"] {
    color: #b78bf7;
    font-size: 10px;
    font-weight: 600;
}
QLabel[acrRole="muted"] {
    color: #9ba1ab;
}
QLabel[acrRole="version"] {
    color: #7f8792;
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 10px;
}
QPushButton {
    background-color: #292c31;
    color: #e9ebef;
    border: 1px solid #3a3f47;
    border-radius: __ACR_RADIUS__px;
    padding: 6px 12px;
}
QPushButton:hover {
    background-color: #33373e;
    border-color: #505660;
}
QPushButton:pressed {
    background-color: #22252a;
}
QPushButton:focus {
    border-color: #b78bf7;
}
QPushButton:disabled {
    background-color: #202226;
    color: #656b74;
    border-color: #2b2e34;
}
QPushButton[acrRole="primary"] {
    background-color: #7c3fd0;
    color: #ffffff;
    border-color: #9149f5;
    font-weight: 600;
}
QPushButton[acrRole="primary"]:hover {
    background-color: #9149f5;
    border-color: #b078f5;
}
QPushButton[acrRole="preview"] {
    background-color: #7c3fd0;
    color: #ffffff;
    border-color: #9149f5;
    font-weight: 600;
}
QPushButton[acrRole="preview"]:hover {
    background-color: #9149f5;
    border-color: #b078f5;
}
QPushButton[acrRole="quiet"] {
    background-color: #23262b;
    color: #b9bec6;
}
QLineEdit, QComboBox, QAbstractSpinBox {
    background-color: #17191d;
    color: #f2f3f5;
    border: 1px solid #3a3f47;
    border-radius: 4px;
    padding: 3px 6px;
}
QLineEdit:hover, QComboBox:hover, QAbstractSpinBox:hover {
    border-color: #545b66;
}
QLineEdit:focus, QComboBox:focus, QAbstractSpinBox:focus {
    border-color: #9149f5;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background-color: #24272c;
    color: #e9ebef;
    border: 1px solid #41464f;
    selection-background-color: #7c3fd0;
}
QSlider::groove:horizontal {
    height: 3px;
    background-color: #383c44;
    border-radius: 1px;
}
QSlider::sub-page:horizontal {
    background-color: #9149f5;
    border-radius: 1px;
}
QSlider::handle:horizontal {
    width: 12px;
    margin: -5px 0;
    background-color: #dfe3e8;
    border: 1px solid #ffffff;
    border-radius: __ACR_RADIUS__px;
}
QSlider::handle:horizontal:hover {
    background-color: #b78bf7;
    border-color: #d4bafc;
}
QCheckBox {
    spacing: 6px;
}
QCheckBox::indicator {
    width: 14px;
    height: 14px;
    background-color: #17191d;
    border: 1px solid #4a505a;
    border-radius: 3px;
}
QCheckBox::indicator:checked {
    background-color: #7c3fd0;
    border-color: #9149f5;
}
QAbstractItemView {
    background-color: #181a1e;
    alternate-background-color: #1c1f23;
    color: #cbd0d7;
    border: 1px solid #30343b;
    border-radius: 5px;
    outline: none;
    padding: 4px;
}
QAbstractItemView::item {
    min-height: 22px;
    padding: 3px 6px;
}
QAbstractItemView::item:selected {
    background-color: #3a2850;
    color: #ffffff;
}
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 1px;
}
QScrollBar::handle:vertical {
    background-color: #454a53;
    min-height: 28px;
    border-radius: 3px;
}
QScrollBar::handle:vertical:hover {
    background-color: #626975;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    height: 0;
    background: transparent;
}
""".replace("__ACR_RADIUS__", str(UI_CORNER_RADIUS))


def _maya_qt_widget(name):
    """Return a QWidget for a Maya command UI name without changing it."""
    if not name:
        return None
    for finder in (omui.MQtUtil.findControl,
                   omui.MQtUtil.findLayout,
                   omui.MQtUtil.findWindow):
        try:
            pointer = finder(name)
        except Exception:
            pointer = None
        if pointer:
            try:
                return wrapInstance(int(pointer), QtWidgets.QWidget)
            except Exception:
                return None
    return None


def _apply_ui_style(window, roles):
    """Apply the scoped theme and semantic roles; cosmetic failure is safe."""
    try:
        window_widget = _maya_qt_widget(window)
        if window_widget is None:
            return False
        window_widget.setProperty("acrRole", "window")
        for name, role in roles:
            widget = _maya_qt_widget(name)
            if widget is not None:
                widget.setProperty("acrRole", role)
        window_widget.setStyleSheet(TOOLCRAFT_QSS)
        style = window_widget.style()
        style.unpolish(window_widget)
        style.polish(window_widget)
        window_widget.update()
        return True
    except Exception as exc:
        # Styling must never prevent the reducer itself from opening.
        cmds.warning("{0}: UI styling was skipped: {1}".format(
            TOOL_NAME, exc
        ))
        return False

# Preserve an active preview if the whole script is accidentally run again.
# show_animator_curve_reducer() will restore it before replacing the window.
if "PREVIEW" not in globals():
    PREVIEW = {
        "active": False,
        "busy": False,
        "requested": False,
        "curves": [],
        "items": [],
        "states": {},
        "source": "",
        "apply_curves": [],
        "apply_items": [],
        "buffer_curves": [],
        "buffer_failures": [],
        "timer": None,
        "pending_verify": False,
        "slope_scales": {},
        "tangent_modes": {},
    }

# Older versions of the tool may have left PREVIEW alive during a script rerun.
PREVIEW.setdefault("timer", None)
PREVIEW.setdefault("pending_verify", False)
PREVIEW.setdefault("slope_scales", {})
PREVIEW.setdefault("tangent_modes", {})
PREVIEW.setdefault("apply_curves", [])
PREVIEW.setdefault("items", [])
PREVIEW.setdefault("apply_items", [])


class _BatchCancelled(RuntimeError):
    """Internal signal used to stop a batch at a safe curve boundary."""


# ---------------------------------------------------------------------------
# Pure curve fitting
# ---------------------------------------------------------------------------

def _sign(value, epsilon):
    if value > epsilon:
        return 1
    if value < -epsilon:
        return -1
    return 0


def _unique_sorted(values, epsilon=1.0e-7):
    result = []
    for value in sorted(float(item) for item in values):
        if not result or abs(value - result[-1]) > epsilon:
            result.append(value)
    return result


def _unique_preserving_order(values):
    result = []
    for value in values or []:
        if value not in result:
            result.append(value)
    return result


def _solve_linear_system(matrix, vector):
    """Solve A*x=b with partial-pivot Gaussian elimination."""
    size = len(vector)
    if not size:
        return []

    augmented = [
        [float(item) for item in matrix[row]] + [float(vector[row])]
        for row in range(size)
    ]

    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1.0e-14:
            continue
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]

        pivot_value = augmented[column][column]
        for item in range(column, size + 1):
            augmented[column][item] /= pivot_value

        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if abs(factor) < 1.0e-18:
                continue
            for item in range(column, size + 1):
                augmented[row][item] -= factor * augmented[column][item]

    solution = []
    for row in range(size):
        diagonal = augmented[row][row]
        if abs(diagonal) < 1.0e-12:
            solution.append(0.0)
        else:
            solution.append(augmented[row][size] / diagonal)
    return solution


def _hermite_value(time_value, time_a, value_a, slope_a,
                   time_b, value_b, slope_b):
    duration = time_b - time_a
    if abs(duration) < 1.0e-12:
        return value_a

    u_value = (time_value - time_a) / duration
    u2 = u_value * u_value
    u3 = u2 * u_value
    h00 = (2.0 * u3) - (3.0 * u2) + 1.0
    h10 = u3 - (2.0 * u2) + u_value
    h01 = (-2.0 * u3) + (3.0 * u2)
    h11 = u3 - u2
    return (
        (h00 * value_a)
        + (h10 * duration * slope_a)
        + (h01 * value_b)
        + (h11 * duration * slope_b)
    )


def _find_extrema(times, values, minimum_prominence, window_size):
    """
    Return sample indices where the direction changes.

    A time-based neighbourhood prominence test rejects tiny baked noise while
    retaining the actual high and low samples. Exact plateaus are collapsed to
    one key at the centre of the plateau.
    """
    count = len(values)
    if count < 3:
        return []

    value_range = max(values) - min(values)
    epsilon = max(value_range * 1.0e-10, 1.0e-12)

    difference_signs = [
        _sign(values[index + 1] - values[index], epsilon)
        for index in range(count - 1)
    ]

    left_direction = [0] * count
    direction = 0
    for index in range(1, count):
        if difference_signs[index - 1]:
            direction = difference_signs[index - 1]
        left_direction[index] = direction

    right_direction = [0] * count
    direction = 0
    for index in range(count - 2, -1, -1):
        if difference_signs[index]:
            direction = difference_signs[index]
        right_direction[index] = direction

    raw = [
        index for index in range(1, count - 1)
        if left_direction[index] and right_direction[index]
        and left_direction[index] != right_direction[index]
    ]
    if not raw:
        return []

    # Collapse contiguous candidates caused by a flat-topped/bottomed run.
    groups = []
    group = [raw[0]]
    for index in raw[1:]:
        if index == group[-1] + 1:
            group.append(index)
        else:
            groups.append(group)
            group = [index]
    groups.append(group)

    extrema = []
    for group in groups:
        middle = group[len(group) // 2]
        is_maximum = left_direction[middle] > 0
        if is_maximum:
            candidate = max(group, key=lambda item: values[item])
        else:
            candidate = min(group, key=lambda item: values[item])

        centre_time = times[candidate]
        left_values = [
            values[index] for index in range(candidate)
            if centre_time - times[index] <= window_size
        ]
        right_values = [
            values[index] for index in range(candidate + 1, count)
            if times[index] - centre_time <= window_size
        ]
        if not left_values or not right_values:
            continue

        if is_maximum:
            prominence = min(
                values[candidate] - min(left_values),
                values[candidate] - min(right_values),
            )
        else:
            prominence = min(
                max(left_values) - values[candidate],
                max(right_values) - values[candidate],
            )

        if prominence + epsilon >= minimum_prominence:
            extrema.append(candidate)

    return extrema


def _slope_prior(key_position, kept, times, values):
    sample_index = kept[key_position]
    if len(kept) == 1:
        return 0.0
    if key_position == 0:
        other = kept[1]
        return (
            (values[other] - values[sample_index])
            / (times[other] - times[sample_index])
        )
    if key_position == len(kept) - 1:
        other = kept[-2]
        return (
            (values[sample_index] - values[other])
            / (times[sample_index] - times[other])
        )

    previous_index = kept[key_position - 1]
    next_index = kept[key_position + 1]
    previous_slope = (
        (values[sample_index] - values[previous_index])
        / (times[sample_index] - times[previous_index])
    )
    next_slope = (
        (values[next_index] - values[sample_index])
        / (times[next_index] - times[sample_index])
    )
    if previous_slope * next_slope <= 0.0:
        return 0.0
    return (
        (values[next_index] - values[previous_index])
        / (times[next_index] - times[previous_index])
    )


def _constrain_smooth_slopes(kept, times, values, slopes):
    """
    Keep fitted Hermite segments monotone between turning points.

    This prevents a mathematically good least-squares tangent from inventing a
    new overshoot between two retained keys.
    """
    if len(kept) < 2:
        return slopes

    constrained = dict(slopes)
    epsilon = max((max(values) - min(values)) * 1.0e-12, 1.0e-12)

    for position, sample_index in enumerate(kept):
        adjacent = []
        if position:
            previous_index = kept[position - 1]
            adjacent.append(
                (values[sample_index] - values[previous_index])
                / (times[sample_index] - times[previous_index])
            )
        if position < len(kept) - 1:
            next_index = kept[position + 1]
            adjacent.append(
                (values[next_index] - values[sample_index])
                / (times[next_index] - times[sample_index])
            )

        nonzero = [item for item in adjacent if abs(item) > epsilon]
        if not nonzero or len(nonzero) != len(adjacent):
            constrained[sample_index] = 0.0
            continue
        if any(item * nonzero[0] < 0.0 for item in nonzero[1:]):
            constrained[sample_index] = 0.0
            continue

        slope = constrained.get(sample_index, 0.0)
        if slope * nonzero[0] < 0.0:
            slope = 0.0
        maximum = 3.0 * min(abs(item) for item in nonzero)
        slope = max(-maximum, min(maximum, slope))
        constrained[sample_index] = slope

    # The Fritsch-Carlson radial constraint is a final overshoot guard.
    for _unused in range(3):
        for position in range(len(kept) - 1):
            index_a = kept[position]
            index_b = kept[position + 1]
            secant = (
                (values[index_b] - values[index_a])
                / (times[index_b] - times[index_a])
            )
            if abs(secant) <= epsilon:
                constrained[index_a] = 0.0
                constrained[index_b] = 0.0
                continue
            alpha = constrained[index_a] / secant
            beta = constrained[index_b] / secant
            radius = (alpha * alpha) + (beta * beta)
            if radius > 9.0:
                scale = 3.0 / math.sqrt(radius)
                constrained[index_a] *= scale
                constrained[index_b] *= scale

    return constrained


def _fit_smooth_slopes(times, values, kept_indices, flat_indices):
    """
    Globally fit one shared slope per retained key.

    Sharing the in/out slope creates animator-friendly unified tangents. Keys
    identified as extrema are explicitly constrained to a zero slope.
    """
    kept = sorted(kept_indices)
    flat = set(flat_indices)
    unknown_samples = [index for index in kept if index not in flat]
    unknown_lookup = dict(
        (sample_index, position)
        for position, sample_index in enumerate(unknown_samples)
    )

    size = len(unknown_samples)
    if not size:
        return dict((index, 0.0) for index in kept)

    matrix = [[0.0 for _column in range(size)] for _row in range(size)]
    vector = [0.0 for _row in range(size)]

    for segment in range(len(kept) - 1):
        index_a = kept[segment]
        index_b = kept[segment + 1]
        time_a = times[index_a]
        time_b = times[index_b]
        value_a = values[index_a]
        value_b = values[index_b]
        duration = time_b - time_a
        if duration <= 1.0e-12:
            continue

        for sample_index in range(index_a + 1, index_b):
            u_value = (times[sample_index] - time_a) / duration
            u2 = u_value * u_value
            u3 = u2 * u_value
            h00 = (2.0 * u3) - (3.0 * u2) + 1.0
            h10 = u3 - (2.0 * u2) + u_value
            h01 = (-2.0 * u3) + (3.0 * u2)
            h11 = u3 - u2

            target = values[sample_index] - (
                (h00 * value_a) + (h01 * value_b)
            )
            coefficients = []
            if index_a in unknown_lookup:
                coefficients.append((unknown_lookup[index_a], h10 * duration))
            if index_b in unknown_lookup:
                coefficients.append((unknown_lookup[index_b], h11 * duration))

            for row, row_value in coefficients:
                vector[row] += row_value * target
                for column, column_value in coefficients:
                    matrix[row][column] += row_value * column_value

    maximum_diagonal = max(
        [abs(matrix[index][index]) for index in range(size)] + [1.0]
    )
    prior_weight = maximum_diagonal * 1.0e-8
    kept_lookup = dict(
        (sample_index, position)
        for position, sample_index in enumerate(kept)
    )
    for sample_index, row in unknown_lookup.items():
        prior = _slope_prior(kept_lookup[sample_index], kept, times, values)
        matrix[row][row] += prior_weight
        vector[row] += prior_weight * prior

    solution = _solve_linear_system(matrix, vector)
    slopes = dict((index, 0.0) for index in kept)
    for sample_index, row in unknown_lookup.items():
        slopes[sample_index] = solution[row]

    return _constrain_smooth_slopes(kept, times, values, slopes)


def _predict_values(times, values, kept_indices, slopes):
    kept = sorted(kept_indices)
    predictions = list(values)
    for segment in range(len(kept) - 1):
        index_a = kept[segment]
        index_b = kept[segment + 1]
        for sample_index in range(index_a, index_b + 1):
            predictions[sample_index] = _hermite_value(
                times[sample_index],
                times[index_a],
                values[index_a],
                slopes[index_a],
                times[index_b],
                values[index_b],
                slopes[index_b],
            )
    return predictions


def _fit_given_kept(times, values, kept_indices, flat_indices):
    kept = sorted(set(kept_indices))
    slopes = _fit_smooth_slopes(times, values, kept, flat_indices)
    predictions = _predict_values(times, values, kept, slopes)
    errors = [
        abs(values[index] - predictions[index])
        for index in range(len(values))
    ]
    return {
        "kept": kept,
        "slopes": slopes,
        "predictions": predictions,
        "errors": errors,
        "maximum_error": max(errors) if errors else 0.0,
    }


def _reduce_samples(times, values, tolerance, preserve_extrema,
                    minimum_prominence, extrema_window, maximum_keys):
    if len(times) != len(values):
        raise ValueError("Sample times and values do not match.")
    if len(times) < 2:
        raise ValueError("At least two samples are required.")

    extrema = []
    if preserve_extrema:
        extrema = _find_extrema(
            times,
            values,
            minimum_prominence,
            extrema_window,
        )

    kept = set([0, len(times) - 1] + extrema)
    flat = set(extrema)
    limit = int(maximum_keys)
    if limit > 0:
        limit = max(limit, len(kept))

    while True:
        fit = _fit_given_kept(times, values, kept, flat)
        if fit["maximum_error"] <= tolerance + 1.0e-12:
            break
        if limit and len(kept) >= limit:
            break

        candidates = [
            index for index in range(1, len(times) - 1)
            if index not in kept
        ]
        if not candidates:
            break
        worst = max(candidates, key=lambda index: fit["errors"][index])
        kept.add(worst)

    fit["flat"] = sorted(flat)
    fit["tolerance_reached"] = fit["maximum_error"] <= tolerance + 1.0e-12
    return fit


# ---------------------------------------------------------------------------
# Maya curve discovery and sampling
# ---------------------------------------------------------------------------

def _is_anim_curve(node):
    if not node or not cmds.objExists(node):
        return False
    try:
        return cmds.nodeType(node).startswith("animCurve")
    except Exception:
        return False


def _is_time_anim_curve(node):
    if not _is_anim_curve(node):
        return False
    try:
        return cmds.nodeType(node) in (
            "animCurveTA", "animCurveTL", "animCurveTT", "animCurveTU"
        )
    except Exception:
        return False


def _append_curves(result, items):
    for item in items or []:
        if not item:
            continue
        node = item.split(".", 1)[0]
        if _is_anim_curve(node):
            if node not in result:
                result.append(node)
            continue

        try:
            connected = cmds.listConnections(
                item,
                source=True,
                destination=False,
                type="animCurve",
            ) or []
        except Exception:
            connected = []
        for curve in connected:
            if curve not in result:
                result.append(curve)

        try:
            keyed = cmds.keyframe(
                item,
                query=True,
                name=True,
                shape=True,
            ) or []
        except Exception:
            keyed = []
        for curve in keyed:
            if curve not in result:
                result.append(curve)


def _graph_editors():
    editors = []
    focused_panel = cmds.getPanel(withFocus=True)
    panels = cmds.getPanel(scriptType="graphEditor") or []
    if focused_panel in panels:
        panels = [focused_panel] + [
            panel for panel in panels if panel != focused_panel
        ]

    for panel in panels:
        candidate = panel + "GraphEd"
        try:
            if cmds.animCurveEditor(candidate, exists=True):
                editors.append(candidate)
        except Exception:
            pass

    for editor in cmds.lsUI(editors=True) or []:
        if editor in editors:
            continue
        try:
            if cmds.animCurveEditor(editor, exists=True):
                editors.append(editor)
        except Exception:
            pass
    return editors


def _selection_connection_members(connection):
    if not connection:
        return []
    try:
        if not cmds.selectionConnection(connection, exists=True):
            return []
        return cmds.selectionConnection(
            connection,
            query=True,
            object=True,
        ) or []
    except Exception:
        return []


def _capture_graph_editor_selection_state():
    """Capture active keys plus curve/channel connections for every editor."""
    connections = []
    seen_connections = set()
    for editor in _graph_editors():
        candidates = []
        try:
            candidates.append(cmds.animCurveEditor(
                editor,
                query=True,
                selectionConnection=True,
            ))
        except Exception:
            pass
        try:
            outliner = cmds.animCurveEditor(
                editor,
                query=True,
                outliner=True,
            )
            if outliner and cmds.outlinerEditor(outliner, exists=True):
                candidates.append(cmds.outlinerEditor(
                    outliner,
                    query=True,
                    selectionConnection=True,
                ))
        except Exception:
            pass
        for connection in candidates:
            if not connection or connection in seen_connections:
                continue
            seen_connections.add(connection)
            connections.append((
                connection,
                list(_selection_connection_members(connection)),
            ))

    key_selection = []
    try:
        curves = cmds.keyframe(
            query=True,
            selected=True,
            name=True,
        ) or []
    except Exception:
        curves = []
    for curve in _unique_preserving_order(curves):
        if not cmds.objExists(curve):
            continue
        try:
            times = cmds.keyframe(
                curve,
                query=True,
                selected=True,
                timeChange=True,
            ) or []
        except Exception:
            times = []
        if times:
            key_selection.append((
                curve,
                [float(value) for value in times],
            ))
    return {
        "connections": connections,
        "keys": key_selection,
    }


def _restore_graph_editor_selection_state(state):
    """Restore Graph Editor selection without adding an undo command."""
    undo_state = None
    try:
        undo_state = bool(cmds.undoInfo(query=True, state=True))
        if undo_state:
            cmds.undoInfo(stateWithoutFlush=False)
    except Exception:
        undo_state = None
    try:
        try:
            cmds.selectKey(clear=True)
        except Exception:
            pass
        for curve, times in state.get("keys", []):
            if not cmds.objExists(curve):
                continue
            for time_value in times:
                try:
                    cmds.selectKey(
                        curve,
                        addTo=True,
                        animation="objects",
                        keyframe=True,
                        time=(time_value, time_value),
                    )
                except Exception:
                    pass

        for connection, members in state.get("connections", []):
            try:
                if not cmds.selectionConnection(connection, exists=True):
                    continue
                current = _selection_connection_members(connection)
                if current == members:
                    continue
                cmds.selectionConnection(
                    connection,
                    edit=True,
                    clear=True,
                )
                for member in members:
                    cmds.selectionConnection(
                        connection,
                        edit=True,
                        select=member,
                    )
            except Exception:
                # Some Maya-managed dynamic connections are read-only. Key
                # selection restoration above still preserves normal edits.
                pass
    finally:
        if undo_state is not None:
            try:
                cmds.undoInfo(stateWithoutFlush=undo_state)
            except Exception:
                pass


def _append_channel_curves(result, channel_items):
    """Resolve only channel rows/plugs, never a whole selected object row."""
    scene_selection = cmds.ls(selection=True, long=True) or []
    for item in channel_items or []:
        if not item:
            continue
        item = str(item)
        if "." in item or _is_anim_curve(item):
            _append_curves(result, [item])
            continue

        # Maya 2026.2+ may return a bare attribute name from
        # animCurveEditor(selectedAttributes=True). Resolve it only against
        # selected scene nodes. Object rows do not pass this plug check.
        for node in scene_selection:
            plug = node + "." + item
            if cmds.objExists(plug):
                _append_curves(result, [plug])


def _graph_editor_selected_channel_curves():
    """Return curves for explicitly selected Graph Editor channel rows."""
    result = []
    for editor in _graph_editors():
        # This direct query is available in Maya 2026.2+. Older Maya versions
        # reject the flag, then fall through to the outliner connection.
        try:
            selected_attributes = cmds.animCurveEditor(
                editor,
                query=True,
                selectedAttributes=True,
            ) or []
        except Exception:
            selected_attributes = []
        _append_channel_curves(result, selected_attributes)

        try:
            outliner = cmds.animCurveEditor(editor, query=True, outliner=True)
            if not outliner or not cmds.outlinerEditor(
                    outliner, exists=True):
                continue
            connection = cmds.outlinerEditor(
                outliner,
                query=True,
                selectionConnection=True,
            )
        except Exception:
            connection = ""

        # The associated outliner owns the channel-row selection. Deliberately
        # ignore the animCurveEditor's curve/key selection connection so the
        # persistent buffer overlay cannot be mistaken for a user channel.
        _append_channel_curves(
            result,
            _selection_connection_members(connection),
        )

    if result:
        return result, "selected Graph Editor channels"
    return [], ""


def _viewport_selected_curves():
    selection = cmds.ls(selection=True, long=True) or []
    result = []
    _append_curves(result, selection)

    for item in selection:
        if not cmds.objExists(item):
            continue
        try:
            curves = cmds.findKeyframe(
                item,
                curve=True,
                shape=True,
            ) or []
        except Exception:
            curves = []
        _append_curves(result, curves)
    return result


def _curve_editability_problem(curve):
    if not _is_time_anim_curve(curve):
        return "not a time-input animation curve"
    try:
        if cmds.referenceQuery(curve, isNodeReferenced=True):
            return "curve is referenced"
    except Exception:
        pass
    try:
        locked = cmds.lockNode(curve, query=True, lock=True) or [False]
        if locked[0]:
            return "curve node is locked"
    except Exception:
        pass

    try:
        destinations = cmds.listConnections(
            curve + ".output",
            source=False,
            destination=True,
            plugs=True,
        ) or []
    except Exception:
        destinations = []
    for plug in destinations:
        try:
            attribute_type = cmds.getAttr(plug, type=True)
        except Exception:
            continue
        if attribute_type in ("bool", "enum", "byte", "char"):
            return "curve drives a discrete attribute"
    return ""


def collect_target_curves():
    curves, source = _graph_editor_selected_channel_curves()
    if not curves:
        curves = _viewport_selected_curves()
        source = (
            "all animated channels on viewport/DAG selection"
            if curves else ""
        )

    valid = []
    skipped = []
    for curve in curves:
        if curve in valid:
            continue
        problem = _curve_editability_problem(curve)
        if problem:
            skipped.append((curve, problem))
        else:
            valid.append(curve)
    return valid, skipped, source


def _selected_key_work_items():
    """Return contiguous Graph Editor key selections across all channels."""
    try:
        selected_curves = cmds.keyframe(
            query=True,
            selected=True,
            name=True,
        ) or []
    except Exception:
        selected_curves = []

    unique_curves = []
    for curve in selected_curves:
        if curve not in unique_curves:
            unique_curves.append(curve)
    selected_curves = unique_curves
    if not selected_curves:
        return [], [], False

    work_items = []
    skipped = []
    for curve in selected_curves:
        problem = _curve_editability_problem(curve)
        if problem:
            skipped.append((curve, problem))
            continue

        all_times = cmds.keyframe(
            curve,
            query=True,
            timeChange=True,
        ) or []
        selected_times = cmds.keyframe(
            curve,
            query=True,
            selected=True,
            timeChange=True,
        ) or []
        all_times = [float(value) for value in all_times]
        selected_times = _unique_sorted(selected_times)
        if not selected_times:
            continue

        selected_lookup = set(
            _time_lookup_key(time_value) for time_value in selected_times
        )
        selected_indices = []
        for index, time_value in enumerate(all_times):
            if _time_lookup_key(time_value) in selected_lookup:
                selected_indices.append(index)

        groups = []
        current = []
        for index in selected_indices:
            if current and index != current[-1] + 1:
                groups.append(current)
                current = []
            current.append(index)
        if current:
            groups.append(current)

        for indices in groups:
            group_times = [all_times[index] for index in indices]
            range_text = "keys {0:g}-{1:g}".format(
                group_times[0],
                group_times[-1],
            )
            if len(group_times) < 3:
                skipped.append((
                    "{0} [{1}]".format(curve, range_text),
                    "fewer than three contiguous selected keys",
                ))
                continue

            # Selecting every key is equivalent to normal whole-curve mode.
            selected_range = None
            if len(group_times) != len(all_times):
                selected_range = group_times
            work_items.append({
                "curve": curve,
                "selected_times": selected_range,
                "selection_label": range_text,
            })

    return work_items, skipped, True


def _evaluate_curve(curve, time_value):
    result = cmds.keyframe(
        curve,
        query=True,
        eval=True,
        time=(time_value, time_value),
    ) or []
    if not result:
        raise RuntimeError(
            "Could not evaluate {0} at {1}.".format(curve, time_value)
        )
    return float(result[0])


def _sample_curve(curve, sample_step):
    key_times = cmds.keyframe(
        curve,
        query=True,
        timeChange=True,
    ) or []
    key_values = cmds.keyframe(
        curve,
        query=True,
        valueChange=True,
    ) or []
    if len(key_times) < 2 or len(key_times) != len(key_values):
        raise RuntimeError("Curve needs at least two readable keys.")

    pairs = sorted(
        (float(key_times[index]), float(key_values[index]))
        for index in range(len(key_times))
    )
    original_times = [item[0] for item in pairs]
    original_values = [item[1] for item in pairs]
    start_time = original_times[0]
    end_time = original_times[-1]

    uniform_times = [start_time]
    time_value = start_time + sample_step
    while time_value < end_time - 1.0e-7:
        uniform_times.append(time_value)
        if len(uniform_times) > MAX_SAMPLES_PER_CURVE:
            raise RuntimeError(
                "More than {0} samples. Increase Sample Step.".format(
                    MAX_SAMPLES_PER_CURVE
                )
            )
        time_value += sample_step
    uniform_times.append(end_time)

    sample_times = _unique_sorted(original_times + uniform_times)
    if len(sample_times) > MAX_SAMPLES_PER_CURVE:
        raise RuntimeError(
            "More than {0} samples. Increase Sample Step.".format(
                MAX_SAMPLES_PER_CURVE
            )
        )

    original_lookup = dict(
        (round(original_times[index], 7), original_values[index])
        for index in range(len(original_times))
    )
    sample_values = []
    for time_value in sample_times:
        lookup_key = round(time_value, 7)
        if lookup_key in original_lookup:
            sample_values.append(original_lookup[lookup_key])
        else:
            sample_values.append(_evaluate_curve(curve, time_value))
    return sample_times, sample_values, len(original_times)


def _sample_selected_key_range(curve, selected_times, sample_step):
    selected_times = _unique_sorted(selected_times)
    if len(selected_times) < 3:
        raise RuntimeError(
            "A selected key range needs at least three contiguous keys."
        )

    key_times = cmds.keyframe(
        curve,
        query=True,
        timeChange=True,
    ) or []
    key_values = cmds.keyframe(
        curve,
        query=True,
        valueChange=True,
    ) or []
    if len(key_times) != len(key_values):
        raise RuntimeError("Could not read the selected curve keys.")

    pairs = sorted(
        (float(key_times[index]), float(key_values[index]))
        for index in range(len(key_times))
    )
    original_times = [item[0] for item in pairs]
    original_values = [item[1] for item in pairs]
    start_time = selected_times[0]
    end_time = selected_times[-1]

    keys_in_range = [
        time_value for time_value in original_times
        if start_time - 1.0e-7 <= time_value <= end_time + 1.0e-7
    ]
    selected_lookup = set(
        _time_lookup_key(time_value) for time_value in selected_times
    )
    if len(keys_in_range) != len(selected_times) or any(
            _time_lookup_key(value) not in selected_lookup
            for value in keys_in_range):
        raise RuntimeError(
            "The selected keys must form one contiguous block on the curve."
        )

    uniform_times = [start_time]
    time_value = start_time + sample_step
    while time_value < end_time - 1.0e-7:
        uniform_times.append(time_value)
        if len(uniform_times) > MAX_SAMPLES_PER_CURVE:
            raise RuntimeError(
                "More than {0} samples. Increase Sample Step.".format(
                    MAX_SAMPLES_PER_CURVE
                )
            )
        time_value += sample_step
    uniform_times.append(end_time)

    sample_times = _unique_sorted(selected_times + uniform_times)
    if len(sample_times) > MAX_SAMPLES_PER_CURVE:
        raise RuntimeError(
            "More than {0} samples. Increase Sample Step.".format(
                MAX_SAMPLES_PER_CURVE
            )
        )

    original_lookup = dict(
        (round(original_times[index], 7), original_values[index])
        for index in range(len(original_times))
    )
    sample_values = []
    for time_value in sample_times:
        lookup_key = round(time_value, 7)
        if lookup_key in original_lookup:
            sample_values.append(original_lookup[lookup_key])
        else:
            sample_values.append(_evaluate_curve(curve, time_value))
    return sample_times, sample_values, len(selected_times)


def _curve_label(curve):
    try:
        destinations = cmds.listConnections(
            curve + ".output",
            source=False,
            destination=True,
            plugs=True,
        ) or []
    except Exception:
        destinations = []
    if destinations:
        return "{0}  ->  {1}".format(curve, destinations[0])
    return curve


def _additive_destination_plugs(curve):
    """Resolve only the exact downstream channel; reject ambiguous graphs."""
    try:
        destinations = cmds.listConnections(
            curve + ".output",
            source=False,
            destination=True,
            plugs=True,
            skipConversionNodes=False,
        ) or []
    except Exception:
        return []
    if len(destinations) != 1:
        return []

    plug = str(destinations[0])
    visited = set()
    for _depth in range(100):
        if plug in visited:
            return []
        visited.add(plug)
        node = plug.split(".", 1)[0]
        attribute = plug.split(".", 1)[1] if "." in plug else ""
        try:
            node_type = cmds.nodeType(node)
        except Exception:
            return []

        try:
            is_transform = cmds.objectType(node, isAType="transform")
        except Exception:
            is_transform = False
        if is_transform:
            if not cmds.objExists(plug):
                return []
            try:
                attribute_type = cmds.getAttr(plug, type=True)
            except Exception:
                return []
            if attribute_type in (
                    "bool", "enum", "byte", "char", "string", "message",
                    "matrix", "double3", "float3"):
                return []
            return [plug]

        output_plug = _exact_intermediate_output_plug(
            node,
            node_type,
            attribute,
        )
        if not output_plug:
            return []
        try:
            destinations = cmds.listConnections(
                output_plug,
                source=False,
                destination=True,
                plugs=True,
                skipConversionNodes=False,
            ) or []
        except Exception:
            return []
        if len(destinations) != 1:
            return []
        plug = str(destinations[0])
    return []


def _axis_from_attribute(attribute):
    leaf = attribute.split("[", 1)[0]
    if leaf and leaf[-1:].upper() in ("X", "Y", "Z"):
        return leaf[-1:].upper()
    # pairBlend input children end in an input number, for example
    # inRotateZ1. The axis is the character immediately before that number.
    if (len(leaf) >= 2 and leaf[-1:].isdigit()
            and leaf[-2:-1].upper() in ("X", "Y", "Z")):
        return leaf[-2:-1].upper()
    return ""


def _exact_intermediate_output_plug(node, node_type, input_attribute):
    """Map a known input branch to its matching output branch."""
    axis = _axis_from_attribute(input_attribute)
    candidates = []
    if node_type.startswith("animBlendNode"):
        candidates.append("output" + axis if axis else "output")
    elif node_type == "pairBlend":
        lower_attribute = input_attribute.lower()
        if "rotate" in lower_attribute and axis:
            candidates.append("outRotate" + axis)
        elif "translate" in lower_attribute and axis:
            candidates.append("outTranslate" + axis)
    elif node_type in (
            "unitConversion", "unitToTimeConversion",
            "timeToUnitConversion", "blendWeighted", "blendTwoAttr"):
        candidates.append("output")
    else:
        return ""

    valid = [
        node + "." + candidate for candidate in candidates
        if cmds.objExists(node + "." + candidate)
    ]
    return valid[0] if len(valid) == 1 else ""


def _work_item_additive_times(work_item, sample_step):
    """Return bake times matching this reducer's whole-curve/block sampling."""
    curve = work_item["curve"]
    all_times = _unique_sorted(cmds.keyframe(
        curve,
        query=True,
        timeChange=True,
    ) or [])
    if len(all_times) < 2:
        raise RuntimeError("Curve needs at least two readable keys.")

    selected_times = work_item.get("selected_times")
    if selected_times:
        original_times = _unique_sorted(selected_times)
    else:
        original_times = all_times
    start_time = original_times[0]
    end_time = original_times[-1]

    uniform_times = [start_time]
    time_value = start_time + sample_step
    while time_value < end_time - 1.0e-7:
        uniform_times.append(time_value)
        if len(uniform_times) > MAX_SAMPLES_PER_CURVE:
            raise RuntimeError(
                "More than {0} samples. Increase Sample Step.".format(
                    MAX_SAMPLES_PER_CURVE
                )
            )
        time_value += sample_step
    uniform_times.append(end_time)
    sample_times = _unique_sorted(original_times + uniform_times)
    if len(sample_times) > MAX_SAMPLES_PER_CURVE:
        raise RuntimeError(
            "More than {0} samples. Increase Sample Step.".format(
                MAX_SAMPLES_PER_CURVE
            )
        )
    return sample_times


def _scalar_attribute_value(plug, time_value):
    value = cmds.getAttr(plug, time=time_value)
    while isinstance(value, (list, tuple)) and len(value) == 1:
        value = value[0]
    if isinstance(value, (list, tuple)):
        raise RuntimeError("Attribute is not scalar: {0}".format(plug))
    return float(value)


def _capture_additive_work_item(work_item, sample_step):
    """Sample original evaluated values before any source curve is reduced."""
    plugs = _additive_destination_plugs(work_item["curve"])
    if not plugs:
        raise RuntimeError("could not resolve a final transform attribute")
    times = _work_item_additive_times(work_item, sample_step)
    captured = {}
    for plug in plugs:
        captured[plug] = dict(
            (float(time_value), _scalar_attribute_value(plug, time_value))
            for time_value in times
        )
    return captured


def _merge_additive_captures(captures, results):
    merged = {}
    for summary in results:
        capture = captures.get(summary.get("work_index"), {})
        for plug, samples in capture.items():
            merged.setdefault(plug, {}).update(samples)
    return merged


def _node_uuid(node):
    try:
        values = cmds.ls(node, uuid=True) or []
        return str(values[0]) if values else ""
    except Exception:
        return ""


def _resolve_link_node(name, uuid_value):
    if name and cmds.objExists(name):
        return name
    if uuid_value:
        try:
            candidates = cmds.ls(uuid_value) or []
        except Exception:
            candidates = []
        for candidate in candidates:
            if cmds.objExists(candidate):
                return candidate
    return ""


def _build_additive_link_specs(captures, results):
    """Pair each final plug with one reduced base curve and its anchors."""
    grouped = {}
    for summary in results:
        capture = captures.get(summary.get("work_index"), {})
        for plug in capture:
            entry = grouped.setdefault(plug, {
                "curves": set(),
                "anchor_times": [],
            })
            entry["curves"].add(summary["curve"])
            entry["anchor_times"].extend(summary.get("anchor_times", []))

    specs = {}
    failures = []
    for plug, entry in grouped.items():
        curves = sorted(entry["curves"])
        if len(curves) != 1:
            failures.append((
                plug,
                "more than one reduced source curve feeds this attribute",
            ))
            continue
        base_curve = curves[0]
        try:
            base_times = [float(value) for value in (
                cmds.keyframe(
                    base_curve,
                    query=True,
                    timeChange=True,
                ) or []
            )]
        except Exception as exc:
            failures.append((plug, "could not read base keys: {0}".format(exc)))
            continue
        anchor_times = _unique_sorted(entry["anchor_times"])
        anchor_indices = []
        for anchor_time in anchor_times:
            matches = [
                index for index, base_time in enumerate(base_times)
                if _same_key_time(base_time, anchor_time)
            ]
            if len(matches) != 1:
                anchor_indices = []
                break
            anchor_indices.append(matches[0])
        if len(anchor_indices) < 2:
            failures.append((
                plug,
                "could not identify at least two unique base anchor keys",
            ))
            continue
        specs[plug] = {
            "base_curve": base_curve,
            "base_curve_uuid": _node_uuid(base_curve),
            "base_key_count": len(base_times),
            "base_anchor_indices": anchor_indices,
            "anchor_times": anchor_times,
        }
    return specs, failures


def _find_layer_curve_for_plug(layer, plug):
    try:
        value = cmds.animLayer(
            layer,
            query=True,
            findCurveForPlug=plug,
        )
    except Exception:
        value = None
    if isinstance(value, (list, tuple)):
        value = value[0] if len(value) == 1 else None
    value = str(value) if value else ""
    return value if value and _is_anim_curve(value) else ""


def _write_link_data(layer, links):
    if not links:
        return False
    attribute = layer + "." + LINK_DATA_ATTR
    if not cmds.objExists(attribute):
        cmds.addAttr(layer, longName=LINK_DATA_ATTR, dataType="string")
    payload = {
        "version": LINK_DATA_VERSION,
        "links": links,
    }
    cmds.setAttr(
        attribute,
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        type="string",
    )
    return True


def _read_link_data(layer):
    attribute = layer + "." + LINK_DATA_ATTR
    if not cmds.objExists(attribute):
        return None
    try:
        raw = cmds.getAttr(attribute) or ""
        payload = json.loads(raw)
    except Exception:
        return None
    if (not isinstance(payload, dict)
            or payload.get("version") != LINK_DATA_VERSION
            or not isinstance(payload.get("links"), list)):
        return None
    return payload


def _key_breakdown_state(curve, index):
    """Query one key through API 2.0; cmds returns vary by Maya version."""
    selection = om.MSelectionList()
    selection.add(curve)
    function = oma.MFnAnimCurve(selection.getDependNode(0))
    key_index = int(index)
    if key_index < 0 or key_index >= int(function.numKeys):
        raise RuntimeError(
            "breakdown key index {0} is outside curve {1}".format(
                key_index,
                curve,
            )
        )
    return bool(function.isBreakdown(key_index))


def _key_breakdown_states(curve, key_count):
    """Return one unambiguous boolean per animation-curve key."""
    selection = om.MSelectionList()
    selection.add(curve)
    function = oma.MFnAnimCurve(selection.getDependNode(0))
    actual_count = int(function.numKeys)
    if actual_count != int(key_count):
        raise RuntimeError(
            "breakdown query found {0} keys but {1} were expected on {2}"
            .format(actual_count, key_count, curve)
        )
    return [
        bool(function.isBreakdown(index))
        for index in range(actual_count)
    ]


def _normalize_linked_detail_breakdowns(
        detail_curve, detail_times, anchor_times):
    """Make only linked anchor times regular; verify Maya accepted it."""
    states = _key_breakdown_states(detail_curve, len(detail_times))
    if len(states) != len(detail_times):
        raise RuntimeError("could not read additive breakdown key states")
    expected = [
        not any(
            _same_key_time(detail_time, anchor_time)
            for anchor_time in anchor_times
        )
        for detail_time in detail_times
    ]
    changed = any(
        bool(actual) != bool(wanted)
        for actual, wanted in zip(states, expected)
    )
    if changed:
        cmds.keyframe(
            detail_curve,
            edit=True,
            animation="objects",
            breakdown=True,
        )
        for anchor_time in anchor_times:
            cmds.keyframe(
                detail_curve,
                edit=True,
                animation="objects",
                time=(anchor_time, anchor_time),
                breakdown=False,
            )
        states = _key_breakdown_states(detail_curve, len(detail_times))
    if (len(states) != len(expected)
            or any(bool(actual) != bool(wanted)
                   for actual, wanted in zip(states, expected))):
        raise RuntimeError(
            "Maya did not assign the required additive breakdown states"
        )
    return changed


def _configure_linked_detail_keys(layer, link_specs):
    """Make base-aligned detail anchors regular and all other keys breakdowns."""
    links = []
    failures = []
    for plug, spec in sorted(link_specs.items()):
        detail_curve = _find_layer_curve_for_plug(layer, plug)
        if not detail_curve:
            failures.append((plug, "could not find the layer animation curve"))
            continue
        try:
            detail_times = [float(value) for value in (
                cmds.keyframe(
                    detail_curve,
                    query=True,
                    timeChange=True,
                ) or []
            )]
            anchor_times = _unique_sorted(spec["anchor_times"])
            for anchor_time in anchor_times:
                if not any(
                        _same_key_time(anchor_time, value)
                        for value in detail_times):
                    raise RuntimeError(
                        "detail curve has no key at anchor {0:g}".format(
                            anchor_time
                        )
                    )

            _normalize_linked_detail_breakdowns(
                detail_curve,
                detail_times,
                anchor_times,
            )

            links.append({
                "plug": plug,
                "baseCurve": spec["base_curve"],
                "baseCurveUuid": spec["base_curve_uuid"],
                "baseKeyCount": int(spec["base_key_count"]),
                "baseKeyTimes": _key_times(spec["base_curve"]),
                "baseAnchorIndices": [
                    int(index) for index in spec["base_anchor_indices"]
                ],
                "detailCurve": detail_curve,
                "detailCurveUuid": _node_uuid(detail_curve),
                "detailAnchorTimes": anchor_times,
                "detailKeyCount": len(detail_times),
            })
        except Exception as exc:
            failures.append((plug, str(exc)))
    if links:
        _write_link_data(layer, links)
    return links, failures


def _selected_base_curve_candidates():
    """Curves explicitly selected as keys or Graph Editor channel rows."""
    result = []
    try:
        selected_key_curves = cmds.keyframe(
            query=True,
            selected=True,
            name=True,
        ) or []
    except Exception:
        selected_key_curves = []
    for curve in selected_key_curves:
        if _is_time_anim_curve(curve) and curve not in result:
            result.append(curve)
    channel_curves, _source = _graph_editor_selected_channel_curves()
    for curve in channel_curves:
        if _is_time_anim_curve(curve) and curve not in result:
            result.append(curve)
    return result


def _base_curve_for_unlinked_plug(plug, detail_curve, selected_curves):
    try:
        root_layer = cmds.animLayer(query=True, root=True)
    except Exception:
        root_layer = ""
    if root_layer:
        root_curve = _find_layer_curve_for_plug(root_layer, plug)
        if root_curve and root_curve != detail_curve:
            return root_curve

    try:
        candidates = cmds.keyframe(
            plug,
            query=True,
            name=True,
        ) or []
    except Exception:
        candidates = []
    candidates = [
        curve for curve in _unique_preserving_order(candidates)
        if curve != detail_curve and _is_time_anim_curve(curve)
    ]
    selected = [
        curve for curve in selected_curves if curve in candidates
    ]
    if len(selected) == 1:
        return selected[0]
    if len(selected) > 1:
        raise RuntimeError(
            "more than one selected base curve drives this channel"
        )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError("could not find a non-detail base curve")
    raise RuntimeError(
        "multiple animation-layer curves drive this channel; select its "
        "base curve or Graph Editor channel, then run Sync again"
    )


def _adopt_anchor_indices(base_curve, anchor_times, detail_times=None):
    """Safely map existing regular detail anchors onto current base keys."""
    base_times = _key_times(base_curve)
    detail_times = [float(value) for value in (detail_times or [])]
    anchor_times = [float(value) for value in anchor_times]

    # v0.8.5 could bake the detail but fail before marking breakdowns. If the
    # whole reduced base was shifted uniformly afterward, its span still
    # matches the baked detail span. Recover the original anchor times by
    # removing that offset, and require every inferred time to exist exactly
    # on the baked detail curve.
    if (len(anchor_times) == len(detail_times)
            and len(base_times) >= 2):
        detail_span = detail_times[-1] - detail_times[0]
        candidates = []
        for start_index in range(len(base_times) - 1):
            target_end = base_times[start_index] + detail_span
            for end_index in range(start_index + 1, len(base_times)):
                if base_times[end_index] > target_end + 1.0e-6:
                    break
                if not _same_key_time(base_times[end_index], target_end):
                    continue
                offset = base_times[start_index] - detail_times[0]
                inferred = [
                    value - offset
                    for value in base_times[start_index:end_index + 1]
                ]
                if all(
                        sum(_same_key_time(value, detail_time)
                            for detail_time in detail_times) == 1
                        for value in inferred):
                    candidates.append((
                        list(range(start_index, end_index + 1)),
                        inferred,
                        offset,
                    ))
        if len(candidates) == 1:
            indices, inferred, offset = candidates[0]
            _link_debug(
                "Recovered legacy partial block on {0}: base indices "
                "{1}-{2}, {3} anchors, uniform offset {4:g}.".format(
                    base_curve,
                    indices[0],
                    indices[-1],
                    len(indices),
                    offset,
                )
            )
            return base_times, indices, inferred
        if len(candidates) > 1:
            raise RuntimeError(
                "legacy detail recovery found {0} possible base-key blocks; "
                "the mapping is ambiguous".format(len(candidates))
            )

    if len(anchor_times) < 2 or len(base_times) < len(anchor_times):
        raise RuntimeError(
            "existing detail layer has no usable regular anchor mapping "
            "(base {0} keys, range {1:g}-{2:g}; detail {3} keys, range "
            "{4:g}-{5:g}; regular detail keys {6})".format(
                len(base_times),
                base_times[0] if base_times else 0.0,
                base_times[-1] if base_times else 0.0,
                len(detail_times),
                detail_times[0] if detail_times else 0.0,
                detail_times[-1] if detail_times else 0.0,
                len(anchor_times),
            )
        )

    # Most generated layers cover the complete reduced curve. This remains
    # valid even when the animator has already retimed every base key.
    if len(base_times) == len(anchor_times):
        return base_times, list(range(len(base_times))), anchor_times

    # For partial-range reductions, unchanged anchor times provide an exact,
    # unambiguous mapping. Never guess across unmatched layered curves.
    indices = []
    for anchor_time in anchor_times:
        matches = [
            index for index, base_time in enumerate(base_times)
            if _same_key_time(anchor_time, base_time)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "partial-range anchors cannot be reconstructed safely after "
                "their base keys moved; recreate the linked detail output"
            )
        indices.append(matches[0])
    if any(
            indices[index] <= indices[index - 1]
            for index in range(1, len(indices))):
        raise RuntimeError("reconstructed base anchors are not ordered")
    return base_times, indices, anchor_times


def _selected_key_indices_on_curve(curve, curve_times):
    try:
        selected_times = cmds.keyframe(
            curve,
            query=True,
            selected=True,
            timeChange=True,
        ) or []
    except Exception:
        selected_times = []
    indices = []
    for selected_time in selected_times:
        matches = [
            index for index, curve_time in enumerate(curve_times)
            if _same_key_time(selected_time, curve_time)
        ]
        if len(matches) == 1 and matches[0] not in indices:
            indices.append(matches[0])
    return sorted(indices)


def _residual_anchor_mapping(
        plug, base_curve, detail_curve, detail_times, original_error):
    """Recover lost legacy metadata from selected base keys and residuals."""
    attribute = plug.rsplit(".", 1)[-1].lower()
    if "translate" not in attribute and "rotate" not in attribute:
        raise RuntimeError(original_error)
    detail_values = [float(value) for value in (
        cmds.keyframe(
            detail_curve,
            query=True,
            valueChange=True,
        ) or []
    )]
    if len(detail_values) != len(detail_times):
        raise RuntimeError(
            "{0}; could not read additive residual values".format(
                original_error
            )
        )
    magnitude = max([abs(value) for value in detail_values] or [0.0])
    zero_tolerance = max(magnitude * 1.0e-8, 1.0e-8)
    residual_indices = [
        index for index, value in enumerate(detail_values)
        if abs(value) <= zero_tolerance
    ]
    residual_times = [detail_times[index] for index in residual_indices]
    base_times = _key_times(base_curve)
    selected_indices = _selected_key_indices_on_curve(
        base_curve,
        base_times,
    )
    diagnostic = (
        "residual recovery found {0} identity-valued detail key(s) "
        "(tolerance {1:g}) and {2} selected base key(s)"
        .format(
            len(residual_times),
            zero_tolerance,
            len(selected_indices),
        )
    )
    if len(residual_times) < 2 or len(selected_indices) != len(residual_times):
        raise RuntimeError(
            "{0}; {1}. Select exactly the {2} corresponding reduced base "
            "keys in the Graph Editor, leave them selected, then run Sync "
            "again.".format(
                original_error,
                diagnostic,
                len(residual_times),
            )
        )
    if any(
            selected_indices[index] != selected_indices[index - 1] + 1
            for index in range(1, len(selected_indices))):
        raise RuntimeError(
            "{0}; selected base keys are not one contiguous block"
            .format(diagnostic)
        )
    _link_debug(
        "Recovered {0} from selected base indices {1}-{2} and {3} "
        "identity residual anchors.".format(
            plug,
            selected_indices[0],
            selected_indices[-1],
            len(residual_times),
        )
    )
    return base_times, selected_indices, residual_times


def _adopt_links_from_layer(layer, selected_curves):
    links = []
    failures = []
    try:
        plugs = cmds.animLayer(
            layer,
            query=True,
            attribute=True,
        ) or []
    except Exception as exc:
        return [], [(layer, "could not read layer channels: {0}".format(exc))]

    for plug in _unique_preserving_order(plugs):
        detail_curve = _find_layer_curve_for_plug(layer, plug)
        if not detail_curve:
            failures.append((plug, "could not find additive detail curve"))
            continue
        try:
            detail_times = _key_times(detail_curve)
            breakdowns = _key_breakdown_states(
                detail_curve,
                len(detail_times),
            )
            anchor_times = [
                time_value for time_value, is_breakdown in zip(
                    detail_times, breakdowns
                ) if not bool(is_breakdown)
            ]
            base_curve = _base_curve_for_unlinked_plug(
                plug,
                detail_curve,
                selected_curves,
            )
            try:
                (
                    base_times,
                    anchor_indices,
                    anchor_times,
                ) = _adopt_anchor_indices(
                    base_curve,
                    anchor_times,
                    detail_times,
                )
            except RuntimeError as timing_error:
                (
                    base_times,
                    anchor_indices,
                    anchor_times,
                ) = _residual_anchor_mapping(
                    plug,
                    base_curve,
                    detail_curve,
                    detail_times,
                    str(timing_error),
                )
            _normalize_linked_detail_breakdowns(
                detail_curve,
                detail_times,
                anchor_times,
            )
            links.append({
                "plug": plug,
                "baseCurve": base_curve,
                "baseCurveUuid": _node_uuid(base_curve),
                "baseKeyCount": len(base_times),
                "baseKeyTimes": base_times,
                "baseAnchorIndices": anchor_indices,
                "detailCurve": detail_curve,
                "detailCurveUuid": _node_uuid(detail_curve),
                "detailAnchorTimes": anchor_times,
                "detailKeyCount": len(detail_times),
            })
        except Exception as exc:
            failures.append((plug, str(exc)))
    return links, failures


def _adopt_unlinked_detail_layers():
    """Attach missing metadata to unambiguous legacy ACR detail layers."""
    selected_curves = _selected_base_curve_candidates()
    adopted_links = 0
    failures = []
    adopted_layers = []
    layers = [
        layer for layer in (cmds.ls(type="animLayer") or [])
        if "_ACR_Detail" in layer
        and not cmds.objExists(layer + "." + LINK_DATA_ATTR)
    ]
    for layer in layers:
        old_lock = None
        try:
            old_lock = bool(cmds.animLayer(layer, query=True, lock=True))
            cmds.animLayer(layer, edit=True, lock=False)
            links, layer_failures = _adopt_links_from_layer(
                layer,
                selected_curves,
            )
            failures.extend(layer_failures)
            if links:
                _write_link_data(layer, links)
                adopted_links += len(links)
                adopted_layers.append(layer)
        except Exception as exc:
            failures.append((layer, "could not save repaired links: {0}".format(exc)))
        finally:
            if old_lock is not None and cmds.objExists(layer):
                try:
                    cmds.animLayer(layer, edit=True, lock=old_lock)
                except Exception:
                    pass
    return adopted_links, adopted_layers, failures


def _safe_additive_layer_name(samples):
    first_plug = sorted(samples)[0]
    node = first_plug.split(".", 1)[0].split("|")[-1]
    cleaned = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in node
    ).strip("_")
    base_name = (cleaned or "Animation") + "_ACR_Detail"
    if not cmds.objExists(base_name):
        return base_name
    suffix = 1
    while cmds.objExists("{0}_{1}".format(base_name, suffix)):
        suffix += 1
    return "{0}_{1}".format(base_name, suffix)


def _capture_animation_layer_ui_state():
    state = {}
    for layer in cmds.ls(type="animLayer") or []:
        try:
            state[layer] = {
                "selected": bool(cmds.animLayer(
                    layer, query=True, selected=True
                )),
                "preferred": bool(cmds.animLayer(
                    layer, query=True, preferred=True
                )),
            }
        except Exception:
            pass
    return state


def _restore_animation_layer_ui_state(state, generated_layer=None):
    """Leave no generated layer selected/preferred after graph rewiring."""
    if generated_layer and cmds.objExists(generated_layer):
        try:
            cmds.animLayer(
                generated_layer,
                edit=True,
                selected=False,
                preferred=False,
            )
        except Exception:
            pass
    for layer, values in state.items():
        if not cmds.objExists(layer):
            continue
        try:
            cmds.animLayer(
                layer,
                edit=True,
                selected=values["selected"],
                preferred=values["preferred"],
            )
        except Exception:
            pass


def _suspend_graph_editor_buffer_display():
    """Hide buffer overlays while Maya constructs animation-layer curves."""
    states = {}
    for editor in _graph_editors():
        try:
            value = cmds.animCurveEditor(
                editor,
                query=True,
                showBufferCurves=True,
            )
            enabled = (
                value is True
                or str(value).strip().lower() in ("1", "true", "on")
            )
            states[editor] = enabled
            if enabled:
                cmds.animCurveEditor(
                    editor,
                    edit=True,
                    showBufferCurves="off",
                )
                try:
                    cmds.animCurveEditor(
                        editor,
                        query=True,
                        curvesShownForceUpdate=True,
                    )
                except Exception:
                    pass
        except Exception:
            pass
    return states


def _restore_graph_editor_buffer_display(states):
    for editor, enabled in states.items():
        try:
            if not cmds.animCurveEditor(editor, exists=True):
                continue
            cmds.animCurveEditor(
                editor,
                edit=True,
                showBufferCurves="on" if enabled else "off",
            )
            try:
                cmds.animCurveEditor(
                    editor,
                    query=True,
                    curvesShownForceUpdate=True,
                )
            except Exception:
                pass
        except Exception:
            pass


def _refresh_animation_layer_ui():
    try:
        cmds.animLayer(forceUIRefresh=True)
    except Exception:
        pass


def _create_additive_detail_layer(samples, link_specs=None, progress=None):
    """Bake absolute originals to a muted additive layer over reduced bases."""
    if not samples:
        return None, 0, [], []
    layer = None
    key_count = 0
    links = []
    link_failures = []
    layer_ui_state = _capture_animation_layer_ui_state()
    buffer_display_state = _suspend_graph_editor_buffer_display()
    try:
        layer = cmds.animLayer(
            _safe_additive_layer_name(samples),
            override=False,
        )
        for plug in sorted(samples):
            cmds.animLayer(layer, edit=True, attribute=plug)
        cmds.animLayer(layer, edit=True, selected=True, preferred=True)

        plug_count = len(samples)
        progress_start = 0
        if progress:
            progress_start = int(progress.get("progress", 0))
            progress["maximum"] = progress_start + plug_count
            if progress.get("open"):
                try:
                    cmds.progressWindow(
                        edit=True,
                        maxValue=progress["maximum"],
                    )
                except Exception:
                    progress["open"] = False
        for plug_index, plug in enumerate(sorted(samples)):
            if _batch_progress_cancelled(progress):
                raise _BatchCancelled(
                    "Additive-layer baking cancelled by user."
                )
            status = "Baking additive detail layer: {0}/{1} channels".format(
                plug_index + 1,
                plug_count,
            )
            if progress:
                _update_batch_progress(
                    progress,
                    progress_start + plug_index + 1,
                    status,
                )
            else:
                _set_status(status)
            for sample_index, (time_value, value) in enumerate(
                    sorted(samples[plug].items())):
                if (sample_index % 50 == 0
                        and _batch_progress_cancelled(progress)):
                    raise _BatchCancelled(
                        "Additive-layer baking cancelled by user."
                    )
                cmds.setKeyframe(
                    plug,
                    time=time_value,
                    value=value,
                    animLayer=layer,
                    noResolve=False,
                )
                key_count += 1
        links, link_failures = _configure_linked_detail_keys(
            layer,
            link_specs or {},
        )
        # Muted is deliberate: the visible result remains the animator-ready
        # reduced base. Unmute or blend the layer to restore original detail.
        cmds.animLayer(layer, edit=True, mute=True)
        return layer, key_count, links, link_failures
    except Exception:
        if layer and cmds.objExists(layer):
            try:
                cmds.delete(layer)
            except Exception:
                pass
        raise
    finally:
        _restore_animation_layer_ui_state(
            layer_ui_state,
            generated_layer=layer,
        )
        _restore_graph_editor_buffer_display(buffer_display_state)
        _refresh_animation_layer_ui()


def _linked_timing_layers():
    result = []
    for layer in cmds.ls(type="animLayer") or []:
        if cmds.objExists(layer + "." + LINK_DATA_ATTR):
            result.append(layer)
    return result


def _key_times(curve):
    return [float(value) for value in (
        cmds.keyframe(
            curve,
            query=True,
            timeChange=True,
        ) or []
    )]


def _previous_base_times(link, base_curve, expected_count):
    """Find the last synchronized full base-key sequence for a link."""
    candidates = [link.get("baseKeyTimes", [])]
    candidates.append(
        LINK_AUTO_SYNC.get("timing_signatures", {}).get(base_curve, ())
    )
    for values in candidates:
        try:
            times = [float(value) for value in values]
        except (TypeError, ValueError):
            continue
        if len(times) == int(expected_count):
            return times
    return []


def _ordered_surviving_key_indices(previous_times, current_times):
    """Map old indices to new indices when current keys are a subsequence."""
    previous = [float(value) for value in previous_times]
    current = [float(value) for value in current_times]
    if len(current) >= len(previous):
        raise RuntimeError("base key deletion mapping was not requested")
    mapping = {}
    current_index = 0
    for previous_index, previous_time in enumerate(previous):
        if (current_index < len(current)
                and _same_key_time(
                    previous_time,
                    current[current_index],
                )):
            mapping[previous_index] = current_index
            current_index += 1
    if current_index != len(current):
        raise RuntimeError(
            "base keys were deleted and retimed in the same edit; the "
            "surviving correspondence is ambiguous"
        )
    return mapping


def _delete_detail_anchor_keys(
        detail_curve, anchor_times, record_undo=True):
    """Delete exact linked anchors directly, without UI/key selection."""
    selection = om.MSelectionList()
    selection.add(detail_curve)
    function = oma.MFnAnimCurve(selection.getDependNode(0))
    curve_times = _key_times(detail_curve)
    indices = []
    for anchor_time in anchor_times:
        matches = [
            index for index, time_value in enumerate(curve_times)
            if _same_key_time(anchor_time, time_value)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "could not identify additive anchor at {0:g}".format(
                    anchor_time
                )
            )
        indices.append(matches[0])
    if record_undo:
        for index in sorted(indices, reverse=True):
            cmds.cutKey(
                detail_curve,
                animation="objects",
                index=index,
                option="keys",
                clear=True,
                selectKey=False,
            )
    else:
        # Automatic synchronization deliberately stays out of Maya's undo
        # queue. API removal also avoids touching active Graph Editor keys.
        for index in sorted(indices, reverse=True):
            function.remove(index)
    expected_count = len(curve_times) - len(indices)
    actual_count = len(_key_times(detail_curve))
    if actual_count != expected_count:
        raise RuntimeError(
            "Maya removed {0} additive keys but {1} were expected".format(
                len(curve_times) - actual_count,
                len(indices),
            )
        )
    return len(indices)


def _piecewise_detail_times(detail_times, current_anchors, target_anchors):
    """Warp every detail key between corresponding linked base anchors."""
    detail = [float(value) for value in detail_times]
    current = [float(value) for value in current_anchors]
    targets = [float(value) for value in target_anchors]
    if len(current) != len(targets) or not current:
        raise RuntimeError("linked anchor count is inconsistent")
    if len(current) == 1:
        offset = targets[0] - current[0]
        return [time_value + offset for time_value in detail]
    for values, label in (
            (current, "stored detail"), (targets, "base")):
        if any(
                values[index] <= values[index - 1] + 1.0e-7
                for index in range(1, len(values))):
            raise RuntimeError(
                "{0} anchor times are no longer strictly ordered".format(
                    label
                )
            )

    warped = []
    segment = 0
    for time_value in detail:
        while (segment + 1 < len(current) - 1
               and time_value > current[segment + 1] + 1.0e-7):
            segment += 1
        if time_value < current[0] - 1.0e-7:
            mapped = time_value + (targets[0] - current[0])
        elif time_value > current[-1] + 1.0e-7:
            mapped = time_value + (targets[-1] - current[-1])
        else:
            left = min(segment, len(current) - 2)
            old_span = current[left + 1] - current[left]
            ratio = (time_value - current[left]) / old_span
            ratio = max(0.0, min(1.0, ratio))
            mapped = targets[left] + ratio * (
                targets[left + 1] - targets[left]
            )
        warped.append(float(mapped))

    if any(
            warped[index] <= warped[index - 1] + 1.0e-7
            for index in range(1, len(warped))):
        raise RuntimeError(
            "the edited base timing compresses detail keys onto the same "
            "time; spread the base anchors farther apart"
        )
    return warped


def _apply_curve_times_with_parking(curve, current_times, target_times):
    """Move all keys through an empty range so no destination can collide."""
    current = [float(value) for value in current_times]
    targets = [float(value) for value in target_times]
    if len(current) != len(targets) or not current:
        raise RuntimeError("detail curve key count changed during retiming")
    if any(
            targets[index] <= targets[index - 1] + 1.0e-7
            for index in range(1, len(targets))):
        raise RuntimeError("target detail key times are not strictly ordered")

    combined_min = min(min(current), min(targets))
    combined_max = max(max(current), max(targets))
    parking_offset = (
        combined_max - min(current)
        + max(combined_max - combined_min, 1.0)
        + 1000.0
    )
    cmds.keyframe(
        curve,
        edit=True,
        animation="objects",
        relative=True,
        timeChange=parking_offset,
        adjustBreakdown=False,
    )
    parked = [value + parking_offset for value in current]
    for parked_time, target_time in zip(parked, targets):
        cmds.keyframe(
            curve,
            edit=True,
            animation="objects",
            time=(parked_time, parked_time),
            timeChange=target_time,
            absolute=True,
            adjustBreakdown=False,
            option="over",
        )


def _retime_curve_keys_collision_free(curve, target_times):
    """Retime a complete curve, restoring original timing if Maya fails."""
    original = _key_times(curve)
    targets = [float(value) for value in target_times]
    if len(original) != len(targets):
        raise RuntimeError("detail curve key count changed before retiming")
    changed = sum(
        not _same_key_time(old_time, new_time)
        for old_time, new_time in zip(original, targets)
    )
    if not changed:
        return 0

    try:
        _apply_curve_times_with_parking(curve, original, targets)
        written = _key_times(curve)
        if (len(written) != len(targets)
                or any(not _same_key_time(actual, expected)
                       for actual, expected in zip(written, targets))):
            raise RuntimeError("Maya did not write the requested detail timing")
    except Exception as exc:
        restoration_error = None
        try:
            partial = _key_times(curve)
            if len(partial) != len(original):
                raise RuntimeError(
                    "key count changed from {0} to {1}".format(
                        len(original), len(partial)
                    )
                )
            _apply_curve_times_with_parking(curve, partial, original)
            restored = _key_times(curve)
            if (len(restored) != len(original)
                    or any(not _same_key_time(actual, expected)
                           for actual, expected in zip(restored, original))):
                raise RuntimeError("original timing verification failed")
        except Exception as restore_exc:
            restoration_error = restore_exc
        if restoration_error is not None:
            raise RuntimeError(
                "detail retiming failed ({0}); automatic restoration also "
                "failed ({1})".format(exc, restoration_error)
            )
        raise RuntimeError(
            "detail retiming failed; original timing was restored ({0})"
            .format(exc)
        )
    return changed


def _move_detail_anchors(detail_curve, current_times, target_times):
    """Piecewise-retime the dense breakdown curve with its linked anchors."""
    detail_times = _key_times(detail_curve)
    target_detail_times = _piecewise_detail_times(
        detail_times,
        current_times,
        target_times,
    )
    return _retime_curve_keys_collision_free(
        detail_curve,
        target_detail_times,
    )


def _sync_one_link_unsafe(link, record_undo=True):
    base_curve = _resolve_link_node(
        link.get("baseCurve", ""),
        link.get("baseCurveUuid", ""),
    )
    detail_curve = _resolve_link_node(
        link.get("detailCurve", ""),
        link.get("detailCurveUuid", ""),
    )
    if not base_curve or not _is_time_anim_curve(base_curve):
        raise RuntimeError("base animation curve no longer exists")
    if not detail_curve or not _is_time_anim_curve(detail_curve):
        raise RuntimeError("additive detail curve no longer exists")

    base_times = _key_times(base_curve)
    expected_base_count = int(link.get("baseKeyCount", -1))
    detail_times = _key_times(detail_curve)
    expected_detail_count = int(link.get("detailKeyCount", -1))
    if len(detail_times) != expected_detail_count:
        raise RuntimeError(
            "detail key count changed ({0}, expected {1}); automatic "
            "correspondence is no longer safe".format(
                len(detail_times),
                expected_detail_count,
            )
        )

    deleted_detail_keys = 0
    if base_times and detail_times:
        base_start = base_times[0]
        base_end = base_times[-1]
        orphan_detail_times = [
            time_value for time_value in detail_times
            if (time_value < base_start - 1.0e-6
                or time_value > base_end + 1.0e-6)
        ]
        if orphan_detail_times:
            link["_acrMutationStarted"] = True
            pruned = _delete_detail_anchor_keys(
                detail_curve,
                orphan_detail_times,
                record_undo=record_undo,
            )
            deleted_detail_keys += pruned
            detail_times = _key_times(detail_curve)
            _link_debug(
                "{0}: pruned {1} additive key(s) outside the current base "
                "span {2:g}-{3:g}.".format(
                    link.get("plug", detail_curve),
                    pruned,
                    base_start,
                    base_end,
                )
            )

    indices = [int(value) for value in link.get("baseAnchorIndices", [])]
    current_anchors = [
        float(value) for value in link.get("detailAnchorTimes", [])
    ]
    if (len(indices) != len(current_anchors) or not indices
            or any(index < 0 or index >= expected_base_count
                   for index in indices)):
        raise RuntimeError("stored anchor mapping is invalid")

    structural_base_change = False
    if len(base_times) < expected_base_count:
        structural_base_change = True
        _link_debug(
            "{0}: detected {1} deleted base key(s); reconciling additive "
            "anchors.".format(
                link.get("plug", base_curve),
                expected_base_count - len(base_times),
            )
        )
        previous_base_times = _previous_base_times(
            link,
            base_curve,
            expected_base_count,
        )
        if not previous_base_times:
            raise RuntimeError(
                "base keys were deleted but this legacy link has no prior "
                "key sequence; run Sync once before deleting keys"
            )
        surviving_indices = _ordered_surviving_key_indices(
            previous_base_times,
            base_times,
        )
        deleted_base_indices = [
            index for index in range(len(previous_base_times))
            if index not in surviving_indices
        ]
        deleted_base_times = [
            previous_base_times[index]
            for index in deleted_base_indices
        ]
        surviving_pairs = []
        deleted_anchor_times = []
        for old_index, anchor_time in zip(indices, current_anchors):
            if old_index in surviving_indices:
                surviving_pairs.append((
                    surviving_indices[old_index],
                    anchor_time,
                ))
            else:
                deleted_anchor_times.append(anchor_time)
        if not surviving_pairs:
            raise RuntimeError(
                "all linked base anchors were deleted; keep at least one "
                "base key or rebuild the additive detail layer"
            )
        # A deleted base key can correspond either to a regular linked anchor
        # or to a dense breakdown key. Remove the exact additive key in both
        # cases; limiting this to stored anchors left visible slave keys behind.
        detail_delete_times = [
            anchor_time for anchor_time in deleted_anchor_times
            if any(_same_key_time(anchor_time, detail_time)
                   for detail_time in detail_times)
        ]
        for deleted_time in deleted_base_times:
            if (any(_same_key_time(deleted_time, detail_time)
                    for detail_time in detail_times)
                    and not any(_same_key_time(deleted_time, existing)
                                for existing in detail_delete_times)):
                detail_delete_times.append(deleted_time)
        if detail_delete_times:
            link["_acrMutationStarted"] = True
            deleted_now = _delete_detail_anchor_keys(
                detail_curve,
                detail_delete_times,
                record_undo=record_undo,
            )
            deleted_detail_keys += deleted_now
            detail_times = _key_times(detail_curve)
            _link_debug(
                "{0}: deleted {1} additive key(s) at deleted base-key "
                "times {2}.".format(
                    link.get("plug", detail_curve),
                    deleted_now,
                    [round(value, 4) for value in detail_delete_times],
                )
            )
        indices = [pair[0] for pair in surviving_pairs]
        current_anchors = [pair[1] for pair in surviving_pairs]
    elif len(base_times) > expected_base_count:
        raise RuntimeError(
            "base key count increased ({0}, expected {1}); newly inserted "
            "base keys cannot yet be linked automatically".format(
                len(base_times),
                expected_base_count,
            )
        )

    detail_anchor_indices = []
    for anchor_time in current_anchors:
        matches = [
            index for index, value in enumerate(detail_times)
            if _same_key_time(anchor_time, value)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "detail anchor at {0:g} was deleted or moved manually"
                .format(anchor_time)
            )
        detail_anchor_indices.append(matches[0])
    repaired_breakdowns = False
    if any(
            _key_breakdown_state(detail_curve, index)
            for index in detail_anchor_indices):
        link["_acrMutationStarted"] = True
        repaired_breakdowns = _normalize_linked_detail_breakdowns(
            detail_curve,
            detail_times,
            current_anchors,
        )
    if repaired_breakdowns:
        _link_debug(
            "{0}: repaired additive anchor/breakdown states before syncing."
            .format(link.get("plug", detail_curve))
        )
    for anchor_time, detail_index in zip(
            current_anchors, detail_anchor_indices):
        if _key_breakdown_state(detail_curve, detail_index):
            raise RuntimeError(
                "linked anchor at {0:g} is no longer a regular key"
                .format(anchor_time)
            )

    target_times = [base_times[index] for index in indices]
    if any(
            not _same_key_time(old_time, new_time)
            for old_time, new_time in zip(current_anchors, target_times)):
        _link_debug(
            "{0}: warping {1} detail keys from anchors {2} to {3}."
            .format(
                link.get("plug", base_curve),
                len(detail_times),
                [round(value, 4) for value in current_anchors],
                [round(value, 4) for value in target_times],
            )
        )
    else:
        _link_debug(
            "{0}: stored base curve {1} still has unchanged anchors {2}."
            .format(
                link.get("plug", base_curve),
                base_curve,
                [round(value, 4) for value in target_times],
            )
        )
    link["_acrMutationStarted"] = True
    moved = _move_detail_anchors(
        detail_curve,
        current_anchors,
        target_times,
    )
    link["baseCurve"] = base_curve
    link["baseCurveUuid"] = _node_uuid(base_curve)
    link["baseKeyCount"] = len(base_times)
    link["baseKeyTimes"] = base_times
    link["baseAnchorIndices"] = indices
    link["detailCurve"] = detail_curve
    link["detailCurveUuid"] = _node_uuid(detail_curve)
    link["detailAnchorTimes"] = target_times
    link["detailKeyCount"] = len(_key_times(detail_curve))
    link.pop("_acrMutationStarted", None)
    changed_keys = moved + deleted_detail_keys
    return changed_keys, bool(changed_keys or structural_base_change)


def _sync_one_link(link, record_undo=True):
    """Synchronize one link transactionally, including key deletion."""
    detail_curve = _resolve_link_node(
        link.get("detailCurve", ""),
        link.get("detailCurveUuid", ""),
    )
    if not detail_curve or not _is_time_anim_curve(detail_curve):
        raise RuntimeError("additive detail curve no longer exists")
    safety_state = _capture_curve_state(detail_curve)
    saved_link = json.loads(json.dumps(link))
    try:
        return _sync_one_link_unsafe(
            link,
            record_undo=record_undo,
        )
    except Exception as exc:
        restoration_error = None
        mutation_started = bool(link.get("_acrMutationStarted"))
        if mutation_started:
            try:
                _restore_curve_state(detail_curve, safety_state)
            except Exception as restore_exc:
                restoration_error = restore_exc
        link.clear()
        link.update(saved_link)
        if restoration_error is not None:
            raise RuntimeError(
                "linked timing failed ({0}); additive curve restoration "
                "also failed ({1})".format(exc, restoration_error)
            )
        if mutation_started:
            raise RuntimeError(
                "linked timing failed; the additive curve was restored ({0})"
                .format(exc)
            )
        raise


def _sync_linked_detail_timing(record_undo=True, quiet=False):
    if LINK_AUTO_SYNC.get("busy"):
        return 0, []
    layers = _linked_timing_layers()
    adopted_links = 0
    adopted_layers = []
    adoption_failures = []
    if not layers and record_undo:
        adoption_undo_open = False
        try:
            cmds.undoInfo(
                openChunk=True,
                chunkName=TOOL_NAME + " Repair Detail Links",
            )
            adoption_undo_open = True
            (
                adopted_links,
                adopted_layers,
                adoption_failures,
            ) = _adopt_unlinked_detail_layers()
        finally:
            if adoption_undo_open:
                cmds.undoInfo(closeChunk=True)
        if adopted_links:
            _link_debug(
                "Repaired {0} missing link(s) on layer(s): {1}.".format(
                    adopted_links,
                    ", ".join(adopted_layers),
                )
            )
            layers = _linked_timing_layers()
    if not layers:
        LINK_AUTO_SYNC["base_curves"] = set()
        if adoption_failures:
            reason = "; ".join(
                "{0}: {1}".format(label, detail)
                for label, detail in adoption_failures[:5]
            )
            message = (
                "No usable linked detail layers found. Automatic repair "
                "could not establish a safe mapping: {0}".format(reason)
            )
        else:
            message = "No Animator Curve Reducer linked detail layers found."
        if not quiet:
            _set_results([message])
            _set_status(message, warning=True)
            cmds.warning("{0}: {1}".format(TOOL_NAME, message))
        return 0, []

    LINK_AUTO_SYNC["busy"] = True
    undo_open = False
    undo_state = None
    refresh_suspended = False
    graph_selection_state = _capture_graph_editor_selection_state()
    moved_total = 0
    failures = list(adoption_failures)
    progress = None
    processed_links = 0
    cancelled = False
    try:
        if record_undo:
            link_total = 0
            for layer in layers:
                payload = _read_link_data(layer)
                if payload:
                    link_total += len(payload["links"])
            progress = _open_batch_progress(
                TOOL_NAME + " - Sync Linked Timing",
                max(1, link_total),
                1,
            )
        if record_undo:
            cmds.undoInfo(openChunk=True, chunkName=TOOL_NAME + " Sync Detail")
            undo_open = True
        else:
            try:
                undo_state = bool(cmds.undoInfo(query=True, state=True))
                cmds.undoInfo(stateWithoutFlush=False)
            except Exception:
                undo_state = None
        cmds.refresh(suspend=True)
        refresh_suspended = True

        for layer in layers:
            payload = _read_link_data(layer)
            if not payload:
                failures.append((layer, "linked timing data is unreadable"))
                continue
            try:
                old_lock = bool(cmds.animLayer(
                    layer, query=True, lock=True
                ))
            except Exception:
                old_lock = False
            try:
                # Direct animCurve edits do not require the additive layer to
                # become active or unmuted. Only release its edit lock; mute,
                # selected and preferred states remain completely untouched.
                if old_lock:
                    cmds.animLayer(layer, edit=True, lock=False)
                layer_changed = False
                for link in payload["links"]:
                    if _batch_progress_cancelled(progress):
                        cancelled = True
                        break
                    processed_links += 1
                    if progress:
                        _update_batch_progress(
                            progress,
                            processed_links,
                            "Syncing linked timing [{0}] - {1}".format(
                                processed_links,
                                link.get("plug", layer),
                            ),
                        )
                    try:
                        moved, link_changed = _sync_one_link(
                            link,
                            record_undo=record_undo,
                        )
                        moved_total += moved
                        layer_changed = layer_changed or link_changed
                    except Exception as exc:
                        failures.append((
                            link.get("plug", layer),
                            str(exc),
                        ))
                if layer_changed:
                    _write_link_data(layer, payload["links"])
            finally:
                if cmds.objExists(layer):
                    try:
                        if old_lock:
                            cmds.animLayer(layer, edit=True, lock=True)
                    except Exception:
                        pass
            if cancelled:
                break
    except Exception as exc:
        failures.append(("linked timing sync", str(exc)))
        traceback.print_exc()
    finally:
        if refresh_suspended:
            cmds.refresh(suspend=False)
        if undo_open:
            cmds.undoInfo(closeChunk=True)
        if undo_state is not None:
            try:
                cmds.undoInfo(stateWithoutFlush=undo_state)
            except Exception:
                pass
        _close_batch_progress(progress)
        # Do not rebuild the Animation Layer UI or toggle buffer display here.
        # Both operations can replace Maya's Graph Editor selectionConnection
        # and expose/select the additive-layer channel after every edit.
        _restore_graph_editor_selection_state(graph_selection_state)
        LINK_AUTO_SYNC["busy"] = False
        _refresh_auto_sync_curve_cache()

    if not quiet:
        lines = [
            "LINKED TIMING  {0} additive detail key(s) retimed."
            .format(moved_total)
        ]
        if adopted_links:
            lines.insert(
                0,
                "REPAIRED LINKS  Adopted {0} channel(s) on: {1}.".format(
                    adopted_links,
                    ", ".join(adopted_layers),
                ),
            )
        if failures:
            lines.append("SYNC SKIPPED / FAILED ({0})".format(len(failures)))
            lines.extend(
                "  {0}: {1}".format(label, reason)
                for label, reason in failures
            )
        if cancelled:
            lines.insert(
                0,
                "CANCELLED SAFELY: completed linked channels remain "
                "synchronized; remaining channels were untouched.",
            )
        _set_results(lines)
        _set_status(
            "Linked timing synchronized: {0} detail key(s) retimed, {1} "
            "skipped/failed.".format(moved_total, len(failures)),
            warning=bool(failures),
        )
    return moved_total, failures


def sync_linked_detail_timing(*_unused):
    if PREVIEW.get("active"):
        cmds.warning(
            "{0}: Apply or cancel the active preview first.".format(
                TOOL_NAME
            )
        )
        return
    _sync_linked_detail_timing(record_undo=True, quiet=False)


def _link_debug(message):
    print(
        "[{0} v{1} LINKED TIMING] {2}".format(
            TOOL_NAME,
            VERSION,
            " ".join(str(message).split()),
        )
    )


def _linked_timing_signature(curve):
    """Return stable key timing data without depending on key selection."""
    return tuple(round(value, 7) for value in _key_times(curve))


def _mouse_button_is_down():
    try:
        return bool(QtWidgets.QApplication.mouseButtons())
    except Exception:
        return False


def _linked_watch_interval(curve_count):
    return min(
        AUTO_SYNC_WATCH_MAX_MS,
        max(AUTO_SYNC_WATCH_MIN_MS, int(curve_count) * 4),
    )


def _refresh_auto_sync_curve_cache():
    curves = set()
    link_samples = []
    for layer in _linked_timing_layers():
        payload = _read_link_data(layer)
        if not payload:
            continue
        for link in payload["links"]:
            curve = _resolve_link_node(
                link.get("baseCurve", ""),
                link.get("baseCurveUuid", ""),
            )
            if curve:
                curves.add(curve)
                if len(link_samples) < 5:
                    link_samples.append((
                        link.get("plug", "unknown plug"),
                        curve,
                        _resolve_link_node(
                            link.get("detailCurve", ""),
                            link.get("detailCurveUuid", ""),
                        ) or "missing detail curve",
                    ))
    LINK_AUTO_SYNC["base_curves"] = curves
    signatures = {}
    for curve in curves:
        try:
            signatures[curve] = _linked_timing_signature(curve)
        except Exception as exc:
            _link_debug(
                "Could not fingerprint {0}: {1}".format(curve, exc)
            )
    LINK_AUTO_SYNC["timing_signatures"] = signatures
    watch_timer = LINK_AUTO_SYNC.get("watch_timer")
    if watch_timer is not None:
        try:
            watch_timer.setInterval(_linked_watch_interval(len(curves)))
        except (RuntimeError, AttributeError):
            pass
    if LINK_AUTO_SYNC.get("enabled"):
        _link_debug(
            "Watching {0} linked base curve(s).".format(len(curves))
        )
        for plug, base_curve, detail_curve in link_samples:
            _link_debug(
                "Link {0}: base={1}, detail={2}.".format(
                    plug,
                    base_curve,
                    detail_curve,
                )
            )
    return curves


def _poll_linked_base_timing():
    """Fallback for Graph Editor edits missed by Maya's queued callback."""
    if (not LINK_AUTO_SYNC.get("enabled")
            or LINK_AUTO_SYNC.get("busy")
            or PREVIEW.get("active")):
        return
    try:
        if cmds.play(query=True, state=True) or _mouse_button_is_down():
            return
    except Exception:
        if _mouse_button_is_down():
            return

    watched = set(LINK_AUTO_SYNC.get("base_curves", set()))
    previous = dict(LINK_AUTO_SYNC.get("timing_signatures", {}))
    current = {}
    changed = []
    missing = []
    for curve in watched:
        if not cmds.objExists(curve):
            missing.append(curve)
            continue
        try:
            signature = _linked_timing_signature(curve)
        except Exception as exc:
            _link_debug("Timing poll failed for {0}: {1}".format(curve, exc))
            continue
        if curve in previous and signature != previous[curve]:
            changed.append(curve)
            # Keep the last synchronized sequence until the debounced sync
            # has reconciled structural edits such as deleted base keys.
            current[curve] = previous[curve]
        else:
            current[curve] = signature
    LINK_AUTO_SYNC["timing_signatures"] = current

    if missing:
        _link_debug(
            "Refreshing links after {0} watched curve(s) disappeared."
            .format(len(missing))
        )
        _refresh_auto_sync_curve_cache()
    if not changed:
        return
    _link_debug(
        "Timing watcher detected base key movement on: {0}; scheduling sync."
        .format(", ".join(sorted(changed)))
    )
    timer = LINK_AUTO_SYNC.get("timer")
    if timer is not None:
        timer.start(AUTO_SYNC_INTERVAL_MS)


def _dispose_link_auto_sync():
    for callback_id in list(LINK_AUTO_SYNC.get("callback_ids", [])):
        try:
            om.MMessage.removeCallback(callback_id)
        except Exception:
            pass
    LINK_AUTO_SYNC["callback_ids"] = []
    for timer_name in ("timer", "watch_timer"):
        timer = LINK_AUTO_SYNC.get(timer_name)
        if timer is not None:
            try:
                timer.stop()
                timer.timeout.disconnect()
                timer.deleteLater()
            except (RuntimeError, TypeError, AttributeError):
                pass
        LINK_AUTO_SYNC[timer_name] = None
    LINK_AUTO_SYNC["timer"] = None
    LINK_AUTO_SYNC["watch_timer"] = None
    LINK_AUTO_SYNC["enabled"] = False
    LINK_AUTO_SYNC["base_curves"] = set()
    LINK_AUTO_SYNC["timing_signatures"] = {}


def _run_debounced_auto_sync():
    if (not LINK_AUTO_SYNC.get("enabled")
            or LINK_AUTO_SYNC.get("busy")
            or PREVIEW.get("active")):
        return
    try:
        if (cmds.play(query=True, state=True)
                or _mouse_button_is_down()):
            timer = LINK_AUTO_SYNC.get("timer")
            if timer is not None:
                timer.start(max(250, AUTO_SYNC_INTERVAL_MS))
            return
    except Exception:
        pass
    _link_debug("Debounced edit received; starting automatic sync.")
    moved, failures = _sync_linked_detail_timing(
        record_undo=False,
        quiet=True,
    )
    for label, reason in failures:
        _link_debug(
            "Automatic sync failure - {0}: {1}".format(label, reason)
        )
    if moved or failures:
        _set_status(
            "Auto-sync: {0} detail key(s) retimed; {1} skipped/failed."
            .format(moved, len(failures)),
            warning=bool(failures),
        )
    _link_debug(
        "Automatic sync finished: {0} detail key(s) retimed, {1} failure(s)."
        .format(moved, len(failures))
    )


def _on_linked_anim_curves_edited(edited_curves, *_unused):
    if (not LINK_AUTO_SYNC.get("enabled")
            or LINK_AUTO_SYNC.get("busy")
            or PREVIEW.get("active")):
        return
    watched = set(LINK_AUTO_SYNC.get("base_curves", set()))
    if not watched:
        return
    edited_names = set()
    try:
        for index in range(len(edited_curves)):
            edited_names.add(
                om.MFnDependencyNode(edited_curves[index]).name()
            )
    except Exception:
        return
    if not edited_names.intersection(watched):
        return
    _link_debug(
        "Base curve edit detected on: {0}; scheduling sync.".format(
            ", ".join(sorted(edited_names.intersection(watched)))
        )
    )
    timer = LINK_AUTO_SYNC.get("timer")
    if timer is not None:
        timer.start(AUTO_SYNC_INTERVAL_MS)


def _install_link_auto_sync():
    _dispose_link_auto_sync()
    _refresh_auto_sync_curve_cache()
    callback_ids = []
    try:
        callback_ids.append(
            oma.MAnimMessage.addAnimCurveEditedCallback(
                _on_linked_anim_curves_edited
            )
        )
    except Exception as exc:
        cmds.warning(
            "{0}: Maya edit callback is unavailable; using the linked "
            "timing watcher instead: {1}".format(
                TOOL_NAME,
                exc,
            )
        )
        _link_debug("Maya edit callback installation failed: {0}".format(exc))
    timer = QtCore.QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(_run_debounced_auto_sync)
    watch_timer = QtCore.QTimer()
    watch_timer.setSingleShot(False)
    watch_timer.setInterval(_linked_watch_interval(
        len(LINK_AUTO_SYNC.get("base_curves", set()))
    ))
    watch_timer.timeout.connect(_poll_linked_base_timing)
    LINK_AUTO_SYNC["callback_ids"] = callback_ids
    LINK_AUTO_SYNC["timer"] = timer
    LINK_AUTO_SYNC["watch_timer"] = watch_timer
    LINK_AUTO_SYNC["enabled"] = True
    _refresh_auto_sync_curve_cache()
    watch_timer.start()
    _link_debug(
        "Auto-sync installed; {0} linked base curve(s) watched by {1} "
        "plus {2} ms timing fallback.".format(
            len(LINK_AUTO_SYNC.get("base_curves", set())),
            "Maya callback" if callback_ids else "timing watcher",
            watch_timer.interval(),
        )
    )
    return True


def _auto_sync_toggled(value=None, *_unused):
    enabled = bool(value)
    cmds.optionVar(intValue=(
        OPTION_PREFIX + "autoSyncLinkedTimingV2",
        int(enabled),
    ))
    if enabled:
        if _install_link_auto_sync():
            _set_status(
                "Linked timing auto-sync enabled while this window is open."
            )
        else:
            control = UI.get("auto_sync_linked_timing")
            if control and cmds.checkBoxGrp(control, exists=True):
                cmds.checkBoxGrp(control, edit=True, value1=False)
    else:
        _dispose_link_auto_sync()
        _set_status("Linked timing auto-sync disabled.")


# ---------------------------------------------------------------------------
# Planning, applying and verification
# ---------------------------------------------------------------------------

def _effective_tolerance(values, error_mode, maximum_error):
    if error_mode == "Percent of curve range":
        value_range = max(values) - min(values)
        return max(value_range * maximum_error * 0.01, 1.0e-10)
    return max(float(maximum_error), 1.0e-10)


def _build_plan(curve, times, values, original_key_count, settings):
    value_range = max(values) - min(values)
    tolerance = _effective_tolerance(
        values,
        settings["error_mode"],
        settings["maximum_error"],
    )
    minimum_prominence = max(
        value_range * settings["extrema_prominence"] * 0.01,
        1.0e-12,
    )

    fit = _reduce_samples(
        times,
        values,
        tolerance,
        settings["preserve_extrema"],
        minimum_prominence,
        settings["extrema_window"],
        settings["maximum_keys"],
    )
    fit.update({
        "curve": curve,
        "times": times,
        "values": values,
        "tolerance": tolerance,
        "original_key_count": original_key_count,
        "sample_count": len(times),
        "minimum_prominence": minimum_prominence,
    })
    return fit


def _make_plan(curve, settings):
    times, values, original_key_count = _sample_curve(
        curve,
        settings["sample_step"],
    )
    return _build_plan(
        curve,
        times,
        values,
        original_key_count,
        settings,
    )


def _make_work_item_plan(work_item, settings):
    curve = work_item["curve"]
    selected_times = work_item.get("selected_times")
    if not selected_times:
        plan = _make_plan(curve, settings)
        plan["selection_label"] = work_item.get("selection_label", "")
        return plan

    try:
        weighted_values = cmds.keyTangent(
            curve,
            query=True,
            weightedTangents=True,
        ) or [False]
    except Exception:
        weighted_values = [False]
    if not isinstance(weighted_values, (list, tuple)):
        weighted_values = [weighted_values]
    if weighted_values and bool(weighted_values[0]):
        raise RuntimeError(
            "Partial selected-key reduction currently requires a "
            "non-weighted animation curve."
        )

    times, values, original_key_count = _sample_selected_key_range(
        curve,
        selected_times,
        settings["sample_step"],
    )
    plan = _build_plan(
        curve,
        times,
        values,
        original_key_count,
        settings,
    )
    plan.update({
        "partial_selection": True,
        "editable_key_times": list(selected_times),
        "selection_label": work_item.get("selection_label", ""),
    })
    return plan


def _angle_in_maya_units(angle_radians):
    return om.MAngle(
        angle_radians, om.MAngle.kRadians
    ).asUnits(om.MAngle.uiUnit())


def _measure_maya_slope_scale(curve, time_a, time_b):
    """
    Measure value-per-frame produced by tan(angle)==1 on this exact curve.

    Maya versions and animCurve output types do not all expose tangent command
    units consistently. Measuring the evaluated derivative avoids assumptions
    about frame rate, linear units and angular units.
    """
    duration = float(time_b) - float(time_a)
    if duration <= 1.0e-8:
        raise RuntimeError("Cannot calibrate a zero-length curve segment.")

    test_angle = _angle_in_maya_units(math.pi * 0.25)
    time_range = (time_a, time_a)
    cmds.keyTangent(
        curve,
        edit=True,
        time=time_range,
        lock=False,
    )
    cmds.keyTangent(
        curve,
        edit=True,
        time=time_range,
        inTangentType="fixed",
        outTangentType="fixed",
    )
    cmds.keyTangent(
        curve,
        edit=True,
        absolute=True,
        time=time_range,
        outAngle=test_angle,
    )

    # Maya stores time at a finite internal tick resolution. The old fixed
    # sub-thousandth-frame probe could quantise both evaluations onto the
    # endpoint and incorrectly report a zero slope. Probe progressively larger
    # intervals and use the first interval Maya can evaluate reliably.
    value_a = _evaluate_curve(curve, time_a)
    maximum_step = max(min(duration * 0.25, 0.5), 1.0e-6)
    raw_steps = (0.02, 0.05, 0.1, 0.25, duration * 0.1)
    steps = []
    for raw_step in raw_steps:
        step = min(max(float(raw_step), 1.0e-6), maximum_step)
        if not any(abs(step - existing) <= 1.0e-9 for existing in steps):
            steps.append(step)

    attempted = []
    for step in steps:
        try:
            value_full = _evaluate_curve(curve, time_a + step)
            value_half = _evaluate_curve(curve, time_a + (step * 0.5))
            delta_full = value_full - value_a
            delta_half = value_half - value_a
            derivative_full = delta_full / step
            derivative_half = delta_half / (step * 0.5)
            # Richardson extrapolation reduces curvature error while keeping
            # the measurement local to the first segment.
            slope_scale = (2.0 * derivative_half) - derivative_full
            attempted.append((step, slope_scale))
            resolved_both_probes = (
                abs(delta_full) >= 1.0e-12
                and abs(delta_half) >= 1.0e-12
                and derivative_full * derivative_half > 0.0
            )
            if (resolved_both_probes
                    and not math.isnan(slope_scale)
                    and not math.isinf(slope_scale)
                    and abs(slope_scale) >= 1.0e-8):
                return slope_scale, step
        except Exception as exc:
            attempted.append((step, str(exc)))

    raise RuntimeError(
        "Maya could not calibrate fixed tangents for {0}; adaptive probes "
        "returned {1}.".format(curve, attempted)
    )


def _set_fixed_tangent(curve, time_value, slope, slope_scale):
    tangent_angle = _angle_in_maya_units(
        math.atan(float(slope) / float(slope_scale))
    )
    cmds.keyTangent(
        curve,
        edit=True,
        time=(time_value, time_value),
        lock=False,
    )
    cmds.keyTangent(
        curve,
        edit=True,
        time=(time_value, time_value),
        inTangentType="fixed",
        outTangentType="fixed",
    )
    # ix/iy/ox/oy are Maya's unit-independent tangent vector components.
    cmds.keyTangent(
        curve,
        edit=True,
        absolute=True,
        time=(time_value, time_value),
        inAngle=tangent_angle,
        outAngle=tangent_angle,
    )
    cmds.keyTangent(
        curve,
        edit=True,
        time=(time_value, time_value),
        lock=True,
    )


def _set_maya_spline_fallback_tangents(plan):
    """Apply Maya-native tangents when fitted-angle writing is unavailable."""
    curve = plan["curve"]
    flat = set(plan.get("flat", []))
    for sample_index in plan["kept"]:
        time_value = plan["times"][sample_index]
        tangent_type = "flat" if sample_index in flat else "spline"
        time_range = (time_value, time_value)
        cmds.keyTangent(
            curve,
            edit=True,
            time=time_range,
            lock=False,
        )
        cmds.keyTangent(
            curve,
            edit=True,
            time=time_range,
            inTangentType=tangent_type,
            outTangentType=tangent_type,
        )
        cmds.keyTangent(
            curve,
            edit=True,
            time=time_range,
            lock=True,
        )


def _write_fit_to_curve(plan):
    curve = plan["curve"]
    times = plan["times"]
    values = plan["values"]
    kept = plan["kept"]
    slopes = plan["slopes"]
    kept_times = [times[sample_index] for sample_index in kept]
    kept_values = [values[sample_index] for sample_index in kept]

    # Never clear the whole curve before rebuilding it. Some Maya versions
    # delete an animCurve node as soon as its final key is removed. Add/update
    # the retained keys first, then remove only keys that are not retained.
    if plan.get("partial_selection"):
        _replace_selected_keys_safely(
            curve,
            plan["editable_key_times"],
            kept_times,
            kept_values,
        )
    else:
        _replace_curve_keys_safely(curve, kept_times, kept_values)

    if not plan.get("partial_selection"):
        cmds.keyTangent(
            curve,
            edit=True,
            animation="objects",
            weightedTangents=False,
        )
    if plan.get("tangent_mode") == "maya spline fallback":
        _set_maya_spline_fallback_tangents(plan)
    else:
        try:
            if "maya_slope_scale" not in plan:
                calibration_errors = []
                slope_scale = None
                calibration_step = None
                calibration_segment = None
                for position in range(min(len(kept_times) - 1, 8)):
                    try:
                        slope_scale, calibration_step = (
                            _measure_maya_slope_scale(
                                curve,
                                kept_times[position],
                                kept_times[position + 1],
                            )
                        )
                        calibration_segment = position
                        break
                    except Exception as exc:
                        calibration_errors.append(str(exc))
                if slope_scale is None:
                    raise RuntimeError(
                        "Fixed-tangent calibration failed on {0} segment(s): "
                        "{1}".format(
                            len(calibration_errors), calibration_errors
                        )
                    )
                plan["maya_slope_scale"] = slope_scale
                plan["tangent_calibration_step"] = calibration_step
                plan["tangent_calibration_segment"] = calibration_segment
            for sample_index in kept:
                _set_fixed_tangent(
                    curve,
                    times[sample_index],
                    slopes[sample_index],
                    plan["maya_slope_scale"],
                )
            plan["tangent_mode"] = "fitted fixed"
        except Exception as exc:
            plan["fixed_tangent_error"] = str(exc)
            plan["tangent_mode"] = "maya spline fallback"
            plan.pop("maya_slope_scale", None)
            _batch_debug(
                "{0}: fixed tangents unavailable ({1}); automatically "
                "switching to verified Maya spline tangents.".format(
                    _curve_label(curve), exc
                ),
                force=True,
            )
            _set_maya_spline_fallback_tangents(plan)
    if plan.get("partial_selection"):
        _restore_partial_outer_tangents(plan)


def _verify_curve(plan):
    errors = []
    for index, time_value in enumerate(plan["times"]):
        actual = _evaluate_curve(plan["curve"], time_value)
        errors.append(abs(actual - plan["values"][index]))
    return errors


def _apply_plan(
        plan,
        maximum_keys,
        status_callback=None,
        cancel_callback=None):
    """
    Write and verify a plan.

    Maya's evaluator is the final authority. If a Maya-version-specific tangent
    conversion differs from the analytical Hermite prediction, add the
    worst-fitting source sample and refit, up to a bounded correction count.
    """
    correction_count = 0
    limit = int(maximum_keys)

    while True:
        if cancel_callback and cancel_callback():
            raise _BatchCancelled("Reduction cancelled by user.")
        if status_callback:
            status_callback("Writing fitted keys", correction_count)
        _write_fit_to_curve(plan)
        if cancel_callback and cancel_callback():
            raise _BatchCancelled("Reduction cancelled by user.")
        if status_callback:
            status_callback("Verifying Maya curve", correction_count)
        errors = _verify_curve(plan)
        actual_maximum = max(errors) if errors else 0.0
        plan["actual_maximum_error"] = actual_maximum
        if cancel_callback and cancel_callback():
            raise _BatchCancelled("Reduction cancelled by user.")

        if actual_maximum <= plan["tolerance"] + 1.0e-10:
            plan["verified"] = True
            break
        correction_limit = (
            FALLBACK_VERIFY_CORRECTION_LIMIT
            if plan.get("tangent_mode") == "maya spline fallback"
            else VERIFY_CORRECTION_LIMIT
        )
        if correction_count >= correction_limit:
            plan["verified"] = False
            break
        if limit and len(plan["kept"]) >= limit:
            plan["verified"] = False
            break

        candidates = [
            index for index in range(1, len(plan["times"]) - 1)
            if index not in plan["kept"]
        ]
        if not candidates:
            plan["verified"] = False
            break

        worst = max(candidates, key=lambda index: errors[index])
        kept = set(plan["kept"])
        kept.add(worst)
        updated = _fit_given_kept(
            plan["times"],
            plan["values"],
            kept,
            plan["flat"],
        )
        for key in ("kept", "slopes", "predictions", "errors",
                    "maximum_error"):
            plan[key] = updated[key]
        correction_count += 1

    plan["verification_corrections"] = correction_count
    if not plan.get("verified"):
        raise RuntimeError(
            "The fitted curve could not be verified within the requested "
            "error: {0} actual versus {1} allowed, using {2} keys after "
            "{3} correction(s), tangent mode {4}, tangent scale {5}. The "
            "original curve will "
            "be restored.".format(
                plan.get("actual_maximum_error", float("inf")),
                plan["tolerance"],
                len(plan["kept"]),
                correction_count,
                plan.get("tangent_mode", "unavailable"),
                plan.get("maya_slope_scale", "unavailable"),
            )
        )
    return plan


def _buffer_target_specs(curve):
    specs = [(curve, {})]
    try:
        destinations = cmds.listConnections(
            curve + ".output",
            source=False,
            destination=True,
            plugs=True,
        ) or []
    except Exception:
        destinations = []

    for plug in destinations:
        if "." not in plug:
            continue
        node, attribute = plug.rsplit(".", 1)
        spec = (node, {"attribute": attribute})
        if spec not in specs:
            specs.append(spec)
    return specs


def _buffer_exists(target, extra_flags):
    kwargs = {
        "query": True,
        "exists": True,
        "animation": "objects",
    }
    kwargs.update(extra_flags)
    try:
        return bool(cmds.bufferCurve(target, **kwargs))
    except Exception:
        return False


def _create_buffer(curve):
    for target, extra_flags in _buffer_target_specs(curve):
        kwargs = {
            "animation": "objects",
            "overwrite": True,
        }
        kwargs.update(extra_flags)
        try:
            created = cmds.bufferCurve(target, **kwargs)
        except Exception:
            continue
        if _buffer_exists(target, extra_flags):
            return True
        try:
            if int(created or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _swap_buffer(curve):
    for target, extra_flags in _buffer_target_specs(curve):
        if not _buffer_exists(target, extra_flags):
            continue
        kwargs = {
            "animation": "objects",
            "swap": True,
        }
        kwargs.update(extra_flags)
        cmds.bufferCurve(target, **kwargs)
        return True
    return False


def _query_key_array(curve, flag_name, default, count):
    try:
        kwargs = {"query": True, flag_name: True}
        values = cmds.keyTangent(curve, **kwargs) or []
    except Exception:
        values = []
    if not isinstance(values, (list, tuple)):
        values = [values]
    if len(values) != count:
        return [default for _index in range(count)]
    return list(values)


def _same_key_time(first, second, epsilon=1.0e-6):
    return abs(float(first) - float(second)) <= epsilon


def _time_lookup_key(value):
    return round(float(value), 6)


def _remove_unwanted_keys_by_index(
        target,
        desired_lookup,
        removable_lookup=None):
    """
    Remove residual keys by index, working backwards to keep indices stable.

    Maya can fail to match a large or sub-frame float passed through cutKey's
    time flag even when keyframe(query=True) returned that same displayed
    value. Index deletion avoids that time conversion completely.
    """
    removed = 0
    for _attempt in range(2):
        current_times = cmds.keyframe(
            target,
            query=True,
            timeChange=True,
        ) or []
        unwanted_indices = []
        for index, time_value in enumerate(current_times):
            lookup = _time_lookup_key(time_value)
            if lookup in desired_lookup:
                continue
            if (removable_lookup is not None
                    and lookup not in removable_lookup):
                continue
            unwanted_indices.append(index)

        if not unwanted_indices:
            break
        for index in reversed(unwanted_indices):
            cmds.cutKey(
                target,
                clear=True,
                index=(index, index),
            )
            removed += 1
    return removed


def _remove_duplicate_time_buckets_by_index(
        target,
        desired_times,
        removable_times=None):
    """Remove surplus sub-frame keys that share a desired rounded time."""
    desired_by_lookup = {}
    for time_value in desired_times:
        desired_by_lookup.setdefault(
            _time_lookup_key(time_value), []
        ).append(float(time_value))

    current_times = cmds.keyframe(
        target,
        query=True,
        timeChange=True,
    ) or []
    current_by_lookup = {}
    for index, time_value in enumerate(current_times):
        current_by_lookup.setdefault(
            _time_lookup_key(time_value), []
        ).append((index, float(time_value)))

    remove_indices = []
    for lookup, expected_times in desired_by_lookup.items():
        actual_items = current_by_lookup.get(lookup, [])
        surplus = len(actual_items) - len(expected_times)
        if surplus <= 0:
            continue
        ranked = sorted(
            actual_items,
            key=lambda item: min(
                abs(item[1] - expected) for expected in expected_times
            ),
        )
        for item in ranked[len(expected_times):]:
            if removable_times is not None and not any(
                    abs(item[1] - float(editable)) <= 1.0e-7
                    for editable in removable_times):
                continue
            remove_indices.append(item[0])

    for index in sorted(remove_indices, reverse=True):
        cmds.cutKey(
            target,
            clear=True,
            index=(index, index),
        )
    return len(remove_indices)


def _replace_curve_keys_safely(target, times, values):
    """
    Replace keys without ever leaving the target with zero keys.

    `target` may be an animCurve node or a driven attribute plug. Keys that
    must survive are created first; obsolete keys are removed afterwards.
    """
    if len(times) != len(values) or not times:
        raise RuntimeError("Replacement key data is incomplete.")
    if not cmds.objExists(target):
        raise RuntimeError(
            "Animation target no longer exists: {0}".format(target)
        )

    desired_times = [float(item) for item in times]
    desired_lookup = set(_time_lookup_key(item) for item in desired_times)
    for index, time_value in enumerate(desired_times):
        # Older Maya releases reject "fixed" on setKeyframe even though the
        # same tangent type is valid through keyTangent. Create/update the key
        # first; callers apply the intended fixed tangent in a separate step.
        cmds.setKeyframe(
            target,
            time=time_value,
            value=float(values[index]),
        )

    _remove_unwanted_keys_by_index(target, desired_lookup)
    _remove_duplicate_time_buckets_by_index(target, desired_times)

    if not cmds.objExists(target):
        raise RuntimeError(
            "Maya removed the animation target while replacing its keys."
        )
    final_times = cmds.keyframe(
        target, query=True, timeChange=True
    ) or []
    final_lookup = set(
        _time_lookup_key(actual) for actual in final_times
    )
    unexpected = [
        float(actual) for actual in final_times
        if _time_lookup_key(actual) not in desired_lookup
    ]
    missing = [
        desired for desired in desired_times
        if _time_lookup_key(desired) not in final_lookup
    ]
    if len(final_times) != len(desired_times) or unexpected or missing:
        raise RuntimeError(
            "Maya wrote {0} keys but {1} were expected; unexpected times "
            "{2}, missing times {3}.".format(
                len(final_times),
                len(desired_times),
                unexpected[:8],
                missing[:8],
            )
        )


def _replace_selected_keys_safely(
        target,
        editable_times,
        kept_times,
        kept_values):
    """Replace only one contiguous selected-key block on an animCurve."""
    if len(kept_times) != len(kept_values) or not kept_times:
        raise RuntimeError("Selected-key replacement data is incomplete.")
    if not cmds.objExists(target):
        raise RuntimeError(
            "Animation target no longer exists: {0}".format(target)
        )

    editable_times = [float(value) for value in editable_times]
    desired_times = [float(value) for value in kept_times]
    editable_lookup = set(
        _time_lookup_key(value) for value in editable_times
    )
    desired_lookup = set(
        _time_lookup_key(value) for value in desired_times
    )
    start_time = min(editable_times)
    end_time = max(editable_times)

    for index, time_value in enumerate(desired_times):
        cmds.setKeyframe(
            target,
            time=time_value,
            value=float(kept_values[index]),
        )

    _remove_unwanted_keys_by_index(
        target,
        desired_lookup,
        removable_lookup=editable_lookup,
    )
    _remove_duplicate_time_buckets_by_index(
        target,
        desired_times,
        removable_times=editable_times,
    )

    final_times = cmds.keyframe(
        target,
        query=True,
        timeChange=True,
    ) or []
    final_range_times = [
        float(time_value)
        for time_value in final_times
        if start_time - 1.0e-7 <= float(time_value) <= end_time + 1.0e-7
    ]
    final_lookup = set(
        _time_lookup_key(actual) for actual in final_range_times
    )
    unexpected = [
        actual for actual in final_range_times
        if _time_lookup_key(actual) not in desired_lookup
    ]
    missing = [
        desired for desired in desired_times
        if _time_lookup_key(desired) not in final_lookup
    ]
    if (len(final_range_times) != len(desired_times)
            or unexpected or missing):
        raise RuntimeError(
            "Maya wrote {0} keys in the selected range but {1} were "
            "expected; unexpected times {2}, missing times {3}.".format(
                len(final_range_times),
                len(desired_times),
                unexpected[:8],
                missing[:8],
            )
        )


def _capture_curve_state(curve):
    """Capture enough key data to restore a curve if an apply step fails."""
    times = cmds.keyframe(curve, query=True, timeChange=True) or []
    values = cmds.keyframe(curve, query=True, valueChange=True) or []
    if len(times) != len(values):
        raise RuntimeError("Could not capture the original curve state.")
    count = len(times)

    try:
        weighted_values = cmds.keyTangent(
            curve,
            query=True,
            weightedTangents=True,
        ) or [False]
    except Exception:
        weighted_values = [False]
    if not isinstance(weighted_values, (list, tuple)):
        weighted_values = [weighted_values]
    breakdowns = _key_breakdown_states(curve, count)

    try:
        destinations = cmds.listConnections(
            curve + ".output",
            source=False,
            destination=True,
            plugs=True,
        ) or []
    except Exception:
        destinations = []

    return {
        "times": list(times),
        "values": list(values),
        "destinations": list(destinations),
        "weighted": bool(weighted_values[0]) if weighted_values else False,
        "in_types": _query_key_array(
            curve, "inTangentType", "fixed", count
        ),
        "out_types": _query_key_array(
            curve, "outTangentType", "fixed", count
        ),
        "ix": _query_key_array(curve, "ix", 1.0, count),
        "iy": _query_key_array(curve, "iy", 0.0, count),
        "ox": _query_key_array(curve, "ox", 1.0, count),
        "oy": _query_key_array(curve, "oy", 0.0, count),
        "locks": _query_key_array(curve, "lock", True, count),
        "weight_locks": _query_key_array(
            curve, "weightLock", True, count
        ),
        "breakdowns": list(breakdowns),
    }


def _curve_state_key_index(state, time_value):
    for index, candidate in enumerate(state.get("times", [])):
        if _same_key_time(candidate, time_value):
            return index
    raise RuntimeError(
        "Could not find selected boundary key at {0}.".format(time_value)
    )


def _attach_partial_boundary_state(plan, curve_state):
    if not plan.get("partial_selection"):
        return
    if curve_state.get("weighted"):
        raise RuntimeError(
            "Partial selected-key reduction requires non-weighted tangents."
        )

    start_time = plan["editable_key_times"][0]
    end_time = plan["editable_key_times"][-1]
    start_index = _curve_state_key_index(curve_state, start_time)
    end_index = _curve_state_key_index(curve_state, end_time)
    last_index = len(curve_state["times"]) - 1
    plan["partial_boundary_state"] = {
        "start_time": start_time,
        "preserve_start_in": start_index > 0,
        "start_ix": curve_state["ix"][start_index],
        "start_iy": curve_state["iy"][start_index],
        "end_time": end_time,
        "preserve_end_out": end_index < last_index,
        "end_out_type": curve_state["out_types"][end_index],
        "end_ox": curve_state["ox"][end_index],
        "end_oy": curve_state["oy"][end_index],
    }


def _restore_partial_outer_tangents(plan):
    boundary = plan.get("partial_boundary_state")
    if not boundary:
        raise RuntimeError(
            "The selected range is missing its boundary tangent snapshot."
        )
    curve = plan["curve"]

    if boundary["preserve_start_in"]:
        time_range = (boundary["start_time"], boundary["start_time"])
        cmds.keyTangent(curve, edit=True, time=time_range, lock=False)
        cmds.keyTangent(
            curve,
            edit=True,
            time=time_range,
            inTangentType="fixed",
        )
        cmds.keyTangent(
            curve,
            edit=True,
            absolute=True,
            time=time_range,
            ix=boundary["start_ix"],
            iy=boundary["start_iy"],
        )

    if boundary["preserve_end_out"]:
        time_range = (boundary["end_time"], boundary["end_time"])
        cmds.keyTangent(curve, edit=True, time=time_range, lock=False)
        if boundary["end_out_type"] in ("step", "stepnext"):
            cmds.keyTangent(
                curve,
                edit=True,
                time=time_range,
                outTangentType=boundary["end_out_type"],
            )
        else:
            cmds.keyTangent(
                curve,
                edit=True,
                time=time_range,
                outTangentType="fixed",
            )
            cmds.keyTangent(
                curve,
                edit=True,
                absolute=True,
                time=time_range,
                ox=boundary["end_ox"],
                oy=boundary["end_oy"],
            )


def _restore_curve_state(curve, state):
    """Best-effort exact restoration used only after a failed curve write."""
    target = curve
    if not cmds.objExists(target):
        target = ""
        for destination in state.get("destinations", []):
            if cmds.objExists(destination):
                target = destination
                break
        if not target:
            raise RuntimeError(
                "The curve and its driven attribute no longer exist."
            )

    _replace_curve_keys_safely(target, state["times"], state["values"])

    cmds.keyTangent(
        target,
        edit=True,
        animation="objects",
        weightedTangents=state["weighted"],
    )
    for index, time_value in enumerate(state["times"]):
        time_range = (time_value, time_value)
        cmds.keyTangent(
            target,
            edit=True,
            time=time_range,
            lock=False,
            inTangentType="fixed",
            outTangentType="fixed",
        )
        if state["weighted"]:
            cmds.keyTangent(
                target,
                edit=True,
                time=time_range,
                weightLock=False,
            )
        cmds.keyTangent(
            target,
            edit=True,
            absolute=True,
            time=time_range,
            ix=state["ix"][index],
            iy=state["iy"][index],
        )
        cmds.keyTangent(
            target,
            edit=True,
            absolute=True,
            time=time_range,
            ox=state["ox"][index],
            oy=state["oy"][index],
        )
        cmds.keyTangent(
            target,
            edit=True,
            time=time_range,
            inTangentType=state["in_types"][index],
            outTangentType=state["out_types"][index],
        )
        cmds.keyTangent(
            target,
            edit=True,
            time=time_range,
            lock=bool(state["locks"][index]),
        )
        if state["weighted"]:
            cmds.keyTangent(
                target,
                edit=True,
                time=time_range,
                weightLock=bool(state["weight_locks"][index]),
            )
        if state["breakdowns"][index]:
            try:
                cmds.keyframe(
                    target,
                    edit=True,
                    time=time_range,
                    breakdown=True,
                )
            except Exception:
                pass


def _graph_editor_curve_selection_connections(editor):
    """Return the curve/key selection list, not the channel-row list."""
    connections = []
    try:
        connection = cmds.animCurveEditor(
            editor,
            query=True,
            selectionConnection=True,
        )
        if connection:
            connections.append(connection)
    except Exception:
        pass
    return connections


def _keep_buffer_curves_active(editor, curves):
    """Select curve nodes in the editor without selecting any keyframes."""
    activated = 0
    for connection in _graph_editor_curve_selection_connections(editor):
        if not cmds.selectionConnection(connection, exists=True):
            continue
        members = _selection_connection_members(connection)
        member_set = set(members)
        for curve in curves:
            if curve in member_set:
                continue
            try:
                cmds.selectionConnection(
                    connection,
                    edit=True,
                    select=curve,
                )
                member_set.add(curve)
                activated += 1
            except Exception:
                # Dynamic Maya-managed selection connections can be read-only.
                # Other editor/outliner connections are still attempted.
                pass
    return activated


def _show_buffer_curves(curves=None):
    """Show snapshots and keep their curves active without active keys."""
    valid_curves = []
    for curve in curves or []:
        if (curve not in valid_curves and cmds.objExists(curve)
                and _is_anim_curve(curve)):
            valid_curves.append(curve)

    shown = 0
    for editor in _graph_editors():
        try:
            cmds.animCurveEditor(
                editor,
                edit=True,
                showBufferCurves="on",
            )
            if valid_curves:
                _keep_buffer_curves_active(editor, valid_curves)
            # This query intentionally forces Maya to rebuild the displayed
            # curve list after the selection connection has changed.
            try:
                cmds.animCurveEditor(
                    editor,
                    query=True,
                    curvesShownForceUpdate=True,
                )
            except Exception:
                pass
            shown += 1
        except Exception:
            pass
    return shown


# ---------------------------------------------------------------------------
# UI and user actions
# ---------------------------------------------------------------------------

def _option_string(name, default):
    key = OPTION_PREFIX + name
    if cmds.optionVar(exists=key):
        return cmds.optionVar(query=key)
    return default


def _option_float(name, default):
    key = OPTION_PREFIX + name
    if cmds.optionVar(exists=key):
        return float(cmds.optionVar(query=key))
    return float(default)


def _option_int(name, default):
    key = OPTION_PREFIX + name
    if cmds.optionVar(exists=key):
        return int(cmds.optionVar(query=key))
    return int(default)


def _read_settings():
    settings = {
        "preset": cmds.optionMenuGrp(
            UI["preset"], query=True, value=True
        ),
        "error_mode": cmds.optionMenuGrp(
            UI["error_mode"], query=True, value=True
        ),
        "maximum_error": cmds.floatSliderGrp(
            UI["maximum_error"], query=True, value=True
        ),
        "sample_step": cmds.floatSliderGrp(
            UI["sample_step"], query=True, value=True
        ),
        "preserve_extrema": cmds.checkBoxGrp(
            UI["preserve_extrema"], query=True, value1=True
        ),
        "extrema_prominence": cmds.floatSliderGrp(
            UI["extrema_prominence"], query=True, value=True
        ),
        "extrema_window": cmds.floatSliderGrp(
            UI["extrema_window"], query=True, value=True
        ),
        "maximum_keys": cmds.intSliderGrp(
            UI["maximum_keys"], query=True, value=True
        ),
        "create_buffer": cmds.checkBoxGrp(
            UI["create_buffer"], query=True, value1=True
        ),
        "create_additive_layer": cmds.checkBoxGrp(
            UI["create_additive_layer"], query=True, value1=True
        ),
        "auto_sync_linked_timing": cmds.checkBoxGrp(
            UI["auto_sync_linked_timing"], query=True, value1=True
        ),
    }
    settings["maximum_error"] = max(settings["maximum_error"], 0.000001)
    settings["sample_step"] = max(settings["sample_step"], 0.01)
    settings["extrema_prominence"] = max(
        settings["extrema_prominence"], 0.0
    )
    settings["extrema_window"] = max(settings["extrema_window"], 0.01)
    settings["maximum_keys"] = max(settings["maximum_keys"], 0)
    return settings


def _save_settings(settings):
    cmds.optionVar(
        stringValue=(
            OPTION_PREFIX + "preset",
            settings.get("preset", "Custom"),
        )
    )
    cmds.optionVar(
        stringValue=(OPTION_PREFIX + "errorMode", settings["error_mode"])
    )
    for name, value in (
        ("maximumError", settings["maximum_error"]),
        ("sampleStep", settings["sample_step"]),
        ("extremaProminence", settings["extrema_prominence"]),
        ("extremaWindow", settings["extrema_window"]),
    ):
        cmds.optionVar(floatValue=(OPTION_PREFIX + name, float(value)))
    for name, value in (
        ("preserveExtrema", int(settings["preserve_extrema"])),
        ("maximumKeys", int(settings["maximum_keys"])),
        ("createBuffer", int(settings["create_buffer"])),
        (
            "createAdditiveLayer",
            int(settings["create_additive_layer"]),
        ),
        (
            "autoSyncLinkedTimingV2",
            int(settings["auto_sync_linked_timing"]),
        ),
    ):
        cmds.optionVar(intValue=(OPTION_PREFIX + name, value))


def _set_preset_menu(value):
    control = UI.get("preset")
    if control and cmds.optionMenuGrp(control, exists=True):
        cmds.optionMenuGrp(control, edit=True, value=value)


def _write_settings_to_ui(settings):
    """Write settings without replacing controls or starting nested work."""
    commands = (
        ("error_mode", cmds.optionMenuGrp, "value"),
        ("maximum_error", cmds.floatSliderGrp, "value"),
        ("sample_step", cmds.floatSliderGrp, "value"),
        ("preserve_extrema", cmds.checkBoxGrp, "value1"),
        ("extrema_prominence", cmds.floatSliderGrp, "value"),
        ("extrema_window", cmds.floatSliderGrp, "value"),
        ("maximum_keys", cmds.intSliderGrp, "value"),
        ("create_buffer", cmds.checkBoxGrp, "value1"),
        ("create_additive_layer", cmds.checkBoxGrp, "value1"),
        ("auto_sync_linked_timing", cmds.checkBoxGrp, "value1"),
    )
    for name, command, flag in commands:
        control = UI.get(name)
        if name not in settings or not control:
            continue
        if not command(control, exists=True):
            continue
        command(control, edit=True, **{flag: settings[name]})


def _mark_preset_custom():
    control = UI.get("preset")
    if not control or not cmds.optionMenuGrp(control, exists=True):
        return
    if cmds.optionMenuGrp(control, query=True, value=True) != "Custom":
        _set_preset_menu("Custom")


def _settings_dragged(*_unused):
    _mark_preset_custom()
    _preview_slider_dragged()


def _settings_changed(*_unused):
    _mark_preset_custom()
    _preview_slider_released()


def _preset_selected(value=None, *_unused):
    name = str(value or "")
    if name not in FIT_PRESETS:
        return
    values = dict(FIT_PRESETS[name])
    _write_settings_to_ui(values)
    _set_preset_menu(name)
    settings = _read_settings()
    settings["preset"] = name
    _save_settings(settings)
    _set_status("{0} reduction preset loaded.".format(name))
    _preview_slider_released()


def reset_settings(*_unused):
    values = dict(DEFAULT_SETTINGS)
    _write_settings_to_ui(values)
    _auto_sync_toggled(values["auto_sync_linked_timing"])
    _set_preset_menu(values["preset"])
    settings = _read_settings()
    settings["preset"] = values["preset"]
    _save_settings(settings)
    advanced = UI.get("advanced_frame")
    if advanced and cmds.frameLayout(advanced, exists=True):
        cmds.frameLayout(advanced, edit=True, collapse=True)
    _set_status("Settings restored to the Balanced defaults.")
    _preview_slider_released()


def _set_status(message, warning=False):
    if UI.get("status") and cmds.control(UI["status"], exists=True):
        cmds.text(
            UI["status"],
            edit=True,
            label=("Warning: " + message) if warning else message,
        )


def _set_results(lines):
    if not UI.get("results") or not cmds.control(UI["results"], exists=True):
        return
    cmds.textScrollList(UI["results"], edit=True, removeAll=True)
    if lines:
        cmds.textScrollList(UI["results"], edit=True, append=lines)


def _collect_action_targets():
    global LAST_TARGETS
    if TARGET_OVERRIDE.get("curves") is not None:
        curves = [
            curve for curve in TARGET_OVERRIDE["curves"]
            if cmds.objExists(curve)
        ]
        skipped = []
        source = TARGET_OVERRIDE.get("source") or "preview targets"
    else:
        curves, skipped, source = collect_target_curves()
    LAST_TARGETS = list(curves)
    if not curves:
        details = ""
        if skipped:
            details = " " + "; ".join(
                "{0}: {1}".format(item[0], item[1])
                for item in skipped[:3]
            )
        raise RuntimeError(
            "No editable time animation curves found.{0}".format(details)
        )
    return curves, skipped, source


def _collect_action_work_items():
    global LAST_TARGETS
    if TARGET_OVERRIDE.get("items") is not None:
        work_items = [
            item for item in TARGET_OVERRIDE["items"]
            if cmds.objExists(item["curve"])
        ]
        skipped = []
        source = TARGET_OVERRIDE.get("source") or "preview targets"
    elif TARGET_OVERRIDE.get("curves") is not None:
        curves, skipped, source = _collect_action_targets()
        work_items = [
            {
                "curve": curve,
                "selected_times": None,
                "selection_label": "",
            }
            for curve in curves
        ]
    else:
        work_items, skipped, had_key_selection = _selected_key_work_items()
        if had_key_selection:
            source = "selected Graph Editor key blocks"
        else:
            curves, skipped, source = collect_target_curves()
            work_items = [
                {
                    "curve": curve,
                    "selected_times": None,
                    "selection_label": "",
                }
                for curve in curves
            ]

    LAST_TARGETS = []
    for item in work_items:
        if item["curve"] not in LAST_TARGETS:
            LAST_TARGETS.append(item["curve"])

    if not work_items:
        details = ""
        if skipped:
            details = " " + "; ".join(
                "{0}: {1}".format(item[0], item[1])
                for item in skipped[:3]
            )
        raise RuntimeError(
            "No reducible animation curve or selected key block found.{0}"
            .format(details)
        )
    return work_items, skipped, source


def _target_summary_text():
    """Describe the current scope without sampling or fitting any curves."""
    if PREVIEW.get("active"):
        items = PREVIEW.get("apply_items") or PREVIEW.get("items") or []
        curves = []
        for item in items:
            curve = item.get("curve")
            if curve and curve not in curves:
                curves.append(curve)
        return "TARGETS  /  Preview locked to {0} channel(s), {1} block(s).".format(
            len(curves), len(items)
        )

    work_items, skipped, had_key_selection = _selected_key_work_items()
    if had_key_selection:
        curves = []
        for item in work_items:
            if item["curve"] not in curves:
                curves.append(item["curve"])
        if not work_items:
            return (
                "TARGETS  /  Selected keys contain no block with at least "
                "3 contiguous keys."
            )
        text = "TARGETS  /  {0} selected key block(s) across {1} channel(s).".format(
            len(work_items), len(curves)
        )
    else:
        curves, skipped, source = collect_target_curves()
        if not curves:
            return (
                "TARGETS  /  Select Graph Editor keys or channels, or "
                "select animated controls."
            )
        if source == "selected Graph Editor channels":
            text = "TARGETS  /  {0} selected Graph Editor channel(s).".format(
                len(curves)
            )
        else:
            text = "TARGETS  /  All {0} animated channel(s) on the selected controls.".format(
                len(curves)
            )

    if skipped:
        text += "  {0} non-editable target(s) will be skipped.".format(
            len(skipped)
        )
    return text


def _update_target_summary(*_unused):
    control = UI.get("target_summary")
    if not control or not cmds.control(control, exists=True):
        return
    try:
        message = _target_summary_text()
        curves = _target_indicator_curves()
        indicators = _target_channel_indicators(curves)
    except Exception as exc:
        message = "TARGETS  /  Unable to inspect selection: {0}".format(exc)
        indicators = []
    signature = (message, tuple(indicators))
    if signature == UI.get("target_signature"):
        return
    UI["target_signature"] = signature
    cmds.text(control, edit=True, label=message)
    _rebuild_target_indicator_bar(indicators)


def _target_indicator_curves():
    """Return current UI targets without sampling or changing selection."""
    if PREVIEW.get("active"):
        items = PREVIEW.get("apply_items") or PREVIEW.get("items") or []
        return _unique_preserving_order(
            item.get("curve") for item in items if item.get("curve")
        )
    work_items, _skipped, had_key_selection = _selected_key_work_items()
    if had_key_selection:
        return _unique_preserving_order(
            item["curve"] for item in work_items
        )
    curves, _skipped, _source = collect_target_curves()
    return curves


def _target_channel_indicators(curves):
    """Return stable label/axis tuples for the compact target feedback bar."""
    indicators = []
    seen = set()
    for curve in curves:
        plugs = _additive_destination_plugs(curve)
        if plugs:
            plug = plugs[0]
        else:
            try:
                plugs = cmds.listConnections(
                    curve + ".output",
                    source=False,
                    destination=True,
                    plugs=True,
                ) or []
            except Exception:
                plugs = []
            plug = str(plugs[0]) if plugs else str(curve)
        attribute = plug.rsplit(".", 1)[-1].split("[", 1)[0]
        lower = attribute.lower()
        axis = _axis_from_attribute(attribute)
        prefix = ""
        for long_name, short_name in (
                ("translate", "T"), ("rotate", "R"), ("scale", "S")):
            if lower.startswith(long_name):
                prefix = short_name
                break
        if not prefix and len(lower) == 2 and lower[0] in "trs":
            prefix = lower[0].upper()
        if prefix and axis:
            label = prefix + axis
        else:
            label = attribute or curve.split(":")[-1]
            label = label.replace("_", " ").upper()[:14]
            axis = "OTHER"
        key = (label, axis)
        if key not in seen:
            seen.add(key)
            indicators.append(key)
    return indicators


def _rebuild_target_indicator_bar(indicators):
    layout = UI.get("target_indicator_flow")
    if not layout or not cmds.flowLayout(layout, exists=True):
        return
    for child in cmds.flowLayout(
            layout, query=True, childArray=True) or []:
        try:
            cmds.deleteUI(child)
        except Exception:
            pass
    palette = {
        "X": (0.67, 0.20, 0.22),
        "Y": (0.24, 0.58, 0.28),
        "Z": (0.20, 0.38, 0.72),
    }
    other_colors = (
        (0.48, 0.25, 0.68),
        (0.72, 0.38, 0.16),
        (0.16, 0.52, 0.58),
        (0.62, 0.48, 0.14),
    )
    visible = indicators[:18]
    estimated_width = sum(
        max(34, min(112, 18 + len(label) * 8)) + 5
        for label, _axis in visible
    )
    row_count = max(1, int(math.ceil(estimated_width / 520.0)))
    cmds.flowLayout(
        layout,
        edit=True,
        height=(row_count * 28),
    )
    for index, (label, axis) in enumerate(visible):
        color = palette.get(axis, other_colors[index % len(other_colors)])
        cmds.text(
            parent=layout,
            label="  {0}  ".format(label),
            align="center",
            height=24,
            width=max(34, min(112, 18 + len(label) * 8)),
            backgroundColor=color,
            font="smallBoldLabelFont",
        )
    if len(indicators) > len(visible):
        cmds.text(
            parent=layout,
            label="  +{0}  ".format(len(indicators) - len(visible)),
            align="center",
            height=24,
            width=44,
            backgroundColor=(0.30, 0.31, 0.34),
            font="smallBoldLabelFont",
        )


def _dispose_target_watch_timer():
    global TARGET_WATCH_TIMER
    timer = TARGET_WATCH_TIMER
    if timer is not None:
        try:
            timer.stop()
            timer.timeout.disconnect()
            timer.deleteLater()
        except (RuntimeError, TypeError, AttributeError):
            pass
    TARGET_WATCH_TIMER = None
    UI["target_watch_timer"] = None


def _install_target_watch_timer():
    global TARGET_WATCH_TIMER
    _dispose_target_watch_timer()
    timer = QtCore.QTimer()
    timer.setSingleShot(False)
    timer.setInterval(180)
    timer.timeout.connect(_update_target_summary)
    timer.start()
    TARGET_WATCH_TIMER = timer
    UI["target_watch_timer"] = timer


def _kill_tool_script_jobs():
    """Kill every unexpired scriptJob owned by this tool."""
    global SCRIPT_JOBS
    for job_id in list(SCRIPT_JOBS):
        try:
            if cmds.scriptJob(exists=job_id):
                cmds.scriptJob(kill=job_id, force=True)
        except Exception:
            pass
    SCRIPT_JOBS = []


def _install_tool_script_jobs(parent):
    """Install one window-owned selection watcher, never a persistent job."""
    _kill_tool_script_jobs()
    try:
        job_id = cmds.scriptJob(
            event=["SelectionChanged", _update_target_summary],
            parent=parent,
            protected=True,
        )
        if job_id:
            SCRIPT_JOBS.append(job_id)
    except Exception as exc:
        cmds.warning(
            "{0}: Live target count is unavailable: {1}".format(
                TOOL_NAME, exc
            )
        )


def _progress_curve_label(curve, maximum_length=90):
    label = _curve_label(curve)
    if len(label) <= maximum_length:
        return label
    return "..." + label[-(maximum_length - 3):]


def _progress_work_item_label(work_item, maximum_length=90):
    label = _curve_label(work_item["curve"])
    selection_label = work_item.get("selection_label")
    if selection_label:
        label += " [{0}]".format(selection_label)
    if len(label) <= maximum_length:
        return label
    return "..." + label[-(maximum_length - 3):]


def _batch_debug_reset(action):
    BATCH_DIAGNOSTICS.update({
        "start_time": time.time(),
        "action": action,
        "lines": [],
    })
    _batch_debug("Started {0}".format(action), force=True)


def _batch_debug(message, force=False):
    start_time = BATCH_DIAGNOSTICS.get("start_time")
    elapsed = 0.0 if start_time is None else time.time() - start_time
    line = "[{0} v{1} DEBUG +{2:.3f}s] {3}".format(
        TOOL_NAME,
        VERSION,
        elapsed,
        " ".join(str(message).split()),
    )
    lines = BATCH_DIAGNOSTICS.setdefault("lines", [])
    lines.append(line)
    if len(lines) > 200:
        del lines[:-200]
    if force:
        print(line)


def _open_batch_progress(title, curve_count, steps_per_curve):
    maximum = max(1, int(curve_count) * int(steps_per_curve))
    state = {
        "open": False,
        "owned": False,
        "maximum": maximum,
        "progress": 0,
        "target_count": int(curve_count),
        "steps_per_target": int(steps_per_curve),
        "last_status": "",
    }
    try:
        created = cmds.progressWindow(
            title=title,
            minValue=0,
            maxValue=maximum,
            progress=0,
            status="Preparing...",
            isInterruptable=True,
        )
        # Maya releases disagree about the success return: some return True,
        # while others create the window successfully and return None. Only
        # an explicit False means another progress window prevented creation.
        progress_created = created is None or bool(created)
        state["open"] = progress_created
        state["owned"] = progress_created
        _batch_debug(
            "Progress window return={0!r}, owned={1}, maximum={2}".format(
                created,
                state["owned"],
                maximum,
            ),
            force=True,
        )
    except Exception:
        # Batch processing still works in Maya modes where progressWindow is
        # unavailable; the main tool status line remains as the fallback.
        state["open"] = False
        _batch_debug(
            "Progress window creation raised an exception; using status "
            "line fallback.",
            force=True,
        )
    return state


def _update_batch_progress(state, progress, status):
    progress = max(0, min(int(progress), state["maximum"]))
    state["progress"] = progress
    clean_status = " ".join(str(status).split())
    state["last_status"] = clean_status
    if state.get("open"):
        try:
            cmds.progressWindow(
                edit=True,
                progress=progress,
                status=clean_status,
            )
        except Exception:
            state["open"] = False
    _set_status(clean_status)

    target_count = state.get("target_count", 0)
    steps_per_target = max(1, state.get("steps_per_target", 1))
    target_index = max(0, (progress - 1) // steps_per_target)
    detailed = target_count <= 20
    periodic = target_index < 3 or (target_index + 1) % 10 == 0
    important = (
        "failed" in clean_status.lower()
        or "cancel" in clean_status.lower()
        or "correction" in clean_status.lower()
    )
    if detailed or important or (
            "Step 1/4" in clean_status and periodic):
        _batch_debug(clean_status, force=True)


def _batch_progress_cancelled(state):
    if not state or not state.get("open"):
        return False
    try:
        return bool(cmds.progressWindow(query=True, isCancelled=True))
    except Exception:
        return False


def _close_batch_progress(state):
    if not state or not state.get("owned"):
        return
    try:
        cmds.progressWindow(endProgress=True)
    except Exception:
        pass
    _batch_debug(
        "Closed progress window at {0}/{1}: {2}".format(
            state.get("progress", 0),
            state.get("maximum", 0),
            state.get("last_status", ""),
        ),
        force=True,
    )
    state["open"] = False
    state["owned"] = False


def _close_stale_tool_progress():
    """Close only a leftover progress window created by this tool."""
    try:
        title = cmds.progressWindow(query=True, title=True) or ""
    except Exception:
        return False
    if not str(title).startswith(TOOL_NAME):
        return False
    try:
        cmds.progressWindow(endProgress=True)
        print(
            "[{0} v{1} DEBUG] Closed stale tool progress window.".format(
                TOOL_NAME,
                VERSION,
            )
        )
        return True
    except Exception:
        return False


def _plan_summary(plan):
    label = _curve_label(plan["curve"])
    if plan.get("selection_label"):
        label += " [{0}]".format(plan["selection_label"])
    return {
        "curve": plan["curve"],
        "label": label,
        "anchor_times": [
            float(plan["times"][index])
            for index in sorted(plan["kept"])
        ],
        "original_key_count": plan["original_key_count"],
        "reduced_key_count": len(plan["kept"]),
        "maximum_error": plan.get(
            "actual_maximum_error",
            plan["maximum_error"],
        ),
        "verified": bool(plan.get("verified")),
        "tolerance_reached": bool(plan.get("tolerance_reached")),
        "verification_corrections": int(
            plan.get("verification_corrections", 0)
        ),
        "tangent_mode": plan.get("tangent_mode", "fitted fixed"),
        "fixed_tangent_error": plan.get("fixed_tangent_error", ""),
    }


def _restore_batch_curve(curve, safety_state, buffer_created):
    restoration_errors = []
    if safety_state:
        try:
            _restore_curve_state(curve, safety_state)
            return True, ""
        except Exception as exc:
            restoration_errors.append(str(exc))
            traceback.print_exc()

    if buffer_created:
        try:
            if _swap_buffer(curve):
                return True, ""
        except Exception as exc:
            restoration_errors.append(str(exc))
            traceback.print_exc()

    return False, "; ".join(restoration_errors) or "no safety snapshot"


def _append_failure_lines(lines, failures):
    if not failures:
        return
    lines.append("SKIPPED / FAILED ({0})".format(len(failures)))
    for curve, reason in failures:
        lines.append("  {0}: {1}".format(curve, reason))


def reduce_selected_curves(*_unused):
    if PREVIEW.get("active"):
        cmds.warning(
            "{0}: Apply or cancel the active preview first.".format(TOOL_NAME)
        )
        return
    _update_target_summary()
    _batch_debug_reset("curve reduction")
    original_selection = None
    original_time = None
    auto_key_state = None
    refresh_suspended = False
    undo_open = False
    progress = None

    try:
        settings = _read_settings()
        _save_settings(settings)
        _batch_debug(
            "Settings: error={0:g} ({1}), sampleStep={2:g}, "
            "preserveExtrema={3}, maximumKeys={4}, buffer={5}, "
            "additiveLayer={6}".format(
                settings["maximum_error"],
                settings["error_mode"],
                settings["sample_step"],
                settings["preserve_extrema"],
                settings["maximum_keys"],
                settings["create_buffer"],
                settings["create_additive_layer"],
            ),
            force=True,
        )
        original_selection = cmds.ls(selection=True, long=True) or []
        original_time = cmds.currentTime(query=True)
        auto_key_state = cmds.autoKeyframe(query=True, state=True)

        _set_status("Collecting target curves...")
        cmds.refresh(force=True)
        work_items, skipped, source = _collect_action_work_items()
        _batch_debug(
            "Discovered {0} target(s) from {1}; {2} initially skipped."
            .format(len(work_items), source, len(skipped)),
            force=True,
        )
        additive_steps = 1 if settings["create_additive_layer"] else 0
        progress = _open_batch_progress(
            TOOL_NAME + " - Reduce",
            len(work_items),
            BATCH_STEPS_PER_CURVE + additive_steps,
        )

        additive_captures = {}
        additive_capture_failures = []
        if settings["create_additive_layer"]:
            for capture_index, work_item in enumerate(work_items):
                if _batch_progress_cancelled(progress):
                    _set_results([
                        "CANCELLED: no curves were changed and no layer was "
                        "created."
                    ])
                    _set_status(
                        "Cancelled safely while sampling additive output."
                    )
                    return
                capture_label = _progress_work_item_label(work_item)
                _update_batch_progress(
                    progress,
                    capture_index + 1,
                    "Sampling original for additive layer [{0}/{1}] - {2}"
                    .format(
                        capture_index + 1,
                        len(work_items),
                        capture_label,
                    ),
                )
                try:
                    additive_captures[capture_index] = (
                        _capture_additive_work_item(
                            work_item,
                            settings["sample_step"],
                        )
                    )
                except Exception as exc:
                    additive_capture_failures.append((
                        capture_label,
                        str(exc),
                    ))
                    _batch_debug(
                        "Additive capture unavailable for {0}: {1}".format(
                            capture_label,
                            exc,
                        ),
                        force=True,
                    )

        cmds.undoInfo(openChunk=True, chunkName=TOOL_NAME)
        undo_open = True
        cmds.autoKeyframe(state=False)
        cmds.refresh(suspend=True)
        refresh_suspended = True
        _batch_debug(
            "Undo chunk open; Auto Key disabled; viewport refresh suspended.",
            force=True,
        )

        results = []
        failures = list(skipped)
        buffered_curves = []
        buffer_failures = []
        buffer_attempted = set()
        cancelled = False
        completed_before_cancel = 0

        for index, work_item in enumerate(work_items):
            curve = work_item["curve"]
            base_progress = (
                len(work_items) * additive_steps
                + index * BATCH_STEPS_PER_CURVE
            )
            if _batch_progress_cancelled(progress):
                cancelled = True
                completed_before_cancel = index
                break

            curve_label = _progress_work_item_label(work_item)
            _update_batch_progress(
                progress,
                base_progress + 1,
                "[{0}/{1}] Step 1/4 - Analysing {2}".format(
                    index + 1,
                    len(work_items),
                    curve_label,
                ),
            )

            plan = None
            safety_state = None
            buffer_created = False
            curve_modified = False
            try:
                plan = _make_work_item_plan(work_item, settings)
            except Exception as exc:
                failures.append((
                    curve_label,
                    "analysis failed: {0}".format(exc),
                ))
                _update_batch_progress(
                    progress,
                    base_progress + BATCH_STEPS_PER_CURVE,
                    "[{0}/{1}] Skipped - analysis failed".format(
                        index + 1,
                        len(work_items),
                    ),
                )
                continue

            try:
                if _batch_progress_cancelled(progress):
                    raise _BatchCancelled("Reduction cancelled by user.")
                _update_batch_progress(
                    progress,
                    base_progress + 2,
                    "[{0}/{1}] Step 2/4 - Saving safety snapshot".format(
                        index + 1,
                        len(work_items),
                    ),
                )
                safety_state = _capture_curve_state(curve)
                _attach_partial_boundary_state(plan, safety_state)

                if _batch_progress_cancelled(progress):
                    raise _BatchCancelled("Reduction cancelled by user.")
                _update_batch_progress(
                    progress,
                    base_progress + 3,
                    "[{0}/{1}] Step 3/4 - {2}".format(
                        index + 1,
                        len(work_items),
                        (
                            "Preparing curve"
                            if not settings["create_buffer"]
                            else (
                                "Using saved original buffer"
                                if curve in buffer_attempted
                                else "Creating original buffer"
                            )
                        ),
                    ),
                )
                if (settings["create_buffer"]
                        and curve not in buffer_attempted):
                    buffer_attempted.add(curve)
                    buffer_created = _create_buffer(curve)
                    if buffer_created:
                        buffered_curves.append(curve)
                    else:
                        buffer_failures.append(curve)

                if _batch_progress_cancelled(progress):
                    raise _BatchCancelled("Reduction cancelled by user.")
                _update_batch_progress(
                    progress,
                    base_progress + 4,
                    "[{0}/{1}] Step 4/4 - Fitting and verifying {2}".format(
                        index + 1,
                        len(work_items),
                        curve_label,
                    ),
                )

                def apply_status(stage, correction_count):
                    correction = ""
                    if correction_count:
                        correction = " (correction {0})".format(
                            correction_count
                        )
                    _update_batch_progress(
                        progress,
                        base_progress + 4,
                        "[{0}/{1}] Step 4/4 - {2}{3}".format(
                            index + 1,
                            len(work_items),
                            stage,
                            correction,
                        ),
                    )

                curve_modified = True
                _apply_plan(
                    plan,
                    settings["maximum_keys"],
                    status_callback=apply_status,
                    cancel_callback=lambda: _batch_progress_cancelled(
                        progress
                    ),
                )
                summary = _plan_summary(plan)
                summary["work_index"] = index
                results.append(summary)
                _update_batch_progress(
                    progress,
                    base_progress + BATCH_STEPS_PER_CURVE,
                    "[{0}/{1}] Complete - {2} -> {3} keys".format(
                        index + 1,
                        len(work_items),
                        plan["original_key_count"],
                        len(plan["kept"]),
                    ),
                )
            except _BatchCancelled:
                if curve_modified:
                    restored, restore_error = _restore_batch_curve(
                        curve,
                        safety_state,
                        buffer_created,
                    )
                else:
                    restored, restore_error = True, ""
                if not restored:
                    raise RuntimeError(
                        "{0} was cancelled during editing and could not be "
                        "restored: {1}. Use Maya Undo immediately.".format(
                            curve,
                            restore_error,
                        )
                    )
                cancelled = True
                completed_before_cancel = index
                break
            except Exception as exc:
                if curve_modified:
                    restored, restore_error = _restore_batch_curve(
                        curve,
                        safety_state,
                        buffer_created,
                    )
                else:
                    restored, restore_error = True, ""
                if not restored:
                    traceback.print_exc()
                    raise RuntimeError(
                        "{0} failed and its safety restoration also failed: "
                        "{1}. Use Maya Undo immediately.".format(
                            curve,
                            restore_error,
                        )
                    )
                failure_text = str(exc)
                if curve_modified and restored:
                    failure_text += " (original restored)"
                failures.append((curve_label, failure_text))
                _batch_debug(
                    "[{0}/{1}] {2} failed: {3}".format(
                        index + 1,
                        len(work_items),
                        curve_label,
                        failure_text,
                    ),
                    force=True,
                )
                _update_batch_progress(
                    progress,
                    base_progress + BATCH_STEPS_PER_CURVE,
                    "[{0}/{1}] Failed safely - original restored".format(
                        index + 1,
                        len(work_items),
                    ),
                )
            finally:
                # Large sampled arrays are intentionally kept for only one
                # curve at a time.
                plan = None
                safety_state = None

        if not results and not cancelled:
            failure_lines = []
            _append_failure_lines(failure_lines, failures)
            _set_results(failure_lines)
            raise RuntimeError(
                "Every target failed; no reduction was applied. "
                "See the results list for details."
            )

        additive_layer = None
        additive_key_count = 0
        additive_layer_error = ""
        additive_links = []
        additive_link_failures = []
        if settings["create_additive_layer"] and results:
            successful_samples = _merge_additive_captures(
                additive_captures,
                results,
            )
            if successful_samples:
                try:
                    link_specs, link_spec_failures = (
                        _build_additive_link_specs(
                            additive_captures,
                            results,
                        )
                    )
                    additive_link_failures.extend(link_spec_failures)
                    (
                        additive_layer,
                        additive_key_count,
                        additive_links,
                        link_configuration_failures,
                    ) = (
                        _create_additive_detail_layer(
                            successful_samples,
                            link_specs=link_specs,
                            progress=progress,
                        )
                    )
                    additive_link_failures.extend(
                        link_configuration_failures
                    )
                    _refresh_auto_sync_curve_cache()
                    _batch_debug(
                        "Created muted additive layer {0} with {1} keys on "
                        "{2} channel(s); {3} linked timing channel(s)."
                        .format(
                            additive_layer,
                            additive_key_count,
                            len(successful_samples),
                            len(additive_links),
                        ),
                        force=True,
                    )
                except Exception as exc:
                    additive_layer_error = str(exc)
                    _batch_debug(
                        "Additive layer creation failed safely: {0}".format(
                            exc
                        ),
                        force=True,
                    )
            else:
                additive_layer_error = (
                    "No successfully reduced target had a supported final "
                    "transform attribute."
                )

        lines = []
        original_total = sum(
            item["original_key_count"] for item in results
        )
        reduced_total = sum(
            item["reduced_key_count"] for item in results
        )
        for summary in results:
            verification = (
                "verified" if summary["verified"] else "limit reached"
            )
            correction_text = ""
            if summary["verification_corrections"]:
                correction_text = ", {0} Maya correction(s)".format(
                    summary["verification_corrections"]
                )
            tangent_text = ""
            if summary["tangent_mode"] == "maya spline fallback":
                tangent_text = ", spline tangent fallback"
            lines.append(
                "{0}: {1} -> {2} keys, error {3:.6g} ({4}{5}{6})".format(
                    summary["label"],
                    summary["original_key_count"],
                    summary["reduced_key_count"],
                    summary["maximum_error"],
                    verification,
                    correction_text,
                    tangent_text,
                )
            )
        if buffered_curves:
            lines.insert(
                0,
                "BUFFER  Original snapshot visible for {0} curve(s).".format(
                    len(buffered_curves)
                ),
            )
        if additive_layer:
            lines.insert(
                0,
                "ADDITIVE  {0}: {1} baked keys, muted by default. Unmute "
                "or adjust its weight to restore original detail. Previous "
                "layer selection restored.".format(
                    additive_layer,
                    additive_key_count,
                ),
            )
            if additive_links:
                lines.insert(
                    1,
                    "LINKED TIMING  {0} channel(s): base-aligned anchors "
                    "are regular keys; baked detail keys are breakdowns."
                    .format(len(additive_links)),
                )
        if additive_layer_error:
            lines.append(
                "ADDITIVE LAYER UNAVAILABLE: {0}".format(
                    additive_layer_error
                )
            )
        if additive_capture_failures:
            lines.append(
                "ADDITIVE CHANNELS SKIPPED ({0})".format(
                    len(additive_capture_failures)
                )
            )
            lines.extend(
                "  {0}: {1}".format(label, reason)
                for label, reason in additive_capture_failures
            )
        if additive_link_failures:
            lines.append(
                "LINKED TIMING SKIPPED ({0})".format(
                    len(additive_link_failures)
                )
            )
            lines.extend(
                "  {0}: {1}".format(label, reason)
                for label, reason in additive_link_failures
            )
        if buffer_failures:
            lines.append(
                "BUFFER UNAVAILABLE ({0})".format(len(buffer_failures))
            )
            lines.extend(
                "  " + curve for curve in buffer_failures
            )
        _append_failure_lines(lines, failures)
        if cancelled:
            lines.insert(
                0,
                "CANCELLED SAFELY: {0} target(s) reduced; remaining targets "
                "were untouched. Undo once to revert completed work."
                .format(len(results)),
            )
        _set_results(lines)
        if cancelled:
            _set_status(
                "Cancelled safely after {0}/{1} targets: {2} reduced. "
                "{3} skipped/failed. Remaining targets untouched; Undo once "
                "to revert.".format(
                    completed_before_cancel,
                    len(work_items),
                    len(results),
                    len(failures),
                )
            )
        else:
            _set_status(
                "Done: {0} succeeded, {1} skipped/failed, {2} -> {3} keys. "
                "Undo once to revert.".format(
                    len(results),
                    len(failures),
                    original_total,
                    reduced_total,
                )
            )
        if buffered_curves:
            _show_buffer_curves(buffered_curves)

    except Exception as exc:
        _batch_debug(
            "Reduction failed: {0}".format(exc),
            force=True,
        )
        traceback.print_exc()
        _set_status(
            "{0} See Script Editor DEBUG lines.".format(exc),
            warning=True,
        )
        cmds.warning("{0}: {1}".format(TOOL_NAME, exc))
    finally:
        _close_batch_progress(progress)
        if refresh_suspended:
            cmds.refresh(suspend=False)

        if undo_open:
            cmds.undoInfo(closeChunk=True)
            undo_open = False

        if original_time is not None:
            cmds.currentTime(original_time, edit=True)
        if auto_key_state is not None:
            cmds.autoKeyframe(state=auto_key_state)

        if original_selection is not None:
            existing = [
                node for node in original_selection
                if cmds.objExists(node)
            ]
            if existing:
                cmds.select(existing, replace=True)
            else:
                cmds.select(clear=True)

        cmds.refresh(force=True)
        _batch_debug(
            "Cleanup complete; Maya state restored.",
            force=True,
        )


def _set_preview_ui_active(active):
    states = (
        ("preview_start", not active),
        ("preview_apply", active),
        ("preview_cancel", active),
        ("reduce_button", not active),
        ("swap_button", not active),
        ("create_buffer", not active),
        ("create_additive_layer", not active),
        ("sync_linked_timing", not active),
        ("auto_sync_linked_timing", not active),
    )
    for name, enabled in states:
        control = UI.get(name)
        if control and cmds.control(control, exists=True):
            cmds.control(control, edit=True, enable=enabled)


def _dispose_preview_timer():
    """Stop and disconnect the preview timer so no stale update can run."""
    timer = PREVIEW.get("timer")
    if timer is not None:
        try:
            timer.stop()
        except (RuntimeError, AttributeError):
            pass
        try:
            timer.timeout.disconnect()
        except (RuntimeError, TypeError, AttributeError):
            pass
        try:
            timer.deleteLater()
        except (RuntimeError, AttributeError):
            pass
    PREVIEW["timer"] = None
    PREVIEW["requested"] = False
    PREVIEW["pending_verify"] = False


def _ensure_preview_timer():
    timer = PREVIEW.get("timer")
    if timer is None:
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(_preview_timer_fired)
        PREVIEW["timer"] = timer
    return timer


def _schedule_preview_update(verify, delay_ms):
    """
    Queue one preview update.

    Drag events are throttled instead of executed immediately. Every drag
    callback merely records that newer settings exist, so Maya never builds a
    backlog of full curve reductions. A release replaces the pending fast
    update with one fully verified update.
    """
    if not PREVIEW.get("active"):
        return

    PREVIEW["requested"] = True
    PREVIEW["pending_verify"] = bool(
        PREVIEW.get("pending_verify") or verify
    )
    timer = _ensure_preview_timer()

    if verify:
        timer.stop()
        timer.start(0)
    elif not timer.isActive():
        timer.start(max(0, int(delay_ms)))


def _preview_timer_fired():
    if not PREVIEW.get("active"):
        return
    verify = bool(PREVIEW.get("pending_verify"))
    PREVIEW["requested"] = False
    PREVIEW["pending_verify"] = False
    _run_preview_update(verify=verify)


def _clear_preview_memory():
    _dispose_preview_timer()
    PREVIEW.update({
        "active": False,
        "busy": False,
        "requested": False,
        "curves": [],
        "items": [],
        "states": {},
        "source": "",
        "apply_curves": [],
        "apply_items": [],
        "buffer_curves": [],
        "buffer_failures": [],
        "timer": None,
        "pending_verify": False,
        "slope_scales": {},
        "tangent_modes": {},
    })


def _restore_preview_curves():
    failures = []
    for curve in PREVIEW.get("curves", []):
        state = PREVIEW.get("states", {}).get(curve)
        if not state:
            failures.append((curve, "missing preview snapshot"))
            continue
        try:
            _restore_curve_state(curve, state)
        except Exception as exc:
            failures.append((curve, str(exc)))
    if failures:
        raise RuntimeError(
            "Could not restore preview baseline: {0}".format(
                "; ".join(
                    "{0} ({1})".format(curve, reason)
                    for curve, reason in failures
                )
            )
        )


def _restore_preview_without_undo():
    undo_state = bool(cmds.undoInfo(query=True, state=True))
    auto_key_state = cmds.autoKeyframe(query=True, state=True)
    current_time = cmds.currentTime(query=True)
    refresh_suspended = False
    try:
        if undo_state:
            cmds.undoInfo(stateWithoutFlush=False)
        cmds.autoKeyframe(state=False)
        cmds.refresh(suspend=True)
        refresh_suspended = True
        _restore_preview_curves()
    finally:
        if refresh_suspended:
            cmds.refresh(suspend=False)
        cmds.currentTime(current_time, edit=True)
        cmds.autoKeyframe(state=auto_key_state)
        if undo_state:
            cmds.undoInfo(stateWithoutFlush=True)
        cmds.refresh(force=True)


def _preview_lines(plans, verified):
    lines = []
    original_total = 0
    reduced_total = 0
    for plan in plans:
        original_total += plan["original_key_count"]
        reduced_total += len(plan["kept"])
        if verified:
            prefix = "PREVIEW"
            error_label = "verified error"
            error_value = plan.get(
                "actual_maximum_error",
                plan["maximum_error"],
            )
        else:
            prefix = "FAST PREVIEW"
            error_label = "predicted error"
            error_value = plan["maximum_error"]
        label = _curve_label(plan["curve"])
        if plan.get("selection_label"):
            label += " [{0}]".format(plan["selection_label"])
        tangent_note = (
            ", spline tangent fallback"
            if plan.get("tangent_mode") == "maya spline fallback"
            else ""
        )
        lines.append(
            "{0}  {1}: {2} -> {3} keys, {4} {5:.6g}{6}".format(
                prefix,
                label,
                plan["original_key_count"],
                len(plan["kept"]),
                error_label,
                error_value,
                tangent_note,
            )
        )
    return lines, original_total, reduced_total


def _run_preview_update(verify=True):
    if not PREVIEW.get("active"):
        return
    if PREVIEW.get("busy"):
        PREVIEW["requested"] = True
        PREVIEW["pending_verify"] = bool(
            PREVIEW.get("pending_verify") or verify
        )
        return

    PREVIEW["busy"] = True
    original_selection = cmds.ls(selection=True, long=True) or []
    original_time = cmds.currentTime(query=True)
    auto_key_state = cmds.autoKeyframe(query=True, state=True)
    undo_state = bool(cmds.undoInfo(query=True, state=True))
    refresh_suspended = False
    result_lines = []
    status_message = ""
    warning = False

    try:
        settings = _read_settings()
        _save_settings(settings)
        if undo_state:
            cmds.undoInfo(stateWithoutFlush=False)
        cmds.autoKeyframe(state=False)
        cmds.refresh(suspend=True)
        refresh_suspended = True

        _restore_preview_curves()
        plans = []
        preview_items = PREVIEW.get("items") or [
            {
                "curve": curve,
                "selected_times": None,
                "selection_label": "",
            }
            for curve in PREVIEW["curves"]
        ]
        for work_item in preview_items:
            curve = work_item["curve"]
            plan = _make_work_item_plan(work_item, settings)
            _attach_partial_boundary_state(
                plan,
                PREVIEW["states"][curve],
            )
            slope_scale = PREVIEW.get("slope_scales", {}).get(curve)
            if slope_scale is not None:
                plan["maya_slope_scale"] = slope_scale
            tangent_mode = PREVIEW.get("tangent_modes", {}).get(curve)
            if tangent_mode:
                plan["tangent_mode"] = tangent_mode

            if verify:
                _apply_plan(plan, settings["maximum_keys"])
            else:
                _write_fit_to_curve(plan)
                plan["verified"] = False
                plan["verification_corrections"] = 0

            if "maya_slope_scale" in plan:
                PREVIEW["slope_scales"][curve] = plan["maya_slope_scale"]
            if plan.get("tangent_mode"):
                PREVIEW["tangent_modes"][curve] = plan["tangent_mode"]
            plans.append(plan)

        result_lines, original_total, reduced_total = _preview_lines(
            plans,
            verified=verify,
        )
        apply_item_count = len(
            PREVIEW.get("apply_items")
            or PREVIEW.get("apply_curves", [])
        )
        if apply_item_count > len(plans):
            result_lines.insert(
                0,
                "SAMPLED PREVIEW  Showing {0} of {1} targets. Apply Preview "
                "will batch-reduce all {1} targets.".format(
                    len(plans),
                    apply_item_count,
                ),
            )
        if PREVIEW.get("buffer_curves"):
            result_lines.insert(
                0,
                "BUFFER  Original snapshot visible for {0} curve(s).".format(
                    len(PREVIEW["buffer_curves"])
                ),
            )
        if PREVIEW.get("buffer_failures"):
            result_lines.append(
                "Buffer unavailable: " + ", ".join(
                    PREVIEW["buffer_failures"]
                )
            )
        if verify:
            status_message = (
                "LIVE PREVIEW VERIFIED: {0} target(s), {1} -> {2} keys. "
                "Drag a slider, then Apply or Cancel.".format(
                    len(plans), original_total, reduced_total
                )
            )
        else:
            status_message = (
                "LIVE PREVIEW (fast): {0} target(s), {1} -> {2} keys. "
                "Release the slider to verify.".format(
                    len(plans), original_total, reduced_total
                )
            )
    except Exception as exc:
        traceback.print_exc()
        warning = True
        status_message = "Preview failed: {0}".format(exc)
        try:
            _restore_preview_curves()
        except Exception as restore_exc:
            traceback.print_exc()
            status_message += " Restoration also failed: {0}".format(
                restore_exc
            )
        result_lines = [status_message]
    finally:
        if refresh_suspended:
            cmds.refresh(suspend=False)
        cmds.currentTime(original_time, edit=True)
        cmds.autoKeyframe(state=auto_key_state)

        existing = [
            node for node in original_selection
            if cmds.objExists(node)
        ]
        if existing:
            cmds.select(existing, replace=True)
        else:
            cmds.select(clear=True)

        if undo_state:
            cmds.undoInfo(stateWithoutFlush=True)
        if PREVIEW.get("buffer_curves"):
            _show_buffer_curves(PREVIEW["buffer_curves"])
        cmds.refresh(force=True)
        PREVIEW["busy"] = False

    _set_results(result_lines)
    _set_status(status_message, warning=warning)

    if PREVIEW.get("active") and PREVIEW.get("requested"):
        delay = (
            0 if PREVIEW.get("pending_verify")
            else PREVIEW_UPDATE_INTERVAL_MS
        )
        timer = _ensure_preview_timer()
        if not timer.isActive():
            timer.start(delay)


def _preview_slider_dragged(*_unused):
    _schedule_preview_update(
        verify=False,
        delay_ms=PREVIEW_UPDATE_INTERVAL_MS,
    )


def _preview_slider_released(*_unused):
    _schedule_preview_update(verify=True, delay_ms=0)


def start_preview(*_unused):
    if PREVIEW.get("active"):
        return
    _update_target_summary()
    try:
        settings = _read_settings()
        _save_settings(settings)
        work_items, skipped, source = _collect_action_work_items()
        apply_items = list(work_items)
        if len(work_items) > PREVIEW_WARNING_CURVE_COUNT:
            first_count = min(PREVIEW_SAMPLE_CURVE_COUNT, len(work_items))
            choice = cmds.confirmDialog(
                title=TOOL_NAME + " - Large Preview",
                message=(
                    "{0} curve targets are selected. Live-previewing all "
                    "of them "
                    "may make Maya unresponsive.\n\nPreviewing the first {1} "
                    "is safer. Apply Preview will still process all {0} "
                    "targets with the cancellable batch progress window."
                ).format(len(work_items), first_count),
                button=[
                    "Preview first {0}".format(first_count),
                    "Preview all",
                    "Cancel",
                ],
                defaultButton="Preview first {0}".format(first_count),
                cancelButton="Cancel",
                dismissString="Cancel",
            )
            if choice == "Cancel":
                _set_status("Large live preview was not started.")
                return
            if choice != "Preview all":
                work_items = work_items[:first_count]

        curves = []
        for work_item in work_items:
            if work_item["curve"] not in curves:
                curves.append(work_item["curve"])
        apply_curves = []
        for work_item in apply_items:
            if work_item["curve"] not in apply_curves:
                apply_curves.append(work_item["curve"])

        states = {}
        for curve in curves:
            states[curve] = _capture_curve_state(curve)

        buffer_curves = []
        buffer_failures = []
        if settings["create_buffer"]:
            undo_state = bool(cmds.undoInfo(query=True, state=True))
            try:
                if undo_state:
                    cmds.undoInfo(stateWithoutFlush=False)
                for curve in curves:
                    if _create_buffer(curve):
                        buffer_curves.append(curve)
                    else:
                        buffer_failures.append(curve)
            finally:
                if undo_state:
                    cmds.undoInfo(stateWithoutFlush=True)

        _dispose_preview_timer()
        PREVIEW.update({
            "active": True,
            "busy": False,
            "requested": False,
            "curves": list(curves),
            "items": list(work_items),
            "states": states,
            "source": source,
            "apply_curves": apply_curves,
            "apply_items": apply_items,
            "buffer_curves": buffer_curves,
            "buffer_failures": buffer_failures,
            "timer": None,
            "pending_verify": False,
            "slope_scales": {},
            "tangent_modes": {},
        })
        _ensure_preview_timer()
        if buffer_curves:
            _show_buffer_curves(buffer_curves)
        _set_preview_ui_active(True)
        _update_target_summary()
        if len(work_items) < len(apply_items):
            _set_status(
                "Starting sampled preview: {0} of {1} targets...".format(
                    len(work_items),
                    len(apply_items),
                )
            )
        else:
            _set_status(
                "Starting live preview for {0} target(s)...".format(
                    len(work_items)
                )
            )
        _run_preview_update(verify=True)
    except Exception as exc:
        traceback.print_exc()
        _clear_preview_memory()
        _set_preview_ui_active(False)
        _set_status(str(exc), warning=True)
        cmds.warning("{0}: {1}".format(TOOL_NAME, exc))


def _cancel_preview(update_ui=True):
    if not PREVIEW.get("active"):
        return True
    _dispose_preview_timer()
    PREVIEW["active"] = False
    try:
        _restore_preview_without_undo()
    except Exception as exc:
        PREVIEW["active"] = True
        traceback.print_exc()
        _set_status(str(exc), warning=True)
        cmds.warning("{0}: {1}".format(TOOL_NAME, exc))
        return False

    _clear_preview_memory()
    if update_ui:
        _set_preview_ui_active(False)
        _set_results(["Preview cancelled; original curves restored."])
        _set_status("Preview cancelled; original curves restored.")
        _update_target_summary()
    return True


def cancel_preview(*_unused):
    _cancel_preview(update_ui=True)


def apply_preview(*_unused):
    if not PREVIEW.get("active"):
        return
    settings = _read_settings()
    items = list(PREVIEW.get("apply_items", []))
    curves = list(PREVIEW.get("apply_curves") or PREVIEW.get("curves", []))
    source = PREVIEW.get("source") or "preview session"
    if not _cancel_preview(update_ui=False):
        return

    _set_preview_ui_active(False)
    TARGET_OVERRIDE["items"] = items or None
    TARGET_OVERRIDE["curves"] = None if items else curves
    TARGET_OVERRIDE["source"] = source + " (preview)"
    try:
        _save_settings(settings)
        reduce_selected_curves()
    finally:
        TARGET_OVERRIDE["items"] = None
        TARGET_OVERRIDE["curves"] = None
        TARGET_OVERRIDE["source"] = ""


def _preview_window_closed(*_unused):
    try:
        _dispose_link_auto_sync()
        if PREVIEW.get("active"):
            _cancel_preview(update_ui=False)
    finally:
        _dispose_target_watch_timer()
        _dispose_preview_timer()
        _kill_tool_script_jobs()


def swap_with_buffer(*_unused):
    if PREVIEW.get("active"):
        cmds.warning(
            "{0}: Apply or cancel the active preview first.".format(TOOL_NAME)
        )
        return
    curves, skipped, source = collect_target_curves()
    if not curves:
        cmds.warning(
            "{0}: Select reduced curves or their animated controls.".format(
                TOOL_NAME
            )
        )
        return

    swapped = []
    failed = list(skipped)
    cmds.undoInfo(openChunk=True, chunkName=TOOL_NAME + " Swap Buffer")
    try:
        for curve in curves:
            try:
                if not _swap_buffer(curve):
                    failed.append((curve, "no buffer curve"))
                    continue
                swapped.append(curve)
            except Exception as exc:
                failed.append((curve, str(exc)))
    finally:
        cmds.undoInfo(closeChunk=True)
        cmds.refresh(force=True)

    lines = ["Swapped current/buffer: " + curve for curve in swapped]
    if failed:
        lines.append("Not swapped: " + "; ".join(
            "{0} ({1})".format(item[0], item[1])
            for item in failed
        ))
    _set_results(lines)
    _set_status(
        "Swapped {0} buffer curve(s) from {1}.".format(len(swapped), source),
        warning=not bool(swapped),
    )


def _delete_existing_ui():
    _close_stale_tool_progress()
    _dispose_link_auto_sync()
    _dispose_target_watch_timer()
    if PREVIEW.get("active") and not _cancel_preview(update_ui=False):
        raise RuntimeError(
            "The existing preview could not be restored; its window was "
            "left open for safety."
        )
    _dispose_preview_timer()
    _kill_tool_script_jobs()
    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME)
    if cmds.windowPref(WINDOW_NAME, exists=True):
        cmds.windowPref(WINDOW_NAME, remove=True)


def show_animator_curve_reducer():
    _delete_existing_ui()
    UI.clear()

    window = cmds.window(
        WINDOW_NAME,
        title="{0}  v{1}".format(TOOL_NAME, VERSION),
        toolbox=True,
        sizeable=True,
        widthHeight=(590, 560),
        closeCommand=_preview_window_closed,
    )
    UI["scroll"] = cmds.scrollLayout(
        childResizable=True,
        horizontalScrollBarThickness=0,
        verticalScrollBarThickness=7,
    )
    root = cmds.columnLayout(
        adjustableColumn=True,
        rowSpacing=UI_DENSITY,
        columnAttach=("both", UI_DENSITY),
    )

    UI["process_frame"] = cmds.frameLayout(
        label="CURVE REDUCTION",
        collapsable=False,
        marginWidth=UI_DENSITY,
        marginHeight=UI_DENSITY,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=8)
    UI["target_summary"] = cmds.text(
        label=(
            "TARGETS  /  Select Graph Editor keys or channels, or select "
            "animated controls."
        ),
        align="left",
        wordWrap=True,
        height=34,
    )
    UI["target_indicator_flow"] = cmds.flowLayout(
        columnSpacing=5,
        generalSpacing=4,
        wrap=True,
        height=28,
    )
    cmds.setParent("..")

    cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(335, 205),
        columnAttach=[(1, "both", 0), (2, "both", 5)],
    )
    UI["reduce_button"] = cmds.button(
        label="Reduce Curves",
        height=40,
        command=reduce_selected_curves,
        annotation=(
            "Selected key blocks first; otherwise selected Graph Editor "
            "channels; otherwise every animated channel on the selected "
            "controls. Press Esc in the progress window to stop safely."
        ),
    )
    UI["sync_linked_timing"] = cmds.button(
        label="Sync Linked Detail Timing",
        height=40,
        command=sync_linked_detail_timing,
        annotation=(
            "Synchronize additive anchors and breakdown timing with the "
            "sparse base animation."
        ),
    )
    cmds.setParent("..")

    # Baking is deliberately opt-in for every newly opened tool window.
    cmds.optionVar(intValue=(OPTION_PREFIX + "createAdditiveLayer", 0))
    UI["create_additive_layer"] = cmds.checkBoxGrp(
        label="Bake detail to additive layer",
        numberOfCheckBoxes=1,
        value1=False,
        columnWidth2=(245, 150),
        changeCommand1=_settings_changed,
        annotation=(
            "After reduction, bake the original motion onto a new muted "
            "additive layer. This is off by default."
        ),
    )
    UI["auto_sync_linked_timing"] = cmds.checkBoxGrp(
        label="Auto-sync linked timing while open",
        numberOfCheckBoxes=1,
        value1=bool(_option_int(
            "autoSyncLinkedTimingV2",
            DEFAULT_SETTINGS["auto_sync_linked_timing"],
        )),
        columnWidth2=(245, 150),
        changeCommand1=_auto_sync_toggled,
        annotation=(
            "Watch linked sparse base curves and synchronize their additive "
            "detail. Callbacks and timers are removed when the window closes."
        ),
    )

    cmds.rowLayout(
        numberOfColumns=3,
        adjustableColumn=1,
        columnWidth3=(230, 155, 155),
        columnAttach=[
            (1, "both", 0),
            (2, "both", 5),
            (3, "both", 5),
        ],
    )
    UI["preview_start"] = cmds.button(
        label="Start Live Preview",
        height=34,
        command=start_preview,
        annotation=(
            "Snapshot the current targets and update a reversible preview "
            "as fitting settings change."
        ),
    )
    UI["preview_apply"] = cmds.button(
        label="Apply Preview",
        height=34,
        enable=False,
        command=apply_preview,
    )
    UI["preview_cancel"] = cmds.button(
        label="Cancel Preview",
        height=34,
        enable=False,
        command=cancel_preview,
    )
    cmds.setParent("..")

    UI["status"] = cmds.text(
        label="Ready. Select targets, preview if needed, then reduce.",
        align="left",
        wordWrap=True,
        height=32,
    )
    cmds.setParent("..")
    cmds.setParent("..")

    UI["advanced_frame"] = cmds.frameLayout(
        label="ADVANCED SETTINGS",
        collapsable=True,
        collapse=True,
        marginWidth=UI_DENSITY,
        marginHeight=UI_DENSITY,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=UI_DENSITY)

    cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(430, 110),
        columnAttach=[(1, "both", 0), (2, "both", 5)],
    )
    UI["preset"] = cmds.optionMenuGrp(
        label="Reduction preset",
        columnWidth2=(185, 230),
        adjustableColumn=2,
        changeCommand=_preset_selected,
        annotation="Light keeps more keys; Aggressive removes more keys.",
    )
    for preset_name in ("Light", "Balanced", "Aggressive", "Custom"):
        cmds.menuItem(label=preset_name)
    preset_option = OPTION_PREFIX + "preset"
    if cmds.optionVar(exists=preset_option):
        saved_preset = _option_string("preset", "Custom")
    else:
        legacy_options = (
            "errorMode", "maximumError", "sampleStep", "preserveExtrema",
            "extremaProminence", "extremaWindow", "maximumKeys",
        )
        has_legacy_settings = any(
            cmds.optionVar(exists=OPTION_PREFIX + name)
            for name in legacy_options
        )
        saved_preset = (
            "Custom" if has_legacy_settings else DEFAULT_SETTINGS["preset"]
        )
    if saved_preset not in ("Light", "Balanced", "Aggressive", "Custom"):
        saved_preset = "Custom"
    cmds.optionMenuGrp(UI["preset"], edit=True, value=saved_preset)
    UI["reset_button"] = cmds.button(
        label="Reset",
        height=28,
        command=reset_settings,
        annotation="Restore the Balanced defaults.",
    )
    cmds.setParent("..")

    maximum_error_value = _option_float(
        "maximumError", DEFAULT_SETTINGS["maximum_error"]
    )
    UI["maximum_error"] = cmds.floatSliderGrp(
        label="Maximum error",
        field=True,
        value=maximum_error_value,
        minValue=0.01,
        maxValue=max(5.0, maximum_error_value),
        fieldMinValue=0.000001,
        fieldMaxValue=max(100.0, maximum_error_value),
        sliderStep=0.01,
        fieldStep=0.01,
        precision=5,
        columnWidth3=(185, 78, 285),
        adjustableColumn=3,
        dragCommand=_settings_dragged,
        changeCommand=_settings_changed,
        annotation=(
            "For Percent mode, 0.5 means half of one percent of that curve's "
            "sampled value range."
        ),
    )
    UI["preserve_extrema"] = cmds.checkBoxGrp(
        label="Preserve high/low keys",
        numberOfCheckBoxes=1,
        value1=bool(_option_int(
            "preserveExtrema", DEFAULT_SETTINGS["preserve_extrema"]
        )),
        columnWidth2=(180, 150),
        changeCommand1=_settings_changed,
    )

    UI["error_mode"] = cmds.optionMenuGrp(
        label="Error mode",
        columnWidth2=(180, 340),
        adjustableColumn=2,
        changeCommand=_settings_changed,
    )
    cmds.menuItem(label="Percent of curve range")
    cmds.menuItem(label="Absolute value")
    saved_mode = _option_string(
        "errorMode", DEFAULT_SETTINGS["error_mode"]
    )
    if saved_mode not in ("Percent of curve range", "Absolute value"):
        saved_mode = DEFAULT_SETTINGS["error_mode"]
    cmds.optionMenuGrp(UI["error_mode"], edit=True, value=saved_mode)

    sample_step_value = _option_float(
        "sampleStep", DEFAULT_SETTINGS["sample_step"]
    )
    UI["sample_step"] = cmds.floatSliderGrp(
        label="Sample step (frames)",
        field=True,
        value=sample_step_value,
        minValue=0.1,
        maxValue=max(4.0, sample_step_value),
        fieldMinValue=0.01,
        fieldMaxValue=max(100.0, sample_step_value),
        sliderStep=0.1,
        fieldStep=0.1,
        precision=3,
        columnWidth3=(185, 78, 285),
        adjustableColumn=3,
        dragCommand=_settings_dragged,
        changeCommand=_settings_changed,
    )
    prominence_value = _option_float(
        "extremaProminence", DEFAULT_SETTINGS["extrema_prominence"]
    )
    UI["extrema_prominence"] = cmds.floatSliderGrp(
        label="Min high/low prominence %",
        field=True,
        value=prominence_value,
        minValue=0.0,
        maxValue=max(5.0, prominence_value),
        fieldMinValue=0.0,
        fieldMaxValue=max(100.0, prominence_value),
        sliderStep=0.01,
        fieldStep=0.01,
        precision=4,
        columnWidth3=(185, 78, 285),
        adjustableColumn=3,
        dragCommand=_settings_dragged,
        changeCommand=_settings_changed,
    )
    extrema_window_value = _option_float(
        "extremaWindow", DEFAULT_SETTINGS["extrema_window"]
    )
    UI["extrema_window"] = cmds.floatSliderGrp(
        label="High/low window (frames)",
        field=True,
        value=extrema_window_value,
        minValue=0.25,
        maxValue=max(20.0, extrema_window_value),
        fieldMinValue=0.01,
        fieldMaxValue=max(1000.0, extrema_window_value),
        sliderStep=0.25,
        fieldStep=0.25,
        precision=2,
        columnWidth3=(185, 78, 285),
        adjustableColumn=3,
        dragCommand=_settings_dragged,
        changeCommand=_settings_changed,
    )
    maximum_keys_value = _option_int(
        "maximumKeys", DEFAULT_SETTINGS["maximum_keys"]
    )
    UI["maximum_keys"] = cmds.intSliderGrp(
        label="Maximum keys per curve/block",
        field=True,
        value=maximum_keys_value,
        minValue=0,
        maxValue=max(100, maximum_keys_value),
        fieldMinValue=0,
        fieldMaxValue=max(100000, maximum_keys_value),
        step=1,
        columnWidth3=(185, 78, 285),
        adjustableColumn=3,
        dragCommand=_settings_dragged,
        changeCommand=_settings_changed,
    )
    UI["create_buffer"] = cmds.checkBoxGrp(
        label="Show original buffer overlay",
        numberOfCheckBoxes=1,
        value1=bool(_option_int(
            "createBuffer", DEFAULT_SETTINGS["create_buffer"]
        )),
        columnWidth2=(180, 150),
        changeCommand1=_preview_slider_released,
    )
    UI["swap_button"] = cmds.button(
        label="Toggle Original / Reduced",
        height=30,
        command=swap_with_buffer,
        annotation="Swap current curves with Maya's saved original buffer.",
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.setParent(root)
    cmds.showWindow(window)

    _apply_ui_style(window, [
        (UI["process_frame"], "panel"),
        (UI["target_summary"], "status"),
        (UI["advanced_frame"], "panel"),
        (UI["reduce_button"], "primary"),
        (UI["sync_linked_timing"], "quiet"),
        (UI["preview_start"], "preview"),
        (UI["preview_apply"], "primary"),
        (UI["preview_cancel"], "quiet"),
        (UI["reset_button"], "quiet"),
        (UI["swap_button"], "quiet"),
        (UI["status"], "muted"),
    ])
    _install_tool_script_jobs(window)
    _install_target_watch_timer()
    if cmds.checkBoxGrp(
            UI["auto_sync_linked_timing"], query=True, value1=True):
        if not _install_link_auto_sync():
            cmds.checkBoxGrp(
                UI["auto_sync_linked_timing"],
                edit=True,
                value1=False,
            )
    _update_target_summary()
    return window


if __name__ == "__main__":
    show_animator_curve_reducer()
