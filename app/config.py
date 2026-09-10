import os


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")
    TELEPHONY_TOKEN = os.getenv("TELEPHONY_TOKEN", "dev-token")
    CRM_WEBHOOK_URL = os.getenv("CRM_WEBHOOK_URL", "")
    CRM_WEBHOOK_TOKEN = os.getenv("CRM_WEBHOOK_TOKEN", "")
    ASTERISK_ARI_URL = os.getenv("ASTERISK_ARI_URL", "http://asterisk:8088/ari")
    ASTERISK_ARI_USER = os.getenv("ASTERISK_ARI_USER", "engineerip")
    ASTERISK_ARI_PASSWORD = os.getenv("ASTERISK_ARI_PASSWORD", "change-me")
    ASTERISK_ARI_APP = os.getenv("ASTERISK_ARI_APP", "engineerip")
    DEFAULT_EXTENSION = os.getenv("DEFAULT_EXTENSION", "101")
    SIP_OUTBOUND_PREFIX = os.getenv("SIP_OUTBOUND_PREFIX", "")
