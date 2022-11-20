# -*- coding: utf-8 -*-
# test_utils
from __future__ import annotations

import time

from pioreactor.background_jobs.stirring import start_stirring
from pioreactor.utils import is_pio_job_running
from pioreactor.utils import local_intermittent_storage
from pioreactor.utils.timing import RepeatedTimer
from pioreactor.whoami import get_unit_name


def test_that_out_scope_caches_cant_access_keys_created_by_inner_scope_cache():
    """
    You can modify caches, and the last assignment is valid.
    """
    with local_intermittent_storage("test") as cache:
        for k in cache.iterkeys():
            del cache[k]

    with local_intermittent_storage("test") as cache1:
        cache1["A"] = "0"
        with local_intermittent_storage("test") as cache2:
            assert cache2["A"] == "0"
            cache2["B"] = "1"

        assert "B" not in cache1  # note this.
        cache1["B"] = "2"  # create, and overwritten.

    with local_intermittent_storage("test") as cache:
        assert cache["A"] == "0"
        assert cache["B"] == "2"


def test_caches_will_always_save_the_lastest_value_provided():
    with local_intermittent_storage("test") as cache:
        for k in cache.iterkeys():
            del cache[k]

    with local_intermittent_storage("test") as cache1:
        with local_intermittent_storage("test") as cache2:
            cache1["A"] = "1"
            cache2["A"] = "0"
            cache2["B"] = "2"

    with local_intermittent_storage("test") as cache:
        assert cache["A"] == "0"
        assert cache["B"] == "2"


def test_caches_will_delete_when_asked():
    with local_intermittent_storage("test") as cache:
        for k in cache.iterkeys():
            del cache[k]

    with local_intermittent_storage("test") as cache:
        cache["test"] = "1"

    with local_intermittent_storage("test") as cache:
        assert "test" in cache
        del cache["test"]
        assert "test" not in cache


def test_repeated_timer_will_not_execute_if_killed_during_run_immediatly_pause():
    class Counter:

        counter = 0

        def __init__(self):

            self.thread = RepeatedTimer(5, self.run, run_immediately=True, run_after=60).start()

        def run(self):
            self.counter += 1

    c = Counter()
    c.thread.join()

    assert c.counter == 0


def test_repeated_timer_run_immediately_works_as_intended():
    class Counter:

        counter = 0

        def __init__(self, run_immediately):

            self.thread = RepeatedTimer(
                5,
                self.run,
                run_immediately=run_immediately,
            ).start()

        def run(self):
            self.counter += 1

    c = Counter(run_immediately=True)
    time.sleep(6)
    c.thread.join()
    assert c.counter == 2

    c = Counter(run_immediately=False)
    time.sleep(6)
    c.thread.join()
    assert c.counter == 1


def test_repeated_timer_run_after_works_as_intended():
    class Counter:

        counter = 0

        def __init__(self, run_after):

            self.thread = RepeatedTimer(
                5, self.run, run_immediately=True, run_after=run_after
            ).start()

        def run(self):
            self.counter += 1

    c = Counter(run_after=0)
    time.sleep(3)
    c.thread.join()
    assert c.counter == 1

    c = Counter(run_after=5)
    time.sleep(3)
    c.thread.join()
    assert c.counter == 0


def test_repeated_timer_pause_works_as_intended():
    class Counter:

        counter = 0

        def __init__(self):

            self.thread = RepeatedTimer(
                3,
                self.run,
                run_immediately=True,
            ).start()

        def run(self):
            self.counter += 1

    c = Counter()
    time.sleep(4)
    assert c.counter == 2

    c.thread.pause()
    time.sleep(5)
    assert c.counter == 2
    c.thread.unpause()

    time.sleep(5)
    assert c.counter > 2


def test_is_pio_job_running_single():
    experiment = "test_is_pio_job_running_single"
    unit = get_unit_name()

    assert not is_pio_job_running("stirring")
    assert not is_pio_job_running("od_reading")

    with start_stirring(target_rpm=0, experiment=experiment, unit=unit):
        assert is_pio_job_running("stirring")
        assert not is_pio_job_running("od_reading")

    assert not is_pio_job_running("stirring")
    assert not is_pio_job_running("od_reading")


def test_is_pio_job_running_multiple():
    experiment = "test_is_pio_job_running_multiple"
    unit = get_unit_name()

    assert not any(is_pio_job_running(["stirring", "od_reading"]))

    with start_stirring(target_rpm=0, experiment=experiment, unit=unit):
        assert any(is_pio_job_running(["stirring", "od_reading"]))
        assert is_pio_job_running(["stirring", "od_reading"]) == [True, False]
        assert is_pio_job_running(["od_reading", "stirring"]) == [False, True]

    assert not any(is_pio_job_running(["stirring", "od_reading"]))
