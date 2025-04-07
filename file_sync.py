import os
import time
import socket
import threading
import shutil
import tempfile
import json
import hashlib
import servicemanager
import win32service
import win32serviceutil
import win32event
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import sys

# Configuration
WATCH_FOLDER = "C:/sample"  # Change this to your sync folder
REMOTE_IP = "192.168.137.75"      # Other machine's IP
REMOTE_PORT = 5000
LOCAL_PORT = 5000
RETRY_COUNT = 5
RETRY_DELAY = 2
SYNC_INTERVAL = 5  # Seconds between full sync checks

# Track file origins to prevent loops
file_origins = {}
origin_file = os.path.join(WATCH_FOLDER, ".sync_origins.json")

def load_origins():
    global file_origins
    try:
        if os.path.exists(origin_file):
            with open(origin_file, 'r') as f:
                file_origins = json.load(f)
    except Exception as e:
        print(f"Error loading origins: {e}")

def save_origins():
    try:
        with open(origin_file, 'w') as f:
            json.dump(file_origins, f)
    except Exception as e:
        print(f"Error saving origins: {e}")

def get_file_hash(filepath):
    """Generate MD5 hash of file contents"""
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

class SyncHandler(FileSystemEventHandler):
    def __init__(self):
        self.ignore_patterns = ['.sync_origins.json']
        self.last_sync = 0

    def should_ignore(self, path):
        return any(ignore in path for ignore in self.ignore_patterns)

    def on_modified(self, event):
        if not event.is_directory and not self.should_ignore(event.src_path):
            self.handle_change(event.src_path)

    def on_created(self, event):
        if not self.should_ignore(event.src_path):
            self.handle_change(event.src_path)

    def on_deleted(self, event):
        if not self.should_ignore(event.src_path):
            self.handle_delete(event.src_path)

    def on_moved(self, event):
        if not self.should_ignore(event.dest_path):
            self.handle_change(event.dest_path)
            self.handle_delete(event.src_path)

    def handle_change(self, file_path):
        if time.time() - self.last_sync < 1:  # Prevent rapid sync
            return
            
        if file_path in file_origins and file_origins[file_path] == "remote":
            return  # Skip files we received from remote
            
        if os.path.isdir(file_path):
            self.sync_directory(file_path)
        else:
            self.send_file(file_path)
        self.last_sync = time.time()

    def handle_delete(self, file_path):
        if file_path in file_origins and file_origins[file_path] == "remote":
            return
            
        file_name = os.path.relpath(file_path, WATCH_FOLDER)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((REMOTE_IP, REMOTE_PORT))
                s.sendall(json.dumps({"action": "delete", "path": file_name}).encode())
        except Exception as e:
            print(f"Error sending delete command: {e}")

    def sync_directory(self, dir_path):
        for root, dirs, files in os.walk(dir_path):
            for file in files:
                file_path = os.path.join(root, file)
                if not self.should_ignore(file_path):
                    self.send_file(file_path)

    def send_file(self, file_path):
        if not os.path.exists(file_path):
            return
            
        file_name = os.path.relpath(file_path, WATCH_FOLDER)
        file_hash = get_file_hash(file_path) if os.path.isfile(file_path) else "dir"

        if file_path in file_origins and file_origins[file_path] == file_hash:
            return  # File hasn't changed

        attempt = 0
        while attempt < RETRY_COUNT:
            try:
                print(f"Sending: {file_name}")
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)
                    s.connect((REMOTE_IP, REMOTE_PORT))
                    
                    # Send metadata first
                    metadata = {
                        "action": "sync",
                        "path": file_name,
                        "hash": file_hash,
                        "is_dir": os.path.isdir(file_path)
                    }
                    s.sendall(json.dumps(metadata).encode())
                    
                    # Send file content if not directory
                    if os.path.isfile(file_path):
                        with open(file_path, 'rb') as f:
                            while chunk := f.read(4096):
                                s.sendall(chunk)
                                
                file_origins[file_path] = "local"
                save_origins()
                return
            except Exception as e:
                attempt += 1
                print(f"Error sending {file_name} (attempt {attempt}): {e}")
                time.sleep(RETRY_DELAY * attempt)
        print(f"Failed to send {file_name} after {RETRY_COUNT} attempts")

def receive_connection(conn, addr):
    try:
        data = conn.recv(4096).decode()
        metadata = json.loads(data)
        
        if metadata["action"] == "delete":
            target_path = os.path.join(WATCH_FOLDER, metadata["path"])
            if os.path.exists(target_path):
                if os.path.isdir(target_path):
                    shutil.rmtree(target_path)
                else:
                    os.remove(target_path)
                if target_path in file_origins:
                    del file_origins[target_path]
            return
            
        file_path = os.path.join(WATCH_FOLDER, metadata["path"])
        file_dir = os.path.dirname(file_path)
        
        # Mark as remote origin before processing to prevent loops
        file_origins[file_path] = "remote"
        save_origins()
        
        if metadata["is_dir"]:
            os.makedirs(file_path, exist_ok=True)
        else:
            os.makedirs(file_dir, exist_ok=True)
            with open(file_path, 'wb') as f:
                while chunk := conn.recv(4096):
                    f.write(chunk)
                    
        # Update hash after successful transfer
        if not metadata["is_dir"]:
            file_origins[file_path] = metadata["hash"]
            save_origins()
            
        print(f"Received: {metadata['path']}")
    except Exception as e:
        print(f"Error handling connection: {e}")

def receive_server(stop_event):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        s.bind(("0.0.0.0", LOCAL_PORT))
        s.listen(5)
        print(f"Sync server listening on port {LOCAL_PORT}")
        
        while not stop_event.is_set():
            try:
                conn, addr = s.accept()
                threading.Thread(target=receive_connection, args=(conn, addr)).start()
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Server error: {e}")

def full_sync(stop_event):
    while not stop_event.is_set():
        time.sleep(SYNC_INTERVAL)
        handler = SyncHandler()
        handler.sync_directory(WATCH_FOLDER)

class FileSyncService(win32serviceutil.ServiceFramework):
    _svc_name_ = "FileSyncService"
    _svc_display_name_ = "File Synchronization Service"
    _svc_description_ = "Bidirectional file synchronization service"

    def __init__(self, args):
        if not args:
            args = [sys.argv[0]]
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.stop_event = threading.Event()
        self.observer = None
        self.server_thread = None
        self.sync_thread = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_event.set()
        if self.observer:
            self.observer.stop()
        win32event.SetEvent(self.hWaitStop)
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)

    def SvcDoRun(self):
        self.ReportServiceStatus(win32service.SERVICE_START_PENDING)
        try:
            load_origins()
            self.start_service()
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            self.main_loop()
        except Exception as e:
            print(f"Service failed: {e}")
            self.SvcStop()

    def start_service(self):
        # Start watchdog observer
        self.observer = Observer()
        self.observer.schedule(SyncHandler(), WATCH_FOLDER, recursive=True)
        self.observer.start()
        
        # Start server thread
        self.server_thread = threading.Thread(target=receive_server, args=(self.stop_event,))
        self.server_thread.daemon = True
        self.server_thread.start()
        
        # Start full sync thread
        self.sync_thread = threading.Thread(target=full_sync, args=(self.stop_event,))
        self.sync_thread.daemon = True
        self.sync_thread.start()

    def main_loop(self):
        while not self.stop_event.is_set():
            time.sleep(1)

def run_as_console():
    load_origins()
    stop_event = threading.Event()
    
    # Start all components
    observer = Observer()
    observer.schedule(SyncHandler(), WATCH_FOLDER, recursive=True)
    observer.start()
    
    server_thread = threading.Thread(target=receive_server, args=(stop_event,))
    server_thread.daemon = True
    server_thread.start()
    
    sync_thread = threading.Thread(target=full_sync, args=(stop_event,))
    sync_thread.daemon = True
    sync_thread.start()
    
    print("Running in console mode. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        stop_event.set()
        observer.stop()
        observer.join()
        save_origins()

if __name__ == '__main__':
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(FileSyncService)
        servicemanager.StartServiceCtrlDispatcher()
    elif len(sys.argv) > 1 and sys.argv[1] == "debug":
        run_as_console()
    else:
        win32serviceutil.HandleCommandLine(FileSyncService)