"""Every test runs with config.yaml redirected to a throwaway folder: no test can ever write the real one
(a test that saved settings once replaced the live config with its temp data folder and switched the mic on)."""
import atexit
import shutil
import tempfile
from pathlib import Path

from watcher import config as _config

_SANDBOX = Path(tempfile.mkdtemp(prefix="strux-tests-"))
_config.USER_DIR = _SANDBOX               # config_path() is USER_DIR / "config.yaml"
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)
