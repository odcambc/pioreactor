# -*- coding: utf-8 -*-
"""
This action checks the following on the Pioreactor (using pytest):

1. Heating and temperature sensor by gradually increase heating's DC, and record temperature
    [x] do we detect the heating PCB over i2c?
    [x] is there a positive correlation between heating DC and temperature?

2. LEDs and PDs, ramp up each LED's output and record outputs from PDs (from ADC)
    [x] do we measure a positive correlation between any LED output and PD?
    [x] output should be a list of pairs (LED_X, PD_Y) where a positive correlation is detected
    [x] Detect the Pioreactor HAT
    [x] detect ambient light?

3. Stirring: ramp up output voltage for stirring and record RPM
    [ ] do we measure a positive correlation between stirring voltage and RPM?


Outputs from each check go into MQTT, and return to the command line.

"""

import time
import json
import click
import pytest
from collections import defaultdict
from pioreactor.whoami import (
    get_unit_name,
    get_latest_testing_experiment_name,
    get_latest_experiment_name,
    is_testing_env,
)
from pioreactor.background_jobs.temperature_control import TemperatureController
from pioreactor.background_jobs.od_reading import ADCReader, PD_CHANNELS
from pioreactor.utils import correlation
from pioreactor.pubsub import publish
from pioreactor.logging import create_logger
from pioreactor.actions.led_intensity import led_intensity, LED_CHANNELS
from pioreactor.utils import is_pio_job_running, publish_ready_to_disconnected_state
from pioreactor.background_jobs.stirring import start_stirring


@pytest.fixture(scope="session")
def unit():
    return get_unit_name()


@pytest.fixture(scope="session")
def experiment():
    return get_latest_testing_experiment_name()


@pytest.fixture(scope="session")
def logger():
    return create_logger("self_test", unit=unit, experiment=get_latest_experiment_name())


class SelfTestPlugin:
    def __init__(self):
        self.unit = get_unit_name()
        self.experiment = get_latest_testing_experiment_name()
        self.did_all_pass = True

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            test = report.nodeid.split("::")[1]
            publish(
                f"pioreactor/{self.unit}/{self.experiment}/self_test/{test}",
                int(report.passed),
            )
            self.did_all_pass &= report.passed

    def pytest_sessionfinish(self):
        publish(
            f"pioreactor/{self.unit}/{self.experiment}/self_test/all_tests_passed",
            int(self.did_all_pass),
            retain=True,
        )


def test_pioreactor_hat_present(unit, experiment):
    try:
        adc_reader = ADCReader(
            channels=PD_CHANNELS,
            unit=unit,
            experiment=experiment,
            dynamic_gain=False,
            initial_gain=16,
            fake_data=is_testing_env(),
        )
        adc_reader.setup_adc()
    except (AssertionError, OSError):
        assert False
    else:
        assert True
        adc_reader.set_state(adc_reader.DISCONNECTED)


def test_atleast_one_correlation_between_pds_and_leds(logger, unit, experiment):
    from pprint import pformat

    INTENSITIES = list(range(2, 58, 8))
    current_experiment_name = get_latest_experiment_name()
    results = {}

    adc_reader = ADCReader(
        channels=PD_CHANNELS,
        unit=unit,
        experiment=experiment,
        dynamic_gain=False,
        initial_gain=16,  # I think a small gain is okay, since we only varying the lower-end of LED intensity
        fake_data=is_testing_env(),
    )
    adc_reader.setup_adc()
    # set all to 0, but use original experiment name, since we indeed are setting them to 0.
    for led_channel in LED_CHANNELS:
        assert led_intensity(
            led_channel,
            intensity=0,
            unit=unit,
            source_of_event="self_test",
            experiment=current_experiment_name,
            verbose=False,
        )

    for led_channel in LED_CHANNELS:
        varying_intensity_results = defaultdict(list)
        for intensity in INTENSITIES:
            # turn on the LED to set intensity
            led_intensity(
                led_channel,
                intensity=intensity,
                unit=unit,
                experiment=current_experiment_name,
                verbose=False,
                source_of_event="self_test",
            )

            # record from ADC
            readings = adc_reader.take_reading()

            # Add to accumulating list
            for pd_channel in PD_CHANNELS:
                varying_intensity_results[pd_channel].append(readings[pd_channel])

        # compute the linear correlation between the intensities and observed PD measurements

        for pd_channel in PD_CHANNELS:
            results[(led_channel, pd_channel)] = round(
                correlation(INTENSITIES, varying_intensity_results[pd_channel]), 2
            )

        # set back to 0
        led_intensity(
            led_channel,
            intensity=0,
            unit=unit,
            experiment=current_experiment_name,
            verbose=False,
            source_of_event="self_test",
        )

    logger.debug(f"Correlations between LEDs and PD:\n{pformat(results)}")
    detected_relationships = []
    for pair, measured_correlation in results.items():
        if measured_correlation > 0.85:
            detected_relationships.append(pair)

    publish(
        f"pioreactor/{unit}/{experiment}/self_test/correlations_between_pds_and_leds",
        json.dumps(detected_relationships),
    )

    adc_reader.set_state(adc_reader.DISCONNECTED)

    assert len(detected_relationships) > 0


def test_ambient_light_interference(unit, experiment):
    # test ambient light IR interference. With all LEDs off, and the Pioreactor not in a sunny room, we should see near 0 light.
    # TODO: it's never 0 because of the common current problem.

    adc_reader = ADCReader(
        channels=PD_CHANNELS,
        unit=unit,
        experiment=experiment,
        dynamic_gain=False,
        initial_gain=16,
        fake_data=is_testing_env(),
    )
    adc_reader.setup_adc()

    for led_channel in LED_CHANNELS:
        assert led_intensity(
            led_channel,
            intensity=0,
            unit=unit,
            source_of_event="self_test",
            experiment=experiment,
            verbose=False,
        )

    readings = adc_reader.take_reading()
    adc_reader.set_state(adc_reader.DISCONNECTED)

    assert all([readings[pd_channel] < 0.005 for pd_channel in PD_CHANNELS])


def test_detect_heating_pcb(unit, experiment):
    try:
        tc = TemperatureController("silent", unit=unit, experiment=experiment)
    except IOError:
        assert False
    else:
        tc.set_state(tc.DISCONNECTED)
        assert True


def test_positive_correlation_between_temp_and_heating(unit, experiment, logger):
    tc = TemperatureController("silent", unit=unit, experiment=experiment)

    measured_pcb_temps = []
    dcs = list(range(0, 48, 6))
    logger.debug("Varying heating.")
    for dc in dcs:
        tc._update_heater(dc)
        time.sleep(0.75)
        measured_pcb_temps.append(tc.read_external_temperature())

    tc._update_heater(0)
    measured_correlation = round(correlation(dcs, measured_pcb_temps), 2)
    logger.debug(f"Correlation between temp sensor and heating: {measured_correlation}")
    assert measured_correlation > 0.9

    tc.set_state(tc.DISCONNECTED)


def test_positive_correlation_between_rpm_and_stirring(unit, experiment):

    st = start_stirring(unit=unit, experiment=experiment)
    time.sleep(4)
    st.set_state(st.DISCONNECTED)
    assert False


@click.command(name="self_test")
def click_self_test():
    """
    Test the input/output in the Pioreactor
    """
    unit = get_unit_name()
    experiment = get_latest_experiment_name()

    with publish_ready_to_disconnected_state(
        unit, get_latest_testing_experiment_name(), "self_test"
    ):

        if (
            is_pio_job_running("od_reading")
            or is_pio_job_running("temperature_control")
            or is_pio_job_running("stirring")
        ):
            logger = create_logger("self_test", unit=unit, experiment=experiment)

            logger.error(
                "Make sure OD Reading, Temperature Control, and Stirring are off before running a self test. Exiting."
            )
            return

        return pytest.main(
            ["./pioreactor/actions/self_test.py", "-s", "-qq", "--tb=no"],
            plugins=[SelfTestPlugin()],
        )
