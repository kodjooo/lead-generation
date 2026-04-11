"""Фоновый воркер для обогащения контактов и отправки писем."""

import logging
import time

from app.modules.utils.db import bootstrap_database
from app.orchestrator import PipelineOrchestrator

LOGGER = logging.getLogger("app.worker")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    bootstrap_database()
    orchestrator = PipelineOrchestrator()
    LOGGER.info("Воркер запущен.")

    try:
        while True:
            enriched = orchestrator.enrich_missing_contacts()
            sent = orchestrator.generate_and_send_emails()
            LOGGER.info("Воркер цикл: enriched=%s, sent=%s", enriched, sent)
            time.sleep(orchestrator.config.poll_interval_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Воркер остановлен пользователем.")


if __name__ == "__main__":
    main()
