"""
crawler.py

Copyright 2018 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
import time

import w3af.core.controllers.output_manager as om

from w3af.core.data.db.disk_set import DiskSet
from w3af.core.controllers.threads.threadpool import Pool, add_traceback_string
from w3af.core.controllers.core_helpers.consumers.base_consumer import BaseConsumer
from w3af.core.controllers.chrome.pool import ChromePool, ChromePoolException
from w3af.core.controllers.chrome.crawler.strategies.dom_dump import ChromeCrawlerDOMDump
from w3af.core.controllers.chrome.crawler.strategies.js import ChromeCrawlerJS
from w3af.core.controllers.chrome.crawler.state import CrawlerState
from w3af.core.controllers.chrome.crawler.exceptions import ChromeCrawlerException
from w3af.core.controllers.chrome.crawler.queue import CrawlerHTTPTrafficQueue
from w3af.core.controllers.chrome.utils.took_line import TookLine
from w3af.core.controllers.chrome.devtools.exceptions import (ChromeInterfaceException,
                                                              ChromeInterfaceTimeout)

from w3af.core.data.fuzzer.utils import rand_alnum


class ChromeCrawler(object):
    """
    Use Google Chrome to crawl a site.

    The basic steps are:
        * Get an InstrumentedChrome instance from the chrome pool
        * Load a URL
        * Receive the HTTP requests generated during loading
        * Send the HTTP requests to the caller
    """

    WORKER_MAX_TASKS = 5
    MIN_WORKER_THREADS = 1
    MIN_CHROME_POOL_INSTANCES = BaseConsumer.THREAD_POOL_SIZE

    def __init__(self,
                 uri_opener,
                 max_instances=None):
        """

        :param uri_opener: The uri opener required by the InstrumentedChrome
                           instances to open URLs via the HTTP proxy daemon.

        :param max_instances: Max number of Chrome processes to spawn. Use None
                              to let the pool decide what is the max.
        """
        #
        # This sizing engineering prevents the pool.get() calls from raising
        # an exception when there is no chrome process ready to use and the
        # threads are idle
        #
        max_instances = max(max_instances, self.MIN_CHROME_POOL_INSTANCES)
        max_worker_threads = max(max_instances - 1, self.MIN_WORKER_THREADS)
        worker_inqueue_max_size = max_worker_threads * 2

        self._worker_pool = Pool(processes=max_worker_threads,
                                 worker_names='ChromeWorkerThread',
                                 max_queued_tasks=worker_inqueue_max_size,
                                 maxtasksperchild=self.WORKER_MAX_TASKS)

        self._chrome_pool = ChromePool(uri_opener,
                                       max_instances=max_instances)

        self._crawled_with_chrome = DiskSet(table_prefix='crawled_with_chrome')
        self._crawler_state = CrawlerState()
        self._uri_opener = uri_opener

    def crawl(self,
              fuzzable_request,
              http_response,
              http_traffic_queue,
              debugging_id=None,
              _async=False):
        """
        Main entry point for the class

        :param fuzzable_request: The HTTP request to use as starting point
        :param http_response: The HTTP response for fuzzable_request
        :param http_traffic_queue: Queue.Queue() where HTTP requests and responses
                                   generated by the browser are sent
        :param debugging_id: A unique identifier for this call
        :param _async: Set to True to crawl using different threads from the worker pool
        :return: True if the crawling process completed (or started) successfully,
                 otherwise exceptions are raised.
        """
        if self._worker_pool is None:
            om.out.debug('ChromeCrawler is in terminate() phase. Ignoring call to crawl()')
            return False

        # Don't run crawling strategies for something that will be rejected anyways
        if not self._should_crawl_with_chrome(fuzzable_request, http_response):
            return False

        debugging_id = debugging_id or rand_alnum(8)

        # Run the different crawling strategies
        func = {True: self._crawl_async,
                False: self._crawl}[_async]

        func(fuzzable_request,
             http_traffic_queue,
             debugging_id=debugging_id)

        return True

    def get_crawl_strategy_instances(self, debugging_id):
        yield ChromeCrawlerJS(self._chrome_pool, self._crawler_state, debugging_id)
        yield ChromeCrawlerDOMDump(self._chrome_pool, debugging_id)

    def _should_crawl_with_chrome(self, fuzzable_request, http_response):
        """
        :param fuzzable_request: The HTTP request to use as starting point
        :param http_response: The HTTP response for fuzzable_request
        :return: True if we should crawl this fuzzable request with Chrome
        """
        # TODO: Add support for fuzzable requests with POST
        if fuzzable_request.get_method() != 'GET':
            return False

        # Only crawl responses that will be rendered
        if 'html' not in http_response.content_type.lower():
            return False

        # Only crawl URIs once
        uri = http_response.get_uri()
        if uri in self._crawled_with_chrome:
            return False

        self._crawled_with_chrome.add(uri)
        return True

    def has_pending_work(self):
        return bool(self.get_pending_tasks())

    def get_pending_tasks(self):
        return self._worker_pool.get_running_task_count()

    def log_pending_tasks(self):
        msg = 'ChromeCrawler status (%s running tasks, %s workers, %s tasks in queue)'
        args = (self.get_pending_tasks() - self._worker_pool.get_inqueue().qsize(),
                self._worker_pool.get_worker_count(),
                self._worker_pool.get_inqueue().qsize())
        om.out.debug(msg % args)

    def _crawl_async(self, fuzzable_request, http_traffic_queue, debugging_id=None):
        """
        Use all the crawling strategies to extract links from the loaded page.

        :return: None
        """
        for crawl_strategy in self.get_crawl_strategy_instances(debugging_id):
            args = (crawl_strategy, fuzzable_request, http_traffic_queue)
            self._worker_pool.apply_async(self._crawl_with_strategy_wrapper,
                                          args=args)

    def _crawl(self, fuzzable_request, http_traffic_queue, debugging_id=None):
        """
        Use all the crawling strategies to extract links from the loaded page.

        :return: None
        """
        for crawl_strategy in self.get_crawl_strategy_instances(debugging_id):
            self._crawl_with_strategy_wrapper(crawl_strategy, fuzzable_request, http_traffic_queue)

    def _crawl_with_strategy_wrapper(self, crawl_strategy, fuzzable_request, http_traffic_queue):
        """
        Wrapper around _crawl_with_strategy() to handle exceptions and create tasks

        :param crawl_strategy: Crawl strategy to run
        :param fuzzable_request: The FuzzableRequest instance that holds the URL to crawl
        :param http_traffic_queue: Queue to send HTTP requests and responses to
        :return: None
        """
        try:
            self._crawl_with_strategy(crawl_strategy,
                                      fuzzable_request.get_uri(),
                                      http_traffic_queue)
        except Exception as e:
            add_traceback_string(e)

            debugging_id = crawl_strategy.get_debugging_id()
            data = (fuzzable_request, e, debugging_id)

            # Sending exceptions to a queue called HTTP traffic queue is not
            # ideal but at this point it is the best way to send the exceptions
            # to the web_spider
            http_traffic_queue.put(data)
            return False

        return True

    def _crawl_with_strategy(self, crawl_strategy, url, http_traffic_queue):
        """
        Use one of the crawling strategies to extract links from the loaded page.

        :param crawl_strategy: Crawl strategy to run
        :param url: URL to crawl
        :param http_traffic_queue: Queue to send HTTP requests and responses to
        :return: None
        """
        chrome = self._get_chrome_from_pool(url,
                                            http_traffic_queue,
                                            crawl_strategy.get_debugging_id())

        try:
            self._crawl_with_strategy_and_chrome(crawl_strategy, url, chrome)
        except Exception:
            self._chrome_pool.remove(chrome, 'generic exception')
            raise
        else:
            # Success! Return the chrome instance to the pool
            self._chrome_pool.free(chrome)

    def _crawl_with_strategy_and_chrome(self, crawl_strategy, url, chrome):
        debugging_id = crawl_strategy.get_debugging_id()

        try:
            chrome = self._initial_page_load(chrome,
                                             url,
                                             debugging_id=debugging_id)
        except (ChromeInterfaceException, ChromeInterfaceTimeout) as cie:
            msg = ('Failed to perform the initial page load of %s in'
                   ' chrome crawler: "%s". Will skip the %s crawl strategy'
                   ' (did: %s)')
            args = (url, cie, crawl_strategy.get_name(), debugging_id)
            om.out.debug(msg % args)

            # These are soft exceptions, just skip this crawl strategy
            # and continue with the next one
            return

        except Exception, e:
            msg = ('Unhandled exception while trying to perform the initial'
                   ' page load of %s in chrome crawler: "%s" (did: %s)')
            args = (url, e, debugging_id)
            om.out.debug(msg % args)

            # We want to raise exceptions in order for them to reach
            # the framework's exception handler
            raise

        args = (crawl_strategy.get_name(), url, debugging_id)
        msg = 'Spent {seconds} seconds in crawl strategy %s for %s (did: %s)' % args
        took_line = TookLine(msg)

        try:
            crawl_strategy.crawl(chrome, url)
        except (ChromeInterfaceException, ChromeInterfaceTimeout, ChromeCrawlerException) as ce:
            msg = ('Failed to crawl %s using chrome crawler: "%s".'
                   ' Will skip this crawl strategy and try the next one.'
                   ' (did: %s)')
            args = (url, ce, debugging_id)
            om.out.debug(msg % args)

            # These are soft exceptions, just skip this crawl strategy
            # and continue with the next one
            return

        except Exception, e:
            msg = 'Failed to crawl %s using chrome instance %s: "%s" (did: %s)'
            args = (url, chrome, e, debugging_id)
            om.out.debug(msg % args)

            took_line.send()

            self._chrome_pool.remove(chrome, 'failed to crawl')

            # We want to raise exceptions in order for them to reach
            # the framework's exception handler
            raise

        try:
            self._cleanup(url,
                          chrome,
                          debugging_id=debugging_id)
        except (ChromeInterfaceException, ChromeInterfaceTimeout) as cie:
            msg = ('Failed to cleanup after crawling: "%s". Will skip this'
                   ' phase and continue. (did: %s)')
            args = (cie, debugging_id)
            om.out.debug(msg % args)

            took_line.send()

            # These are soft exceptions, just skip this crawl strategy
            # and continue with the next one
            return

        except Exception, e:
            msg = 'Failed to crawl %s using chrome instance %s: "%s" (did: %s)'
            args = (url, chrome, e, debugging_id)
            om.out.debug(msg % args)

            took_line.send()

            # We want to raise exceptions in order for them to reach
            # the framework's exception handler
            raise

        took_line.send()

    def _cleanup(self,
                 url,
                 chrome,
                 debugging_id=None):

        args = (chrome.http_traffic_queue.count, url, chrome, debugging_id)
        msg = 'Extracted %s new HTTP requests from %s using %s (did: %s)'
        om.out.debug(msg % args)

        #
        # In order to remove all the DOM from the chrome instance and clear
        # some memory we load the about:blank page
        #
        took_line = TookLine('Spent {seconds} seconds cleaning up')

        try:
            chrome.load_about_blank()
        except (ChromeInterfaceException, ChromeInterfaceTimeout) as cie:
            msg = 'Failed to load about:blank page in chrome browser %s: "%s" (did: %s)'
            args = (chrome, cie, debugging_id)
            om.out.debug(msg % args)

            # Since we got an error we remove this chrome instance from the
            # pool it might be in an error state
            self._chrome_pool.remove(chrome, 'failed to load about:blank')

            raise

        took_line.send()

        return True

    def _get_chrome_from_pool(self, url, http_traffic_queue, debugging_id):
        args = (url, debugging_id)
        msg = 'Getting chrome crawler from pool for %s (did: %s)'
        om.out.debug(msg % args)

        crawler_http_traffic_queue = CrawlerHTTPTrafficQueue(http_traffic_queue)

        try:
            chrome = self._chrome_pool.get(http_traffic_queue=crawler_http_traffic_queue,
                                           debugging_id=debugging_id)
        except ChromePoolException as cpe:
            args = (cpe, debugging_id)
            msg = 'Failed to get a chrome instance: "%s" (did: %s)'
            om.out.debug(msg % args)

            raise ChromeCrawlerException('Failed to get a chrome instance: "%s"' % cpe)

        return chrome

    def _initial_page_load(self, chrome, url, debugging_id=None):
        """
        Get a chrome instance from the pool and load the initial URL

        :return: A chrome instance which has the initial URL loaded and is
                 ready to be used during crawling.
        """
        args = (chrome, url, debugging_id)
        om.out.debug('Using %s to load %s (did: %s)' % args)

        chrome.set_debugging_id(debugging_id)
        start = time.time()

        msg = 'Spent {seconds} seconds loading URL %s in chrome' % url
        took_line = TookLine(msg)

        try:
            chrome.load_url(url)
        except (ChromeInterfaceException, ChromeInterfaceTimeout) as cie:
            args = (url, chrome, cie, debugging_id)
            msg = 'Failed to load %s using %s: "%s" (did: %s)'
            om.out.debug(msg % args)

            # Since we got an error we remove this chrome instance from the pool
            # it might be in an error state
            self._chrome_pool.remove(chrome, 'failed to load URL')

            raise

        try:
            successfully_loaded = chrome.wait_for_load()
        except (ChromeInterfaceException, ChromeInterfaceTimeout) as cie:
            #
            # Note: Even if we get here, the InstrumentedChrome might have sent
            # a few HTTP requests. Those HTTP requests are immediately sent to
            # the output queue.
            #
            args = (url, chrome, cie, debugging_id)
            msg = ('Exception raised while waiting for page load of %s '
                   'using %s: "%s" (did: %s)')
            om.out.debug(msg % args)

            # Since we got an error we remove this chrome instance from the pool
            # it might be in an error state
            self._chrome_pool.remove(chrome, 'exception raised')

            raise

        if not successfully_loaded:
            #
            # Just log the fact that the page is not done loading yet
            #
            spent = time.time() - start
            msg = ('Chrome did not successfully load %s in %.2f seconds '
                   'but will try to use the loaded DOM anyway (did: %s)')
            args = (url, spent, debugging_id)
            om.out.debug(msg % args)

        took_line.send()

        took_line = TookLine('Spent {seconds} seconds in chrome.stop()')

        #
        # Even if the page has successfully loaded (which is a very subjective
        # term) we click on the stop button to prevent any further requests,
        # changes, etc.
        #
        try:
            chrome.stop()
        except (ChromeInterfaceException, ChromeInterfaceTimeout) as cie:
            msg = 'Failed to stop chrome browser %s: "%s" (did: %s)'
            args = (chrome, cie, debugging_id)
            om.out.debug(msg % args)

            # Since we got an error we remove this chrome instance from the
            # pool it might be in an error state
            self._chrome_pool.remove(chrome, 'failed to stop')

            raise

        took_line.send()

        return chrome

    def terminate(self):
        om.out.debug('ChromeCrawler.terminate()')

        self._terminate_worker_pool()

        self._chrome_pool.terminate()
        self._chrome_pool = None

        self._crawler_state = None

        self._uri_opener = None

    def _terminate_worker_pool(self):
        if self._worker_pool is None:
            om.out.debug('ChromeCrawler pool is None. No shutdown required.')
            return

        #
        # Close the pool and wait for everyone to finish
        #
        # Quickly set the thread pool attribute to None to prevent other calls
        # to this method from running close() or join() twice on the same pool
        #
        pool = self._worker_pool
        self._worker_pool = None

        msg_fmt = 'Exception found while %s pool in ChromeCrawler: "%s"'

        try:
            pool.close()
        except Exception, e:
            args = ('closing', e)
            om.out.debug(msg_fmt % args)

        om.out.debug('ChromeCrawler pool is closed')

        try:
            pool.join()
        except Exception, e:
            args = ('joining', e)
            om.out.debug(msg_fmt % args)

            # First try to call join(), which is nice and waits for all the
            # tasks to complete. If that fails, then call terminate()
            try:
                pool.terminate()
            except Exception, e:
                args = ('terminating', e)
                om.out.debug(msg_fmt % args)
            else:
                msg = 'ChromeCrawler pool has been terminated after failed call to join'
                om.out.debug(msg)

        om.out.debug('ChromeCrawler pool has been joined')

    def print_all_console_messages(self):
        """
        This method will get the first chrome instance from the pool and print
        all the console.log() messages that it has.

        The method should only be used during unittests, when there is only one
        chrome instance in the pool!

        :return: None, output is written to stdout
        """
        msg = 'Chrome pool has %s instances, one is required' % len(self._chrome_pool.get_free_instances())
        assert len(self._chrome_pool.get_free_instances()) == 1, msg

        instrumented_chrome = list(self._chrome_pool.get_free_instances())[0]
        for console_message in instrumented_chrome.get_console_messages():
            print(console_message)

    def get_js_errors(self):
        """
        This method will get the first chrome instance from the pool and return
        the captured JS errors.

        The method should only be used during unittests, when there is only one
        chrome instance in the pool!

        :return: A list of JS errors
        """
        msg = 'Chrome pool has %s instances, one is required' % len(self._chrome_pool.get_free_instances())
        assert len(self._chrome_pool.get_free_instances()) == 1, msg

        instrumented_chrome = list(self._chrome_pool.get_free_instances())[0]
        return instrumented_chrome.get_js_errors()