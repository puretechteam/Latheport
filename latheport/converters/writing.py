"""Markup languages and scanned books via bookport (no Pandoc, no DjVuLibre): LaTeX,
reStructuredText, Org, AsciiDoc, MediaWiki, Typst, DocBook, man pages and Textile to and from
Markdown, HTML, Word, OpenDocument and EPUB; Markdown and HTML to Word and OpenDocument; DjVu to PDF
(searchable) and to text.

A document may pull in pictures, included files and a bibliography only from its own folder or one
level up (bookport.limits.safe_path); anything else is left out and the report says so. Text
outputs that refer to pictures come back as a folder: the document and an images folder."""

from pathlib import Path

from ..errors import ConversionError
from ..losses import loss
from ..registry import converter, note

MARKUP = {"tex", "rst", "org", "adoc", "wiki", "typ", "dbk", "man", "textile"}
HUB = {"md", "html", "docx", "odt", "epub"}
TEXT_OUT = MARKUP  # end results written as text: their pictures go beside them, in a folder


def _read(src: Path, fmt: str):
    import bookport
    try:
        book = bookport.read(src, fmt)
    except bookport.BookportError as exc:
        raise ConversionError(str(exc)) from None
    for line in getattr(book, "lost", ()):
        note(line)
    return book


def _write(book, dst: Path, fmt: str) -> Path:
    import bookport
    from bookport.registry import write_options
    if fmt not in TEXT_OUT or not book.images() or "pictures" not in write_options(fmt):
        try:
            book.write(dst, fmt)
        except bookport.BookportError as exc:
            raise ConversionError(str(exc)) from None
        return dst
    work = dst.parent / "_doc"
    work.mkdir()
    try:
        book.write(work / dst.name, fmt, pictures=True)
    except bookport.BookportError as exc:
        raise ConversionError(str(exc)) from None
    files = list(work.iterdir())
    if len(files) == 1:
        files[0].rename(dst)
        return dst
    folder = dst.parent / f"{dst.stem} ({fmt})"
    work.rename(folder)
    note(f"{fmt.upper()} is text: its pictures are in an images folder beside it")
    return folder


def _notes(src: str, dst: str, opts: dict) -> list:
    if dst == "man":
        return [loss("man pages hold text only: pictures are left out", "pictures", weight=0)]
    return []


# a markup language is where a route ends (it may be a folder with pictures): never a step on the way
@converter(inputs=MARKUP, outputs=HUB, notes=_notes)
@converter(inputs=MARKUP | HUB, outputs=MARKUP, notes=_notes, terminal=True)
@converter(inputs={"md", "html"}, outputs={"docx", "odt"})
def markup_convert(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> Path:
    return _write(_read(src, src_fmt), dst, dst_fmt)


@converter(inputs={"djvu"}, outputs={"pdf"})
@converter(inputs={"djvu"}, outputs={"txt"}, terminal=True)
def djvu_convert(src: Path, dst: Path, src_fmt: str, dst_fmt: str) -> None:
    import bookport
    from bookport import djvu
    try:
        if dst_fmt == "pdf":
            djvu.to_pdf(src, dst)
            return
        text = djvu.to_text(src)
    except bookport.BookportError as exc:
        raise ConversionError(str(exc)) from None
    if not text.strip():
        raise ConversionError("This DjVu file has no text layer (it was scanned without OCR)")
    dst.write_text(text, encoding="utf-8")
