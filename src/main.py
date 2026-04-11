"""Entry point for the GLM CC Chess application."""

from src.gui import ChessGUI


def main():
    gui = ChessGUI()
    gui.run()


if __name__ == "__main__":
    main()