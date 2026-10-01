# PyInstaller entry point for recover_pst.exe
import multiprocessing

from email_date_range.recover import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # the orphan scan runs in a child process
    raise SystemExit(main())
