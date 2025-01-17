"""The worker that runs jobs in parallel.  Called from main module."""

# The code below is subject to the license contained in the LICENSE file, which is part of the source code.

from __future__ import annotations

import difflib
import logging
import os
import random
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from typing import Iterable, Optional, TYPE_CHECKING

from .handler import JobState
from .jobs import BrowserJob, NotModifiedError, UrlJobBase

# https://stackoverflow.com/questions/39740632
if TYPE_CHECKING:
    from typing import List

    from .jobs import JobBase
    from .main import Urlwatch

logger = logging.getLogger(__name__)


def run_jobs(urlwatcher: Urlwatch) -> None:
    """Process (run) jobs in parallel.

    :raises IndexError: If any index(es) is/are out of range.
    """

    def insert_delay(jobs: List[JobBase]) -> List[JobBase]:  # pragma: no cover
        """
        TODO: Evaluate whether this is necessary; currently not being called.  Remove pragma no cover.

        Sets a _delay value for URL jobs hitting the network location already hit. Used to prevent multiple jobs
        hitting the same network location at the exact same time and being blocked as a result.

        CHANGELOG:
        * When multiple URL jobs have the same network location (www.example.com), a random delay between 0.1 and 1.0
          seconds is added to all jobs to that network location after the first one. This prevents being blocked by
          the site as a result of being flooded by **webchanges**'s parallelism sending multiple requests from the same
          source at the same exact time.

        :param jobs: The list of jobs.
        :return: The list of jobs with the _delay value set.
        """
        previous_netlocs = set()
        for job in jobs:
            if isinstance(job, UrlJobBase):
                netloc = urllib.parse.urlparse(job.url).netloc
                if netloc in previous_netlocs:
                    job._delay = random.uniform(0.1, 1)  # nosec [B311:blacklist] Standard pseudo-random generator
                else:
                    previous_netlocs.add(netloc)
        return jobs

    def job_runner(
        stack: ExitStack,
        jobs: Iterable[JobBase],
        max_workers: Optional[int] = None,
    ) -> None:
        """
        Runs the jobs in parallel.

        :param stack: The context manager.
        :param jobs: The jobs to run.
        :param max_workers: The number of maximum workers for ThreadPoolExecutor.
        :return: None
        """
        executor = ThreadPoolExecutor(max_workers=max_workers)

        # launch future to retrieve if new version is available
        if urlwatcher.report.new_release_future is None:
            urlwatcher.report.new_release_future = executor.submit(urlwatcher.get_new_release_version)

        for job_state in executor.map(
            lambda jobstate: jobstate.process(headless=not urlwatcher.urlwatch_config.no_headless),
            (stack.enter_context(JobState(urlwatcher.cache_storage, job)) for job in jobs),
        ):

            max_tries = 0 if not job_state.job.max_tries else job_state.job.max_tries
            # tries is incremented by JobState.process when an exception (including 304) is encountered.

            if job_state.exception is not None:
                # Oops, we have captured an error (which could also be 304 or a Playwright timeout)
                if job_state.error_ignored:
                    # We captured an error to ignore
                    logger.info(
                        f'Job {job_state.job.index_number}: Error while executing job was ignored (e.g. due to job '
                        f'config or browser timing out)'
                    )
                elif isinstance(job_state.exception, NotModifiedError):
                    # We captured a 304 Not Modified
                    logger.info(
                        f'Job {job_state.job.index_number}: Job has not changed (HTTP 304 response or same strong '
                        f'ETag)'
                    )
                    if job_state.tries > 0:
                        job_state.tries = 0
                        job_state.save(use_old_data=True)  # data is not returned by 304 therefore reuse old data
                    urlwatcher.report.unchanged(job_state)
                elif job_state.tries < max_tries:
                    # We're not reporting the error yet because we haven't yet hit 'max_tries'
                    logger.debug(
                        f'Job {job_state.job.index_number}: Error suppressed as cumulative number of '
                        f'failures ({job_state.tries}) does not exceed max_tries={max_tries}'
                    )
                    job_state.save(use_old_data=True)  # do not save error data but reuse old data
                else:
                    # Reporting the error
                    logger.debug(
                        f'Job {job_state.job.index_number}: Flagged as error as max_tries={max_tries} has been '
                        f'met or exceeded ({job_state.tries}'
                    )
                    job_state.save(use_old_data=True)  # do not save error data but reuse old data
                    urlwatcher.report.error(job_state)

            elif len(job_state.old_data) or job_state.old_timestamp != 0:
                # This is not the first time running this job (we have snapshots)
                if job_state.new_data in (job_state.old_data, job_state.history_data):
                    # exactly matches one of the previous snapshots
                    if job_state.tries > 0:
                        job_state.tries = 0
                        job_state.save()
                    urlwatcher.report.unchanged(job_state)
                else:
                    # no match
                    if len(job_state.history_data) > 1:
                        # replace old with best "good enough" previous snapshot
                        close_matches = difflib.get_close_matches(job_state.new_data, job_state.history_data, n=1)
                        if close_matches:
                            job_state.old_data = close_matches[0]
                            job_state.old_timestamp = job_state.history_data[close_matches[0]]
                    job_state.tries = 0
                    job_state.save()
                    urlwatcher.report.changed(job_state)
            else:
                # We have never run this job before (there are no snapshots)
                job_state.tries = 0
                job_state.save()
                urlwatcher.report.new(job_state)

    def get_virt_mem() -> int:
        try:
            import psutil
        except ImportError:
            raise ImportError(
                "Python package psutil is not installed; cannot use 'use_browser: true'. Please install "
                "dependencies with 'pip install webchanges[use_browser]'."
            ) from None
        try:
            virt_mem = psutil.virtual_memory().available
            logger.debug(
                f'Found {virt_mem / 1e6:,.0f} MB of available physical memory (plus '
                f'{psutil.swap_memory().free / 1e6:,.0f} MB of swap).'
            )
        except psutil.Error as e:  # pragma: no cover
            virt_mem = 0
            logger.debug(f'Could not read memory: {e}')

        return virt_mem

    # extract subset of jobs to run if joblist CLI was set
    if urlwatcher.urlwatch_config.joblist:
        for idx in urlwatcher.urlwatch_config.joblist:
            if not (-len(urlwatcher.jobs) <= idx <= -1 or 1 <= idx <= len(urlwatcher.jobs)):
                raise IndexError(f'Job index {idx} out of range (found {len(urlwatcher.jobs)} jobs)')
        urlwatcher.urlwatch_config.joblist = [
            jn if jn > 0 else len(urlwatcher.jobs) + jn + 1 for jn in urlwatcher.urlwatch_config.joblist
        ]
        jobs = [
            job.with_defaults(urlwatcher.config_storage.config)
            for job in urlwatcher.jobs
            if job.index_number in urlwatcher.urlwatch_config.joblist
        ]
        logger.debug(
            f'Processing {len(jobs)} job{"s" if len(jobs) else ""} as specified in command line: # '
            f'{", ".join(str(j) for j in urlwatcher.urlwatch_config.joblist)}'
        )
    else:
        jobs = [job.with_defaults(urlwatcher.config_storage.config) for job in urlwatcher.jobs]
        logger.debug(f'Processing {len(jobs)} job{"s" if len(jobs) else ""}')

    #    jobs = insert_delay(jobs)

    with ExitStack() as stack:
        # run non-BrowserJob jobs first
        jobs_to_run = [job for job in jobs if type(job) != BrowserJob]
        if jobs_to_run:
            logger.debug(
                "Running jobs that do not require Chrome (without 'use_browser: true') in parallel with Python's "
                'default max_workers.'
            )
            job_runner(stack, jobs_to_run)
        else:
            logger.debug("Found no jobs that do not require Chrome (i.e. without 'use_browser: true').")

        # run BrowserJob jobs after
        jobs_to_run = [job for job in jobs if type(job) == BrowserJob]
        if jobs_to_run:
            virt_mem = get_virt_mem()
            max_workers = max(int(virt_mem / 120e6), 1)
            max_workers = min(max_workers, os.cpu_count() or 1)
            logger.debug(
                f"Running jobs that require Chrome (i.e. with 'use_browser: true') in parallel with {max_workers} "
                f'max_workers.'
            )
            job_runner(stack, jobs_to_run, max_workers)
        else:
            logger.debug("Found no jobs that require Chrome (i.e. with 'use_browser: true').")
