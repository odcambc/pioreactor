# -*- coding: utf-8 -*-
"""
Continuously take an optical density reading (more accurately: a turbidity reading, which is a proxy for OD).
Topics published to

    pioreactor/<unit>/<experiment>/od_raw/<angle>/<label>

Ex:

    pioreactor/pioreactor1/trial15/od_raw/135/0

Also published to

    pioreactor/<unit>/<experiment>/od_reading/od_raw_batched

a serialized json like: "{"135/0": 0.086, "135/1": 0.086, "135/2": 0.0877, "135/3": 0.0873}"


Internally, the subjob ADCReader reads all channels from the ADC and pushes to MQTT. The ODReader listens to
these MQTT topics, and re-publishes only the data that represents optical densities. Why do it this way? In
the future, there could be other photodiodes / analog signals that plug into the ADS, and they listen (and republish)
in the same manner.

In the ADCReader class, we publish the `first_ads_obs_time` to MQTT so other jobs can read it and
make decisions. For example, if a bubbler/visible light LED is active, it should time itself
s.t. it is _not_ running when an turbidity measurement is about to occur. `interval` is there so
that it's clear the duration between readings, and in case the config.ini is changed between this job
starting and the downstream job starting. It takes about 0.5-0.6 seconds to read (and publish) *all
the channels. This can be shortened by changing the data_rate in the config to a higher value.

"""
import time
import json
import signal

import click
from adafruit_ads1x15.analog_in import AnalogIn
import adafruit_ads1x15.ads1115 as ADS
import busio

from pioreactor.utils.streaming_calculations import ExponentialMovingAverage

from pioreactor.whoami import get_unit_name, get_latest_experiment_name
from pioreactor.config import config
from pioreactor.utils.timing import RepeatedTimer, catchtime
from pioreactor.utils.mock import MockAnalogIn, MockI2C
from pioreactor.background_jobs.base import BackgroundJob
from pioreactor.background_jobs.subjobs.base import BackgroundSubJob
from pioreactor.actions.led_intensity import led_intensity
from pioreactor.hardware_mappings import SCL, SDA
from pioreactor.pubsub import QOS, subscribe


class ADCReader(BackgroundSubJob):
    """
    This job publishes the voltage reading from _all_ channels, and downstream
    jobs can selectively choose a channel to listen to. We don't publish until
    `start_periodic_reading()` is called, otherwise, call `take_reading` manually.
    The read values are stored in A0, A1, A2, and A3.


    We publish the `first_ads_obs_time` to MQTT so other jobs can read it and
    make decisions. For example, if a bubbler is active, it should time itself
    s.t. it is _not_ running when an turbidity measurement is about to occur.
    `interval` is there so that it's clear the duration between readings,
    and in case the config.ini is changed between this job starting and the downstream
    job starting.



    """

    ADS_GAIN_THRESHOLDS = {
        2 / 3: (4.096, 6.144),
        1: (2.048, 4.096),
        2: (1.024, 2.048),
        4: (0.512, 1.024),
        8: (0.256, 0.512),
        16: (-1, 0.256),
    }

    JOB_NAME = "adc_reader"
    editable_settings = [
        "interval",
        "first_ads_obs_time",
        "A0",
        "A1",
        "A2",
        "A3",
        "batched_readings",
    ]

    def __init__(
        self,
        interval=None,
        fake_data=False,
        unit=None,
        experiment=None,
        dynamic_gain=True,
        initial_gain=1,
    ):
        super(ADCReader, self).__init__(
            job_name=self.JOB_NAME, unit=unit, experiment=experiment
        )
        self.fake_data = fake_data
        self.interval = interval
        self.dynamic_gain = dynamic_gain
        self.initial_gain = initial_gain
        self.counter = 0
        self.ema = ExponentialMovingAverage(alpha=0.15)
        self.ads = None
        self.analog_in = []

        # this is actually important to set in the init. When this job starts, setting these the "default" values
        # will clear any cache in mqtt (if a cache exists).
        self.first_ads_obs_time = None
        self.timer = None
        self.A0 = None
        self.A1 = None
        self.A2 = None
        self.A3 = None
        self.batched_readings = dict()

        if self.interval:
            self.timer = RepeatedTimer(self.interval, self.take_reading)

        self.setup_adc()

    def start_periodic_reading(self):
        # start publishing after `interval` seconds.
        if self.timer:
            self.timer.start()

    def setup_adc(self):
        if self.fake_data:
            i2c = MockI2C(SCL, SDA)
        else:
            i2c = busio.I2C(SCL, SDA)

        try:
            # we will change the gain dynamically later.
            # data_rate is measured in signals-per-second, and generally has less noise the lower the value. See datasheet.
            self.ads = ADS.ADS1115(
                i2c,
                gain=self.initial_gain,
                data_rate=config.getint("od_config.od_sampling", "data_rate"),
            )
        except ValueError as e:
            self.logger.error(
                "Is the Pioreactor hardware installed on the RaspberryPi? Unable to find I²C for ADC measurements."
            )
            self.logger.debug(e, exc_info=True)
            raise e

        # check if using correct gain
        # this may need to be adjusted for higher rates of data collection
        if self.dynamic_gain:

            # we will instantiate and sweep through to set the gain
            raw_signals = []
            for channel in [0, 1, 2, 3]:
                if self.fake_data:
                    ai = MockAnalogIn(self.ads, getattr(ADS, f"P{channel}"))
                else:
                    ai = AnalogIn(self.ads, getattr(ADS, f"P{channel}"))
                self.analog_in.append((channel, ai))

                raw_signal_ = ai.voltage
                raw_signals.append(raw_signal_)

            max_signal = max(raw_signals)
            self.check_on_max(max_signal)
            self.check_on_gain(max_signal)

    def check_on_max(self, value):
        if value > 3.1:
            self.logger.error(
                f"An ADC channel is recording a very high voltage, {round(value, 2)}V. We are shutting it down to keep the ADC safe."
            )
            self.set_state("disconnected")

    def check_on_gain(self, value):
        for gain, (lb, ub) in self.ADS_GAIN_THRESHOLDS.items():
            if (0.925 * lb <= value < 0.925 * ub) and (self.ads.gain != gain):
                self.ads.gain = gain
                self.logger.debug(f"ADC gain updated to {self.ads.gain}.")
                break

    def on_disconnect(self):
        for attr in ["first_ads_obs_time", "interval"]:
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{attr}",
                None,
                retain=True,
                qos=QOS.EXACTLY_ONCE,
            )

        try:
            self.timer.cancel()
        except AttributeError:
            pass

    def take_reading(self):
        if self.first_ads_obs_time is None:
            self.first_ads_obs_time = time.time()

        self.counter += 1
        try:
            raw_signals = {}
            for channel, ai in self.analog_in:
                raw_signal_ = ai.voltage
                raw_signals[f"A{channel}"] = raw_signal_
                # the below will publish to pioreactor/{self.unit}/{self.experiment}/{self.job_name}/A{channel}
                setattr(self, f"A{channel}", raw_signal_)

                # since we don't show the user the raw voltage values, they may miss that they are near saturation of the op-amp (and could
                # also damage the ADC). We'll alert the user if the voltage gets higher than V, which is well above anything normal.
                # This is not for culture density saturation (different, harder problem)
                if (
                    (self.counter % 60 == 0)
                    and (raw_signal_ >= 2.75)
                    and not self.fake_data
                ):
                    self.logger.warning(
                        f"ADC channel {channel} is recording a very high voltage, {round(raw_signal_, 2)}V. It's recommended to keep it less than 3.3V."
                    )
                # check if more than 3V, and shut down something? to prevent damage to ADC.
                self.check_on_max(raw_signal_)

            # publish the batch of data, too, for reading,
            # publishes to pioreactor/{self.unit}/{self.experiment}/{self.job_name}/batched_readings
            self.batched_readings = raw_signals

            # the max signal should determine the ADS1x15's gain
            if self.dynamic_gain:
                self.ema.update(max(raw_signals.values()))

            # check if using correct gain
            # this should update after first observation
            # this may need to be adjusted for higher rates of data collection
            check_gain_every_n = 5
            if (
                self.dynamic_gain
                and self.counter % check_gain_every_n == 1
                and self.ema.value is not None
            ):
                self.check_on_gain(self.ema.value)

            return raw_signals

        except OSError as e:
            # just pause, not sure why this happens when add_media or remove_waste are called.
            self.logger.error(f"error {str(e)}. Attempting to continue.")
            time.sleep(5.0)
        except Exception as e:
            self.logger.error(f"failed with {str(e)}")
            raise e


class ODReader(BackgroundJob):
    """
    Produce a stream of OD readings from the sensors.

    Parameters
    -----------

    channel_label_map: dict of (ADS channel: label) pairs, ex: {"A0": "135/0", "A1": "90/1"}

    """

    editable_settings = []

    def __init__(
        self,
        channel_label_map,
        sampling_rate=1,
        fake_data=False,
        unit=None,
        experiment=None,
        stop_IR_led_between_ADC_readings=True,
    ):
        super(ODReader, self).__init__(
            job_name="od_reading", unit=unit, experiment=experiment
        )
        self.logger.debug(
            f"Starting od_reading with sampling_rate {sampling_rate}s and channels {channel_label_map}."
        )
        self.channel_label_map = channel_label_map
        self.fake_data = fake_data

        # start IR led before ADC starts, as it needs it.
        self.start_ir_led()

        self.adc_reader = ADCReader(
            interval=sampling_rate,
            fake_data=fake_data,
            unit=self.unit,
            experiment=self.experiment,
        )
        self.sub_jobs = [self.adc_reader]
        self.adc_reader.start_periodic_reading()

        # somewhere here we should test the relationship between light and ADC readings
        self.start_passive_listeners()
        if stop_IR_led_between_ADC_readings:
            self.set_IR_led_during_ADC_readings()

    def set_IR_led_during_ADC_readings(self):
        """
        This supposes IR LED is always on, and the "sneak in" turns it off. We also turn off all other LEDs
        when we turn the IR LED on.

        post_duration: how long to wait (seconds) after the ADS reading before running sneak_in
        pre_duration: duration between stopping the action and the next ADS reading
        """

        post_duration = (
            0.6
        )  # can be lowered to < 0.3 safely I believe since each reading takes 1/8=0.125 seconds
        pre_duration = 1.0  # just to be safe

        def sneak_in():
            with catchtime() as delta_to_stop:
                self.stop_ir_led()

            time.sleep(
                max(0, ads_interval - (post_duration + pre_duration + delta_to_stop()))
            )
            self.start_ir_led()

        ads_start_time = float(
            subscribe(
                f"pioreactor/{self.unit}/{self.experiment}/adc_reader/first_ads_obs_time"
            ).payload
        )

        ads_interval = float(
            subscribe(
                f"pioreactor/{self.unit}/{self.experiment}/adc_reader/interval"
            ).payload
        )

        if ads_interval < 1.5:
            # if this is too small, like 1.5s, we should just skip this whole thing and keep the IR LED always on.
            return

        self.sneak_in_timer = RepeatedTimer(ads_interval, sneak_in, run_immediately=False)

        time_to_next_ads_reading = ads_interval - (
            (time.time() - ads_start_time) % ads_interval
        )

        time.sleep(time_to_next_ads_reading + post_duration)
        self.sneak_in_timer.start()

    def start_ir_led(self):
        ir_channel = config.get("leds", "ir_led")
        r = led_intensity(
            ir_channel,
            intensity=config.getint("od_config.od_sampling", "ir_intensity"),
            unit=self.unit,
            experiment=self.experiment,
            source_of_event=self.job_name,
            verbose=False,
            mock=self.fake_data,
        )
        if not r:
            raise ValueError("IR LED could not be started. Stopping OD reading.")

        return

    def stop_ir_led(self):
        if not self.fake_data:
            ir_channel = config.get("leds", "ir_led")
            led_intensity(
                ir_channel,
                intensity=0,
                unit=self.unit,
                experiment=self.experiment,
                source_of_event=self.job_name,
                verbose=False,
                mock=self.fake_data,
            )

    def on_disconnect(self):
        for job in self.sub_jobs:
            job.set_state("disconnected")

        # turn off the LED after we have take our last ADC reading..
        try:
            self.sneak_in_timer.cancel()
        except Exception:
            pass
        self.stop_ir_led()

    def publish_batch(self, message):
        if self.state != self.READY:
            return
        ads_readings = json.loads(message.payload)
        od_readings = {}
        for channel, label in self.channel_label_map.items():
            try:
                od_readings[label] = ads_readings[str(channel)]
            except KeyError:
                self.logger.error(
                    f"Input wrong channel, provided {label}. Only valid channels are 0, 1, 2, 3."
                )
                self.set_state(self.DISCONNECTED)

        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/od_raw_batched",
            json.dumps(od_readings),
            qos=QOS.EXACTLY_ONCE,
        )

    def publish_single(self, message):
        if self.state != self.READY:
            return

        channel = message.topic.rsplit("/", maxsplit=1)[1]
        label = self.channel_label_map[channel]

        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/od_raw/{label}",
            message.payload,
            qos=QOS.EXACTLY_ONCE,
        )

    def start_passive_listeners(self):

        # process incoming data
        # allow_retained is False because we don't want to process (stale) retained ADS values
        self.subscribe_and_callback(
            self.publish_batch,
            f"pioreactor/{self.unit}/{self.experiment}/{ADCReader.JOB_NAME}/batched_readings",
            qos=QOS.EXACTLY_ONCE,
            allow_retained=False,
        )
        for channel in self.channel_label_map:
            self.subscribe_and_callback(
                self.publish_single,
                f"pioreactor/{self.unit}/{self.experiment}/{ADCReader.JOB_NAME}/{channel}",
                qos=QOS.EXACTLY_ONCE,
                allow_retained=False,
            )


def create_channel_label_map_from_string(od_angle_channel):
    # We split input of the form ["135,0", "135,1", "90,3"] into the form
    # {"A0": 135/0", "A1": "135/1", "A3":"90/3"}
    channel_label_map = {}
    for input_ in od_angle_channel:
        angle, channel = input_.split(",")

        angle_label = f"{angle}/{channel}"
        channel_label_map[f"A{channel}"] = angle_label
    return channel_label_map


def od_reading(
    od_angle_channel,
    sampling_rate=1 / config.getfloat("od_config.od_sampling", "samples_per_second"),
    fake_data=False,
    unit=None,
    experiment=None,
):

    unit = unit or get_unit_name()
    experiment = experiment or get_latest_experiment_name()
    channel_label_map = create_channel_label_map_from_string(od_angle_channel)

    ODReader(
        channel_label_map,
        sampling_rate=sampling_rate,
        unit=unit,
        experiment=experiment,
        fake_data=fake_data,
    )

    signal.pause()


@click.command(name="od_reading")
@click.option(
    "--od-angle-channel",
    multiple=True,
    default=config.get("od_config.photodiode_channel", "od_angle_channel").split("|"),
    type=click.STRING,
    show_default=True,
    help="""
pair of angle,channel for optical density reading. Can be invoked multiple times. Ex:

--od-angle-channel 135,0 --od-angle-channel 90,1 --od-angle-channel 45,3

""",
)
@click.option("--fake-data", is_flag=True, help="produce fake data (for testing)")
def click_od_reading(od_angle_channel, fake_data):
    """
    Start the optical density reading job
    """
    od_reading(od_angle_channel, fake_data=fake_data)
