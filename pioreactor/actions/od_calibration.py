# -*- coding: utf-8 -*-
from __future__ import annotations

from time import sleep

import click
from msgspec.json import encode

from pioreactor.background_jobs.od_reading import start_od_reading
from pioreactor.background_jobs.stirring import start_stirring as stirring
from pioreactor.config import config
from pioreactor.utils import is_pio_job_running
from pioreactor.utils import local_persistant_storage
from pioreactor.utils import publish_ready_to_disconnected_state
from pioreactor.utils.timing import current_utc_timestamp
from pioreactor.whoami import get_latest_testing_experiment_name
from pioreactor.whoami import get_unit_name
from pioreactor.whoami import is_testing_env


def introduction():
    click.clear()
    click.echo(
        """This routine will calibrate the current Pioreactor to (offline) OD600 readings. You'll need:
    1. A Pioreactor
    2. 10ml of a culture with density the most you'll ever observe, with it's OD600 measurement.
    3. Micro-pipette with available range 100-1000 uL volume
"""
    )


def get_metadata_from_user():
    with local_persistant_storage("od_calibrations") as cache:
        while True:
            name = click.prompt("Provide a unique name for this calibration", type=str)
            if name not in cache:
                break
            else:
                click.echo("❗️ Name already exists. Try again.")

    initial_od600 = click.prompt(
        "Provide the OD600 measurement of your initial culture", type=float
    )
    minimum_od600 = click.prompt(
        "Provide the minimum OD600 measurement you want to calibrate to", default=0.1, type=float
    )
    dilution_amount = click.prompt(
        "Provide the volume to be added to your vial (default = 1 mL)", default=1, type=float
    )
    click.confirm(
        f"Confirm using angle {config['od_config.photodiode_channel']['2']}°",
        abort=True,
        default=True,
    )
    angle = str(config["od_config.photodiode_channel"]["2"])
    return name, initial_od600, minimum_od600, dilution_amount, angle


def setup_HDC_instructions():
    click.clear()
    click.echo(
        """ Setting up:
    1. Add 10ml of your culture to the glass vial, with a stir bar. Add cap.
    2. Place into Pioreactor.
"""
    )


def start_stirring():
    while not click.confirm("Reading to start stirring?", default=True):
        pass

    click.echo("Starting stirring.")

    st = stirring(
        target_rpm=config.getfloat("stirring", "target_rpm"),
        unit=get_unit_name(),
        experiment=get_latest_testing_experiment_name(),
    )
    st.block_until_rpm_is_close_to_target(abs_tolerance=100)
    return st


def plot_data(
    x, y, title, x_min=None, x_max=None, interpolation_curve=None, highlight_recent_point=True
):
    import plotext as plt

    plt.clf()

    plt.scatter(x, y)

    if highlight_recent_point:
        plt.scatter([x[-1]], [y[-1]], color=204)

    plt.theme("pro")
    plt.title(title)
    plt.plot_size(105, 22)
    
    if interpolation_curve:
        plt.plot(x, [interpolation_curve(x_) for x_ in x], color=204)
        plt.plot_size(145, 42)
    
    plt.xlim(x_min, x_max)
    plt.show()


def start_recording_and_diluting(initial_od600, minimum_od600, dilution_amount):

    inferred_od600 = initial_od600
    voltages = []
    inferred_od600s = []
    current_volume_in_vial = initial_volume_in_vial = 10
    number_of_plotpoints = initial_volume_in_vial / dilution_amount #dilution_amount of 2 mL, number of plotpoints is 5. 
    click.echo("Starting OD recordings.")

    with start_od_reading(
        config.get("od_config.photodiode_channel", "1"),
        config.get("od_config.photodiode_channel", "2"),
        interval=None,
        unit=get_unit_name(),
        fake_data=is_testing_env(),
        experiment=get_latest_testing_experiment_name(),
        use_calibration=False,
        # calibration=False,,..
    ) as od_reader:

        for _ in range(4):
            od_reader.record_from_adc()

        while inferred_od600 > minimum_od600:
            od_readings1 = od_reader.record_from_adc()
            od_readings2 = od_reader.record_from_adc()

            voltages.append(
                0.5 * (od_readings1.od_raw["2"].voltage + od_readings2.od_raw["2"].voltage)
            )
            inferred_od600s.append(inferred_od600)

            for i in range(number_of_plotpoints):  # 10 assumes 1ml dilutions
                click.clear()
                plot_data(
                    inferred_od600s,
                    voltages,
                    title="OD Calibration (ongoing)",
                    x_min=minimum_od600,
                    x_max=initial_od600,
                )
                click.echo()
                click.echo("Add 1ml of DI water to vial.")

                while not click.confirm("Continue?", default=True):
                    pass
                click.echo(".", nl=False)

                current_volume_in_vial = current_volume_in_vial + dilution_amount  # assumes 1ml

                sleep(1.20)
                click.echo(".", nl=False)
                od_readings1 = od_reader.record_from_adc()
                click.echo(".", nl=False)
                od_readings2 = od_reader.record_from_adc()
                voltages.append(
                    0.5 * (od_readings1.od_raw["2"].voltage + od_readings2.od_raw["2"].voltage)
                )
                click.echo(".", nl=False)

                inferred_od600 = (
                    inferred_od600
                    * (current_volume_in_vial - 1)
                    / current_volume_in_vial  # assumes 1ml
                )
                inferred_od600s.append(inferred_od600)

                if inferred_od600 <= minimum_od600:
                    break

            else:
                # executed if the loop did not break
                click.clear()
                plot_data(
                    inferred_od600s,
                    voltages,
                    title="OD Calibration (ongoing)",
                    x_min=minimum_od600,
                    x_max=initial_od600,
                )
                click.echo()
                click.echo(click.style("Stop❗", fg="red"))
                click.echo("Remove vial and reduce volume back to 10ml.")
                click.echo("Confirm vial outside is dry and clean. Place back into Pioreactor.")
                while not click.confirm("Continue?", default=True):
                    pass
                current_volume_in_vial = initial_volume_in_vial
                sleep(1.0)

        return inferred_od600s, voltages


def calculate_curve_of_best_fit(voltages, inferred_od600s):
    import numpy as np

    coefs = np.polyfit(inferred_od600s, voltages, 4).tolist()

    return coefs, "poly"


def show_results_and_confirm_with_user(curve, curve_type, voltages, inferred_od600s):
    click.clear()

    if curve_type == "poly":
        import numpy as np

        def curve_callable(x):
            return np.polyval(curve, x)

    else:
        curve_callable = None

    plot_data(
        inferred_od600s,
        voltages,
        title="OD Calibration with curve of best fit",
        interpolation_curve=curve_callable,
        highlight_recent_point=False,
    )
    click.confirm("Save calibration?", abort=True, default=True)


def save_results_locally(
    curve, curve_type, voltages, inferred_od600s, angle, name, initial_od600, minimum_od600
):
    timestamp = current_utc_timestamp()
    data_blob = encode(
        {
            "angle": angle,
            "timestamp": timestamp,
            "name": name,
            "initial_od600": initial_od600,
            "minimum_od600": minimum_od600,
            "curve_data": curve,
            "curve_type": curve_type,  # poly
            "voltages": voltages,
            "inferred_od600s": inferred_od600s,
            "ir_led_intensity": config["od_config"]["ir_led_intensity"],
        }
    )

    with local_persistant_storage("od_calibrations") as cache:
        cache[name] = data_blob

    with local_persistant_storage("current_od_calibration") as cache:
        cache[angle] = data_blob

    return data_blob


def od_calibration():
    unit = get_unit_name()
    experiment = get_latest_testing_experiment_name()

    if is_pio_job_running("stirring", "od_reading"):
        raise ValueError("Stirring and OD reading should be turned off.")

    with publish_ready_to_disconnected_state(unit, experiment, "od_calibration"):

        introduction()
        name, initial_od600, minimum_od600, dilution_amount, angle = get_metadata_from_user()
        setup_HDC_instructions()

        with start_stirring():
            inferred_od600s, voltages = start_recording_and_diluting(initial_od600, minimum_od600, dilution_amount)

        curve, curve_type = calculate_curve_of_best_fit(voltages, inferred_od600s)

        show_results_and_confirm_with_user(curve, curve_type, voltages, inferred_od600s)
        data_blob = save_results_locally(
            curve, curve_type, voltages, inferred_od600s, angle, name, initial_od600, minimum_od600
        )

        click.echo(data_blob)
        click.echo(f"Finished calibration of {name} ✅")
        return


@click.command(name="od_calibration")
def click_od_calibration():
    """
    Calibrate OD600 to voltages
    """
    od_calibration()
