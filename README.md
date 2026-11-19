# Animator Curve Reducer for Maya

Animator Curve Reducer is a single-file Autodesk Maya Python tool for creating
sparse, animator-friendly animation curves from dense or baked keys.

## Features

- Reduces whole curves or selected Graph Editor key blocks.
- Preserves important high and low points and fits editable Maya tangents.
- Supports reversible live preview and original buffer-curve overlays.
- Can bake residual detail to a muted additive animation layer.
- Synchronizes linked additive detail when sparse base keys are retimed or
  deleted.
- Handles Graph Editor key, channel, or animated-control selection.
- Can target every curve on the anim layers selected in the Layer Editor.

## Installation

1. Open Maya's **Script Editor** and select the **Python** tab.
2. Paste the complete contents of `animator_curve_reducer.py`.
3. Run the script.

No third-party Python packages are required. The window owns and cleans up its
selection watchers, timers, callbacks, and script jobs when it closes.

## Usage

1. Select keys or channels in the Graph Editor, or select animated controls.
2. Optionally use **Start Live Preview** to tune the reduction.
3. Click **Reduce Curves**.
4. Enable **Bake detail to additive layer** only when residual detail output is
   required.

To reduce whole anim layers, turn on **Target selected anim layers** and
select the layers in the Layer Editor (the base layer counts too). Reduce,
Live Preview and Swap then work on every curve of those layers, ignoring the
Graph Editor and viewport selection. Locked layers and linked additive detail
layers are skipped and listed, so protected layers are never edited.

Advanced fitting, sampling, extrema, key-limit, and buffer settings are in the
collapsed **Advanced Settings** panel.

## Testing

`python tests/test_selected_layers.py` checks the anim layer targeting without
Maya, using a fake `maya.cmds`.
