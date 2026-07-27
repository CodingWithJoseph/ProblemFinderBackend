"""Run the durable Signal analysis worker as a separate process.

Usage:
    python -m app.workers.signal_analysis
"""

import asyncio
import logging

from app.services.signal_analysis_worker import run_signal_worker


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await run_signal_worker(asyncio.Event())


if __name__ == "__main__":
    asyncio.run(main())

