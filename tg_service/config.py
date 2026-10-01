from pydantic_settings import BaseSettings, SettingsConfigDict

from .tg.allowlist import parse_chat_ids


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = "data/user.session"
    tg_proxy: str = ""  # e.g. socks5://127.0.0.1:1080 or socks5://user:pass@host:port
    # Comma-separated marked peer ids. Empty is deliberately fail-closed at service startup.
    tg_allowed_chat_ids: str = ""

    # DATABASE_URL remains available for an explicitly supplied external DSN.
    # The local Compose deployment uses separate fields so a valid PostgreSQL password
    # never has to be interpolated into a URI.
    database_url: str = ""
    db_host: str = "127.0.0.1"
    db_port: int = 5436
    db_name: str = "tg"
    db_user: str = "tg"
    db_password: str = ""

    api_host: str = "127.0.0.1"
    api_port: int = 8077
    api_token: str = ""

    initial_history: int = 200
    catchup_limit: int = 2000
    backfill_batch: int = 200
    flood_sleep_threshold: int = 600

    tg_api_url: str = "http://127.0.0.1:8077"

    # media
    media_dir: str = "data/media"
    media_auto_transcribe: bool = False       # opt in only after a data-transfer decision
    media_auto_extract: bool = False          # opt in only after a data-transfer decision
    media_auto_extract_max_mb: int = 20
    media_max_download_mb: int = 2000
    whisper_model: str = "mlx-community/whisper-large-v3-turbo"
    whisper_language: str = ""                # empty = auto-detect
    media_retention_days: int = 30            # delete downloaded files after N days (derived text is kept); 0 = keep forever
    media_worker_idle_exit: int = 0           # >0: worker exits after N idle seconds; 0: stays up and unloads models after MEDIA_MODEL_IDLE
    media_model_idle: int = 300               # seconds without work before Whisper / embedding models are unloaded from memory
    media_worker_port: int = 8078             # worker's local HTTP (query embeddings, health)

    # images: description + OCR through a vision model on OpenRouter
    openrouter_api_key: str = ""
    vision_model: str = "minimax/minimax-m3"
    media_auto_describe: bool = False         # may use an external provider; opt in only
    media_auto_describe_max_mb: int = 5
    vision_pdf_pages: int = 3                 # scanned PDFs: pages rendered and sent to the vision model

    # semantic search
    embed_model: str = "intfloat/multilingual-e5-small"   # 384-dim, multilingual, runs locally
    embed_enabled: bool = False
    embed_channels: bool = False              # opt in only after resource review

    # outbox (sending on behalf of the owner, always confirmed in Saved Messages)
    send_enabled: bool = False
    bot_token: str = ""                        # Bot API token: prompts, results and the daily digest come from this bot (with buttons)
    digest_time: str = ""                       # local time for the daily outbox digest, empty = off
    health_interval: int = 0                   # seconds between health checks (alerts via the bot on state changes), 0 = off
    health_quiet_hours: str = "01:00-08:00"    # no "listener is silent" alerts in this window
    outbox_chat: str = "me"                   # where confirmation prompts go and where "ok/no/stop" replies are read: "me", @username or id
    send_draft_ttl_minutes: int = 10
    send_max_chars: int = 4000
    send_rate_per_hour: int = 0                # 0 = unlimited
    send_rate_per_chat_per_hour: int = 0
    send_file_roots: str = ""                  # comma-separated dirs agents may attach files from (MEDIA_DIR is always allowed)
    send_max_file_mb: int = 2000
    send_max_attachments: int = 10

    log_level: str = "INFO"

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        return parse_chat_ids(self.tg_allowed_chat_ids)

    def is_allowed_chat_id(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self.allowed_chat_ids


settings = Settings()
