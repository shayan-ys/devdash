# Prior art: how terminal dashboards let plugins take keys

Research for [Prior art: how terminal dashboards let plugins take keys](https://github.com/shayan-ys/devdash/issues/9), on the map [Map: integration key contract](https://github.com/shayan-ys/devdash/issues/8). Primary sources only; gaps are marked "not covered".

## k9s plugins
- **Delivery/state:** A shortcut invokes an ad-hoc command; command args can interpolate host environment values such as selected `$NAME`, `$NAMESPACE`, `$CONTEXT`, resource group/version, filter, and columns. It is per activation, not a documented event stream. `background` selects background execution; docs do not specify redraw-after-run or overlap semantics. [k9s plugin docs](https://k9scli.io/topics/plugins/)
- **Focus/conflicts/discovery:** `scopes` restricts availability to resource views or `all`; cluster/context-specific plugin files are supported. The description is printed next to the shortcut in the menu. Collision rejection/precedence is not covered.
- **Syntax:** documented examples use `Ctrl-L`, `Shift-B`, `Shift-Z`; a full case/special-key grammar is not covered.

## lazygit customCommands
- **Delivery/state:** Key fires a custom command; selection/context is interpolated from Go templates, e.g. `.SelectedFile.Name`, `.SelectedLocalCommit.Hash`. Prompts gather input and expose values as `.Form.<key>`. Output may be discarded, shown in terminal, log/logWithPty, or popup. A command invocation, not a persistent key-event API. [Custom Command Keybindings](https://github.com/jesseduffield/lazygit/blob/master/docs/Custom_Command_Keybindings.md)
- **Focus/conflicts/discovery:** contexts can be panel-specific, comma-separated, or `global`; commands appear in the `?` keybindings menu alongside built-ins. Collision policy and in-flight behaviour/redraw are not covered.
- **Syntax:** printable characters are literal (`A` means shifted A); special keys use `<enter>`, `<f1>`, `<up>`; modifiers e.g. `<ctrl+shift+up>`. [Key syntax](https://github.com/jesseduffield/lazygit/blob/master/docs/keybindings/Custom_Keybindings.md)

## WTF (wtfutil)
- **Delivery/state:** In-process module widgets, not external-command key dispatch. Per-module key handling for external commands is not covered. [Common Settings](https://wtfutil.com/configuration/common_settings/)
- **Focus/conflicts/discovery:** `focusable` defaults false; numeric `focusChar` (0–9) jumps to a widget; Tab / Shift-Tab cycles focus; Esc removes focus. Quick start documents `q` quit and Ctrl-R refresh. [Quick Start](https://wtfutil.com/quick_start/)
- **Refresh/syntax:** global refresh documented; key-triggered redraw, in-flight behaviour, key grammar, and duplicate rejection not covered.

## Zellij plugins
- **Delivery/state:** A long-lived WASM plugin subscribes to `EventType::Key`; `update` receives `KeyWithModifier` while the plugin pane has focus; state lives in the plugin. `run_command` is asynchronous; completion arrives as `RunCommandResult` with stdout/stderr/exit status and a caller context map. [Plugin events](https://zellij.dev/documentation/plugin-api-events)
- **Focus/conflicts/discovery:** key events go to the focused plugin pane. Keybindings are mode-scoped; configured bindings override defaults individually, and keys can be unbound. Duplicate validation and plugin shortcut discovery not covered. [Binding/overriding](https://zellij.dev/documentation/keybindings-binding)
- **Refresh/syntax:** `update` returns true to request a render. Overlap policy not covered. Keys like `Ctrl a`, `Alt a`, `F8`, `Left`. [Keys](https://zellij.dev/documentation/keybindings-keys)

## tmux
- **Delivery/state:** `bind-key` binds an action in a key table; `run-shell` runs a `/bin/sh` command with formats expanded first. One-shot, no persistent event channel. [tmux(1)](https://github.com/tmux/tmux/blob/master/tmux.1)
- **Focus/conflicts/discovery:** root table for unprefixed keys, `prefix` table after the prefix, custom tables via `switch-client -T`; tables resolve many collisions and a later binding replaces an earlier one. `list-keys` shows bindings.
- **Syntax:** literal keys, `C-`/`^` Ctrl, `S-` Shift, `M-` Alt, names such as `Up`, `Enter`, `F1`.

## Patterns and trade-offs for devdash
- **One-shot vs long-lived:** k9s, lazygit, and tmux run a one-shot command per activation, which matches devdash's current contract; none documents a running command consuming later key events. Zellij is the long-lived, event-driven counterpoint with a different lifecycle and state model.
- **State passing:** host snapshot via environment/arguments (k9s) or templates (lazygit) makes each invocation self-contained; a long-lived plugin keeps its own state (Zellij). For devdash, passing current selection/scope with each re-run keeps the command stateless; host-held state plus an event command is another split. *Inference.*
- **Focus vs unique keys:** context scopes (k9s, lazygit), focus routing (WTF, Zellij), and key tables (tmux) all avoid globally unique keys. Global uniqueness is simplest but forbids reuse. Collision precedence is generally undocumented; do not assume any tool rejects duplicates.
- **In-flight handling:** no source specifies what happens when a key arrives during a prior run. Options: serialize/queue, ignore while busy, cancel and replace; each must define when the frame updates. Design options, not observed behaviour.
- **Discoverability:** lazygit lists custom bindings in `?`; k9s shows shortcut plus description in its menu. External-command bindings benefit from an equivalent help surface. *Inference.*
