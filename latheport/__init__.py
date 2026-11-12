"""Latheport: convert, compress, edit or extract any file.

    from latheport import convert, plan, Report
    report = Report()
    out = convert("photo.heic", "jpg", report=report, options={"quality": 80})
    print(report.notes, report.warnings)
"""

__version__ = "1.5.1"

__all__ = ["Report", "__version__", "convert", "extract", "plan"]


def __getattr__(name: str):
    # loaded on first use, so starting the window or handing it files doesn't load the converters
    if name == "Report":
        from .assurance import Report
        return Report
    if name in ("convert", "extract", "plan"):
        from . import pipeline
        return getattr(pipeline, name)
    raise AttributeError(f"module 'latheport' has no attribute {name!r}")
