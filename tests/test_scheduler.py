"""Cooperative cancellation and single-cycle scheduling without API calls."""
from types import SimpleNamespace
import threading
import time

from scheduler import Scheduler


def test_pause_blocks_progress_until_resume_and_stop_interrupts_pause():
    stop,pause,completed = threading.Event(),threading.Event(),threading.Event()
    pause.set()
    results = []

    def wait_checkpoint():
        results.append(Scheduler.checkpoint(stop,pause))
        completed.set()

    thread = threading.Thread(target=wait_checkpoint)
    thread.start()
    assert not completed.wait(.05)
    pause.clear()
    assert completed.wait(1)
    thread.join(1)
    assert results == [True]
    pause.set()
    completed.clear()
    thread = threading.Thread(target=wait_checkpoint)
    thread.start()
    stop.set()
    assert completed.wait(1)
    thread.join(1)
    assert results[-1] is False


class FakeService:
    def __init__(self):
        self.config = SimpleNamespace(search_interval=60)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.refreshes = 0
        self.prepares = 0

    def refresh(self):
        self.refreshes += 1

    def prepare_pending(self,stop_event,pause_event,progress):
        self.prepares += 1
        self.entered.set()
        while not self.release.wait(.01):
            if stop_event.is_set():
                break
        return {"prepared":0,"stopped":stop_event.is_set()}


def test_only_one_cycle_can_run_and_stopped_scheduler_can_restart():
    service = FakeService()
    updates = []
    scheduler = Scheduler(service,updates.append)
    assert scheduler.start() is True
    assert service.entered.wait(1)
    assert scheduler.start() is False
    scheduler.pause()
    assert scheduler.pause_event.is_set()
    scheduler.resume()
    assert not scheduler.pause_event.is_set()
    scheduler.stop()
    assert not scheduler.running
    assert updates[-1]["stopped"] is True
    service.entered.clear()
    service.release.set()
    assert scheduler.start() is True
    assert service.entered.wait(1)
    scheduler.stop()
    assert service.prepares == 2


def test_periodic_wait_is_cancelled_promptly():
    service = FakeService()
    service.release.set()
    scheduler = Scheduler(service)
    scheduler.start(periodic=True)
    assert service.entered.wait(1)
    start = time.monotonic()
    scheduler.stop()
    assert time.monotonic()-start < 1
    assert not scheduler.running


def test_background_error_is_recorded_without_secret_message():
    class BrokenService:
        config = SimpleNamespace(search_interval=1)

        def refresh(self):
            raise RuntimeError("secret api token must not escape")

    updates = []
    scheduler = Scheduler(BrokenService(),updates.append)
    scheduler.start()
    scheduler._thread.join(1)
    assert scheduler.error == "RuntimeError"
    assert updates == [{"error":"RuntimeError"}]
