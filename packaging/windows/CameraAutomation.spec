# CameraAutomation PyInstaller spec.
# Keep this build in ONEDIR mode because AI/CV dependencies are large.

from PyInstaller.utils.hooks import collect_all, collect_submodules


block_cipher = None

datas = [
    ("../../camera_service/web/setup.html", "camera_service/web"),
    ("../../camera_service/web/favicon.ico", "camera_service/web"),
    ("../../camera_service/web/static", "camera_service/web/static"),
    ("../../config.example.yaml", "."),
]
binaries = []
hiddenimports = [
    "camera_service.api",
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
]

for package_name in ("insightface", "onnxruntime", "ultralytics", "torch"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

hiddenimports += collect_submodules("camera_service")


a = Analysis(
    ["../../camera_service/launcher.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "tkinter.constants",
        "tkinter.filedialog",
        "tkinter.simpledialog",
        "tkinter.messagebox",
        "tkinter.colorchooser",
        "tkinter.commondialog",
        "tkinter.dnd",
        "tkinter.scrolledtext",
        "tkinter.tix",
        "tkinter.tix_hystub",
        "tkinter.ttk",
        "_tkinter",
        "PIL.ImageTk",
        "PIL.ImageQt",
        "config.yaml",
        ".env",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        "data",
        "logs",
        "clips",
        "snapshots",
        "unknown",
        ".venv",
        ".venv311",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CameraAutomation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon="../../camera_service/web/favicon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="CameraAutomation",
)
