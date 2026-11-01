import sys

if len(sys.argv) > 1 and sys.argv[1] == "--cli":
    # started by convertall-cli.exe (packaging/launcher): take its console, so Ctrl+C reaches
    # this program too; output goes to the handles it passed on (its console, a pipe or a file)
    import ctypes

    ctypes.windll.kernel32.AttachConsole(-1)  # ATTACH_PARENT_PROCESS

    # a window program doesn't get Python's Ctrl+C handling: without this, Windows ends it on the
    # spot (temporary folder and all). Ctrl+C becomes Python's SIGINT, which the CLI turns into
    # a clean stop.
    import _thread
    import signal

    @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
    def _on_ctrl(event: int) -> int:
        if event in (0, 1):  # CTRL_C_EVENT, CTRL_BREAK_EVENT
            _thread.interrupt_main(signal.SIGINT)
            return 1
        return 0

    ctypes.windll.kernel32.SetConsoleCtrlHandler(_on_ctrl, 1)
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:  # nothing passed on: write to the console itself
            setattr(sys, name, open("CONOUT$", "w", encoding="utf-8", errors="replace"))  # noqa: SIM115
    from convertall.cli import main as cli_main

    sys.exit(cli_main(sys.argv[2:]))

from convertall.instance import main

sys.exit(main())
