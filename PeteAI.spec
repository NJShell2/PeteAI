# -*- mode: python ; coding: utf-8 -*-
"""Build PeteAI.exe with its support files in an adjacent Engine folder.

Run from the project root:
    .venv\\Scripts\\python.exe -m PyInstaller PeteAI.spec --noconfirm

Produces, in dist\\:
    PeteAI.exe        the app
    Engine\\           PyInstaller's support folder -- the collected third-party
                      assets (Playwright driver, CA certificates, and so on)
                      plus app\\static (the UI) and .env

Why an Engine folder and not a single self-contained .exe: a one-file build
unpacks itself into a temp directory on every launch and deletes it on exit.
On a network share that is slow, and it puts the assets somewhere the app would
have to treat as volatile. The Engine folder is stable across restarts and
inspectable, which keeps stack traces meaningful.

The name "Engine" is set by COLLECT(contents_directory=...) at build time, NOT
by renaming "_internal" afterwards -- PyInstaller bakes that name into the exe
and renaming it afterwards stops the exe from launching at all. See 8.6 in
PETE_UX_NOTES.md.

app\\config.py anchors BASE_DIR to this folder when frozen, so the app finds its
static files and data here rather than in the temp unpack directory.
"""
import os

PROJECT = SPECPATH

a = Analysis(
    [os.path.join(PROJECT, "main.py")],
    pathex=[PROJECT],
    binaries=[],
    # The UI files the app serves, and the .env it reads for the API key.
    datas=[
        (os.path.join(PROJECT, "app", "static"), "app/static"),
        (os.path.join(PROJECT, ".env"), "."),
    ],
    hiddenimports=[
        # uvicorn.run("app.api:app", ...) imports the app by name at runtime,
        # from inside the bundle, so PyInstaller's static analysis cannot see it.
        "app.api",
        "app.config",
        "app.console",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Large things this app never imports. Dropping them keeps the Engine
    # folder to a sensible size.
    excludes=[
        "tkinter", "matplotlib", "numpy", "pandas", "PIL",
        "pytest", "notebook", "IPython", "PyQt5", "PySide2",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PeteAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    # This name decides the bundle folder: it produces dist\PeteAI\ containing
    # PeteAI.exe and the support subfolder. Naming it "_internal" instead
    # produces dist\_internal\_internal\ and puts the exe in the wrong place,
    # which is what happened on the first two attempts.
    name="PeteAI",
    # THE fix for the _internal trap (see PETE_UX_NOTES.md 8.6), and the reason
    # the finished layout is just PeteAI.exe + Engine\.
    #
    # PyInstaller bakes the support folder's name into the executable: the
    # bootloader resolves python312.dll against that exact name. Renaming
    # _internal\ by hand after the build is therefore not cosmetic -- it makes
    # the exe die at launch with
    #     [PYI-xxxxx:ERROR] Failed to load Python DLL '...\_internal\python312.dll'
    #
    # Earlier builds worked around that by keeping _internal\ and having
    # build_exe.bat xcopy a second ~300 MB copy of it to Engine\, so the folder
    # beside the exe was duplicated. This option sets the name at build time
    # instead, so PyInstaller creates Engine\ as the one and only support
    # folder and the exe is built to look there. No rename, no copy, no
    # duplicate -- and config.py finds its files exactly where it already looks.
    contents_directory="Engine",
)
