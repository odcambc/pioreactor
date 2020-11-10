# -*- coding: utf-8 -*-
"""
This job runs on the leader, and is a replacement for the NodeRed aggregation job.
"""
import signal
import time
import os
import traceback
import click
import json

from morbidostat.pubsub import subscribe_and_callback
from morbidostat.background_jobs import BackgroundJob
from morbidostat.whoami import unit, experiment, hostname

JOB_NAME = os.path.splitext(os.path.basename((__file__)))[0]


def current_time():
    return time.time_ns() // 1_000_000


class LogAggregation(BackgroundJob):
    def __init__(self, topics, output, **kwargs):
        super(LogAggregation, self).__init__(job_name=JOB_NAME, **kwargs)
        self.topics = topics
        self.output = output
        self.aggregated_log_table = self.read()
        self.start_passive_listeners()

    def on_message(self, message):
        try:
            unit = message.topic.split("/")[1]
            self.aggregated_log_table.append(
                {"timestamp": current_time(), "message": message.payload.decode(), "topic": message.topic}
            )

            self.write()
        except:
            traceback.print_exc()
        return

    def clear(self, message):
        payload = message.payload
        if message is None:
            self.aggregated_log_table = []
            self.write()
        else:
            pubsub.publish(
                f"morbidostat/{self.unit}/{self.experiment}/log", "Only empty messages allowed to empty the log table."
            )

    def read(self):
        try:
            with open(self.output, "r") as f:
                return json.dump(f)
        except Exception as e:
            print(e)
            return []

    def write(self):
        with open(self.output, "w") as f:
            json.dump(self.aggregated_log_table, f)

    def start_passive_listeners(self):
        subscribe_and_callback(self.on_message, self.topics)
        subscribe_and_callback(self.clear, f"morbidostat/{self.unit}/{self.experiment}/{self.job_name}/aggregated_log_table/set")

        super(LogAggregation, self).start_passive_listeners()


@click.command()
@click.option(
    "--output", "-o", default="/home/pi/morbidostatui/backend/build/data/all_morbidostat.log.json", help="the output file"
)
@click.option("--verbose", "-v", count=True, help="print to std.out")
def run(output, verbose):
    logs = LogAggregation(
        [f"morbidostat/+/{experiment}/log", f"morbidostat/+/{experiment}/error_log"],
        output,
        experiment=experiment,
        unit=unit,
        verbose=verbose,
    )

    while True:
        signal.pause()


if __name__ == "__main__":
    run()
