"""
keep_alive.py

A tiny, dependency-free module holding the keep-alive registry used to
stop PyQt/SIP from losing track of our custom QgsLayoutItem subclasses.
Deliberately kept separate from layout_plugin.py (which imports the
item modules) so that the item modules can import this too, for their
clone() overrides, without creating a circular import.

See the long comment in layout_plugin.py for the full explanation of
why this is necessary: in short, QgsLayoutItem's scene membership goes
through the QGraphicsItem system rather than the QObject parent/child
tree, so a Python-created item handed to C++ (via addLayoutItem(), or
returned from a createItem()/clone() virtual override) needs an
explicit, durable Python reference kept somewhere -- otherwise its
locally-scoped wrapper can be garbage collected, and any later
C++->Python call for that same item gets a generic base QgsLayoutItem
wrapper instead of ours.
"""

from .reliability import record_suppressed_exception


_ITEM_KEEP_ALIVE = {}


def _release_item(item_key):
    """Drop the Python wrapper after QGIS destroys the underlying item."""
    _ITEM_KEEP_ALIVE.pop(item_key, None)


def keep_alive(item):
    """Keep an active custom item alive until its C++ QObject is destroyed."""
    item_key = id(item)
    if _ITEM_KEEP_ALIVE.get(item_key) is item:
        return item

    _ITEM_KEEP_ALIVE[item_key] = item
    try:
        item.destroyed.connect(
            lambda *_args, key=item_key: _release_item(key))
    except Exception:
        # Older bindings may not expose QObject.destroyed on this wrapper.  In
        # that case retain the original session-long keep-alive behaviour.
        record_suppressed_exception()
    return item
