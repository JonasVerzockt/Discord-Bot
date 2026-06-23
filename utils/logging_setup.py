"""
utils/logging_setup.py – Zentrales Logging mit rotierenden Logdateien.

Ersetzt alle print()-Aufrufe im Bot schrittweise.
Logdateien: bot_log_YYYYMMDD.log (max 1 MB, 5 Backups).

Verwendung:
    from utils.logging_setup import setup_logging
    setup_logging()   # einmalig in main.py aufrufen

    import logging
    logging.info("Bot gestartet")
    logging.warning("Warnung!")
    logging.error("Fehler!")
"""
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from config import BASE_DIR


def setup_logging(level: int = logging.INFO) -> None:
    """Konfiguriert Root-Logger mit Datei- und Console-Handler."""
    log_file = BASE_DIR / f"bot_log_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger()
    logger.setLevel(level)

    # Verhindert doppelte Handler bei Hot-Reload
    if logger.handlers:
        logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Datei (rotierend) ──────────────────────────────────────────────────────
    fh = RotatingFileHandler(
        log_file, maxBytes=1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # ── Konsole ────────────────────────────────────────────────────────────────
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # discord.py-interne Logs nur auf WARNING reduzieren
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
