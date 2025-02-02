# -*- coding: utf-8 -*-
# test_automation.py
from __future__ import annotations

from pioreactor.structs import Automation


def test_str_representation():
    a = Automation(
        automation_name="test",
        args={"growth": 0.1, "intensity": "high", "value": True},
    )

    assert str(a) == "test(growth=0.1, intensity=high, value=True)"


def test_str_representation_of_skip_first_run():
    a = Automation(
        automation_name="test",
        args={"skip_first_run": 0, "intensity": "high", "value": True},
    )

    assert str(a) == "test(skip_first_run=False, intensity=high, value=True)"

    b = Automation(
        automation_name="test",
        args={"skip_first_run": 1, "intensity": "high", "value": True},
    )

    assert str(b) == "test(skip_first_run=True, intensity=high, value=True)"
