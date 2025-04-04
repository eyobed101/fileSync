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
REMOTE_IP = "192.168.137.75"  # Remote machine's IP
REMOTE_PORT = 5000
LOCAL_PORT = 5000
RETRY_COUNT = 5
RETRY_DELAY = 2  # Initial delay for retries, in seconds

# Track received files to prevent re-sending
received_files = set()

def cleanup_old_temp_dirs():
    """Removes old _MEI* temp folders to avoid PyInstaller warnings."""
    temp_dir = tempfile.gettempdir()
    servicemanager.LogInfoMsg("Cleaning up old temporary directories.")
    for folder in os.listdir(temp_dir):
        if folder.startswith("_MEI"):
            folder_path = os.path.join(temp_dir, folder)
            try:
                shutil.rmtree(folder_path)
                servicemanager.LogInfoMsg(f"Deleted old temp folder: {folder_path}")
            except Exception as e:
                servicemanager.LogErrorMsg(f"Failed to delete {folder_path}: {e}")

class FileTransferHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            file_name = os.path.basename(event.src_path)

            if file_name in received_files:
                servicemanager.LogInfoMsg(f"Ignoring received file: {file_name}")
                return  # Ignore files that were received from the remote peer

            servicemanager.LogInfoMsg(f"File created: {file_name}. Preparing to send.")
            self.send_file(event.src_path)

    def send_file(self, file_path):
        """Retries to send file with backoff strategy."""
        attempt = 0
        while attempt < RETRY_COUNT:
            try:
                servicemanager.LogInfoMsg(f"Attempting to send file: {file_path}, Attempt: {attempt + 1}")
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(10)  # Set a timeout for socket connection to avoid indefinite block
                    s.connect((REMOTE_IP, REMOTE_PORT))
                    with open(file_path, 'rb') as file:
                        s.sendall(file.read())
                servicemanager.LogInfoMsg(f"Sent file successfully: {file_path}")
                return  # Success, exit the function
            except socket.timeout:
                attempt += 1
                servicemanager.LogErrorMsg(f"Socket timeout while sending file {file_path} (attempt {attempt}/{RETRY_COUNT})")
            except Exception as e:
                attempt += 1
                servicemanager.LogErrorMsg(f"Error sending file {file_path} (attempt {attempt}/{RETRY_COUNT}): {e}")
            time.sleep(RETRY_DELAY * attempt)  # Exponential backoff
        servicemanager.LogErrorMsg(f"Failed to send file {file_path} after {RETRY_COUNT} attempts.")

def receive_file():
    try:
        servicemanager.LogInfoMsg("Starting to receive files.")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.bind(("0.0.0.0", LOCAL_PORT))
            server_socket.listen(5)
            servicemanager.LogInfoMsg(f"Listening for incoming connections on port {LOCAL_PORT}.")

            while True:
                conn, addr = server_socket.accept()
                sender_ip = addr[0]
                servicemanager.LogInfoMsg(f"Connection established with {sender_ip}.")

                with conn:
                    file_name = f"received_{int(time.time())}.txt"
                    file_path = os.path.join(WATCH_FOLDER, file_name)

                    with open(file_path, 'wb') as file:
                        while chunk := conn.recv(4096):
                            file.write(chunk)

                    if sender_ip == REMOTE_IP:
                        received_files.add(file_name)  # Mark as received from trusted source
                        servicemanager.LogInfoMsg(f"Ignored file from trusted peer: {file_path}")
                    else:
                        servicemanager.LogInfoMsg(f"Received file from unknown source {sender_ip}: {file_path}")
    except Exception as e:
        servicemanager.LogErrorMsg(f"Error in receive_file: {e}")

class FileSyncServiceBase(win32serviceutil.ServiceFramework):
    _svc_name_ = "FileSyncService"
    _svc_display_name_ = "File Synchronization Service"
    _svc_description_ = "Synchronizes files between machines while avoiding loops."
    @classmethod
    def parse_command_line(self):
        win32serviceutil.HandleCommandLine(self)
    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        servicemanager.LogInfoMsg("Stopping FileSyncService.")
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop()
        win32event.SetEvent(self.hWaitStop)
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)
        servicemanager.LogInfoMsg("FileSyncService stopped.")

    def SvcDoRun(self):
        self.start()
        servicemanager.LogInfoMsg("FileSyncService started.")
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ''))
        self.main()
        # Cleanup temp folders
        # cleanup_old_temp_dirs()

        # observer = Observer()
        # observer.schedule(FileTransferHandler(), WATCH_FOLDER, recursive=False)
        # observer.start()
        # servicemanager.LogInfoMsg("Started observer to watch folder.")

        # receiver_thread = threading.Thread(target=receive_file)
        # receiver_thread.daemon = True
        # receiver_thread.start()
        # servicemanager.LogInfoMsg("Started receiver thread.")

        # try:
        #     servicemanager.LogInfoMsg("Waiting for stop signal...")
        #     win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
        # except Exception as e:
        #     servicemanager.LogErrorMsg(f"Error in FileSyncService wait: {e}")
        # finally:
        #     observer.stop()
        #     observer.join()
        #     servicemanager.LogInfoMsg("FileSyncService shutting down.")
    def start(self):
        pass
        
    def stop(self):
        pass
    def main(self):
        pass
    
    
class FileSyncService(FileSyncServiceBase):

   
    _svc_name_ = "FileSyncService"
    _svc_display_name_ = "File Synchronization Service"
    _svc_description_ = "Synchronizes files between machines while avoiding loops."

    
    def start(self):
        self.isrunning = True
        # Cleanup temp folders
        cleanup_old_temp_dirs()

        observer = Observer()
        observer.schedule(FileTransferHandler(), WATCH_FOLDER, recursive=False)
        observer.start()
        servicemanager.LogInfoMsg("Started observer to watch folder.")

        receiver_thread = threading.Thread(target=receive_file)
        receiver_thread.daemon = True
        receiver_thread.start()
        servicemanager.LogInfoMsg("Started receiver thread.")

        try:
            servicemanager.LogInfoMsg("Waiting for stop signal...")
            win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
        except Exception as e:
            servicemanager.LogErrorMsg(f"Error in FileSyncService wait: {e}")
        finally:
            observer.stop()
            observer.join()
            servicemanager.LogInfoMsg("FileSyncService shutting down.")       
       
        

    def stop(self):
        self.isrunning = False

    def main(self):
         while self.isrunning:
              time.sleep(1)


if __name__ == '__main__':
    # For running the service normally
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(FileSyncService)
        servicemanager.StartServiceCtrlDispatcher()

    # For handling command line service commands
    else:
        win32serviceutil.HandleCommandLine(FileSyncService)
