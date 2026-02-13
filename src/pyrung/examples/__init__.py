"""Example callback factories for custom/acustom escape-hatch instructions."""

from pyrung.examples.click_email import email_instruction
from pyrung.examples.custom_math import weighted_average

__all__ = ["email_instruction", "weighted_average"]
