# devdash

A terminal dashboard that combines its own sections with output from configured integrations.

## Language

**Integration**:
A configured command whose output forms one dashboard section.
_Avoid_: plugin, widget

**Built-in key**:
A devdash key for refresh, quit, or focus.
_Avoid_: integration binding, action

**Binding**:
A key mapped to an action in one integration.
_Avoid_: built-in key, shortcut

**Action**:
A name an integration defines and understands as a response to a binding.
_Avoid_: command, key

**Focus**:
The one integration that receives binding keys.
_Avoid_: selection, active section

**State file**:
An integration's state for one devdash process.
_Avoid_: cache, configuration file
