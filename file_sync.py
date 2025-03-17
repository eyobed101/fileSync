import os
import time
import socket
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import servicemanager
import win32service
import win32serviceutil
import win32event

# Configuration
WATCH_FOLDER = "C:/sample"
REMOTE_IP = "192.168.1.100"  # Change this to the other machine's IP
REMOTE_PORT = 5000
LOCAL_PORT = 5000

class FileTransferHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            self.send_file(event.src_path)

    def send_file(self, file_path):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((REMOTE_IP, REMOTE_PORT))
                with open(file_path, 'rb') as file:
                    s.sendall(file.read())
        except Exception as e:
            print(f"Error sending file {file_path}: {e}")

def receive_file():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind(("0.0.0.0", LOCAL_PORT))
        server_socket.listen(5)
        while True:
            conn, addr = server_socket.accept()
            with conn:
                file_path = os.path.join(WATCH_FOLDER, f"received_{int(time.time())}.txt")
                with open(file_path, 'wb') as file:
                    while chunk := conn.recv(4096):
                        file.write(chunk)

class FileSyncService(win32serviceutil.ServiceFramework):
    _svc_name_ = "FileSyncService"
    _svc_display_name_ = "File Synchronization Service"

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              0xF000, ("Starting File Sync Service", ""))

        # Start File Watcher
        observer = Observer()
        observer.schedule(FileTransferHandler(), WATCH_FOLDER, recursive=False)
        observer.start()

        # Start File Receiver
        receiver_thread = threading.Thread(target=receive_file)
        receiver_thread.daemon = True
        receiver_thread.start()

        try:
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
        finally:
            observer.stop()
            observer.join()

if __name__ == '__main__':
    if len(os.sys.argv) == 1:
        win32serviceutil.HandleCommandLine(FileSyncService)
    else:
        win32serviceutil.HandleCommandLine(FileSyncService)
