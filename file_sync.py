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
import struct
from collections import defaultdict
import uuid

# Configuration
WATCH_FOLDER = "C:/sample"  # Change this to your sync folder
REMOTE_IP = "192.168.137.1"  # Other machine's IP
REMOTE_PORT = 5000
LOCAL_PORT = 5000
RETRY_COUNT = 5
RETRY_DELAY = 2
SYNC_INTERVAL = 5  # Seconds between full sync checks
BLOCK_SIZE = 128 * 1024  # 128KB blocks like Syncthing
MAX_CONNECTION_ATTEMPTS = 5
DEVICE_ID = str(uuid.uuid4())  # Unique identifier for this device
LOCAL_CHANGE_EXPIRE = 10  # Seconds to remember local changes

# Track file versions and origins
file_versions = defaultdict(dict)
version_file = os.path.join(WATCH_FOLDER, ".sync_versions.json")

def get_file_content_hash(filepath):
    """Generate SHA-256 hash of the entire file content"""
    if not os.path.isfile(filepath):
        return None
    
    sha256 = hashlib.sha256()
    with open(filepath, 'rb') as f:
        while True:
            block = f.read(BLOCK_SIZE)
            if not block:
                break
            sha256.update(block)
    return sha256.hexdigest()

def load_versions():
    global file_versions
    try:
        if os.path.exists(version_file):
            with open(version_file, 'r', encoding='utf-8') as f:
                file_versions = json.load(f)
    except Exception as e:
        print(f"Error loading versions: {e}")
        file_versions = defaultdict(dict)

def save_versions():
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as tf:
            json.dump(file_versions, tf)
        # Atomic rename
        os.replace(tf.name, version_file)
    except Exception as e:
        print(f"Error saving versions: {e}")

def get_file_blocks(filepath):
    """Generate block hashes for a file (similar to Syncthing's block protocol)"""
    block_hashes = []
    if not os.path.isfile(filepath):
        return block_hashes
        
    with open(filepath, "rb") as f:
        while True:
            block = f.read(BLOCK_SIZE)
            if not block:
                break
            block_hash = hashlib.sha256(block).hexdigest()
            block_hashes.append(block_hash)
    return block_hashes

def get_file_version(filepath):
    """Generate a version vector for the file"""
    if not os.path.exists(filepath):
        return None
        
    if os.path.isdir(filepath):
        return {
            "type": "directory",
            "device": DEVICE_ID,
            "version": time.time_ns()
        }
        
    stat = os.stat(filepath)
    return {
        "type": "file",
        "size": stat.st_size,
        "mtime": stat.st_mtime_ns,
        "blocks": get_file_blocks(filepath),
        "content_hash": get_file_content_hash(filepath),
        "device": DEVICE_ID,
        "version": time.time_ns()
    }

class SyncHandler(FileSystemEventHandler):
    def __init__(self):
        self.ignore_patterns = ['.sync_versions.json']
        self.last_sync = 0
        self.pending_changes = set()
        self.lock = threading.Lock()
        self.local_changes = {}  # Track changes we initiated locally {path: {hash, timestamp}}

    def should_ignore(self, path):
        return any(ignore in path for ignore in self.ignore_patterns)

    def on_modified(self, event):
        if not event.is_directory and not self.should_ignore(event.src_path):
            self.queue_change(event.src_path)

    def on_created(self, event):
        if not self.should_ignore(event.src_path):
            self.queue_change(event.src_path)

    def on_deleted(self, event):
        if not self.should_ignore(event.src_path):
            self.queue_delete(event.src_path)

    def on_moved(self, event):
        if not self.should_ignore(event.dest_path):
            self.queue_change(event.dest_path)
            self.queue_delete(event.src_path)

    def queue_change(self, file_path):
        with self.lock:
            if not self.is_local_change(file_path):
                self.pending_changes.add(file_path)
                threading.Thread(target=self.process_changes).start()

    def queue_delete(self, file_path):
        with self.lock:
            if not self.is_local_change(file_path):
                self.pending_changes.add(("delete", file_path))
                threading.Thread(target=self.process_changes).start()

    def is_local_change(self, file_path):
        """Check if this change was initiated locally"""
        with self.lock:
            # Clean up expired local changes
            now = time.time()
            self.local_changes = {k: v for k, v in self.local_changes.items() 
                                if now - v['timestamp'] < LOCAL_CHANGE_EXPIRE}
            
            return file_path in self.local_changes

    def mark_local_change(self, file_path):
        """Mark a file as changed locally to prevent echo"""
        with self.lock:
            content_hash = get_file_content_hash(file_path) if os.path.isfile(file_path) else None
            self.local_changes[file_path] = {
                'hash': content_hash,
                'timestamp': time.time()
            }

    def process_changes(self):
        with self.lock:
            if not self.pending_changes:
                return
            changes = self.pending_changes.copy()
            self.pending_changes.clear()
            
        for change in changes:
            if isinstance(change, tuple) and change[0] == "delete":
                self.handle_delete(change[1])
            else:
                self.handle_change(change if not isinstance(change, tuple) else change[1])

    def handle_change(self, file_path):
        current_time = time.time()
        if current_time - self.last_sync < 1:  # Prevent rapid sync
            return
            
        if os.path.isdir(file_path):
            self.sync_directory(file_path)
        else:
            self.send_file(file_path)
        self.last_sync = current_time

    def handle_delete(self, file_path):
        file_name = os.path.relpath(file_path, WATCH_FOLDER)
        file_key = f"{DEVICE_ID}:{file_name}"
        
        # Only send delete if we have a record of this file
        if file_key not in file_versions:
            return
            
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(10)
                s.connect((REMOTE_IP, REMOTE_PORT))
                message = {
                    "action": "delete",
                    "path": file_name,
                    "device": DEVICE_ID,
                    "version": time.time_ns()
                }
                self._send_message(s, message)
                
                # Mark as local change to prevent echo
                self.mark_local_change(file_path)
                
        except Exception as e:
            print(f"Error sending delete command: {e}")

    def sync_directory(self, dir_path):
        for root, dirs, files in os.walk(dir_path):
            for file in files:
                file_path = os.path.join(root, file)
                if not self.should_ignore(file_path):
                    self.send_file(file_path)

    def _send_message(self, sock, message):
        """Send a message with length prefix"""
        data = json.dumps(message).encode('utf-8')
        sock.sendall(struct.pack('!I', len(data)) + data)

    def send_file(self, file_path):
        if not os.path.exists(file_path):
            return
            
        file_name = os.path.relpath(file_path, WATCH_FOLDER)
        current_version = get_file_version(file_path)
        file_key = f"{DEVICE_ID}:{file_name}"

        # Enhanced version and content check
        if file_key in file_versions:
            existing_version = file_versions[file_key]
            
            # Skip if version is newer or equal AND content is identical
            if (existing_version.get("version", 0) >= current_version["version"] and 
                existing_version.get("content_hash") == current_version.get("content_hash")):
                return
                
            # Skip if content is identical regardless of version
            if existing_version.get("content_hash") == current_version.get("content_hash"):
                # Update version only if content is same but version was older
                file_versions[file_key] = current_version
                save_versions()
                return

        attempt = 0
        while attempt < RETRY_COUNT:
            try:
                print(f"Sending: {file_name}")
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(30)
                    s.connect((REMOTE_IP, REMOTE_PORT))
                    
                    # Send metadata first
                    metadata = {
                        "action": "sync",
                        "path": file_name,
                        "version": current_version,
                        "device": DEVICE_ID,
                        "content_hash": current_version.get("content_hash")
                    }
                    self._send_message(s, metadata)
                    
                    # Send file content if not directory
                    if os.path.isfile(file_path):
                        with open(file_path, 'rb') as f:
                            while True:
                                block = f.read(BLOCK_SIZE)
                                if not block:
                                    break
                                s.sendall(struct.pack('!I', len(block)))  # Block size prefix
                                s.sendall(block)
                                
                        # Send end marker
                        s.sendall(struct.pack('!I', 0))
                                
                # Update version after successful transfer
                file_versions[file_key] = current_version
                save_versions()
                
                # Mark as local change to prevent echo
                self.mark_local_change(file_path)
                
                return
            except Exception as e:
                attempt += 1
                print(f"Error sending {file_name} (attempt {attempt}): {e}")
                time.sleep(RETRY_DELAY * attempt)
        print(f"Failed to send {file_name} after {RETRY_COUNT} attempts")

def receive_connection(conn, addr):
    try:
        # Read message length
        raw_msglen = conn.recv(4)
        if not raw_msglen:
            return
        msglen = struct.unpack('!I', raw_msglen)[0]
        
        # Read message data
        data = conn.recv(msglen)
        if len(data) != msglen:
            raise ValueError("Incomplete message received")
            
        metadata = json.loads(data.decode('utf-8'))
        
        if metadata["action"] == "delete":
            target_path = os.path.join(WATCH_FOLDER, metadata["path"])
            file_key = f"{metadata['device']}:{metadata['path']}"
            
            # Only delete if the incoming version is newer
            if file_key in file_versions:
                if file_versions[file_key].get("version", 0) >= metadata["version"]:
                    return
            
            if os.path.exists(target_path):
                if os.path.isdir(target_path):
                    shutil.rmtree(target_path)
                else:
                    os.remove(target_path)
            
            file_versions[file_key] = {
                "device": metadata["device"],
                "version": metadata["version"],
                "deleted": True
            }
            save_versions()
            return
            
        if metadata["action"] == "sync":
            file_path = os.path.join(WATCH_FOLDER, metadata["path"])
            file_key = f"{metadata['device']}:{metadata['path']}"
            
            # Enhanced conflict resolution
            if file_key in file_versions:
                local_version = file_versions[file_key]
                
                # If content is identical, just update version
                if (local_version.get("content_hash") == metadata["version"].get("content_hash") and
                    local_version.get("device") != metadata["device"]):
                    file_versions[file_key] = metadata["version"]
                    save_versions()
                    return
                
                # If remote version is older, skip
                if local_version.get("version", 0) > metadata["version"]["version"]:
                    return
                    
                # If versions are equal but different content, implement conflict resolution
                if (local_version.get("version", 0) == metadata["version"]["version"] and
                    local_version.get("content_hash") != metadata["version"].get("content_hash")):
                    # Conflict resolution: append device ID to filename
                    base, ext = os.path.splitext(metadata["path"])
                    conflict_path = f"{base}_{metadata['device']}{ext}"
                    file_path = os.path.join(WATCH_FOLDER, conflict_path)
                    print(f"Conflict detected, saving as: {conflict_path}")
            
            # Mark as remote change to prevent echo
            handler = SyncHandler()
            handler.mark_local_change(file_path)
            
            if metadata["version"]["type"] == "directory":
                os.makedirs(file_path, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, 'wb') as f:
                    while True:
                        block_size_data = conn.recv(4)
                        if not block_size_data:
                            break
                        block_size = struct.unpack('!I', block_size_data)[0]
                        if block_size == 0:
                            break  # End of file marker
                        
                        # Read block
                        received = 0
                        block = bytearray()
                        while received < block_size:
                            chunk = conn.recv(min(block_size - received, 4096))
                            if not chunk:
                                raise ConnectionError("Incomplete block received")
                            block.extend(chunk)
                            received += len(chunk)
                        f.write(block)
                        
            # Update version after successful transfer
            file_versions[file_key] = metadata["version"]
            save_versions()
            
            print(f"Received: {metadata['path']}")
    except Exception as e:
        print(f"Error handling connection: {e}")
    finally:
        conn.close()

def receive_server(stop_event):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
                if not stop_event.is_set():
                    print(f"Server error: {e}")

def full_sync(stop_event):
    while not stop_event.is_set():
        time.sleep(SYNC_INTERVAL)
        try:
            handler = SyncHandler()
            handler.sync_directory(WATCH_FOLDER)
        except Exception as e:
            print(f"Full sync error: {e}")

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
            load_versions()
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
    load_versions()
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
        save_versions()

if __name__ == '__main__':
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(FileSyncService)
        servicemanager.StartServiceCtrlDispatcher()
    elif len(sys.argv) > 1 and sys.argv[1] == "debug":
        run_as_console()
    else:
        win32serviceutil.HandleCommandLine(FileSyncService)