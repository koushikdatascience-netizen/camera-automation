# SnapKey Vision AI PyInstaller spec.
# Keep this build in ONEDIR mode because AI/CV dependencies are large.

from pathlib import Path

import insightface
import certifi
from PyInstaller.utils.hooks import collect_all, collect_submodules


block_cipher = None
INSIGHTFACE_OBJECTS = Path(insightface.__file__).parent / "data" / "objects"
ROOT = Path(SPECPATH).resolve().parents[1]
SCISSORS_MODEL = ROOT / "kaggle-model/scissors_yolo11m_960.pt"

datas = [
    (str(ROOT / "camera_service/web/setup.html"), "camera_service/web"),
    (str(ROOT / "camera_service/web/favicon.ico"), "camera_service/web"),
    (str(ROOT / "camera_service/web/static"), "camera_service/web/static"),
    (str(ROOT / "assets/brand"), "assets/brand"),
    (str(ROOT / "config.example.yaml"), "."),
    (str(INSIGHTFACE_OBJECTS / "meanshape_68.pkl"), "objects"),
    (certifi.where(), "certifi"),
]
if SCISSORS_MODEL.exists():
    datas.append((str(SCISSORS_MODEL), "kaggle-model"))
for model_name in ("yolo11m.pt", "yolo26n.pt"):
    model_path = ROOT / model_name
    if model_path.exists():
        datas.append((str(model_path), "."))
FACE_MODELS = Path.home() / ".insightface/models/buffalo_l"
for name in ("det_10g.onnx", "w600k_r50.onnx"):
    if not (FACE_MODELS / name).exists():
        raise FileNotFoundError(f"Offline face recognition model missing: {name}")
    datas.append((str(FACE_MODELS / name), "face_models/models/buffalo_l"))
for artifact in ("yolo26n.onnx", "yolo26n_openvino_model"):
    if (ROOT / artifact).exists():
        datas.append((str(ROOT / artifact), artifact if (ROOT / artifact).is_dir() else "."))
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

for package_name in ("insightface", "onnxruntime", "openvino", "ultralytics", "torch", "livekit"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

# torchvision's native ops (NMS / ROI align) are loaded dynamically by
# ultralytics and are not a dependency imported by camera_service itself.
# collect_all("torchvision") includes the package data but may omit these .pyd
# extension modules, leaving tracking streams alive with inference failing.
torchvision_datas, torchvision_binaries, torchvision_hiddenimports = collect_all("torchvision")
datas += torchvision_datas
binaries += torchvision_binaries
import importlib.util
torchvision_spec = importlib.util.find_spec("torchvision")
if torchvision_spec is None or not torchvision_spec.submodule_search_locations:
    raise RuntimeError("TorchVision package is required for person tracking")
torchvision_extension = Path(next(iter(torchvision_spec.submodule_search_locations))) / "_C.pyd"
if not torchvision_extension.is_file():
    raise FileNotFoundError(f"TorchVision native operators are missing: {torchvision_extension}")
binaries.append((str(torchvision_extension), "torchvision"))
hiddenimports += torchvision_hiddenimports

hiddenimports += collect_submodules("camera_service")


a = Analysis(
    [str(ROOT / "camera_service/launcher.py")],
    pathex=[str(ROOT)],
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
    name="SnapKeyVisionAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(ROOT / "assets/brand/app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="SnapKeyVisionAI",
)
