import os
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

WATCH_FOLDER = "C:/sample"

class FileTransferHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            print(f"File created: {event.src_path}")

def main():
    observer = Observer()
    observer.schedule(FileTransferHandler(), WATCH_FOLDER, recursive=False)
    observer.start()

    try:
        print("Observer started, waiting for file system events...")
        while True:
            time.sleep(1)  # Keep the main thread alive
    except KeyboardInterrupt:
        observer.stop()
        print("Observer stopped.")
    observer.join()

if __name__ == "__main__":
    main()
