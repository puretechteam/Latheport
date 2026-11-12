# Security

Latheport opens files people download from anywhere, so a file that makes it read, write or run something it shouldn't is a security bug. Examples: a file that writes outside the output folder when extracted, pulls other files from the computer into its output, or makes Latheport contact a server.

## What Latheport does about it

- Archives extract only inside their folder: no escaping names, links or device files, nothing overwritten, and a disk-space budget against archive bombs.
- A file may pull in pictures only from its own folder or one level up; an SVG only the pictures carried inside it. Included files and bibliographies (LaTeX, reStructuredText, AsciiDoc, Org, Typst, DocBook, man pages) follow the same rule, and FFmpeg reads local files only.
- Engines are found on PATH or by full path, never in the current folder.
- Compressed pictures and maps (`.svgz`, `.kmz`) are refused past 512 MB unpacked.
- PDF pages are drawn with at most 150 million pixels, however big the page says it is.
- PostScript and EPS run in bookport's own interpreter, which can't open, run, rename or delete files and stops a program that runs too long or nests too deep.
- The open window takes files only from the same Windows account.
- Apart from downloads you agree to (an engine, gmsh for a CAD part, a smart tool), the only network request Latheport makes is the update check: at most once a day, from the window, a GET of GitHub's latest-release page for Latheport, with no data about you or your files. Its answer is shown as a link (to a github.com/puretechteam page; anything else is ignored). An update is downloaded only when you click *Update now*, only from this repository's own release downloads on GitHub, and only kept if its size and SHA-256 match what GitHub lists for it; it unpacks only into its own folder beside the app, refusing a zip with any file outside its `Latheport` folder. The check can be turned off in ☰. Watch folders write only into the `Converted` folder inside the watched folder.

`tests/test_hardening.py` and `tests/test_archive_safety.py` build hostile files in code and check each of these.

## Reporting

Please don't open a public issue. Use **Security → Report a vulnerability** on this repository's GitHub page, which only the maintainers can see. Include the file that triggers it, if you can, and what it makes Latheport do.

You'll get a reply within a week. Fixes ship in a new release, and the changelog credits you unless you'd rather it didn't.

## Supported versions

Only the latest release gets fixes.

The engines Latheport can call (FFmpeg, LibreOffice) are separate programs. gmsh, for CAD parts, is a library Latheport downloads from PyPI the first time a part needs it, pinned by version and SHA-256 (a download that doesn't match is refused). Report problems in them to their own projects.
