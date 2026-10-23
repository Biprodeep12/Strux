"""PyInstaller entry point (absolute import; `python -m watcher` stays the dev entry)."""
import multiprocessing

from watcher.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
