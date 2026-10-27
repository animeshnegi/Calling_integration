"""Shared assertion helper for dynamically registered PJSIP contacts."""


def dynamic_dial(*endpoints):
    """The one-Dial dialplan string generated for reachable PJSIP contacts."""
    parts = ["${PJSIP_DIAL_CONTACTS(" + name + ")}" for name in endpoints]
    return "Dial(" + "&".join(parts) + ",30)"
