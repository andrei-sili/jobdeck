"""Entry point: `python -m jobdeck` or the `jobdeck` console script."""


def main() -> None:
    from jobdeck.ui.app import run_app

    run_app()


if __name__ == "__main__":
    main()
