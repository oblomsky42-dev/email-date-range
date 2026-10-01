# PyInstaller entry point for email_date_range.exe
import multiprocessing

from email_date_range.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # --workers spawns child processes
    raise SystemExit(main())
