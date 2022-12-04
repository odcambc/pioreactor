# -*- coding: utf-8 -*-
from __future__ import annotations

from contextlib import contextmanager
from json import dumps
from typing import Any
from typing import Iterator
from typing import Optional

from pioreactor.exc import PWMError
from pioreactor.logging import create_logger
from pioreactor.logging import Logger
from pioreactor.pubsub import Client
from pioreactor.pubsub import create_client
from pioreactor.types import GpioPin
from pioreactor.utils import gpio_helpers
from pioreactor.utils import local_intermittent_storage
from pioreactor.whoami import is_testing_env

if is_testing_env():
    from pioreactor.utils.mock import MockHardwarePWM as HardwarePWM
else:
    try:
        from rpi_hardware_pwm import HardwarePWM  # type: ignore
    except ImportError:
        pass

PWM_LOCKED = "locked"


class PWM:
    """
    This class abstracts out the Rpi's PWM library details


    Notes
    -------
    There is a soft locking feature, `lock` and `is_locked`, that a program can use to
    present other programs from using the PWM channel. This may move to a hard lock in the future.



    Example
    -----------
    > from pioreactor.utils.pwm import PWM
    > pwm = PWM(12, 50)
    > pwm.start(20) # 20% duty cycle
    >
    > pwm.change_duty_cycle(25) # 25% duty cycle
    >
    > pwm.stop()
    > pwm.cleanup() # make sure to cleanup! Or use context manager, see below.


    Use as a context manager:

    >with PMW(12, 15) as pwm:
    >    pwm.start(100
    >    time.sleep(10)


    > # locking
    > pwm.lock()
    > pwm.is_locked() # true, and will be true for any other PWM on this channel.
    > pwm.unlock()
    > pwm.is_locked() # false, .cleanup() and Python's deconstruction will also unlock.
    >
    > with pwm.lock_temporarily():
    >    # do stuff, will unlock on exit of context statement.
    >
    """

    HARDWARE_PWM_CHANNELS: dict[GpioPin, int] = {12: 0, 13: 1}

    def __init__(
        self,
        pin: GpioPin,
        hz: float,
        unit: str,
        experiment: str,
        always_use_software: bool = False,
        pubsub_client: Optional[Client] = None,
        logger: Optional[Logger] = None,
    ) -> None:
        self.unit = unit
        self.experiment = experiment

        if pubsub_client is None:
            self.pubsub_client = create_client()
        else:
            self.pubsub_client = pubsub_client

        if logger is None:
            self.logger = create_logger("PWM", experiment=self.experiment, unit=self.unit)
        else:
            self.logger = logger

        self.pin = pin
        self.hz = hz
        self.duty_cycle = 0.0

        if self.is_locked():
            self.logger.error(
                f"GPIO-{self.pin} is currently locked but a task is overwriting it. Either too many jobs are trying to access this pin, or a job didn't clean up properly."
            )
            raise PWMError(
                f"GPIO-{self.pin} is currently locked but a task is overwriting it. Either too many jobs are trying to access this pin, or a job didn't clean up properly."
            )

        gpio_helpers.set_gpio_availability(self.pin, False)

        if (not always_use_software) and (pin in self.HARDWARE_PWM_CHANNELS):

            self.pwm = HardwarePWM(self.HARDWARE_PWM_CHANNELS[self.pin], self.hz)

        else:

            import RPi.GPIO as GPIO  # type: ignore

            GPIO.setwarnings(
                False
            )  # we already "registered" this GPIO in the EEPROM, ignore GPIO telling us again.
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT, initial=GPIO.LOW)

            if self.hz >= 1000:
                self.logger.warning(
                    "Setting a PWM to a very high frequency with software. Did you mean to use a hardware PWM?"
                )

            self.pwm = GPIO.PWM(self.pin, self.hz)

        with local_intermittent_storage("pwm_hz") as cache:
            cache[self.pin] = self.hz

        self.logger.debug(
            f"Initialized GPIO-{self.pin} using {'hardware' if self.using_hardware else 'software'}-timing, initial frequency = {self.hz} hz."
        )

    @property
    def using_hardware(self) -> bool:
        try:
            return isinstance(self.pwm, HardwarePWM)
        except AttributeError:
            return False

    def start(self, initial_duty_cycle: float) -> None:
        if not (0 <= initial_duty_cycle <= 100):
            raise PWMError("duty_cycle should be between 0 and 100, inclusive.")

        self.duty_cycle = float(initial_duty_cycle)
        self.pwm.start(round(self.duty_cycle, 5))

        current_values = {}
        with local_intermittent_storage("pwm_dc") as cache:
            cache[self.pin] = self.duty_cycle
            for pin in cache.iterkeys():
                current_values[pin] = cache[pin]

        self.pubsub_client.publish(
            f"pioreactor/{self.unit}/{self.experiment}/pwms/dc", dumps(current_values), retain=True
        )

    def stop(self) -> None:
        self.pwm.stop()

        current_values = {}
        with local_intermittent_storage("pwm_dc") as cache:
            cache[self.pin] = 0.0
            for pin in cache.iterkeys():
                current_values[pin] = cache[pin]

        self.pubsub_client.publish(
            f"pioreactor/{self.unit}/{self.experiment}/pwms/dc", dumps(current_values), retain=True
        )

    def change_duty_cycle(self, duty_cycle: float) -> None:
        if not (0.0 <= duty_cycle <= 100.0):
            raise PWMError("duty_cycle should be between 0 and 100, inclusive.")

        self.duty_cycle = float(duty_cycle)

        if self.using_hardware:
            self.pwm.change_duty_cycle(round(self.duty_cycle, 5))
        else:
            self.pwm.ChangeDutyCycle(self.duty_cycle)  # type: ignore

        current_values = {}
        with local_intermittent_storage("pwm_dc") as cache:
            cache[self.pin] = self.duty_cycle
            for pin in cache.iterkeys():
                current_values[pin] = cache[pin]

        self.pubsub_client.publish(
            f"pioreactor/{self.unit}/{self.experiment}/pwms/dc", dumps(current_values), retain=True
        )

    def cleanup(self) -> None:
        self.stop()
        self.unlock()

        with local_intermittent_storage("pwm_dc") as cache:
            cache.pop(self.pin)

        with local_intermittent_storage("pwm_hz") as cache:
            cache.pop(self.pin)

        gpio_helpers.set_gpio_availability(self.pin, True)

        if self.using_hardware:
            # `stop` handles cleanup.
            pass
        else:

            import RPi.GPIO as GPIO

            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT, initial=GPIO.LOW)
            GPIO.cleanup(self.pin)

        self.logger.debug(f"Cleaned up GPIO-{self.pin}.")

    def is_locked(self) -> bool:
        with local_intermittent_storage("pwm_locks") as pwm_locks:
            return pwm_locks.get(self.pin) == PWM_LOCKED

    def lock(self) -> None:
        with local_intermittent_storage("pwm_locks") as pwm_locks:
            pwm_locks[self.pin] = PWM_LOCKED

    def unlock(self) -> None:
        with local_intermittent_storage("pwm_locks") as pwm_locks:
            if self.pin in pwm_locks:
                del pwm_locks[self.pin]

    @contextmanager
    def lock_temporarily(self) -> Iterator[None]:
        try:
            self.lock()
            yield
        finally:
            self.unlock()

    def __exit__(self, *args: Any) -> None:
        self.cleanup()

    def __enter__(self) -> PWM:
        return self
