# Importing a module registers its converters. Engine-free modules come first so
# they win ties against engine-backed routes of equal cost.
# isort: off
from . import (  # noqa: F401
    archives,
    cad,
    comics,
    contacts,
    data,
    writing,  # before documents: Markdown and HTML to Word go through bookport, not LibreOffice
    documents,
    ebooks,
    email,
    fonts,
    images,
    maps,
    markup,
    media,
    medical,
    models,
    notebooks,
    pdf,
    pictures,
    subtitles,
)
# isort: on
