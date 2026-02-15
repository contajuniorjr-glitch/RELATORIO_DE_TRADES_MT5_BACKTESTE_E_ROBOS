import os
import shutil
import sys
from pathlib import Path


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base).resolve()
    return Path(__file__).resolve().parent


def _ensure_resources(runtime_dir: Path, bundle_dir: Path) -> Path:
    resources = [
        ("POST_TRADE_APP_REAL.py", True),
        ("asset_type_map.json", False),
        ("candle_colors.json", False),
        ("stored_mt5_magics.json", False),
    ]
    for name, overwrite in resources:
        src = bundle_dir / name
        dst = runtime_dir / name
        if not src.exists():
            continue
        if overwrite or not dst.exists():
            try:
                shutil.copy2(src, dst)
            except Exception:
                continue
    streamlit_script = runtime_dir / "POST_TRADE_APP_REAL.py"
    if not streamlit_script.exists():
        raise FileNotFoundError("POST_TRADE_APP_REAL.py not found in runtime directory.")
    return streamlit_script


def main() -> None:
    runtime_dir = _runtime_dir()
    bundle_dir = _bundle_dir()
    os.chdir(runtime_dir)

    streamlit_script = _ensure_resources(runtime_dir, bundle_dir)

    try:
        from streamlit.web import cli as stcli
    except ImportError as exc:
        raise SystemExit(
            "Streamlit is not installed. Install dependencies first "
            "with `pip install -r requirements.txt`."
        ) from exc

    sys.argv = [
        "streamlit",
        "run",
        str(streamlit_script),
        "--server.headless=false",
        "--browser.gatherUsageStats=false",
    ]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
