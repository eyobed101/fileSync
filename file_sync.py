import os
import time
import socket
import threading
import shutil
import tempfile
import servicemanager
import win32service
import win32serviceutil
import win32event
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import sys

# Configuration
WATCH_FOLDER = "C:/sample"
REMOTE_IP = "192.168.137.1"  # Remote machine's IP
REMOTE_PORT = 5000
LOCAL_PORT = 5000
RETRY_COUNT = 5
RETRY_DELAY = 2  # Initial delay for retries, in seconds

# Track received files to prevent re-sending
received_files = set()

def cleanup_old_temp_dirs():
    """Removes old _MEI* temp folders to avoid PyInstaller warnings."""
    temp_dir = tempfile.gettempdir()
    print("Cleaning up old temporary directories.")
    for folder in os.listdir(temp_dir):
        if folder.startswith("_MEI"):
            folder_path = os.path.join(temp_dir, folder)
            try:
                shutil.rmtree(folder_path)
                print(f"Deleted old temp folder: {folder_path}")
            except Exception as e:
                print(f"Failed to delete {folder_path}: {e}")

class FileTransferHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            file_name = os.path.basename(event.src_path)

            if file_name in received_files:
                print(f"Ignoring received file: {file_name}")
                return  # Ignore files that were received from the remote peer

            print(f"File created: {file_name}. Preparing to send.")
            self.send_file(event.src_path)

    def send_file(self, file_path):
        """Retries to send file with backoff strategy."""
        attempt = 0
        while attempt < RETRY_COUNT:
            try:
                print(f"Attempting to send file: {file_path}, Attempt: {attempt + 1}")
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)  # Set a timeout for socket connection to avoid indefinite block
                    s.connect((REMOTE_IP, REMOTE_PORT))
                    with open(file_path, 'rb') as file:
                        s.sendall(file.read())
                print(f"Sent file successfully: {file_path}")
                return  # Success, exit the function
            except socket.timeout:
                attempt += 1
                print(f"Socket timeout while sending file {file_path} (attempt {attempt}/{RETRY_COUNT})")
            except Exception as e:
                attempt += 1
                print(f"Error sending file {file_path} (attempt {attempt}/{RETRY_COUNT}): {e}")
            time.sleep(RETRY_DELAY * attempt)  # Exponential backoff
        print(f"Failed to send file {file_path} after {RETRY_COUNT} attempts.")

def receive_file(stop_event):
    try:
        print("Starting to receive files.")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.settimeout(1)  # Add timeout to allow checking stop_event
            server_socket.bind(("0.0.0.0", LOCAL_PORT))
            server_socket.listen(5)
            print(f"Listening for incoming connections on port {LOCAL_PORT}.")

            while not stop_event.is_set():
                try:
                    conn, addr = server_socket.accept()
                    sender_ip = addr[0]
                    print(f"Connection established with {sender_ip}.")

                    with conn:
                        file_name = f"received_{int(time.time())}.txt"
                        file_path = os.path.join(WATCH_FOLDER, file_name)

                        with open(file_path, 'wb') as file:
                            while chunk := conn.recv(4096):
                                file.write(chunk)

                        if sender_ip == REMOTE_IP:
                            received_files.add(file_name)  # Mark as received from trusted source
                            print(f"Ignored file from trusted peer: {file_path}")
                        else:
                            print(f"Received file from unknown source {sender_ip}: {file_path}")
                except socket.timeout:
                    continue  # This is expected due to our timeout setting
                except Exception as e:
                    print(f"Error in receive_file connection handling: {e}")
    except Exception as e:
        print(f"Error in receive_file: {e}")

class FileSyncService(win32serviceutil.ServiceFramework):
    _svc_name_ = "FileSyncService"
    _svc_display_name_ = "File Synchronization Service"
    _svc_description_ = "Synchronizes files between machines while avoiding loops."

    def __init__(self, args):
        if not args:  # Handle case when args is empty (debug mode)
            args = [sys.argv[0]]  # Provide at least one argument
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.stop_event = threading.Event()
        self.observer = None
        self.receiver_thread = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        print("Stopping FileSyncService...")
        self.stop_event.set()
        if self.observer:
            self.observer.stop()
        win32event.SetEvent(self.hWaitStop)
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)
        print("FileSyncService stopped.")

    def SvcDoRun(self):
        self.ReportServiceStatus(win32service.SERVICE_START_PENDING)
        try:
            print("Starting FileSyncService...")
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            self.main()
        except Exception as e:
            print(f"Service failed to start: {e}")
            self.SvcStop()

    def main(self):
        # Cleanup temp folders
        cleanup_old_temp_dirs()

        # Initialize file observer
        self.observer = Observer()
        self.observer.schedule(FileTransferHandler(), WATCH_FOLDER, recursive=False)
        self.observer.start()
        print("Started observer to watch folder.")

        # Start receiver thread
        self.stop_event.clear()
        self.receiver_thread = threading.Thread(target=receive_file, args=(self.stop_event,))
        self.receiver_thread.daemon = True
        self.receiver_thread.start()
        print("Started receiver thread.")

        # Main service loop
        try:
            while not self.stop_event.is_set():
                time.sleep(1)
                
                # Check if our threads are still alive
                if not self.receiver_thread.is_alive():
                    print("Receiver thread died, restarting...")
                    self.receiver_thread = threading.Thread(target=receive_file, args=(self.stop_event,))
                    self.receiver_thread.daemon = True
                    self.receiver_thread.start()
        except Exception as e:
            print(f"Error in service main loop: {e}")
        finally:
            # Cleanup
            if self.observer:
                self.observer.stop()
                self.observer.join()
            print("FileSyncService shutdown complete.")

def run_as_console():
    # Create a minimal service instance that won't trigger the argument error
    class DebugService(FileSyncService):
        def __init__(self):
            # Bypass the normal service initialization
            self.stop_event = threading.Event()
            self.observer = None
            self.receiver_thread = None
            
    service = DebugService()
    
    print("Running in console mode. Press Ctrl+C to stop.")
    try:
        service.main()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        service.stop_event.set()
        if service.observer:
            service.observer.stop()
            service.observer.join()
        print("Service stopped.")

if __name__ == '__main__':
    if len(sys.argv) == 1:
        # Run as service
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(FileSyncService)
        servicemanager.StartServiceCtrlDispatcher()
    elif len(sys.argv) > 1 and sys.argv[1] == "debug":
        # Run in console mode for debugging
        run_as_console()
    else:
        # Handle service installation/removal etc.
        win32serviceutil.HandleCommandLine(FileSyncService)