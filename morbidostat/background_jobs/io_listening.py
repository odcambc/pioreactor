# -*- coding: utf-8 -*-
"""
Continuously monitor the bioreactor and provide summary statistics on what's going on
"""
import json
import time
import traceback
import subprocess
import signal
import threading

import paho.mqtt.subscribe as paho_subscribe
import click

from morbidostat.utils.pubsub import publish, subscribe, subscribe_and_callback
from morbidostat.utils import leader_hostname, get_unit_from_hostname, get_latest_experiment_name, leader_hostname


VIAL_VOLUME = 14


def get_initial_alt_media_fraction(experiment, unit):
    """
    This is a hack to use a timeout (not available in paho-mqtt) to
    see if a value is present in the MQTT cache (retained message)

    """
    test_mqtt = subprocess.run(
        [f'mosquitto_sub -t "morbidostat/{unit}/{experiment}/alt_media_fraction" -W 3 -h {leader_hostname}'],
        shell=True,
        capture_output=True,
    )
    if test_mqtt.stdout == b"":
        return 0
    else:
        return float(test_mqtt.stdout.strip())


class AltMediaCalculator:
    """
    Computes the fraction of the vial that is from the alt-media vs the regular media.

    Parameters
    -----------
    ignore_cache: ignore any retained values in the MQTT bus
    """

    def __init__(self, unit=None, experiment=None, verbose=0, **kwargs):
        self.unit = unit
        self.experiment = experiment
        self.verbose = verbose

    @property
    def latest_alt_media_fraction(self):
        if hasattr(self, "_latest_alt_media_fraction"):
            return self._latest_alt_media_fraction
        else:
            try:
                self._latest_alt_media_fraction = get_initial_alt_media_fraction(self.experiment, self.unit)
            except:
                self._latest_alt_media_fraction = 0
        return self._latest_alt_media_fraction

    @latest_alt_media_fraction.setter
    def latest_alt_media_fraction(self, value):
        self._latest_alt_media_fraction = value

    def on_message(self, client, userdata, message):
        try:
            assert message.topic == f"morbidostat/{self.unit}/{self.experiment}/io_events"
            payload = json.loads(message.payload)
            volume, event = float(payload["volume_change"]), payload["event"]
            if event == "add_media":
                self.update_alt_media_fraction(volume, 0)
            elif event == "add_alt_media":
                self.update_alt_media_fraction(0, volume)
            elif event == "remove_waste":
                pass
            else:
                raise ValueError()
        except:
            # paho swallows exceptions in callbacks, this spits them back up.
            traceback.print_exc()
            return

    def update_alt_media_fraction(self, media_delta, alt_media_delta):

        total_delta = media_delta + alt_media_delta

        # current mL
        alt_media_ml = VIAL_VOLUME * self.latest_alt_media_fraction
        media_ml = VIAL_VOLUME * (1 - self.latest_alt_media_fraction)

        # remove
        alt_media_ml = alt_media_ml * (1 - total_delta / VIAL_VOLUME)
        media_ml = media_ml * (1 - total_delta / VIAL_VOLUME)

        # add (alt) media
        alt_media_ml = alt_media_ml + alt_media_delta
        media_ml = media_ml + media_delta

        self.latest_alt_media_fraction = alt_media_ml / VIAL_VOLUME

        publish(
            f"morbidostat/{self.unit}/{self.experiment}/alt_media_fraction",
            self.latest_alt_media_fraction,
            verbose=self.verbose,
            retain=True,
        )

        return self.latest_alt_media_fraction


@click.command()
@click.option("--verbose", default=0, help="print to std.out")
def click_io_listening(verbose):
    unit = get_unit_from_hostname()
    experiment = get_latest_experiment_name()

    publish(f"morbidostat/{unit}/{experiment}/log", f"[io_listening]: starting", verbose=verbose)

    subscribe_and_callback(
        callback=AltMediaCalculator(unit=unit, experiment=experiment, verbose=verbose).on_message,
        topics=f"morbidostat/{unit}/{experiment}/io_events",
    )

    signal.pause()


if __name__ == "__main__":
    click_io_listening()
