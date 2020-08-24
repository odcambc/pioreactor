"""
Continuously take an optical density reading (more accurately: a backscatter reading, which is a proxy for OD).
This script is designed to run in a background process and push data to MQTT.

>>> nohup python3 -m morbidostat.long_running.od_reading &
"""
import configparser
import time

import click
from adafruit_ads1x15.analog_in import AnalogIn
import adafruit_ads1x15.ads1115 as ADS
from paho.mqtt import publish
import board
import busio

from morbidostat.utils.streaming import MovingStats, LowPassFilter
from morbidostat.utils import ADS_GAIN_THRESHOLDS


config = configparser.ConfigParser()
config.read("config.ini")


@click.command()
@click.option("--unit", default="1", help="The morbidostat unit")
@click.option("--verbose", default=False, help="print to console")
def od_reading(unit, verbose):

    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c, gain=2)
    chan = AnalogIn(ads, ADS.P0, ADS.P1)

    sampling_rate = 1 / int(config["od_sampling"]["samples_per_second"])
    sm = LowPassFilter(int(config["od_sampling"]["samples_per_second"]), 0.0001, sampling_rate)
    ma = MovingStats(lookback=20)

    publish.single(f"morbidostat/{unit}/log", "starting od_reading.py")

    i = 1
    while True:
        time.sleep(sampling_rate)
        try:
            raw_signal = chan.voltage
            sm.update(raw_signal)
            ma.update(raw_signal)

            # publish
            if sm.latest_reading is not None and i % int(config["od_sampling"]["mqtt_publish_rate"]) == 0:
                publish.single(f"morbidostat/{unit}/od_low_pass", sm.latest_reading)
                publish.single(f"morbidostat/{unit}/od_raw", raw_signal)


            # check if using correct gain
            if i % 100 == 0 and ma.mean is not None:
                for gain, (lb, ub) in ADS_GAIN_THRESHOLDS:
                    if lb <= ma.mean < ub:
                        ads.gain = gain


            if verbose:
                print(raw_signal, sm.latest_reading, ads.gain)

            i += 1

        except OSError as e:
            # just pause, not sure why this happens when add_media or remove_waste are called.
            publish.single(
                f"morbidostat/{unit}/error_log", f"{unit} od_reading.py failed with {str(e)}. Attempting to continue."
            )
            time.sleep(5.0)
        except Exception as e:
            publish.single(f"morbidostat/{unit}/log", f"od_reading.py failed with {str(e)}")
            publish.single(f"morbidostat/{unit}/error_log", f"{unit} od_reading.py failed with {str(e)}")
            raise e


if __name__ == "__main__":
    od_reading()
