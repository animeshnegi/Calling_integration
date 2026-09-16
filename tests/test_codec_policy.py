import pytest

from app.telephony_config import TelephonyConfigSync


def test_provider_codec_policy_requires_internal_codec_overlap():
    with pytest.raises(ValueError, match="ulaw or alaw"):
        TelephonyConfigSync._codecs("opus,g722")


def test_provider_codec_policy_accepts_ulaw_or_alaw():
    assert TelephonyConfigSync._codecs("opus,ulaw") == "opus,ulaw"
