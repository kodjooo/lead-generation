"""Точка входа основной службы оркестрации."""

import argparse
import logging

from app.orchestrator import OrchestratorConfig, PipelineOrchestrator


def main() -> None:
    """Стартует оркестратор пайплайна по переданным параметрам."""
    parser = argparse.ArgumentParser(description="Lead generation orchestrator")
    parser.add_argument(
        "--mode",
        choices=["once", "loop"],
        default="loop",
        help="Режим работы: один прогон или бесконечный цикл",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=60,
        help="Интервал между циклами в секундах (для loop)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Количество сущностей, обрабатываемых за один проход",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    orchestrator = PipelineOrchestrator(
        OrchestratorConfig(
            batch_size=args.batch_size,
            poll_interval_seconds=args.poll_interval,
        )
    )

    if args.mode == "once":
        orchestrator.run_once()
    else:
        orchestrator.run_forever()


if __name__ == "__main__":
    main()
