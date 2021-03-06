"""Process a list of repository arguments."""
# =============================================================================
# CONTENTS
# -----------------------------------------------------------------------------
# abdi_processrepoarglist
#
# Public Functions:
#   do
#   fetch_if_needed
#
# -----------------------------------------------------------------------------
# (this contents block is generated, edits will be lost)
# =============================================================================
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import json
import logging
import multiprocessing
import os
import time

import phlcon_reviewstatecache
import phlgitx_refcache
import phlmp_cyclingpool
import phlsys_conduit
import phlsys_git
import phlsys_subprocess
import phlsys_timer
import phlurl_watcher

import abdmail_mailer
import abdt_classicnaming
import abdt_compositenaming
import abdt_conduit
import abdt_errident
import abdt_exhandlers
import abdt_fs
import abdt_git
import abdt_rbranchnaming
import abdt_tryloop

import abdi_processrepo
import abdi_repoargs


_LOGGER = logging.getLogger(__name__)


def do(
        repo_configs,
        sys_admin_emails,
        kill_file,
        sleep_secs,
        is_no_loop,
        external_report_command,
        mail_sender,
        max_workers,
        overrun_secs):

    conduit_manager = _ConduitManager()

    fs_accessor = abdt_fs.make_default_accessor()
    url_watcher_wrapper = phlurl_watcher.FileCacheWatcherWrapper(
        fs_accessor.layout.urlwatcher_cache_path)

    finished = False

    # decide max workers based on number of CPUs if no value is specified
    if max_workers is None:
        try:
            # use the same default as multiprocessing.Pool
            max_workers = multiprocessing.cpu_count()
            _LOGGER.debug(
                "max_workers unspecified, defaulted to cpu_count: {}".format(
                    max_workers))
        except NotImplementedError:
            _LOGGER.warning(
                "multiprocessing.cpu_count() not supported, disabling "
                "multiprocessing. Specify max workers explicitly to enable.")
            max_workers = 0

    repo_list = []
    for name, config in repo_configs:
        repo_list.append(
            _ArcydManagedRepository(
                name,
                config,
                conduit_manager,
                url_watcher_wrapper,
                sys_admin_emails,
                mail_sender))

    # if we always overrun half our workers then the loop is sustainable, if we
    # overrun more than that then we'll be lagging too far behind. In the event
    # that we only have one worker then we can't overrun any.
    max_overrun_workers = max_workers // 2

    pool = phlmp_cyclingpool.CyclingPool(
        repo_list, max_workers, max_overrun_workers)

    cycle_timer = phlsys_timer.Timer()
    cycle_timer.start()
    while not finished:

        # This timer needs to be separate from the cycle timer. The cycle timer
        # must be reset every time it is reported. The sleep timer makes sure
        # that each run of the loop takes a minimum amount of time.
        sleep_timer = phlsys_timer.Timer()
        sleep_timer.start()

        # refresh git snoops
        abdt_tryloop.critical_tryloop(
            url_watcher_wrapper.watcher.refresh,
            abdt_errident.GIT_SNOOP,
            '')

        conduit_manager.refresh_conduits()

        if max_workers:
            for i, res in pool.cycle_results(overrun_secs=overrun_secs):
                repo = repo_list[i]
                repo.merge_from_worker(res)
        else:
            for r in repo_list:
                r()

        # important to do this before stopping arcyd and as soon as possible
        # after doing fetches
        url_watcher_wrapper.save()

        # report cycle stats
        if external_report_command:
            report = {
                "cycle_time_secs": cycle_timer.restart(),
                "overrun_jobs": pool.num_active_jobs,
            }
            report_json = json.dumps(report)
            full_path = os.path.abspath(external_report_command)
            try:
                phlsys_subprocess.run(full_path, stdin=report_json)
            except phlsys_subprocess.CalledProcessError as e:
                _LOGGER.error("CycleReportJson: {}".format(e))

        # look for killfile
        if os.path.isfile(kill_file):

            # finish any jobs that overran
            for i, res in pool.finish_results():
                repo = repo_list[i]
                repo.merge_from_worker(res)

            # important to do this before stopping arcyd and as soon as
            # possible after doing fetches
            url_watcher_wrapper.save()

            os.remove(kill_file)
            finished = True
            break

        if is_no_loop:
            finished = True
            break

        # sleep to pad out the cycle
        secs_to_sleep = float(sleep_secs) - float(sleep_timer.duration)
        if secs_to_sleep > 0:
            time.sleep(secs_to_sleep)


class _RecordingWatcherWrapper(object):

    def __init__(self, watcher):
        self._watcher = watcher
        self._tested_urls = set()

    def peek_has_url_recently_changed(self, url):
        return self._watcher.peek_has_url_recently_changed(url)

    def has_url_recently_changed(self, url):
        self._tested_urls.add(url)
        return self._watcher.peek_has_url_recently_changed(url)

    @property
    def tested_urls(self):
        return self._tested_urls


class _ArcydManagedRepository(object):

    def __init__(
            self,
            repo_name,
            repo_args,
            conduit_manager,
            url_watcher_wrapper,
            sys_admin_emails,
            mail_sender):

        self._is_disabled = False
        self._refcache_repo = phlgitx_refcache.Repo(
            phlsys_git.Repo(
                repo_args.repo_path))
        self._abd_repo = abdt_git.Repo(
            self._refcache_repo,
            "origin",
            repo_args.repo_desc)
        self._name = repo_name
        self._args = repo_args
        self._conduit_manager = conduit_manager

        conduit_cache = conduit_manager.get_conduit_and_cache_for_args(
            repo_args)
        self._arcyd_conduit, self._review_cache = conduit_cache

        self._mail_sender = mail_sender
        self._url_watcher_wrapper = url_watcher_wrapper
        self._mail_sender = mail_sender
        self._on_exception = abdt_exhandlers.make_exception_delay_handler(
            sys_admin_emails, repo_name)

    def __call__(self):
        watcher = _RecordingWatcherWrapper(
            self._url_watcher_wrapper.watcher)

        old_active_reviews = set(self._review_cache.active_reviews)

        if not self._is_disabled:
            try:
                _process_repo(
                    self._abd_repo,
                    self._name,
                    self._args,
                    self._arcyd_conduit,
                    watcher,
                    self._mail_sender)
            except Exception:
                self._on_exception(None)
                self._is_disabled = True

        return (
            self._review_cache.active_reviews - old_active_reviews,
            self._is_disabled,
            watcher.tested_urls,
            self._refcache_repo.peek_hash_ref_pairs()
        )

    def merge_from_worker(self, results):

        active_reviews, is_disabled, tested_urls, hash_ref_pairs = results
        self._review_cache.merge_additional_active_reviews(active_reviews)
        self._is_disabled = is_disabled
        self._refcache_repo.set_hash_ref_pairs(hash_ref_pairs)

        # merge in the consumed urls from the worker
        for url in tested_urls:
            self._url_watcher_wrapper.watcher.has_url_recently_changed(url)


class _ConduitManager(object):

    def __init__(self):
        super(_ConduitManager, self).__init__()
        self._conduits_caches = {}

    def get_conduit_and_cache_for_args(self, args):
        key = (
            args.instance_uri,
            args.arcyd_user,
            args.arcyd_cert,
            args.https_proxy
        )

        if key not in self._conduits_caches:
            # create an array so that the 'connect' closure binds to the
            # 'conduit' variable as we'd expect, otherwise it'll just
            # modify a local variable and this 'conduit' will remain 'None'
            # XXX: we can _process_repo better in python 3.x (nonlocal?)
            conduit = [None]

            def connect():
                # XXX: we'll rebind in python 3.x, instead
                # nonlocal conduit
                conduit[0] = phlsys_conduit.MultiConduit(
                    args.instance_uri,
                    args.arcyd_user,
                    args.arcyd_cert,
                    https_proxy=args.https_proxy)

            abdt_tryloop.tryloop(
                connect, abdt_errident.CONDUIT_CONNECT, args.instance_uri)

            multi_conduit = conduit[0]
            cache = phlcon_reviewstatecache.make_from_conduit(multi_conduit)
            arcyd_conduit = abdt_conduit.Conduit(multi_conduit, cache)
            self._conduits_caches[key] = (arcyd_conduit, cache)
        else:
            arcyd_conduit, cache = self._conduits_caches[key]

        return arcyd_conduit, cache

    def refresh_conduits(self):
        for conduit, cache in self._conduits_caches.itervalues():
            abdt_tryloop.critical_tryloop(
                cache.refresh_active_reviews,
                abdt_errident.CONDUIT_REFRESH,
                conduit.describe())


def fetch_if_needed(url_watcher, snoop_url, repo, repo_desc):

    did_fetch = False

    # fetch only if we need to
    if not snoop_url or url_watcher.peek_has_url_recently_changed(snoop_url):
        abdt_tryloop.tryloop(
            repo.checkout_master_fetch_prune,
            abdt_errident.FETCH_PRUNE,
            repo_desc)
        did_fetch = True

    if did_fetch and snoop_url:
        # consume the 'newness' of this repo, since fetching succeeded
        url_watcher.has_url_recently_changed(snoop_url)

    return did_fetch


def _process_repo(
        repo,
        unused_repo_name,
        args,
        arcyd_conduit,
        url_watcher,
        mail_sender):

    fetch_if_needed(
        url_watcher,
        abdi_repoargs.get_repo_snoop_url(args),
        repo,
        args.repo_desc)

    admin_emails = set(_flatten_list(args.admin_emails))

    # TODO: this should be a URI for users not conduit
    mailer = abdmail_mailer.Mailer(
        mail_sender,
        admin_emails,
        args.repo_desc,
        args.instance_uri)

    branch_url_callable = None
    if args.branch_url_format:
        def make_branch_url(branch_name):
            return args.branch_url_format.format(
                branch=branch_name,
                repo_url=args.repo_url)
        branch_url_callable = make_branch_url

    branch_naming = abdt_compositenaming.Naming(
        abdt_classicnaming.Naming(),
        abdt_rbranchnaming.Naming())

    branches = abdt_git.get_managed_branches(
        repo,
        args.repo_desc,
        branch_naming,
        branch_url_callable)

    abdi_processrepo.process_branches(branches, arcyd_conduit, mailer)


def _flatten_list(hierarchy):
    for x in hierarchy:
        # recurse into hierarchy if it's a list
        if hasattr(x, '__iter__') and not isinstance(x, str):
            for y in _flatten_list(x):
                yield y
        else:
            yield x


# -----------------------------------------------------------------------------
# Copyright (C) 2014 Bloomberg Finance L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------ END-OF-FILE ----------------------------------
