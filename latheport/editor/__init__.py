"""The editor: crop, adjust and put text on pictures and videos before they're converted, in a dark
photo-editor window (tools on the left, their options on top, the files along the bottom)."""

from .frames import Frame, load_frame, size_of
from .model import Edit, Edits, History
from .window import EditorWindow

__all__ = ["Edit", "EditorWindow", "Edits", "Frame", "History", "load_frame", "size_of"]
