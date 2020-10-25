# -*- coding: utf-8 -*-
# test_stirring
import time
import pytest
from morbidostat.background_jobs.stirring import stirring, Stirrer
from morbidostat.whoami import unit, experiment as exp
from morbidostat.pubsub import publish


def pause():
    # to avoid race conditions
    time.sleep(0.5)


def test_stirring_runs():
    stirring(50, verbose=2, duration=0.1)


def test_change_stirring_mid_cycle():
    original_dc = 50

    st = Stirrer(original_dc, unit, exp, verbose=2)
    assert st.duty_cycle == original_dc
    pause()

    new_dc = 75
    publish(f"morbidostat/{unit}/{exp}/stirring/duty_cycle/set", new_dc)

    pause()

    assert st.duty_cycle == new_dc
    assert st.active == 1

    publish(f"morbidostat/{unit}/{exp}/stirring/duty_cycle/set", 0)
    pause()
    assert st.duty_cycle == 0
    assert st.active == 0
    pause()


def test_pause_stirring_mid_cycle():
    original_dc = 50

    st = Stirrer(original_dc, unit, exp, verbose=2)
    assert st.duty_cycle == original_dc
    pause()

    publish(f"morbidostat/{unit}/{exp}/stirring/active/set", 0)
    pause()

    assert st.duty_cycle == 0

    publish(f"morbidostat/{unit}/{exp}/stirring/active/set", 1)
    pause()

    assert st.duty_cycle == 50
