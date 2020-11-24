# -*- coding: utf-8 -*-
# pubsub
import socket
import time
import threading
import traceback
from click import echo, style
from paho.mqtt import publish as mqtt_publish
from paho.mqtt import subscribe as mqtt_subscribe
from morbidostat.config import leader_hostname


class QOS:
    AT_MOST_ONCE = 0
    AT_LEAST_ONCE = 1
    EXACTLY_ONCE = 2


def publish(topic, message, hostname=leader_hostname, verbose=0, retries=10, **mqtt_kwargs):
    retry_count = 1
    while True:
        try:
            mqtt_publish.single(topic, payload=message, hostname=hostname, **mqtt_kwargs)

            if (verbose == 1 and topic.endswith("log")) or verbose > 1:
                current_time = time.strftime("%Y-%m-%d %H:%M:%S")
                echo(
                    style(f"{current_time} ", bold=True) + style(f"{topic}: ", fg="bright_blue") + style(f"{message}", fg="green")
                )
            return

        except (ConnectionRefusedError, socket.gaierror, OSError, socket.timeout) as e:
            # possible that leader is down/restarting, keep trying, but log to local machine.
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            echo(
                style(f"{current_time}:", fg="white")
                + style(f"Attempt {retry_count}: Unable to connect to host: {hostname}. {str(e)}", fg="red")
            )
            time.sleep(5 * retry_count)  # linear backoff
            retry_count += 1

        if retry_count == retries:
            raise ConnectionRefusedError(f"Unable to connect to host: {hostname}.")


def subscribe(topics, hostname=leader_hostname, retries=10, **mqtt_kwargs):
    retry_count = 1
    while True:
        try:
            return mqtt_subscribe.simple(topics, hostname=hostname, **mqtt_kwargs)

        except (ConnectionRefusedError, socket.gaierror, OSError, socket.timeout) as e:
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            # possible that leader is down/restarting, keep trying, but log to local machine.
            echo(
                style(f"{current_time}:", fg="white")
                + style(f"Attempt {retry_count}: Unable to connect to host: {hostname}. {str(e)}", fg="red")
            )
            time.sleep(5 * retry_count)  # linear backoff
            retry_count += 1

        if retry_count == retries:
            current_time = time.strftime("%Y-%m-%d %H:%M:%S")
            raise ConnectionRefusedError(f"Unable to connect to host: {hostname}.")


def subscribe_and_callback(callback, topics, hostname=leader_hostname, timeout=None, max_msgs=None, **mqtt_kwargs):
    """
    Creates a new thread, wrapping around paho's subscribe.callback. Callbacks only accept a single parameter, message.

    Parameters
    -------------
    timeout: float
        the client will  only listen for <timeout> seconds before disconnecting. (kinda)
    max_msgs: int
        the client will process <max_msgs> messages before disconnecting.


    """

    import paho.mqtt.client as mqtt

    assert callable(callback), "callback should be callable - do you need to change the order of arguments?"

    def on_connect(client, userdata, flags, rc):
        client.subscribe(userdata["topics"])

    def wrap_callback(actual_callback):
        def _callback(client, userdata, message):
            try:

                if "max_msgs" in userdata:
                    userdata["count"] += 1
                    if userdata["count"] > userdata["max_msgs"]:
                        client.loop_stop()
                        client.disconnect()
                        return

                return actual_callback(message)

            except Exception as e:
                traceback.print_exc()

                from morbidostat.whoami import unit, experiment

                publish(f"morbidostat/{unit}/{experiment}/error_log", str(e), verbose=1)
                raise e

        return _callback

    topics = [topics] if isinstance(topics, str) else topics
    userdata = {"topics": [(topic, mqtt_kwargs.pop("qos", 0)) for topic in topics]}

    if max_msgs:
        userdata["count"] = 0
        userdata["max_msgs"] = max_msgs

    client = mqtt.Client(userdata=userdata)
    client.on_connect = on_connect
    client.on_message = wrap_callback(callback)
    client.connect(leader_hostname, **mqtt_kwargs)
    client.loop_start()

    if timeout:
        threading.Timer(timeout, lambda: client.loop_stop()).start()

    return client


def prune_retained_messages(topics_to_prune="#", hostname=leader_hostname):
    topics = []

    def on_message(message):
        topics.append(message.topic)

    thread = subscribe_and_callback(on_message, topics_to_prune, hostname=hostname)
    thread.join(timeout=1)

    for topic in topics.copy():
        publish(topic, None, retain=True, hostname=hostname)
