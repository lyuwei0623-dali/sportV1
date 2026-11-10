"""A random per-browser bearer secret, not a hardware identifier/fingerprint."""
from pathlib import Path
from streamlit.components.v1 import declare_component

_device = declare_component("vidali_device", path=str(Path(__file__).with_name("device_component")))


def browser_device():
    result = _device(key="vidali_browser_device", default=None)
    if isinstance(result, dict) and result.get("ok") is True:
        return result.get("token")
    return None
