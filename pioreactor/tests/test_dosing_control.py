# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import time
from typing import Any

from pioreactor import pubsub
from pioreactor.automations import DosingAutomation
from pioreactor.automations import events
from pioreactor.automations.dosing.base import AltMediaCalculator
from pioreactor.automations.dosing.continuous_cycle import ContinuousCycle
from pioreactor.automations.dosing.morbidostat import Morbidostat
from pioreactor.automations.dosing.pid_morbidostat import PIDMorbidostat
from pioreactor.automations.dosing.pid_turbidostat import PIDTurbidostat
from pioreactor.automations.dosing.silent import Silent
from pioreactor.automations.dosing.turbidostat import Turbidostat
from pioreactor.background_jobs.dosing_control import DosingController
from pioreactor.utils import local_persistant_storage
from pioreactor.whoami import get_latest_experiment_name
from pioreactor.whoami import get_unit_name


unit = get_unit_name()
experiment = get_latest_experiment_name()


def pause() -> None:
    # to avoid race conditions when updating state
    time.sleep(0.5)


def setup_function() -> None:
    with local_persistant_storage("pump_calibration") as cache:
        cache["media_ml_calibration"] = json.dumps(
            {"duration_": 1.0, "bias_": 0, "dc": 60, "hz": 100, "timestamp": "2010-01-01"}
        )
        cache["alt_media_ml_calibration"] = json.dumps(
            {"duration_": 1.0, "bias_": 0, "dc": 60, "hz": 100, "timestamp": "2010-01-01"}
        )
        cache["waste_ml_calibration"] = json.dumps(
            {"duration_": 1.0, "bias_": 0, "dc": 60, "hz": 100, "timestamp": "2010-01-01"}
        )


def test_silent_automation() -> None:
    with Silent(volume=None, duration=60, unit=unit, experiment=experiment) as algo:
        pause()
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.01}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 1.0}',
        )
        pause()
        assert algo.run() is None

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.02}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 1.1}',
        )
        pause()
        assert algo.run() is None


def test_turbidostat_automation() -> None:
    target_od = 1.0
    with Turbidostat(
        target_od=target_od, duration=60, volume=0.25, unit=unit, experiment=experiment
    ) as algo:

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.01}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 0.98}',
        )
        pause()
        assert isinstance(algo.run(), events.NoEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.01}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 1.0}',
        )
        pause()
        assert isinstance(algo.run(), events.DilutionEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.01}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 1.01}',
        )
        pause()
        assert isinstance(algo.run(), events.DilutionEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.01}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 0.99}',
        )
        pause()
        assert isinstance(algo.run(), events.NoEvent)


def test_pid_turbidostat_automation() -> None:

    target_od = 2.4
    with PIDTurbidostat(
        target_od=target_od, duration=20, unit=unit, experiment=experiment
    ) as algo:

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.01}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 2.6}',
        )
        pause()
        e = algo.run()
        assert isinstance(e, events.DilutionEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.01}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 2.8}',
        )
        pause()
        e = algo.run()
        assert isinstance(e, events.DilutionEvent)


def test_morbidostat_automation() -> None:
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        None,
        retain=True,
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        None,
        retain=True,
    )

    target_od = 1.0
    algo = Morbidostat(
        target_od=target_od, duration=60, volume=0.25, unit=unit, experiment=experiment
    )

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.01}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.95}',
    )
    pause()
    assert isinstance(algo.run(), events.NoEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.01}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.99}',
    )
    pause()
    assert isinstance(algo.run(), events.DilutionEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.01}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 1.05}',
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.01}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 1.03}',
    )
    pause()
    assert isinstance(algo.run(), events.DilutionEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.01}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 1.04}',
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.01}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.99}',
    )
    pause()
    assert isinstance(algo.run(), events.DilutionEvent)
    algo.set_state(algo.DISCONNECTED)


def test_pid_morbidostat_automation() -> None:
    target_growth_rate = 0.09
    algo = PIDMorbidostat(
        target_od=1.0,
        target_growth_rate=target_growth_rate,
        duration=60,
        unit=unit,
        experiment=experiment,
    )

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.08}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.5}',
    )
    pause()
    assert isinstance(algo.run(), events.NoEvent)
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.08}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.95}',
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.07}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.95}',
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.065}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.95}',
    )
    pause()
    assert isinstance(algo.run(), events.AddAltMediaEvent)
    algo.set_state(algo.DISCONNECTED)


def test_changing_morbidostat_parameters_over_mqtt() -> None:

    target_growth_rate = 0.05
    algo = PIDMorbidostat(
        target_growth_rate=target_growth_rate,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.target_growth_rate == target_growth_rate
    pause()
    new_target = 0.07
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_automation/target_growth_rate/set",
        new_target,
    )
    pause()
    assert algo.target_growth_rate == new_target
    assert algo.pid.pid.setpoint == new_target
    algo.set_state(algo.DISCONNECTED)


def test_changing_turbidostat_params_over_mqtt() -> None:

    og_volume = 0.5
    og_target_od = 1.0
    algo = Turbidostat(
        volume=og_volume,
        target_od=og_target_od,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.volume == og_volume

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.05}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 1.0}',
    )
    pause()
    algo.run()

    pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_automation/volume/set", 1.0)
    pause()

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.05}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 1.0}',
    )
    algo.run()

    assert algo.volume == 1.0

    new_od = 1.5
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_automation/target_od/set", new_od
    )
    pause()
    assert algo.target_od == new_od
    algo.set_state(algo.DISCONNECTED)


def test_changing_parameters_over_mqtt_with_unknown_parameter() -> None:

    with DosingAutomation(
        target_growth_rate=0.05,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    ):

        logs = []

        def append_logs(msg):
            if "garbage" in msg.payload.decode():
                logs.append(msg.payload)

        pubsub.subscribe_and_callback(
            append_logs, f"pioreactor/{unit}/{experiment}/logs/app"
        )

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/garbage/set", 0.07
        )
        # there should be a log published with "Unable to set garbage in dosing_automation"
        pause()

        assert len(logs) > 0


def test_pause_in_dosing_automation() -> None:

    with DosingAutomation(
        target_growth_rate=0.05,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    ) as algo:
        pause()
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/$state/set", "sleeping"
        )
        pause()
        assert algo.state == "sleeping"

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/$state/set", "ready"
        )
        pause()
        assert algo.state == "ready"


def test_pause_in_dosing_control_also_pauses_automation() -> None:

    algo = DosingController(
        "turbidostat",
        target_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    )
    pause()
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_control/$state/set", "sleeping"
    )
    pause()
    assert algo.state == "sleeping"
    assert algo.automation_job.state == "sleeping"

    pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_control/$state/set", "ready")
    pause()
    assert algo.state == "ready"
    assert algo.automation_job.state == "ready"
    algo.set_state(algo.DISCONNECTED)


def test_old_readings_will_not_execute_io() -> None:
    algo = DosingAutomation(
        target_growth_rate=0.05,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    algo._latest_growth_rate = 1
    algo._latest_od = 1

    algo.latest_od_at = time.time() - 10 * 60
    algo.latest_growth_rate_at = time.time() - 4 * 60

    assert algo.most_stale_time == algo.latest_od_at

    assert isinstance(algo.run(), events.NoEvent)
    algo.set_state(algo.DISCONNECTED)


def test_throughput_calculator() -> None:
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = "0.0"

    algo = DosingController(
        "pid_morbidostat",
        target_growth_rate=0.05,
        target_od=1.0,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.automation_job.media_throughput == 0
    pause()
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.08}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 1.0}',
    )
    pause()
    algo.automation_job.run()

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.08}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.95}',
    )
    pause()
    algo.automation_job.run()
    assert algo.automation_job.media_throughput > 0
    assert algo.automation_job.alt_media_throughput > 0

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.07}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.95}',
    )
    pause()
    algo.automation_job.run()
    assert algo.automation_job.media_throughput > 0
    assert algo.automation_job.alt_media_throughput > 0

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.065}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.95}',
    )
    pause()
    algo.automation_job.run()
    assert algo.automation_job.media_throughput > 0
    assert algo.automation_job.alt_media_throughput > 0
    algo.set_state(algo.DISCONNECTED)


def test_throughput_calculator_restart() -> None:

    with local_persistant_storage("media_throughput") as c:
        c[experiment] = str(1.0)

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = str(1.5)

    with DosingController(
        "turbidostat",
        target_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:
        pause()
        assert algo.automation_job.media_throughput == 1.0
        assert algo.automation_job.alt_media_throughput == 1.5


def test_throughput_calculator_manual_set() -> None:

    with local_persistant_storage("media_throughput") as c:
        c[experiment] = str(1.0)

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = str(1.5)

    with DosingController(
        "turbidostat",
        target_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:

        pause()
        assert algo.automation_job.media_throughput == 1.0
        assert algo.automation_job.alt_media_throughput == 1.5

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/alt_media_throughput/set",
            0,
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/media_throughput/set", 0
        )
        pause()
        pause()
        assert algo.automation_job.media_throughput == 0
        assert algo.automation_job.alt_media_throughput == 0


def test_execute_io_action() -> None:
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = "0.0"

    with DosingController("silent", unit=unit, experiment=experiment) as ca:
        ca.automation_job.execute_io_action(
            media_ml=0.65, alt_media_ml=0.35, waste_ml=0.65 + 0.35
        )
        pause()
        assert ca.automation_job.media_throughput == 0.65
        assert ca.automation_job.alt_media_throughput == 0.35

        ca.automation_job.execute_io_action(
            media_ml=0.15, alt_media_ml=0.15, waste_ml=0.3
        )
        pause()
        assert ca.automation_job.media_throughput == 0.80
        assert ca.automation_job.alt_media_throughput == 0.50

        ca.automation_job.execute_io_action(media_ml=1.0, alt_media_ml=0, waste_ml=1)
        pause()
        assert ca.automation_job.media_throughput == 1.80
        assert ca.automation_job.alt_media_throughput == 0.50

        ca.automation_job.execute_io_action(media_ml=0.0, alt_media_ml=1.0, waste_ml=1)
        pause()
        assert ca.automation_job.media_throughput == 1.80
        assert ca.automation_job.alt_media_throughput == 1.50


def test_execute_io_action2() -> None:
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = "0.0"

    with DosingController("silent", unit=unit, experiment=experiment) as ca:
        ca.automation_job.execute_io_action(
            media_ml=1.25, alt_media_ml=0.01, waste_ml=1.26
        )
        pause()
        assert ca.automation_job.media_throughput == 1.25
        assert ca.automation_job.alt_media_throughput == 0.01
        assert abs(ca.automation_job.alt_media_fraction - 0.0007142) < 0.000001


def test_execute_io_action_outputs1() -> None:
    # regression test
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = "0.0"

    ca = DosingAutomation(unit=unit, experiment=experiment)
    result = ca.execute_io_action(media_ml=1.25, alt_media_ml=0.01, waste_ml=1.26)
    assert result[0] == 1.25
    assert result[1] == 0.01
    assert result[2] == 1.26
    ca.set_state(ca.DISCONNECTED)


def test_execute_io_action_outputs_will_be_null_if_calibration_is_not_defined() -> None:
    # regression test
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("pump_calibration") as cache:
        del cache["media_ml_calibration"]
        del cache["alt_media_ml_calibration"]

    ca = DosingAutomation(unit=unit, experiment=experiment, skip_first_run=True)
    result = ca.execute_io_action(media_ml=1.0, alt_media_ml=1.0, waste_ml=2.0)
    assert result[0] == 0
    assert result[1] == 0.0
    assert result[2] == 2.0
    ca.set_state(ca.DISCONNECTED)

    # add back to cache
    with local_persistant_storage("pump_calibration") as cache:
        cache["media_ml_calibration"] = '{"duration_" : 1.0}'
        cache["alt_media_ml_calibration"] = '{"duration_" : 1.0}'


def test_execute_io_action_outputs_will_shortcut_if_disconnected() -> None:
    # regression test
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = "0.0"

    ca = DosingAutomation(unit=unit, experiment=experiment)
    ca.set_state(ca.DISCONNECTED)
    result = ca.execute_io_action(media_ml=1.25, alt_media_ml=0.01, waste_ml=1.26)
    assert result[0] == 0.0
    assert result[1] == 0.0
    assert result[2] == 0.0


def test_duration_and_timer() -> None:
    algo = PIDMorbidostat(
        target_od=1.0,
        target_growth_rate=0.01,
        duration=5 / 60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.latest_event is None
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.08}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.5}',
    )
    time.sleep(10)
    pause()
    assert isinstance(algo.latest_event, events.NoEvent)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 0.08}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 0.95}',
    )
    time.sleep(10)
    pause()
    assert isinstance(algo.latest_event, events.AddAltMediaEvent)
    algo.set_state(algo.DISCONNECTED)


def test_changing_duration_over_mqtt() -> None:
    with PIDMorbidostat(
        target_od=1.0,
        target_growth_rate=0.01,
        duration=5 / 60,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.latest_event is None
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.08}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 0.5}',
        )
        time.sleep(10)

        assert isinstance(algo.latest_event, events.NoEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/duration/set",
            1,  # in minutes
        )
        time.sleep(10)
        assert algo.run_thread.interval == 60  # in seconds


def test_changing_duration_over_mqtt_will_start_next_run_earlier() -> None:
    with PIDMorbidostat(
        target_od=1.0,
        target_growth_rate=0.01,
        duration=10 / 60,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.latest_event is None
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
            '{"growth_rate": 0.08}',
        )
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
            '{"od_filtered": 0.5}',
        )
        time.sleep(15)

        assert isinstance(algo.latest_event, events.NoEvent)

        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/duration/set",
            15 / 60,  # in minutes
        )
        time.sleep(5)
        assert algo.run_thread.interval == 15  # in seconds
        assert algo.run_thread.run_after > 0


def test_changing_algo_over_mqtt_solo() -> None:

    with DosingController(
        "turbidostat",
        target_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.automation["automation_name"] == "turbidostat"
        assert isinstance(algo.automation_job, Turbidostat)
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
            '{"automation_name": "pid_morbidostat", "duration": 60, "target_od": 1.0, "target_growth_rate": 0.07}',
        )
        time.sleep(8)
        assert algo.automation["automation_name"] == "pid_morbidostat"
        assert isinstance(algo.automation_job, PIDMorbidostat)
        assert algo.automation_job.target_growth_rate == 0.07


def test_changing_algo_over_mqtt_when_it_fails_will_rollback() -> None:

    with DosingController(
        "turbidostat",
        target_od=1.0,
        duration=5 / 60,
        volume=1.0,
        unit=unit,
        experiment=experiment,
    ) as algo:
        assert algo.automation["automation_name"] == "turbidostat"
        assert isinstance(algo.automation_job, Turbidostat)
        pause()
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
            '{"automation_name": "pid_morbidostat", "duration": 60}',
        )
        time.sleep(10)
        assert algo.automation["automation_name"] == "turbidostat"
        assert isinstance(algo.automation_job, Turbidostat)
        assert algo.automation_job.target_od == 1.0
        pause()
        pause()
        pause()


def test_changing_algo_over_mqtt_will_not_produce_two_dosing_jobs() -> None:
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_throughput") as c:
        c[experiment] = "0.0"

    with local_persistant_storage("alt_media_fraction") as c:
        c[experiment] = "0.0"

    algo = DosingController(
        "pid_turbidostat",
        volume=1.0,
        target_od=0.4,
        duration=60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.automation["automation_name"] == "pid_turbidostat"
    pause()
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
        '{"automation_name": "turbidostat", "duration": 60, "target_od": 1.0, "volume": 1.0, "skip_first_run": 1}',
    )
    time.sleep(
        10
    )  # need to wait for all jobs to disconnect correctly and threads to join.
    assert isinstance(algo.automation_job, Turbidostat)

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        '{"growth_rate": 1.0}',
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        '{"od_filtered": 1.0}',
    )
    pause()

    # note that we manually run, as we have skipped the first run in the json
    algo.automation_job.run()
    time.sleep(5)
    assert algo.automation_job.media_throughput == 1.0

    pubsub.publish(f"pioreactor/{unit}/{experiment}/dosing_automation/target_od/set", 1.5)
    pause()
    pause()
    assert algo.automation_job.target_od == 1.5
    algo.set_state(algo.DISCONNECTED)


def test_changing_algo_over_mqtt_with_wrong_type_is_okay() -> None:
    with local_persistant_storage("media_throughput") as c:
        c[experiment] = "0.0"

    algo = DosingController(
        "pid_turbidostat",
        volume=1.0,
        target_od=0.4,
        duration=2 / 60,
        unit=unit,
        experiment=experiment,
    )
    assert algo.automation["automation_name"] == "pid_turbidostat"
    assert algo.automation_name == "pid_turbidostat"
    pause()
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_control/automation/set",
        '{"automation_name": "pid_turbidostat", "duration": "60", "target_od": "1.0", "volume": "1.0"}',
    )
    time.sleep(
        7
    )  # need to wait for all jobs to disconnect correctly and threads to join.
    assert isinstance(algo.automation_job, PIDTurbidostat)
    assert algo.automation_job.target_od == 1.0
    algo.set_state(algo.DISCONNECTED)


def test_disconnect_cleanly() -> None:

    algo = DosingController(
        "turbidostat",
        target_od=1.0,
        duration=50,
        unit=unit,
        volume=1.0,
        experiment=experiment,
    )
    assert algo.automation["automation_name"] == "turbidostat"
    assert isinstance(algo.automation_job, Turbidostat)
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/dosing_control/$state/set", "disconnected"
    )
    time.sleep(10)
    assert algo.state == algo.DISCONNECTED


def test_custom_class_will_register_and_run() -> None:
    class NaiveTurbidostat(DosingAutomation):

        automation_name = "naive_turbidostat"
        published_settings = {
            "target_od": {"datatype": "float", "settable": True, "unit": "AU"},
            "duration": {"datatype": "float", "settable": True, "unit": "min"},
        }

        def __init__(self, target_od: float, **kwargs: Any) -> None:
            super(NaiveTurbidostat, self).__init__(**kwargs)
            self.target_od = target_od

        def execute(self) -> None:
            if self.latest_od > self.target_od:
                self.execute_io_action(media_ml=1.0, waste_ml=1.0)

    algo = DosingController(
        "naive_turbidostat",
        target_od=2.0,
        duration=10,
        unit=get_unit_name(),
        experiment=get_latest_experiment_name(),
    )
    algo.set_state(algo.DISCONNECTED)


def test_what_happens_when_no_od_data_is_coming_in() -> None:

    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/growth_rate",
        None,
        retain=True,
    )
    pubsub.publish(
        f"pioreactor/{unit}/{experiment}/growth_rate_calculating/od_filtered",
        None,
        retain=True,
    )

    algo = Turbidostat(
        target_od=0.1, duration=40 / 60, volume=0.25, unit=unit, experiment=experiment
    )
    pause()
    event = algo.run()
    assert isinstance(event, events.ErrorOccurred)
    algo.set_state(algo.DISCONNECTED)


def test_changing_duty_cycle_over_mqtt() -> None:
    with ContinuousCycle(unit=unit, experiment=experiment) as algo:

        assert algo.duty_cycle == 100
        pubsub.publish(
            f"pioreactor/{unit}/{experiment}/dosing_automation/duty_cycle/set", 50
        )
        pause()
        assert algo.duty_cycle == 50


def test_AltMediaCalculator() -> None:

    ac = AltMediaCalculator()

    data = {"volume_change": 1.0, "event": "media_pump"}
    assert 0.0 == ac.update(data, 0.0)

    data = {"volume_change": 1.0, "event": "alt_media_pump"}
    assert 1 / 14.0 == 0.07142857142857142 == ac.update(data, 0.0)

    data = {"volume_change": 1.0, "event": "alt_media_pump"}
    assert 0.13775510204081634 == ac.update(data, 1 / 14.0) < 2 / 14.0
