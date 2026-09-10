from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = "data/user.session"
    tg_proxy: str = ""  # e.g. socks5://127.0.0.1:1080 or socks5://user:pass@host:port

    database_url: str = "postgresql://tg:tg@127.0.0.1:5436/tg"

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
    media_auto_transcribe: bool = True        # voice / video notes in non-channel chats
    media_auto_extract: bool = True           # text documents in non-channel chats
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
    media_auto_describe: bool = True          # photos / image files in non-channel chats
    media_auto_describe_max_mb: int = 5
    vision_pdf_pages: int = 3                 # scanned PDFs: pages rendered and sent to the vision model

    # semantic search
    embed_model: str = "intfloat/multilingual-e5-small"   # 384-dim, multilingual, runs locally
    embed_enabled: bool = True
    embed_channels: bool = True               # also embed channel posts (more rows, more noise)

    # outbox (sending on behalf of the owner, always confirmed in Saved Messages)
    send_enabled: bool = True
    bot_token: str = ""                        # Bot API token: prompts, results and the daily digest come from this bot (with buttons)
    digest_time: str = "21:00"                 # local time for the daily outbox digest, empty = off
    health_interval: int = 300                 # seconds between health checks (alerts via the bot on state changes), 0 = off
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


settings = Settings()
