"""Background scheduler for periodic tasks.

Uses APScheduler to run recurring tasks like the notes-to-KB sync.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.logging import get_logger
from app.notes_sync import _sync_to_kb

logger = get_logger(__name__)

scheduler = AsyncIOScheduler()


def setup_scheduler() -> None:
    """Configure and start the background scheduler."""
    # Notes sync job
    if settings.NOTES_SYNC_INTERVAL > 0 and settings.NOTES_SYNC_KB_ID:
        if not settings.has_static_owui_token():
            # is_owui_configured() alone would be a false positive here: a
            # URL with no credentials is not a working sync configuration.
            logger.warning(
                "NOTES_SYNC_INTERVAL and NOTES_SYNC_KB_ID are set but "
                "OWUI_TOKEN is missing — auto notes sync disabled (the job "
                "runs as the static token). Set OWUI_TOKEN to enable it."
            )
        else:
            interval = settings.NOTES_SYNC_INTERVAL
            logger.info(
                "Scheduling notes sync job",
                interval_seconds=interval,
                kb_id=settings.NOTES_SYNC_KB_ID,
            )
            scheduler.add_job(
                _run_notes_sync,
                "interval",
                seconds=interval,
                id="notes_sync",
                name="Notes to KB sync",
                replace_existing=True,
                max_instances=1,  # Prevent overlapping runs
            )
    elif settings.NOTES_SYNC_INTERVAL > 0 and not settings.NOTES_SYNC_KB_ID:
        logger.warning(
            "NOTES_SYNC_INTERVAL is set but NOTES_SYNC_KB_ID is missing. Auto-sync disabled."
        )

    scheduler.start()
    logger.info("Scheduler started", job_count=len(scheduler.get_jobs()))


async def _run_notes_sync() -> None:
    """Wrapper for the notes sync job (handles errors gracefully).

    Runs as the static OWUI_TOKEN, with no Request in scope (per-call
    HTTP client).
    """
    try:
        logger.info("Running scheduled notes sync")
        results = await _sync_to_kb()
        logger.info(
            "Scheduled notes sync complete",
            total=results.get("total_notes", 0),
            uploaded=len(results.get("uploaded", [])),
            errors=len(results.get("errors", [])),
        )
    except Exception as e:
        logger.exception("Scheduled notes sync failed", error=str(e))


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down")
