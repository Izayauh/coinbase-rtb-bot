import pytest

from bot.single_instance import AlreadyRunningError, SingleInstance


def test_second_runtime_lock_is_rejected(tmp_path):
    path = tmp_path / "runtime.lock"
    first = SingleInstance(path)
    second = SingleInstance(path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()


def test_lock_can_be_reacquired_after_release(tmp_path):
    path = tmp_path / "runtime.lock"
    first = SingleInstance(path)
    first.acquire()
    first.release()

    second = SingleInstance(path)
    second.acquire()
    second.release()


def test_pid_sidecar_tracks_owner_and_is_removed_on_release(tmp_path):
    lock = SingleInstance(tmp_path / "runtime.lock")
    lock.acquire()
    try:
        assert int(lock.pid_path.read_text(encoding="ascii").strip()) > 0
    finally:
        lock.release()
    assert not lock.pid_path.exists()
