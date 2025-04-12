import os
import sys
from Configs import getConfig
from linetimer import CodeTimer

if __name__ == "__main__":
    SCRIPT_DIR = os.path.dirname(
        os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__)))
    )
    SCRIPT_DIR = os.path.join(SCRIPT_DIR, "..")
    sys.path.append(os.path.normpath(SCRIPT_DIR))


from dataloader.download_data import download
from dataloader.load_data import load_data, load_data_mp


# Adapt to CovidGraph Dataloaders env API


if __name__ == "__main__":
    config = getConfig()
    print(
        "Start with loglevel '{}' and ENV={}".format(
            config.LOG_LEVEL, os.getenv("ENV", "DEVELOPMENT")
        )
    )

    with CodeTimer("Downloader", unit="s"):
        download()
    with CodeTimer("Importer", unit="s"):
        load_data()

