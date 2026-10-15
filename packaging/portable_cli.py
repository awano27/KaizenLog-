"""PyInstaller entrypoint for the KaizenLog command line interface."""

from kaizenlog.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
