import asyncio
import signal

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.worker.runner import WorkerRunner


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    runner = WorkerRunner(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, runner.stop)
        except NotImplementedError:
            pass
    await runner.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
