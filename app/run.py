import asyncio
import logging

import uvicorn

from app.main import app, container
from app.smtp import create_smtp_controller

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("iris")


async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)

    smtp_controller = create_smtp_controller(
        host="0.0.0.0",
        port=2525,
        mailer=container.mailer,
    )

    logger.info("Starting Iris Unified Gateway...")
    logger.info("HTTP API listening on port 8000")
    logger.info("SMTP Server listening on port 2525")

    smtp_controller.start()

    try:
        await server.serve()
    finally:
        logger.info("Shutting down SMTP Server...")
        smtp_controller.stop()


if __name__ == "__main__":
    asyncio.run(main())
