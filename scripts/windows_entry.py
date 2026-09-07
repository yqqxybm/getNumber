"""Absolute import entry point for Windows PyInstaller builds."""
import sys
from tingma.app import main
from tingma.build_smoke import run as smoke_check

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--build-smoke':
        raise SystemExit(smoke_check(sys.argv[2]))
    else:
        main()
