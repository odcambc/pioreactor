# -*- coding: utf-8 -*-
"""
Continuously take an optical density reading (more accurately: a turbidity reading, which is a proxy for OD).
Topics published to

    pioreactor/<unit>/<experiment>/od_reading/od_raw/<channel>

Ex:

    pioreactor/pioreactor1/trial15/od_reading/od_raw/1

a json blob like:

    {
        "voltage": 0.10030799136835057,
        "timestamp": "2021-06-06T15:08:12.080594",
        "angle": "90,135"
    }


All signals published together to

    pioreactor/<unit>/<experiment>/od_reading/od_raw_batched

a serialized json blob like:

    {
      "od_raw": {
        "2": {
          "voltage": 0.1008556663221068,
          "angle": "135,45"
        },
        "1": {
          "voltage": 0.10030799136835057,
          "angle": "90,135"
        }
      },
      "timestamp": "2021-06-06T15:08:12.081153"
    }


Internally, the ODReader runs a function every `interval` seconds. The function
 1. turns off all non-IR LEDs
 2. turns on the IR LED
 3. calls ADCReader tp read channels from the ADC.
 4. Performs any transformations (see below)
 5. Switches back LEDs to previous state from step 1.
 6. Publishes data to MQTT

Transforms are ex: sin regression, and LED output compensation. See diagram below.

Dataflow of raw signal to final output:

┌────────────────────────────────────────────────────────────────────────────────┐
│ODReader                                                                        │
│                                                                                │
│                                                                                │
│   ┌──────────────────────────────────────────┐    ┌────────────────────────┐   │
│   │ADCReader                                 │    │IrLedOutputTracker      │   │
│   │                                          │    │                        │   │
│   │                                          │    │                        │   │
│   │ ┌──────────────┐       ┌───────────────┐ │    │  ┌─────────────────┐   │   │
│   │ │              ├───────►               │ │    │  │                 │   │   │
│   │ │              │       │               │ │    │  │                 │   │   │    MQTT
│   │ │ samples from ├───────►      sin      ├─┼────┼──►  IR output      ├───┼───┼───────►
│   │ │     ADC      │       │   regression  │ │    │  │  compensation   │   │   │
│   │ │              ├───────►               │ │    │  │                 │   │   │
│   │ └──────────────┘       └───────────────┘ │    │  └─────────────────┘   │   │
│   │                                          │    │                        │   │
│   │                                          │    │                        │   │
│   └──────────────────────────────────────────┘    └────────────────────────┘   │
│                                                                                │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘


In the ODReader class, we publish the `first_od_obs_time` to MQTT so other jobs can read it and
make decisions. For example, if a bubbler/visible light LED is active, it should time itself
s.t. it is _not_ running when an turbidity measurement is about to occur.

"""
from __future__ import annotations

import math
import os
import threading
from time import sleep
from time import time
from typing import Callable
from typing import cast
from typing import Optional

import click
from msgspec.json import encode

import pioreactor.actions.led_intensity as led_utils
from pioreactor import error_codes
from pioreactor import exc
from pioreactor import hardware
from pioreactor import structs
from pioreactor import types as pt
from pioreactor import whoami
from pioreactor.background_jobs.base import BackgroundJob
from pioreactor.background_jobs.base import LoggerMixin
from pioreactor.config import config
from pioreactor.pubsub import publish
from pioreactor.pubsub import QOS
from pioreactor.utils import argextrema
from pioreactor.utils import local_intermittent_storage
from pioreactor.utils import timing
from pioreactor.utils.streaming_calculations import ExponentialMovingAverage
from pioreactor.utils.timing import catchtime
from pioreactor.version import hardware_version_info

ALL_PD_CHANNELS: list[pt.PdChannel] = ["1", "2"]
VALID_PD_ANGLES: list[pt.PdAngle] = ["45", "90", "135", "180"]

REF_keyword = "REF"
IR_keyword = "IR"


class ADCReader(LoggerMixin):
    """
    Notes
    ------
    It's currently highly specific to the ADS1x15 family AND it's connection to ODReader.
    It's not advisable to use this class for other use cases - best to develop your own.

    Parameters
    ------------
    channels: list
        a list of channels, a subset of [1, 2, 3, 4]
    fake_data: bool
        generate fake ADC readings internally.
    dynamic_gain: bool
        dynamically change the gain based on the max reading from channels
    initial_gain:
        set the initial gain - see data sheet for values.

    """

    _logger_name = "adc_reader"
    DATA_RATE: int = 128
    ADS1X15_GAIN_THRESHOLDS = {
        2 / 3: (4.096, 6.144),
        1: (2.048, 4.096),
        2: (1.024, 2.048),
        4: (0.512, 1.024),
        8: (0.256, 0.512),
        16: (-1, 0.256),
    }

    ADS1X15_PGA_RANGE = {
        2 / 3: 6.144,
        1: 4.096,
        2: 2.048,
        4: 1.024,
        8: 0.512,
        16: 0.256,
    }

    oversampling_count: int = 26
    readings_completed: int = 0
    _setup_complete = False
    most_appropriate_AC_hz: Optional[float] = None

    def __init__(
        self,
        channels: list[pt.PdChannel],
        fake_data: bool = False,
        interval: float = 1.0,
        dynamic_gain: bool = True,
        initial_gain: float = 1,
    ) -> None:
        super().__init__()
        self.fake_data = fake_data
        self.dynamic_gain = dynamic_gain
        self.gain = initial_gain
        self.max_signal_moving_average = ExponentialMovingAverage(alpha=0.05)
        self.channels = channels
        self.batched_readings: dict[pt.PdChannel, float] = {}
        self.interval = interval

        if config.get("od_config", "local_ac_hz"):
            self.most_appropriate_AC_hz = config.getfloat("od_config", "local_ac_hz")

        if not hardware.is_HAT_present():
            raise exc.HardwareNotFoundError("Pioreactor HAT must be present.")

    def setup_adc(self) -> ADCReader:
        """
        This configures the ADC for reading, performs an initial read, and sets variables based on that reading.

        It doesn't occur in the classes __init__ because it often requires an LED to be on (and this class doesn't control LEDs.).
        See ODReader for an example.
        """

        if self.fake_data:
            from pioreactor.utils.mock import MockAnalogIn as AnalogIn, MockI2C as I2C
        else:
            from adafruit_ads1x15.analog_in import AnalogIn  # type: ignore
            from busio import I2C  # type: ignore

        channel_to_adc_map: dict[pt.PdChannel, int] = {
            "1": 1,
            "2": 0,
        }

        if hardware_version_info[0] == 0 and hardware_version_info[1] == 1:
            channel_to_adc_map = {
                "1": 0,
                "2": 1,
            }
        if hardware_version_info[0] == 0 and hardware_version_info[1] <= 2:
            from adafruit_ads1x15.ads1115 import ADS1115 as ADS  # type: ignore

        else:
            from adafruit_ads1x15.ads1015 import ADS1015 as ADS  # type: ignore

        self.ads = ADS(
            I2C(hardware.SCL, hardware.SDA),
            data_rate=self.DATA_RATE,
            gain=self.gain,
            address=hardware.ADC,
        )
        self.analog_in: dict[pt.PdChannel, AnalogIn] = {}

        for channel in self.channels:
            self.analog_in[channel] = AnalogIn(self.ads, channel_to_adc_map[channel])

        # check if using correct gain
        # this may need to be adjusted for higher rates of data collection
        if self.dynamic_gain:
            max_signal = -1.0
            # we will instantiate and sweep through to set the gain
            for ai in self.analog_in.values():

                raw_signal_ = ai.voltage
                max_signal = max(raw_signal_, max_signal)

            self.check_on_max(max_signal)
            self.check_on_gain(max_signal)

        self._setup_complete = True
        self.logger.debug(
            f"ADC ready to read from PD channels {', '.join(map(str, self.channels))}, with gain {self.gain}."
        )
        return self

    def check_on_max(self, value: float) -> None:

        if value > 3.2:
            self.logger.error(
                f"An ADC channel is recording a very high voltage, {round(value, 2)}V. We are shutting down components and jobs to keep the ADC safe."
            )

            unit, exp = whoami.get_unit_name(), whoami.get_latest_experiment_name()

            with local_intermittent_storage("led_locks") as cache:
                for c in led_utils.ALL_LED_CHANNELS:
                    del cache[c]

            # turn off all LEDs that might be causing problems
            # however, ODReader may turn on the IR LED again.
            led_utils.led_intensity(
                {c: 0.0 for c in led_utils.ALL_LED_CHANNELS},
                source_of_event="ADCReader",
                unit=unit,
                experiment=exp,
                verbose=True,
            )

            publish(
                f"pioreactor/{unit}/{exp}/monitor/flicker_led_with_error_code",
                error_codes.ADC_INPUT_TOO_HIGH,
            )
            # kill ourselves - this will hopefully kill ODReader.
            # we have to send a signal since this is often called in a thread (timing.RepeatedTimer)
            import os
            import signal

            os.kill(os.getpid(), signal.SIGTERM)
            return

        elif value > 3.0:
            self.logger.warning(
                f"An ADC channel is recording a very high voltage, {round(value, 2)}V. It's recommended to keep it less than 3.3V. Suggestion: decrease the IR intensity, or change the PD angle to a lower angle."
            )
            publish(
                f"pioreactor/{whoami.get_unit_name()}/{whoami.get_latest_experiment_name()}/monitor/flicker_led_with_error_code",
                error_codes.ADC_INPUT_TOO_HIGH,
            )
            return

    def check_on_gain(self, value: Optional[float], tol=0.925) -> None:
        if value is None:
            return

        for gain, (lb, ub) in self.ADS1X15_GAIN_THRESHOLDS.items():
            if (tol * lb <= value < tol * ub) and (self.gain != gain):
                self.gain = gain
                self.set_ads_gain(gain)
                self.logger.debug(f"ADC gain updated to {self.gain}.")
                break

    def set_ads_gain(self, gain) -> None:
        # this isn't _always_ equal to self.gain, ex: if another process is using the ADC to measure fluor.,
        # then they might use a different gain value. However, on take_reading, we always set it back to the
        # ADCReader's gain.
        self.ads.gain = gain  # this assignment will check to see if the gain is allowed.

    def sin_regression_with_known_freq(
        self,
        x: list,
        y: list,
        freq: float,
        prior_C: float = None,
        penalizer_C: float = None,
    ) -> tuple[tuple[float, Optional[float], Optional[float]], float]:
        r"""
        Assumes a known frequency.
        Formula is

        f(t) = C + A*sin(2*pi*freq*t + phi)

        # TODO: is it implemented as C - A*sin(2*pi*freq*t - phi) ??


        However, estimation occurs as:

        \sum_k (f(t_i) - y_i)^2 + penalizer_C * (C - prior_C)^2

        Parameters
        -----------
        x: iterable
        y: iterable
        freq: the frequency
        prior_C: scalar (optional)
            specify value that will be compared against using ridge regression.
        penalizer_C: scalar (optional)
            penalizer values for the ridge regression

        Returns
        ---------
        (C, A, phi):
            tuple of scalars
        AIC: float
            the AIC of the fit, used for model comparison


        Reference
        ------------
        https://scikit-guess.readthedocs.io/en/latest/appendices/references.html#concept


        Notes
        ------
        This clips the max and min values from the input.

        """
        import numpy as np

        assert len(x) == len(y), "shape mismatch"

        # remove the max and min values. We need to do this in two steps, since
        # removing the first element may change the location of the second element.
        argmin_y_, _ = argextrema(y)
        y.pop(argmin_y_)
        x.pop(argmin_y_)

        _, argmax_y = argextrema(y)
        y.pop(argmax_y)
        x.pop(argmax_y)

        x_ = np.asarray(x)
        y_ = np.asarray(y)
        n = x_.shape[0]

        tau = 2 * np.pi
        sin_x = np.sin(freq * tau * x_)
        cos_x = np.cos(freq * tau * x_)

        sum_sin = sin_x.sum()
        sum_cos = cos_x.sum()
        sum_sin2 = (sin_x**2).sum()
        sum_cos2 = (cos_x**2).sum()
        sum_cossin = (cos_x * sin_x).sum()

        sum_y = y_.sum()
        sum_ysin = (y_ * sin_x).sum()
        sum_ycos = (y_ * cos_x).sum()

        rhs_penalty_term = 0.0
        lhs_penalty_term = 0.0

        if prior_C and penalizer_C:
            rhs_penalty_term = penalizer_C * prior_C
            lhs_penalty_term = penalizer_C

        M = np.array(
            [
                [n + lhs_penalty_term, sum_sin, sum_cos],
                [sum_sin, sum_sin2, sum_cossin],
                [sum_cos, sum_cossin, sum_cos2],
            ]
        )
        Y = np.array([sum_y + rhs_penalty_term, sum_ysin, sum_ycos])

        try:
            C, b, c = np.linalg.solve(M, Y)
        except np.linalg.LinAlgError as e:
            self.logger.error(f"Error in regression. {e}")
            self.logger.debug(f"{x=}")
            self.logger.debug(f"{y=}")
            return (y_.mean(), None, None), 1e10

        y_model = C + b * np.sin(freq * tau * x_) + c * np.cos(freq * tau * x_)
        SSE = np.sum((y_ - y_model) ** 2)

        if SSE > 1e-20:
            AIC = n * np.log(SSE / n) + 2 * 3
        else:
            AIC = math.inf

        if np.sqrt(b**2 + c**2) <= 1e-20:
            A = 0
            phi = 0
        else:
            A = np.sqrt(b**2 + c**2)
            phi = np.arcsin(c / np.sqrt(b**2 + c**2))

        return (float(C), float(A), float(phi)), AIC

    def from_voltage_to_raw(self, voltage: float) -> int:
        # from https://github.com/adafruit/Adafruit_CircuitPython_ADS1x15/blob/e33ed60b8cc6bbd565fdf8080f0057965f816c6b/adafruit_ads1x15/analog_in.py#L61
        return cast(int, voltage * 32767 / self.ADS1X15_PGA_RANGE[self.gain])

    def from_raw_to_voltage(self, raw: float | int) -> float:
        # from https://github.com/adafruit/Adafruit_CircuitPython_ADS1x15/blob/e33ed60b8cc6bbd565fdf8080f0057965f816c6b/adafruit_ads1x15/analog_in.py#L61
        return raw / 32767 * self.ADS1X15_PGA_RANGE[self.gain]

    def take_reading(self) -> dict[pt.PdChannel, float]:
        """
        Sample from the ADS - likely this has been optimized for use for optical density in the Pioreactor system.

        Returns
        ---------
        readings: dict
            a dict with specified channels (as ints) and their reading
            Ex: {"1": 0.10240, "2": 0.1023459}


        """
        if not self._setup_complete:
            raise ValueError("Must call setup_adc() first.")

        max_signal = -1.0
        oversampling_count = self.oversampling_count

        # we pre-allocate these arrays to make the for loop faster => more accurate
        aggregated_signals: dict[pt.PdChannel, list[int]] = {
            channel: [0] * oversampling_count for channel in self.channels
        }
        timestamps: dict[pt.PdChannel, list[float]] = {
            channel: [0.0] * oversampling_count for channel in self.channels
        }

        # in case some other process is also using the ADC chip and changes the gain, we want
        # to always confirm our settings before take a reading.
        self.set_ads_gain(self.gain)

        try:
            with catchtime() as time_since_start:
                for counter in range(oversampling_count):
                    with catchtime() as time_sampling_took_to_run:
                        for channel, ai in self.analog_in.items():
                            timestamps[channel][counter] = time_since_start()
                            aggregated_signals[channel][counter] = ai.value

                    sleep(
                        max(
                            0,
                            -time_sampling_took_to_run()  # the time_sampling_took_to_run() reduces the variance by accounting for the duration of each sampling.
                            + 0.80 / (oversampling_count - 1)
                            + 0.001
                            * (
                                (counter * 0.618034) % 1
                            ),  # this is to artificially jitter the samples, so that we observe less aliasing. That constant is phi.
                        )
                    )

            batched_estimates_: dict[pt.PdChannel, float] = {}

            if self.most_appropriate_AC_hz is None:
                self.most_appropriate_AC_hz = self.determine_most_appropriate_AC_hz(
                    timestamps, aggregated_signals
                )

            if os.environ.get("DEBUG") is not None:
                self.logger.debug(f"{timestamps=}")
                self.logger.debug(f"{aggregated_signals=}")

            for channel in self.channels:
                (
                    best_estimate_of_signal_,
                    *_other_param_estimates,
                ), _ = self.sin_regression_with_known_freq(
                    timestamps[channel],
                    aggregated_signals[channel],
                    self.most_appropriate_AC_hz,
                    prior_C=(self.from_voltage_to_raw(self.batched_readings[channel]))
                    if (channel in self.batched_readings)
                    else None,
                    penalizer_C=(300.0 / self.oversampling_count / self.interval)
                    if self.interval
                    else None
                    # arbitrary, but should scale with number of samples, and duration between samples
                )

                # convert to voltage
                best_estimate_of_signal_v = self.from_raw_to_voltage(best_estimate_of_signal_)

                # force value to be non-negative. Negative values can still occur due to the IR LED reference
                batched_estimates_[channel] = max(best_estimate_of_signal_v, 0)

                # check if more than 3.x V, and shut down to prevent damage to ADC.
                # we use max_signal to modify the PGA, too
                max_signal = max(max_signal, best_estimate_of_signal_v)

            self.check_on_max(max_signal)
            self.batched_readings = batched_estimates_

            # the max signal should determine the ADS1x15's gain
            self.max_signal_moving_average.update(max_signal)

            # check if using correct gain
            # this may need to be adjusted for higher rates of data collection
            if self.dynamic_gain and self.readings_completed % 5 == 4:
                self.check_on_gain(self.max_signal_moving_average())

            self.readings_completed += 1

            return batched_estimates_

        except Exception as e:
            self.logger.debug(e, exc_info=True)
            self.logger.error(e)
            raise e

    def determine_most_appropriate_AC_hz(
        self,
        timestamps: dict[pt.PdChannel, list[float]],
        aggregated_signals: dict[pt.PdChannel, list[int]],
    ) -> float:
        def _compute_best_freq(timestamps, aggregated_signals):
            FREQS_TO_TRY = [60.0, 50.0]
            argmin_freq = FREQS_TO_TRY[0]
            min_AIC = float("inf")

            for freq in FREQS_TO_TRY:
                _, AIC = self.sin_regression_with_known_freq(
                    timestamps, aggregated_signals, freq=freq
                )
                if AIC < min_AIC:
                    min_AIC = AIC
                    argmin_freq = freq

            return argmin_freq

        channel = self.channels[0]
        argmin_freq1 = _compute_best_freq(timestamps[channel], aggregated_signals[channel])

        self.logger.debug(f"AC hz estimate: {argmin_freq1}")
        return argmin_freq1


class IrLedReferenceTracker(LoggerMixin):

    _logger_name = "ir_led_ref"
    channel: pt.PdChannel

    def __init__(self) -> None:
        super().__init__()

    def update(self, ir_output_reading: float) -> None:
        pass

    def set_blank(self, ir_output_reading: float) -> None:
        pass

    def get_reference_reading(self, batched_readings: dict[pt.PdChannel, float]) -> float:
        return batched_readings[self.channel]

    def __call__(self, od_signal: float) -> float:
        return od_signal


class PhotodiodeIrLedReferenceTracker(IrLedReferenceTracker):
    """
    This class contains the logic on how we incorporate the
    direct IR LED output into OD readings.

    Tracking and "normalizing" (see below) the OD signals by the IR LED output is important
    because the OD signal is linearly proportional to the LED output.

    The following are causes of LED output changing:
    - change in temperature of LED, caused by change in ambient temperature, or change in intensity of LED
    - LED dimming over time
    - drop in 3.3V rail -> changes the reference voltage for LED driver -> changes the output

    Internally, we track the _initial_ led output signal, and use this as a reference value. For example, let's say
    that the blank led output signal (i.e. led intensity is 0%, but we still detect a small value) is 0.0001,
    and the initial led output is 0.1. The latest IR led output is 0.09 (perhaps the ambient temp increased),
    so then we use the factor: (0.09 - 0.0001) / (0.1 - 0.0001) = 0.8998998999

    This factor is then used to normalize OD readings. Let's say the OD reading is 0.45, so the new value
    is 0.45 / 0.8998998999 = 0.500055617

    Inside, we also use an EMA to smooth the LED output readings, to reduce noise in this signal.

    """

    initial_led_output: Optional[float] = None
    blank_reading: float = 0.0

    def __init__(self, channel: pt.PdChannel, ignore_blank: bool = False) -> None:
        super().__init__()
        self.led_output_ema = ExponentialMovingAverage(
            config.getfloat("od_config", "pd_reference_ema")
        )
        self.channel = channel
        self.ignore_blank = ignore_blank
        self.logger.debug(f"Using PD channel {channel} as IR LED reference.")
        self._count: int = 0

    def update(self, ir_output_reading: float) -> None:
        if self.initial_led_output is None:
            self.initial_led_output = ir_output_reading
            self.logger.debug(f"{self.initial_led_output=}")
            self._count = 1
        elif self._count < 11:  # dumb way to take average of the first N values...
            self.initial_led_output = (
                self.initial_led_output * self._count + ir_output_reading
            ) / (self._count + 1)
            self._count += 1
            self.logger.debug(f"{self.initial_led_output=}")

        # Note, in extreme circumstances, this can be negative, or even blow up to some large number.
        self.led_output_ema.update(
            (ir_output_reading - self.blank_reading)
            / (self.initial_led_output - self.blank_reading)
        )

    def set_blank(self, ir_output_reading: float) -> None:
        if not self.ignore_blank:
            self.blank_reading = ir_output_reading
            self.logger.debug(f"{self.blank_reading=}")
        return

    def __call__(self, od_signal: float) -> float:
        led_output = self.led_output_ema()
        if led_output is None:
            return od_signal
        else:
            return od_signal / led_output


class NullIrLedReferenceTracker(IrLedReferenceTracker):
    def __init__(self) -> None:
        super().__init__()
        self.logger.debug("Not using any IR LED reference.")

    def get_reference_reading(self, batched_readings) -> float:
        return 0.0


class ODReader(BackgroundJob):
    """
    Produce a stream of OD readings from the sensors.

    Parameters
    -----------
    channel_angle_map: dict
        dict of (channel: angle) pairs, ex: {1: "135", 2: "90"}
    interval: float
        seconds between readings. If None, then don't periodically read.
    adc_reader: ADCReader
    ir_led_reference_tracker: IrLedReferenceTracker

    Attributes
    ------------
    adc_reader: ADCReader
    ir_led_reference_tracker: ir_led_reference_tracker
    latest_reading:
        represents the most recent dict from the adc_reader


    Examples
    ---------

    Initializing this class will start reading in the background, if ``interval`` is not ``None``.

    > od_reader = ODReader({'1': '45'}, 5)
    > # readings will start to be published to MQTT, and the latest reading will be available as od_reader.latest_reading

    It can also be iterated over:

    > od_reader = ODReader({'1': '45'}, 5)
    > for od_reading in od_reader:
    >    # do things...

    If ``interval`` is ``None``, then users need to call ``record_from_adc``.

    >> od_reading = od_reader.record_from_adc()

    """

    published_settings = {
        "first_od_obs_time": {"datatype": "float", "settable": False},
        "ir_led_intensity": {"datatype": "float", "settable": True, "unit": "%"},
        "interval": {"datatype": "float", "settable": False, "unit": "s"},
        "relative_intensity_of_ir_led": {"datatype": "float", "settable": False},
    }
    latest_reading: structs.ODReadings

    _pre_read: list[Callable] = []
    _post_read: list[Callable] = []

    def __init__(
        self,
        channel_angle_map: dict[pt.PdChannel, pt.PdAngle],
        interval: Optional[float],
        adc_reader: ADCReader,
        ir_led_reference_tracker: IrLedReferenceTracker,
        unit: str,
        experiment: str,
    ) -> None:
        super(ODReader, self).__init__(job_name="od_reading", unit=unit, experiment=experiment)

        self.adc_reader = adc_reader
        self.channel_angle_map = channel_angle_map
        self.interval = interval
        self.ir_led_reference_tracker = ir_led_reference_tracker

        self.first_od_obs_time: Optional[float] = None
        self._set_for_iterating = threading.Event()

        self.ir_channel: pt.LedChannel = self.get_ir_channel_from_configuration()
        self.ir_led_intensity: float = config.getfloat("od_config", "ir_led_intensity")
        self.non_ir_led_channels: list[pt.LedChannel] = [
            ch for ch in led_utils.ALL_LED_CHANNELS if ch != self.ir_channel
        ]

        if not hardware.is_HAT_present():
            self.clean_up()
            raise exc.HardwareNotFoundError("Pioreactor HAT must be present.")

        self.logger.debug(
            f"Starting od_reading with PD channels {channel_angle_map}, with IR LED intensity {self.ir_led_intensity}% from channel {self.ir_channel}."
        )

        self.add_post_read_callback(self._publish_single)
        self.add_post_read_callback(self._publish_batch)
        self.add_post_read_callback(self._log_relative_intensity_of_ir_led)
        self.add_post_read_callback(self._unblock_internal_event)

        # setup the ADC and IrLedReference by turning off all LEDs.
        with led_utils.change_leds_intensities_temporarily(
            {channel: 0.0 for channel in led_utils.ALL_LED_CHANNELS},
            unit=self.unit,
            experiment=self.experiment,
            source_of_event=self.job_name,
            pubsub_client=self.pub_client,
            verbose=False,
        ):
            with led_utils.lock_leds_temporarily(self.non_ir_led_channels):

                # start IR led before ADC starts, as it needs it.
                self.start_ir_led()
                sleep(0.1)
                self.adc_reader.setup_adc()  # determine best gain, max-signal, etc.
                self.stop_ir_led()

                # get blank values of reference PD.
                # This slightly improves the accuracy of the IR LED output tracker,
                # See that class's docs.
                blank_reading = self.adc_reader.take_reading()
                blank_ir_output_reading = self.ir_led_reference_tracker.get_reference_reading(
                    blank_reading
                )
                self.ir_led_reference_tracker.set_blank(blank_ir_output_reading)

        if self.interval is not None:
            if self.interval < 1.0:
                self.logger.warning(
                    f"Recommended to have the interval between readings be larger than 1.0 second. Currently {self.interval} s."
                )

            self.record_from_adc_timer = timing.RepeatedTimer(
                self.interval,
                self.record_from_adc,
                run_immediately=True,
            ).start()

    @classmethod
    def add_pre_read_callback(cls, function: Callable):
        cls._pre_read.append(function)

    @classmethod
    def add_post_read_callback(cls, function: Callable):
        cls._post_read.append(function)

    def get_ir_channel_from_configuration(self) -> pt.LedChannel:
        try:
            return cast(pt.LedChannel, config.get("leds_reverse", IR_keyword))
        except Exception:
            self.logger.error(
                """`leds` section must contain `IR` value. Ex:
        [leds]
        A=IR
            """
            )
            raise KeyError("`IR` value not found in section.")

    def _read_from_adc(self) -> dict[pt.PdChannel, float]:
        """
        Read from the ADC. This function normalizes by the IR ref.

        Note
        -----
        The IR LED needs to be turned on for this function to report accurate OD signals.
        """
        batched_readings = self.adc_reader.take_reading()
        ir_output_reading = self.ir_led_reference_tracker.get_reference_reading(batched_readings)
        self.ir_led_reference_tracker.update(ir_output_reading)
        return self._normalize_by_led_output(batched_readings)

    def record_from_adc(self) -> structs.ODReadings:

        if self.first_od_obs_time is None:
            self.first_od_obs_time = time()

        for pre_function in self._pre_read:
            try:
                pre_function(self)
            except Exception:
                self.logger.debug(f"Error in {pre_function=}.", exc_info=True)

        # we put a soft lock on the LED channels - it's up to the
        # other jobs to make sure they check the locks.
        with led_utils.change_leds_intensities_temporarily(
            {channel: 0.0 for channel in led_utils.ALL_LED_CHANNELS}
            | {self.ir_channel: self.ir_led_intensity},
            unit=self.unit,
            experiment=self.experiment,
            source_of_event=self.job_name,
            pubsub_client=self.pub_client,
            verbose=False,
        ):
            with led_utils.lock_leds_temporarily(self.non_ir_led_channels):
                sleep(0.1)  # pause to make sure all LEDs are off
                timestamp_of_readings = timing.current_utc_timestamp()
                adc_reading_by_channel = self._read_from_adc()

                od_readings = structs.ODReadings(
                    timestamp=timestamp_of_readings,
                    od_raw={
                        channel: structs.ODReading(
                            voltage=adc_reading_by_channel[channel],
                            angle=angle,
                            timestamp=timestamp_of_readings,
                            channel=channel,
                        )
                        for channel, angle in self.channel_angle_map.items()
                    },
                )

        self.latest_reading = od_readings

        for post_function in self._post_read:
            try:
                post_function(self, od_readings)
            except Exception:
                self.logger.debug(f"Error in {post_function=}.", exc_info=True)

        return od_readings

    def start_ir_led(self) -> None:
        r = led_utils.led_intensity(
            {self.ir_channel: self.ir_led_intensity},
            unit=self.unit,
            experiment=self.experiment,
            source_of_event=self.job_name,
            pubsub_client=self.pub_client,
            verbose=False,
        )
        if not r:
            raise OSError("IR LED could not be started. Stopping OD reading.")

        return

    def stop_ir_led(self) -> None:
        led_utils.led_intensity(
            {self.ir_channel: 0.0},
            unit=self.unit,
            experiment=self.experiment,
            source_of_event=self.job_name,
            pubsub_client=self.pub_client,
            verbose=False,
        )

    def on_sleeping(self) -> None:
        self.record_from_adc_timer.pause()

    def on_sleeping_to_ready(self) -> None:
        self.record_from_adc_timer.unpause()

    def on_disconnected(self) -> None:

        # turn off the LED after we have take our last ADC reading..
        self.stop_ir_led()

        # tech debt: clear _pre and _post
        self._pre_read.clear()
        self._post_read.clear()

        try:
            self.record_from_adc_timer.cancel()
        except Exception:
            pass

    @staticmethod
    def _publish_batch(cls, od_readings: structs.ODReadings) -> None:

        if cls.state != cls.READY:
            return

        cls.publish(
            f"pioreactor/{cls.unit}/{cls.experiment}/{cls.job_name}/od_raw_batched",
            encode(od_readings),
            qos=QOS.EXACTLY_ONCE,
        )

    @staticmethod
    def _publish_single(cls, od_readings: structs.ODReadings) -> None:
        if cls.state != cls.READY:
            return

        for channel, _ in cls.channel_angle_map.items():
            cls.publish(
                f"pioreactor/{cls.unit}/{cls.experiment}/{cls.job_name}/od_raw/{channel}",
                encode(od_readings.od_raw[channel]),
                qos=QOS.EXACTLY_ONCE,
            )

    @staticmethod
    def _log_relative_intensity_of_ir_led(cls, od_readings) -> None:
        if int(od_readings.timestamp[-3:-1]) % 3 == 0:  # some pseudo randomness
            cls.relative_intensity_of_ir_led = {
                # represents the relative intensity of the LED.
                "relative_intensity_of_ir_led": 1 / cls.ir_led_reference_tracker(1.0),
                "timestamp": od_readings.timestamp,
            }

    @staticmethod
    def _unblock_internal_event(cls, _) -> None:
        # post
        if cls.state != cls.READY:
            return

        cls._set_for_iterating.set()

    def _normalize_by_led_output(
        self, batched_readings: dict[pt.PdChannel, float]
    ) -> dict[pt.PdChannel, float]:
        return {
            ch: self.ir_led_reference_tracker(od_signal)
            for (ch, od_signal) in batched_readings.items()
        }

    def __iter__(self):
        return self

    def __next__(self):
        while self._set_for_iterating.wait():
            self._set_for_iterating.clear()
            return self.latest_reading


def find_ir_led_reference(od_angle_channel1, od_angle_channel2) -> Optional[pt.PdChannel]:
    if od_angle_channel1 == REF_keyword:
        return "1"
    elif od_angle_channel2 == REF_keyword:
        return "2"
    else:
        return None


def create_channel_angle_map(
    od_angle_channel1: Optional[pt.PdAngleOrREF], od_angle_channel2: Optional[pt.PdAngleOrREF]
) -> dict[pt.PdChannel, pt.PdAngle]:
    # Inputs are either None, or a string like "135", "90", "REF", ...
    # Example return dict: {"1": "90", "2": "45"}
    channel_angle_map: dict[pt.PdChannel, pt.PdAngle] = {}

    if od_angle_channel1 and od_angle_channel1 != REF_keyword:
        if od_angle_channel1 not in VALID_PD_ANGLES:
            raise ValueError(
                f"{od_angle_channel1=} is not a valid angle. Must be one of {VALID_PD_ANGLES}"
            )
        od_angle_channel1 = cast(pt.PdAngle, od_angle_channel1)
        channel_angle_map["1"] = od_angle_channel1

    if od_angle_channel2 and od_angle_channel2 != REF_keyword:
        if od_angle_channel2 not in VALID_PD_ANGLES:
            raise ValueError(
                f"{od_angle_channel2=} is not a valid angle. Must be one of {VALID_PD_ANGLES}"
            )

        od_angle_channel2 = cast(pt.PdAngle, od_angle_channel2)
        channel_angle_map["2"] = od_angle_channel2

    return channel_angle_map


def start_od_reading(
    od_angle_channel1: Optional[pt.PdAngleOrREF] = None,
    od_angle_channel2: Optional[pt.PdAngleOrREF] = None,
    interval: float = 1 / config.getfloat("od_config", "samples_per_second"),
    fake_data: bool = False,
    unit: Optional[str] = None,
    experiment: Optional[str] = None,
) -> ODReader:

    unit = unit or whoami.get_unit_name()
    experiment = experiment or whoami.get_latest_experiment_name()

    ir_led_reference_channel = find_ir_led_reference(od_angle_channel1, od_angle_channel2)
    channel_angle_map = create_channel_angle_map(od_angle_channel1, od_angle_channel2)

    channels = list(channel_angle_map.keys())

    ir_led_reference_tracker: IrLedReferenceTracker
    if ir_led_reference_channel is not None:
        ir_led_reference_tracker = PhotodiodeIrLedReferenceTracker(
            ir_led_reference_channel, ignore_blank=fake_data
        )
        channels.append(ir_led_reference_channel)
    else:
        ir_led_reference_tracker = NullIrLedReferenceTracker()

    return ODReader(
        channel_angle_map,
        interval=interval,
        unit=unit,
        experiment=experiment,
        adc_reader=ADCReader(channels=channels, fake_data=fake_data, interval=interval),
        ir_led_reference_tracker=ir_led_reference_tracker,
    )


@click.command(name="od_reading")
@click.option(
    "--od-angle-channel1",
    default=config.get("od_config.photodiode_channel", "1", fallback=None),
    type=click.STRING,
    show_default=True,
    help="specify the angle(s) between the IR LED(s) and the PD in channel 1, separated by commas. Don't specify if channel is empty.",
)
@click.option(
    "--od-angle-channel2",
    default=config.get("od_config.photodiode_channel", "2", fallback=None),
    type=click.STRING,
    show_default=True,
    help="specify the angle(s) between the IR LED(s) and the PD in channel 2, separated by commas. Don't specify if channel is empty.",
)
@click.option("--fake-data", is_flag=True, help="produce fake data (for testing)")
def click_od_reading(
    od_angle_channel1: pt.PdAngleOrREF, od_angle_channel2: pt.PdAngleOrREF, fake_data: bool
):
    """
    Start the optical density reading job
    """
    od = start_od_reading(
        od_angle_channel1,
        od_angle_channel2,
        fake_data=fake_data or whoami.is_testing_env(),
    )
    od.block_until_disconnected()
