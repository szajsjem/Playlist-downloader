# py2exe freeze script for Playlist-downloader
from py2exe import freeze

def options():
    return {
        "packages": ["main"],
        "optimize": 2,
        "compressed": False,
    }

if __name__ == "__main__":
    freeze(console=["main.py"], options=options())