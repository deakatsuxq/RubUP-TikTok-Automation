"""Cooperative, bounded scheduling; durable task states live in SQLite."""
import threading
import time
from pathlib import Path


class ProcessLock:
    """Nonblocking OS lock, automatically released even after process failure."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            import os
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.file = stream
        return True

    def release(self):
        if self.file is None:
            return
        import os
        stream, self.file = self.file, None
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class Scheduler:
    def __init__(self, service, on_progress=None):
        self.service = service
        self.on_progress = on_progress or (lambda value: None)
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()  # set means paused
        self._thread = None
        self._lock = threading.Lock()
        self.error = None

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self, periodic=False, prepare=True):
        with self._lock:
            if self.running:
                return False
            self.stop_event.clear()
            self.pause_event.clear()
            self.error = None
            self._thread = threading.Thread(
                target=self._run, args=(periodic, prepare), daemon=False,
                name='RubUP-scheduler',
            )
            self._thread.start()
            return True

    def _run(self, periodic, prepare):
        try:
            while not self.stop_event.is_set():
                self.service.refresh()
                if prepare:
                    result = self.service.prepare_pending(
                        self.stop_event, self.pause_event, self.on_progress,
                    )
                    self.on_progress(result)
                if not periodic or self.stop_event.wait(self.service.config.search_interval):
                    break
        except Exception as exc:
            # Do not retain potentially secret-bearing exception messages.
            self.error = type(exc).__name__
            self.on_progress({'error': self.error})

    def pause(self):
        self.pause_event.set()

    def resume(self):
        self.pause_event.clear()

    def stop(self, wait=True):
        self.stop_event.set()
        self.pause_event.clear()
        if wait and self._thread and self._thread is not threading.current_thread():
            self._thread.join()

    @staticmethod
    def checkpoint(stop_event=None, pause_event=None):
        while pause_event and pause_event.is_set():
            if stop_event and stop_event.wait(.1):
                return False
            if not stop_event:
                time.sleep(.1)
        return not (stop_event and stop_event.is_set())

