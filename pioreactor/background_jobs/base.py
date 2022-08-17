# -*- coding: utf-8 -*-
from __future__ import annotations

import atexit
import signal
import threading
import time
import typing as t

from msgspec.json import decode as loads
from msgspec.json import encode as dumps

from pioreactor import structs
from pioreactor import types as pt
from pioreactor.logging import create_logger
from pioreactor.pubsub import Client
from pioreactor.pubsub import create_client
from pioreactor.pubsub import QOS
from pioreactor.utils import append_signal_handlers
from pioreactor.utils import local_intermittent_storage
from pioreactor.whoami import get_uuid
from pioreactor.whoami import is_testing_env
from pioreactor.whoami import UNIVERSAL_IDENTIFIER

T = t.TypeVar("T")


def cast_bytes_to_type(value: bytes, type_: str):
    try:
        if type_ == "string":
            return value.decode()
        elif type_ == "float":
            return float(value)
        elif type_ == "integer":
            return int(value)
        elif type_ == "boolean":
            return bool(value)
        elif type_ == "json":
            return loads(value)
        elif type_ == "Automation":
            return loads(value, type=t.Union[structs.LEDAutomation, structs.TemperatureAutomation, structs.DosingAutomation])  # type: ignore
        raise TypeError(f"{type_} not found.")
    except Exception as e:
        raise e


def format_with_optional_units(value: pt.PublishableSettingDataType, units: t.Optional[str]) -> str:
    """
    Ex:
    > format_with_optional_units(25.0, "cm") # returns "25.0 cm"
    > format_with_optional_units(25.0, None) # returns "25.0"
    """
    if units is None:
        return f"{value}"
    elif units == "%":
        return f"{value}{units}"
    else:
        return f"{value} {units}"


class LoggerMixin:

    _logger_name: t.Optional[str] = None
    _logger = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def logger(self):
        if self._logger is None:
            self._logger = create_logger(name=self._logger_name or self.__class__.__name__)
        return self._logger


class PostInitCaller(type):
    def __call__(cls, *args, **kwargs):
        obj = type.__call__(cls, *args, **kwargs)
        obj.__post__init__()
        return obj


class _BackgroundJob(metaclass=PostInitCaller):

    """
    State management & hooks
    ---------------------------

    So this class controls most of the state convention that we follow (states inspired by Homie):

                                        ┌──────────┐
                                        │          │
         ┌──────────────────────┬───────►   lost   ◄────────┐
         │                      │       │          │        │
         │                      │       └─────▲────┘        │
         │                      │             │             │
    ┌────┴─────┐          ┌─────┴──────┐      │     ┌───────┴──────┐
    │          │          │            │      │     │              │
    │   init   ├──────────►    ready   ├──────┼─────► disconnected │
    │          │          │            │      │     │              │
    └──────────┘          └────┬──▲────┘      │     └──────▲───────┘
                               │  │           │            │
                               │  │           │            │
                               │  │           │            │
                          ┌────▼──┴────┬──────┘            │
                          │            │                   │
                          │  sleeping  ├───────────────────┘
                          │            │
                          └────────────┘

    https://asciiflow.com/#/share/eJzNVEsKgzAQvYrM2lW7Uc%2BSjehQAmksmoIi3qJ4kC7F0%2FQkTS0tRE0crYuGWWSEeZ95wRpkfEaI5FUIH0RcYQ4R1AxKBlEYhD6DSt8OwVHfFJZKNww8wnnc%2BsViTBKhjOYzRqHYXm2nKURWqBdT2xFsGDrnDYytzLsioEzVjr5sQ7b2GoOyNWOGWCbn2pzewizaR0bmyCab%2BAJyyRVJ0PBSvBzjtFphQE%2BlvEgyKTFRmBrU%2B3rZIzXL%2B3Kn1t4dqXn2M6Cafqbu%2FxxicYHUSBZpHC0VoBCIFy5PP%2F9TV%2Bgkb87BBg00T7Hk%2FaY%3D)

    1. The job starts in `init`,
        - we publish `published_settings`: a list of variables that will be sent to the broker on initialization and retained.
        - we set up how to disconnect
        - the subclass runs their __init__ method
    2. The job moves to `ready`, and can be paused by entering `sleeping`.
    3. We catch key interrupts and kill signals from the underlying machine, and set the state to `disconnected`.
    4. If the job exits otherwise (kill -9, power loss, bug), the state is `lost`, and a last-will saying so is broadcast.

    When changing state, it's recommend to use `set_state(new_state)`.

    When going from state S to state T, a function `on_{S}_to_{T}` is called, and then a
    function `on_{T}` is called. These can be overwritten in subclasses for specific usecases (ex: sleeping should turn off a motor,
    and  going from sleeping to ready should restart the motor.)


    Editing properties
    ---------------------

    This class handles the fanning out of class attributes, and the setting of those attributes. Use
    `pioreactor/<unit>/<experiment>/<job_name>/<attr>/set` to set an attribute remotely.

    Hooks can be set up when property `p` changes. The function `set_p(self, new_value)`
    will be called (if defined) whenever `p` changes over MQTT.

    On __init__, attributes are broadcast under `pioreactor/<unit>/<experiment>/<job_name>/$properties`,
    and each has
     - `pioreactor/<unit>/<experiment>/<job_name>/$settable` set to True or False
     - `pioreactor/<unit>/<experiment>/<job_name>/$datatype` set to its datatype
     - `pioreactor/<unit>/<experiment>/<job_name>/$unit` set to its unit (optional)


    Best code practices of background jobs
    ---------------------------------------

    Because of the setup, connections, and tear downs of background jobs, the best practices of using
    background jobs is as follows:

    1. Use context managers

    > with Stirrer(duty_cycle=50, unit=unit, experiment=experiment) as stirrer:
    >     stirrer.start_stirring()
    >     ...
    >

    This will gracefully disconnect and cleanup the job, provided you clean up in the `on_disconnected` function.

    2. Clean up yourself. The following is **not** recommended as it does not cleanup connections and state even after the function exits:

    > def do_some_stirring():
    >     st = Stirrer(duty_cycle=50, unit=unit, experiment=experiment)
    >     return

    Instead do something like:

    > def do_some_stirring():
    >     st = Stirrer(duty_cycle=50, unit=unit, experiment=experiment)
    >     ...
    >     st.clean_up()
    >    return

    When Python exits, jobs will also clean themselves up, so this also works as a script:

    > if __name__ == "__main__":
    >    st = Stirrer(...)
    >

    If you want the script to pause until the job disconnects, use

    > if __name__ == "__main__":
    >    st = Stirrer(...)
    >
    >    st.block_until_disconnected()
    >



    Parameters
    -----------

    job_name: str
        the name of the job
    source: str
        the source of where this job lives. "app" if main code base, <plugin name> if from a plugin, etc. This is used in logging.
    experiment: str
    unit: str
    """

    # Homie lifecycle (normally per device (i.e. an rpi) but we are using it for "nodes", in Homie parlance)
    INIT: pt.JobState = "init"
    READY: pt.JobState = "ready"
    DISCONNECTED: pt.JobState = "disconnected"
    SLEEPING: pt.JobState = "sleeping"
    LOST: pt.JobState = "lost"
    LIFECYCLE_STATES: set[pt.JobState] = {INIT, READY, DISCONNECTED, SLEEPING, LOST}

    # initial state is disconnected
    state: pt.JobState = DISCONNECTED

    # published_settings is typically overwritten in the subclasses. Attributes here will
    # be published to MQTT and available settable attributes will be editable. Currently supported
    # attributes are
    # {'datatype', 'unit', 'settable', 'persist'}
    # See pt.PublishableSetting type
    published_settings: dict[str, pt.PublishableSetting] = dict()

    # these are used elsewhere in our software
    DISALLOWED_JOB_NAMES = {
        "run",
        "dosing_events",
        "leds",
        "led_change_events",
        "unit_label",
    }

    def __init__(self, job_name: str, source: str, experiment: str, unit: str) -> None:
        if job_name in self.DISALLOWED_JOB_NAMES:
            raise ValueError("job name not allowed")

        self.job_name = job_name
        self.experiment = experiment
        self.unit = unit
        self._clean = False

        self.logger = create_logger(
            self.job_name, unit=self.unit, experiment=self.experiment, source=source
        )

        # _check_for_duplicate_activity checks _before_ the pubsub client,
        # as they will set (and revoke) a new last will.
        # Ex: job X is running, but we try to rerun it, causing the latter job to abort, and
        # potentially firing the last_will
        # TODO: I don't think this is true anymore, since we append an id() to the client id now.
        self._check_for_duplicate_activity()

        # why do we need two clients? Paho lib can't publish a message in a callback,
        # but this is critical to our usecase: listen for events, and fire a response (ex: state change)
        # so we split the listening and publishing. I've tried combining them and got stuck a lot
        # https://github.com/Pioreactor/pioreactor/blob/cb54974c9be68616a7f4fb45fe60fdc063c81238/pioreactor/background_jobs/base.py
        # See issue: https://github.com/eclipse/paho.mqtt.python/issues/527
        # The order we add them to the list is important too, as disconnects occur async,
        # we want to give the sub_client (has the will msg) as much time as possible to disconnect.
        self.pub_client = self._create_pub_client()
        self.sub_client = self._create_sub_client()

        self._set_up_exit_protocol()
        self._blocking_event = threading.Event()

        try:
            # this is one function in the __init__ that we may deliberately raise an error
            # if we do raise an error, the class needs to be cleaned up correctly
            # (hence the _cleanup bit, don't use set_state, as it will no-op since we are already in state DISCONNECTED)
            # but we still raise the error afterwards.
            self._check_published_settings(self.published_settings)
        except ValueError as e:
            self._clean_up_resources()
            raise e
        finally:
            self._publish_properties_to_broker(self.published_settings)
            self._publish_settings_to_broker(self.published_settings)

        # this happens _after_ pub clients are set up
        self.add_to_published_settings(
            "state",
            {
                "datatype": "string",
                "settable": True,
                "persist": True,
            },
        )

        self.start_general_passive_listeners()

        # let's move to init, next thing that run is the subclasses __init__
        self.set_state(self.INIT)

    def __post__init__(self) -> None:
        # this function is called AFTER the subclass' __init__ finishes
        self.set_state(self.READY)

    def start_passive_listeners(self) -> None:
        # overwrite this to in subclasses to subscribe to topics in MQTT
        # using this handles reconnects correctly.
        pass

    # subclasses to override these to perform certain actions on a state transfer
    def on_ready(self) -> None:
        # specific things to do when is ready (again)
        pass

    def on_init(self) -> None:
        # Note: this is called after this classes __init__, but before the subclasses __init__
        pass

    def on_sleeping(self) -> None:
        # specific things to do when a job sleeps / pauses
        pass

    def on_disconnected(self) -> None:
        # specific things to do when a job disconnects / exits
        pass

    def on_disconnected_to_ready(self) -> None:
        pass

    def on_ready_to_disconnected(self) -> None:
        pass

    def on_disconnected_to_sleeping(self) -> None:
        pass

    def on_sleeping_to_disconnected(self) -> None:
        pass

    def on_disconnected_to_init(self) -> None:
        pass

    def on_init_to_disconnected(self) -> None:
        pass

    def on_ready_to_sleeping(self) -> None:
        pass

    def on_sleeping_to_ready(self) -> None:
        pass

    def on_ready_to_init(self) -> None:
        pass

    def on_init_to_ready(self) -> None:
        pass

    def on_sleeping_to_init(self) -> None:
        pass

    def on_init_to_sleeping(self) -> None:
        pass

    def publish(
        self,
        topic: str,
        payload: pt.PublishableSettingDataType | dict | bytes | None,
        qos: int = 0,
        **kwargs,
    ) -> None:
        """
        Publish payload to topic.

        This will convert the payload to a json blob if MQTT does not allow its original type.
        """

        if not isinstance(payload, (str, bytearray, bytes, int, float)) and (payload is not None):
            payload = dumps(payload)

        self.pub_client.publish(topic, payload=payload, **kwargs)

    def subscribe_and_callback(
        self,
        callback: t.Callable[[pt.MQTTMessage], None],
        subscriptions: list[str] | str,
        allow_retained: bool = True,
        qos: int = 0,
    ) -> None:
        """
        Parameters
        -------------
        callback: callable
            Callbacks only accept a single parameter, message.
        subscriptions: str, list of str
        allow_retained: bool
            if True, all messages are allowed, including messages that the broker has retained. Note
            that client can fire a msg with retain=True, but because the broker is serving it to a
            subscriber "fresh", it will have retain=False on the client side. More here:
            https://github.com/eclipse/paho.mqtt.python/blob/master/src/paho/mqtt/client.py#L364
        qos: int
            see pioreactor.pubsub.QOS
        """

        def wrap_callback(actual_callback: t.Callable[..., T]) -> t.Callable[..., t.Optional[T]]:
            def _callback(client, userdata, message: pt.MQTTMessage) -> t.Optional[T]:
                if not allow_retained and message.retain:
                    return None
                try:
                    return actual_callback(message)
                except Exception as e:
                    self.logger.error(e)
                    self.logger.debug(e, exc_info=True)
                    raise e

            return _callback

        assert callable(
            callback
        ), "callback should be callable - do you need to change the order of arguments?"

        subscriptions = [subscriptions] if isinstance(subscriptions, str) else subscriptions

        for sub in subscriptions:
            self.sub_client.message_callback_add(sub, wrap_callback(callback))
            self.sub_client.subscribe(sub, qos=qos)
        return

    def set_state(self, new_state: pt.JobState) -> None:
        if new_state not in self.LIFECYCLE_STATES:
            self.logger.error(f"saw {new_state}: not a valid state")
            return

        if new_state == self.state:
            return

        if hasattr(self, f"on_{self.state}_to_{new_state}"):
            try:
                getattr(self, f"on_{self.state}_to_{new_state}")()
            except Exception as e:
                self.logger.debug(f"Error in on_{self.state}_to_{new_state}")
                self.logger.debug(e, exc_info=True)
                self.logger.error(e)
                return

        getattr(self, new_state)()

    def block_until_disconnected(self) -> None:
        """
        This will block the main thread until disconnected() is called.

        This will unblock if:

        1. a kill/keyboard interrupt signal is sent
        2. state is set to "disconnected" over MQTT or programmatically

        Useful for standalone jobs (and with click). Ex:

        > if __name__ == "__main__":
        >     job = Job(...)
        >     job.block_until_disconnected()


        """
        self.logger.debug(f"{self.job_name} is blocking until disconnected")
        self._blocking_event.wait()

    def blink_error_code(self, error_code: int) -> None:
        """
        Publish the error code to `monitor` job s.t. it will make the Pioreactor blink.
        See pioreactor.error_codes
        """
        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/monitor/flicker_led_with_error_code",
            error_code,
        )

    def clean_up(self):
        """
        Disconnect from brokers, set state to "disconnected", stop any activity.
        """
        self.set_state(self.DISCONNECTED)
        self._clean_up_resources()

    def add_to_published_settings(self, setting: str, props: pt.PublishableSetting) -> None:
        """
        Add a pair to self.published_settings.
        """
        new_setting_pair = {setting: props}
        self._check_published_settings(new_setting_pair)
        # we need create a new dict (versus just a key update), since published_settings is a class level prop, and editing this would have effects for other BackgroundJob classes.
        self.published_settings = self.published_settings | new_setting_pair
        self._publish_properties_to_broker(self.published_settings)
        self._publish_settings_to_broker(new_setting_pair)

    ########### Private #############

    @staticmethod
    def _check_published_settings(published_settings: dict[str, pt.PublishableSetting]) -> None:
        necessary_properies = set(["datatype", "settable"])
        optional_properties = set(["unit", "persist"])
        all_properties = optional_properties.union(necessary_properies)
        for setting, properties in published_settings.items():
            # look for extra properties
            if not all_properties.issuperset(properties.keys()):
                raise ValueError(f"Found extra property in setting `{setting}`.")

            # look for missing properties
            if not set(properties.keys()).issuperset(necessary_properies):
                raise ValueError(
                    f"Missing necessary property in setting `{setting}`. All settings require at least {necessary_properies}"
                )

            # correct syntax in setting name?
            if not all(ss.isalnum() for ss in setting.split("_")):
                # only alphanumeric separated by _ is allowed.
                raise ValueError(
                    f"setting {setting} has bad characters - must be alphanumeric, and only separated by underscore."
                )

    def _create_pub_client(self) -> Client:
        # see note above as to why we split pub and sub.
        client = create_client(
            client_id=f"{self.unit}-pub-{self.experiment}-{self.job_name}-{get_uuid()}-{id(self)}"
        )

        return client

    def _create_sub_client(self) -> Client:
        # see note above as to why we split pub and sub.

        # the client will try to automatically reconnect if something bad happens
        # when we reconnect to the broker, we want to republish our state
        # to overwrite potential last-will losts...
        # also reconnect to our old topics.
        def reconnect_protocol(client: Client, userdata, flags, rc: int, properties=None):
            self.logger.info("Reconnected to the MQTT broker on leader.")  # type: ignore
            self._publish_attr("state")
            self.start_general_passive_listeners()
            self.start_passive_listeners()

        def on_disconnect(client, userdata, rc: int) -> None:
            self._on_mqtt_disconnect(client, rc)

        # we give the last_will to this sub client because when it reconnects, it
        # will republish state.
        last_will = {
            "topic": f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/$state",
            "payload": self.LOST,
            "qos": QOS.EXACTLY_ONCE,
            "retain": True,
        }

        client = create_client(
            client_id=f"{self.unit}-sub-{self.experiment}-{self.job_name}-{get_uuid()}-{id(self)}",
            last_will=last_will,
            keepalive=60,
            clean_session=False,  # this, in theory, will reconnect to old subs when we reconnect.
        )
        # we catch exceptions and report them in our software
        client.suppress_exceptions = True

        # the client connects async, but we want it to be connected before adding
        # our reconnect callback
        for _ in range(200):
            if not client.is_connected():
                time.sleep(0.01)
            else:
                break

        client.on_connect = reconnect_protocol
        client.on_disconnect = on_disconnect
        return client

    def _on_mqtt_disconnect(self, client, rc: int) -> None:
        from paho.mqtt import client as mqtt  # type: ignore

        if rc == mqtt.MQTT_ERR_SUCCESS:
            # MQTT_ERR_SUCCESS means that the client disconnected using disconnect()
            self.logger.debug("Disconnected successfully from MQTT.")

        # we won't exit, but the client object will try to reconnect
        # Error codes are below, but don't always align
        # https://github.com/eclipse/paho.mqtt.python/blob/42f0b13001cb39aee97c2b60a3b4807314dfcb4d/src/paho/mqtt/client.py#L147
        elif rc == mqtt.MQTT_ERR_KEEPALIVE:
            self.logger.error(
                "Lost contact with MQTT server. Is the leader Pioreactor still online?"
            )
        else:
            self.logger.debug(f"Disconnected from MQTT with {rc=}: {mqtt.error_string(rc)}")
        return

    def _publish_attr(self, attr: str) -> None:
        """
        Publish the current value of the class attribute `attr` to MQTT.
        """
        if attr == "state":
            attr_name = "$state"
        else:
            attr_name = attr

        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{attr_name}",
            getattr(self, attr),
            retain=True,
            qos=QOS.EXACTLY_ONCE,
        )

    def _set_up_exit_protocol(self) -> None:
        # here, we set up how jobs should disconnect and exit.
        def exit_gracefully(reason: int | str, *args) -> None:
            if self._clean:
                return

            if isinstance(reason, int):
                self.logger.debug(f"Exiting caused by signal {signal.strsignal(reason)}.")
            elif isinstance(reason, str):
                self.logger.debug(f"Exiting caused by {reason}.")

            self.clean_up()

            if (reason == signal.SIGTERM) or (reason == getattr(signal, "SIGHUP", None)):
                import sys

                sys.exit()

        # signals only work in main thread - and if we set state via MQTT,
        # this would run in a thread - so just skip.
        if threading.current_thread() is threading.main_thread():
            atexit.register(exit_gracefully, "Python atexit")

            # terminate command, ex: pkill, kill
            append_signal_handlers(signal.SIGTERM, [exit_gracefully])

            # keyboard interrupt
            append_signal_handlers(
                signal.SIGINT,
                [
                    exit_gracefully,
                    # add a "ignore all future SIGINTs" onto the top of the stack.
                    lambda *args: signal.signal(signal.SIGINT, signal.SIG_IGN),
                ],
            )

            try:
                # ssh closes
                append_signal_handlers(
                    signal.SIGHUP,
                    [
                        exit_gracefully,
                        # add a "ignore all future SIGUPs" onto the top of the stack.
                        lambda *args: signal.signal(signal.SIGHUP, signal.SIG_IGN),
                    ],
                )
            except AttributeError:
                # SIGHUP is only available on unix machines
                pass

    def init(self) -> None:
        self.state = self.INIT

        try:
            # we delay the specific on_init until after we have done our important protocols.
            self.on_init()
        except Exception as e:
            self.logger.error(e)
            self.logger.debug(e, exc_info=True)
            self.clean_up()
            raise e

        self._log_state(self.state)

    def ready(self) -> None:
        self.state = self.READY
        with local_intermittent_storage("pio_jobs_running") as cache:
            # we set the "lock" in ready as then we know the __init__ finished successfully. Previously,
            # __init__ might fail, and not clean up pio_jobs_running correctly.
            # the catch is that there is a window where two jobs can be started, see growth_rate_calculating.
            # sol for authors: move the long-running parts to the on_init_to_ready function.
            # TODO: a potential fix is to include a timestamp of when the value changed??
            cache[self.job_name] = b"1"
        try:
            self.on_ready()
        except Exception as e:
            self.logger.error(e)
            self.logger.debug(e, exc_info=True)

        self._log_state(self.state)

    def sleeping(self) -> None:
        self.state = self.SLEEPING

        try:
            self.on_sleeping()
        except Exception as e:
            self.logger.error(e)
            self.logger.debug(e, exc_info=True)

        self._log_state(self.state)

    def lost(self) -> None:
        # TODO: what should happen when a running job recieves a lost signal? When does it ever
        # receive a lost signal?
        # 1. Monitor can send a lost signal if `check_against_processes_running` triggers.
        # I think it makes sense to ignore it?

        self.state = self.LOST
        self._log_state(self.state)

    def disconnected(self) -> None:
        # set state to disconnect
        # call this first to make sure that it gets published to the broker.
        self.state = self.DISCONNECTED

        # call job specific on_disconnected to clean up subjobs, etc.
        # however, if it fails, nothing below executes, so we don't get a clean
        # disconnect, etc.
        # ideally, the on_disconnected shouldn't care what state it was in prior to being called.
        try:
            self.on_disconnected()

        except Exception as e:
            # since on_disconnected errors are common (see point below), we don't bother
            # making the visible to the user.
            # They are common when the user quickly starts a job then stops a job.
            self.logger.debug(e, exc_info=True)

        # remove attrs from MQTT
        self._clear_mqtt_cache()

        self._log_state(self.state)

        # we "set" the internal event, which will cause any event.waits to finishing blocking.
        self.logger.debug(f"{self.job_name} is unblocking.")
        self._blocking_event.set()

    def _remove_from_cache(self):
        with local_intermittent_storage("pio_jobs_running") as cache:
            if self.job_name in cache:
                del cache[self.job_name]

    def _disconnect_from_loggers(self):
        # clean up logger handlers
        while len(self.logger.handlers) > 0:
            handler = self.logger.handlers[0]
            handler.close()
            self.logger.removeHandler(handler)

    def _disconnect_from_mqtt_clients(self):
        # disconnect from MQTT
        self.sub_client.loop_stop()
        self.sub_client.disconnect()

        # this HAS to happen last, because this contains our publishing client
        self.pub_client.loop_stop()  # pretty sure this doesn't close the thread if in a thread: https://github.com/eclipse/paho.mqtt.python/blob/master/src/paho/mqtt/client.py#L1835
        self.pub_client.disconnect()

    def _clean_up_resources(self):
        # Explicitly cleanup resources...
        self._disconnect_from_mqtt_clients()
        self._disconnect_from_loggers()
        # remove from temp. `pio_jobs_running` cache
        self._remove_from_cache()

        self._clean = True

    def _publish_properties_to_broker(
        self, published_settings: dict[str, pt.PublishableSetting]
    ) -> None:
        # this follows some of the Homie convention: https://homieiot.github.io/specification/
        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/$properties",
            ",".join(published_settings),
            qos=QOS.AT_LEAST_ONCE,
            retain=True,
        )

    def _publish_settings_to_broker(
        self, published_settings: dict[str, pt.PublishableSetting]
    ) -> None:
        # this follows some of the Homie convention: https://homieiot.github.io/specification/
        for setting, props in published_settings.items():
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{setting}/$settable",
                props["settable"],
                qos=QOS.AT_LEAST_ONCE,
                retain=True,
            )
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{setting}/$datatype",
                props["datatype"],
                qos=QOS.AT_LEAST_ONCE,
                retain=True,
            )
            if props.get("unit"):
                self.publish(
                    f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{setting}/$unit",
                    props["unit"],
                    qos=QOS.AT_LEAST_ONCE,
                    retain=True,
                )

    def _log_state(self, state: pt.JobState) -> None:
        if state == self.READY or state == self.DISCONNECTED:
            self.logger.info(state.capitalize() + ".")
        else:
            self.logger.debug(state.capitalize() + ".")

    def _set_attr_from_message(self, message: pt.MQTTMessage) -> None:
        def get_attr_from_topic(topic: str) -> str:
            pieces = topic.split("/")
            return pieces[4].lstrip("$")

        attr = get_attr_from_topic(message.topic)

        if attr not in self.published_settings:
            self.logger.debug(f"Unable to set `{attr}` in {self.job_name}.")
            return
        elif not self.published_settings[attr]["settable"]:
            self.logger.debug(f"Unable to set `{attr}` in {self.job_name}. `{attr}` is read-only.")
            return

        previous_value = getattr(self, attr)
        new_value = cast_bytes_to_type(message.payload, self.published_settings[attr]["datatype"])

        # a subclass may want to define a `set_<attr>` method that will be used instead
        # for example, see Stirring.set_target_rpm, and `set_state` here
        if hasattr(self, f"set_{attr}"):
            getattr(self, f"set_{attr}")(new_value)

        else:
            setattr(self, attr, new_value)

        units = self.published_settings[attr].get("unit")
        self.logger.info(
            f"Updated {attr} from {format_with_optional_units(previous_value, units)} to {format_with_optional_units(getattr(self, attr), units)}."
        )

    def start_general_passive_listeners(self) -> None:
        # listen to changes in editable properties
        self.subscribe_and_callback(
            self._set_attr_from_message,
            [
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/+/set",
                # everyone listens to $BROADCAST
                f"pioreactor/{UNIVERSAL_IDENTIFIER}/{self.experiment}/{self.job_name}/+/set",
            ],
            allow_retained=False,
        )

    def _clear_mqtt_cache(self) -> None:
        """
        From homie: Devices can remove old properties and nodes by publishing a zero-length payload on the respective topics.
        Use "persist" to keep it from clearing.
        """
        self.publish(
            f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/$properties",
            None,
            retain=True,
        )

        for attr, metadata_on_attr in self.published_settings.items():
            if not metadata_on_attr.get("persist", False):
                self.publish(
                    f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{attr}",
                    None,
                    retain=True,
                )

            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{attr}/$settable",
                None,
                retain=True,
            )
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{attr}/$datatype",
                None,
                retain=True,
            )
            self.publish(
                f"pioreactor/{self.unit}/{self.experiment}/{self.job_name}/{attr}/$unit",
                None,
                retain=True,
            )

    def _check_for_duplicate_activity(self) -> None:
        with local_intermittent_storage("pio_jobs_running") as cache:
            if not is_testing_env() and (cache.get(self.job_name) == b"1"):
                self.logger.error(f"{self.job_name} is already running. Exiting.")
                raise RuntimeError(f"{self.job_name} is already running. Exiting.")

    def __setattr__(self, name: str, value: t.Any) -> None:
        super(_BackgroundJob, self).__setattr__(name, value)
        if name in self.published_settings:
            self._publish_attr(name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.clean_up()


class BackgroundJob(_BackgroundJob):
    """
    Native jobs should inherit from this class.
    """

    def __init__(self, job_name: str, experiment: str, unit: str) -> None:
        super().__init__(job_name=job_name, source="app", experiment=experiment, unit=unit)


class BackgroundJobContrib(_BackgroundJob):
    """
    Plugin jobs should inherit from this class.
    """

    def __init__(self, job_name: str, experiment: str, unit: str, plugin_name: str) -> None:
        super().__init__(job_name=job_name, source=plugin_name, experiment=experiment, unit=unit)
