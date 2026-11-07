# Security

ConvertAll opens files people download from anywhere, so a file that makes it read, write or run something it shouldn't is a security bug. Examples: a file that writes outside the output folder when extracted, pulls other files from the computer into its output, or makes ConvertAll contact a server.

## What ConvertAll does about it

- Archives extract only inside their folder: no escaping names, links or device files, nothing overwritten, and a disk-space budget against archive bombs.
- A file may pull in pictures only from its own folder or one level up; an SVG only the pictures carried inside it. Included files and bibliographies (LaTeX, reStructuredText, AsciiDoc, Org, Typst, DocBook, man pages) follow the same rule, and FFmpeg reads local files only.
- Engines are found on PATH or by full path, never in the current folder.
- Compressed pictures and maps (`.svgz`, `.kmz`) are refused past 512 MB unpacked.
- PDF pages are drawn with at most 150 million pixels, however big the page says it is.
- The open window takes files only from the same Windows account.
- Apart from downloads you agree to (an engine, gmsh for a CAD part), the only network request ConvertAll makes is the update check: at most once a day, from the window, a GET of GitHub's latest-release page for ConvertAll, with no data about you or your files. Its answer is only shown as a link (to a github.com/puretechteam page; anything else is ignored), never downloaded. It can be turned off in ☰. Watch folders write only into the `Converted` folder inside the watched folder.

`tests/test_hardening.py` and `tests/test_archive_safety.py` build hostile files in code and check each of these.

## Reporting

Please don't open a public issue. Use **Security → Report a vulnerability** on this repository's GitHub page, which only the maintainers can see. Include the file that triggers it, if you can, and what it makes ConvertAll do.

You'll get a reply within a week. Fixes ship in a new release, and the changelog credits you unless you'd rather it didn't.

## Supported versions

Only the latest release gets fixes.

The engines ConvertAll can call (FFmpeg, LibreOffice, Ghostscript) are separate programs. gmsh, for CAD parts, is a library ConvertAll downloads from PyPI the first time a part needs it, pinned by version and SHA-256 (a download that doesn't match is refused). Report problems in them to their own projects.
