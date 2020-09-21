# -*- coding: utf-8 -*-
# remove waste
import time
from json import loads

import click
import RPi.GPIO as GPIO

from morbidostat.utils import pump_ml_to_duration
from morbidostat.utils import config, get_unit_from_hostname
from morbidostat.utils.pubsub import publish


def remove_waste(ml=None, duration=None, duty_cycle=33, verbose=False):
    assert 0 <= duty_cycle <= 100

    unit = get_unit_from_hostname()
    hz = 100

    try:
        GPIO.setmode(GPIO.BCM)

        WASTE_PIN = int(config["rpi_pins"]["waste"])
        GPIO.setup(WASTE_PIN, GPIO.OUT)
        GPIO.output(WASTE_PIN, 0)
        pwm = GPIO.PWM(WASTE_PIN, hz)

        pwm.start(duty_cycle)

        if ml is not None:
            assert ml >= 0
            duration = pump_ml_to_duration(ml, duty_cycle, **loads(config["pump_calibration"][f"waste{unit}_ml_calibration"]))

        assert duration >= 0
        time.sleep(duration)

        pwm.stop()
        GPIO.output(WASTE_PIN, 0)
        publish(f"morbidostat/{unit}/io_events", '{"volume_change": "-%s", "event": "remove_waste"}' % ml, verbose=verbose)

        publish(f"morbidostat/{unit}/log", "remove waste: %smL" % ml, verbose=verbose)
    except Exception as e:
        publish(f"morbidostat/{unit}/error_log", f"[remove_waste]: failed with {str(e)}", verbose=verbose)
        raise e

    finally:
        GPIO.cleanup()
    return


@click.command()
@click.option("--ml", type=float)
@click.option("--duration", type=float)
@click.option("--duty_cycle", default=33, type=int)
@click.option("--verbose", is_flag=True, help="print to std out")
def click_remove_waste(ml, duration, duty_cycle, verbose):
    assert (ml is not None) or (duration is not None)
    assert not ((ml is not None) and (duration is not None)), "Only select ml or duration"
    return remove_waste(ml, duration, duty_cycle, verbose)


if __name__ == "__main__":
    click_remove_waste()
