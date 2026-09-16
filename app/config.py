import os


class Config:
    FLASK_ENV = os.getenv("FLASK_ENV", "production").lower()
    SECRET_KEY = os.getenv("SECRET_KEY", "")
    TELEPHONY_TOKEN = os.getenv("TELEPHONY_TOKEN", "")
    CRM_WEBHOOK_URL = os.getenv("CRM_WEBHOOK_URL", "")
    CRM_WEBHOOK_TOKEN = os.getenv("CRM_WEBHOOK_TOKEN", "")
    ASTERISK_ARI_URL = os.getenv("ASTERISK_ARI_URL", "http://asterisk:8088/ari")
    ASTERISK_ARI_USER = os.getenv("ASTERISK_ARI_USER", "")
    ASTERISK_ARI_PASSWORD = os.getenv("ASTERISK_ARI_PASSWORD", "")
    ASTERISK_ARI_APP = os.getenv("ASTERISK_ARI_APP", "engineerip")
    ASTERISK_AMI_HOST = os.getenv("ASTERISK_AMI_HOST", "asterisk")
    ASTERISK_AMI_PORT = int(os.getenv("ASTERISK_AMI_PORT", "5038"))
    ASTERISK_AMI_USER = os.getenv("ASTERISK_AMI_USER", "engineerip")
    ASTERISK_AMI_PASSWORD = os.getenv("ASTERISK_AMI_PASSWORD", "")
    ASTERISK_DYNAMIC_CONFIG_PATH = os.getenv("ASTERISK_DYNAMIC_CONFIG_PATH", "/app/asterisk-config/pjsip.dynamic.conf")
    ARI_READY_PATH = os.getenv("ARI_READY_PATH", "/app/instance/ari.ready")
    ASTERISK_EXTENSIONS = tuple(
        ext.strip()
        for ext in os.getenv("ASTERISK_EXTENSIONS", "101").split(",")
        if ext.strip()
    )
    DEFAULT_EXTENSION = os.getenv("DEFAULT_EXTENSION", "101").strip()
    SIP_OUTBOUND_PREFIX = os.getenv("SIP_OUTBOUND_PREFIX", "")
    ENABLE_BROWSER_API = os.getenv("ENABLE_BROWSER_API", "false").lower() == "true"
    ENABLE_DIAGNOSTIC_UI = os.getenv("ENABLE_DIAGNOSTIC_UI", "false").lower() == "true"
    RECORDING_VOLUME_PATH = os.getenv("RECORDING_VOLUME_PATH", "/recordings")
    CALLS_DB_PATH = os.getenv("CALLS_DB_PATH", "/app/instance/calls.db")
    MAX_CONTENT_LENGTH = 64 * 1024

    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
    SETTINGS_DB_PATH = os.getenv("SETTINGS_DB_PATH", "/app/instance/settings.db")

    @classmethod
    def is_extension_configured(cls, extension: str) -> bool:
        return extension in cls.ASTERISK_EXTENSIONS

    @classmethod
    def validate(cls) -> None:
        if cls.FLASK_ENV not in {"production", "development", "testing"}:
            raise RuntimeError("FLASK_ENV must be production, development, or testing")
        if cls.FLASK_ENV != "production":
            return
        required = {
            "SECRET_KEY": cls.SECRET_KEY,
            "TELEPHONY_TOKEN": cls.TELEPHONY_TOKEN,
            "ASTERISK_ARI_USER": cls.ASTERISK_ARI_USER,
            "ASTERISK_ARI_PASSWORD": cls.ASTERISK_ARI_PASSWORD,
            "ASTERISK_AMI_PASSWORD": cls.ASTERISK_AMI_PASSWORD,
            "ADMIN_USERNAME": cls.ADMIN_USERNAME,
            "ADMIN_PASSWORD": cls.ADMIN_PASSWORD,
        }
        if cls.CRM_WEBHOOK_URL and not cls.CRM_WEBHOOK_TOKEN:
            raise RuntimeError("CRM_WEBHOOK_TOKEN must be set when CRM_WEBHOOK_URL is configured")
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required production security settings: {', '.join(missing)}")
        if len(cls.SECRET_KEY) < 32:
            raise RuntimeError("SECRET_KEY must be at least 32 characters in production")
        if len(cls.TELEPHONY_TOKEN) < 32:
            raise RuntimeError("TELEPHONY_TOKEN must be at least 32 characters in production")
        if len(cls.ASTERISK_ARI_PASSWORD) < 20:
            raise RuntimeError("ASTERISK_ARI_PASSWORD must be at least 20 characters in production")
        if len(cls.ASTERISK_AMI_PASSWORD) < 20:
            raise RuntimeError("ASTERISK_AMI_PASSWORD must be at least 20 characters in production")
        if len(cls.ADMIN_PASSWORD) < 14:
            raise RuntimeError("ADMIN_PASSWORD must be at least 14 characters in production")
        if not cls.ASTERISK_EXTENSIONS:
            raise RuntimeError("ASTERISK_EXTENSIONS must contain at least one extension")
        if cls.DEFAULT_EXTENSION not in cls.ASTERISK_EXTENSIONS:
            raise RuntimeError("DEFAULT_EXTENSION must appear in ASTERISK_EXTENSIONS")
