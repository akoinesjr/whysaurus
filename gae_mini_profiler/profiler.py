from __future__ import with_statement

import datetime
import time
import logging
import os
import re
import urlparse

try:
    import threading
except ImportError:
    import dummy_threading as threading

# use json in Python 2.7, fallback to simplejson for Python 2.5
try:
    import json
except ImportError:
    import simplejson as json

import StringIO
from types import GeneratorType
import zlib

from google.appengine.api import logservice
from google.appengine.api import memcache
from google.appengine.ext.appstats import recording
from google.appengine.ext.webapp import RequestHandler

import cookies
import pickle
import config
import util

dev_server = os.environ.get("SERVER_SOFTWARE", "").startswith("Devel")


class CurrentRequestId(object):
    """A per-request identifier accessed by other pieces of mini profiler.
    
    It is managed as part of the middleware lifecycle."""

    # In production use threading.local() to make request ids threadsafe
    _local = threading.local()
    _local.request_id = None

    @staticmethod
    def get():
        return CurrentRequestId._local.request_id

    @staticmethod
    def set(request_id):
        CurrentRequestId._local.request_id = request_id


class SharedStatsHandler(RequestHandler):

    def get(self):
        path = os.path.join(os.path.dirname(__file__), "templates/shared.html")

        request_id = self.request.get("request_id")
        if not RequestStats.get(request_id):
            self.response.out.write("Profiler stats no longer exist for this request.")
            return

        # Late-bind templatetags to avoid a circular import.
        # TODO(chris): remove late-binding once templatetags has been teased
        # apart and no longer contains so many broad dependencies.

        import templatetags
        profiler_includes = templatetags.profiler_includes_request_id(request_id, True)

        # We are not using a templating engine here to avoid pulling in Jinja2
        # or Django. It's an admin page anyway, and all other templating lives
        # in javascript right now.

        with open(path, 'rU') as f:
            template = f.read()

        template = template.replace('{{profiler_includes}}', profiler_includes)
        self.response.out.write(template)


class RequestLogHandler(RequestHandler):
    """Handler for retrieving and returning a RequestLog from GAE's logs API.

    See https://developers.google.com/appengine/docs/python/logs.
    
    This GET request accepts a logging_request_id via query param that matches
    the request_id from an App Engine RequestLog.

    It returns a JSON object that contains the pieces of RequestLog info we
    find most interesting, such as pending_ms and loading_request.
    """

    def get(self):

        self.response.headers["Content-Type"] = "application/json"
        dict_request_log = None

        # This logging_request_id should match a request_id from an App Engine
        # request log.
        # https://developers.google.com/appengine/docs/python/logs/functions
        logging_request_id = self.request.get("logging_request_id")

        # Grab the single request log from logservice
        logs = logservice.fetch(request_ids=[logging_request_id])

        # This slightly strange query result implements __iter__ but not next,
        # so we have to iterate to get our expected single result.
        for log in logs:
            dict_request_log = {
                "pending_ms": log.pending_time,  # time spent in pending queue
                "loading_request": log.was_loading_request,  # loading request?
                "logging_request_id": logging_request_id
            }
            # We only expect a single result.
            break

        # Log fetching doesn't work on the dev server and this data isn't
        # relevant in dev server's case, so we return a simple fake response.
        if dev_server:
            dict_request_log = {
                "pending_ms": 0,
                "loading_request": False,
                "logging_request_id": logging_request_id
            }

        self.response.out.write(json.dumps(dict_request_log))


class RequestStatsHandler(RequestHandler):

    def get(self):

        self.response.headers["Content-Type"] = "application/json"

        list_request_ids = []

        request_ids = self.request.get("request_ids")
        if request_ids:
            list_request_ids = request_ids.split(",")

        list_request_stats = []

        for request_id in list_request_ids:

            request_stats = RequestStats.get(request_id)

            if request_stats and not request_stats.disabled:

                dict_request_stats = {}
                for property in RequestStats.serialized_properties:
                    dict_request_stats[property] = request_stats.__getattribute__(property)

                list_request_stats.append(dict_request_stats)

                # Don't show temporary redirect profiles more than once automatically, as they are
                # tied to URL params and may be copied around easily.
                if request_stats.temporary_redirect:
                    request_stats.disabled = True
                    request_stats.store()

        self.response.out.write(json.dumps(list_request_stats))

class RequestStats(object):

    serialized_properties = ["request_id", "method", "url", "url_short", "s_dt",
                             "profiler_results", "appstats_results", "mode",
                             "temporary_redirect", "logs",
                             "logging_request_id"]

    def __init__(self, profiler, environ):
        # unique mini profiler request id
        self.request_id = profiler.request_id

        # App Engine's logservice request_id
        # https://developers.google.com/appengine/docs/python/logs/
        self.logging_request_id = profiler.logging_request_id

        self.method = environ.get("REQUEST_METHOD")

        self.url = environ.get("PATH_INFO")
        if environ.get("QUERY_STRING"):
            self.url += "?%s" % environ.get("QUERY_STRING")

        self.url_short = unicode(self.url, 'utf-8')
        if len(self.url_short) > 26:
            self.url_short = self.url_short[:26] + "..."

        self.mode = profiler.mode
        self.s_dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.profiler_results = profiler.profiler_results()
        self.appstats_results = profiler.appstats_results()
        self.logs = profiler.logs

        self.temporary_redirect = profiler.temporary_redirect
        self.disabled = False

    def store(self):
        # Store compressed results so we stay under the memcache 1MB limit
        pickled = pickle.dumps(self)
        compressed_pickled = zlib.compress(pickled)
        if len(compressed_pickled) > memcache.MAX_VALUE_SIZE:
            logging.warning('RequestStats bigger (%d) '
                + 'than max memcache size (%d), even after compression',
                len(compressed_pickled), memcache.MAX_VALUE_SIZE)
            return False

        return memcache.set(RequestStats.memcache_key(self.request_id), compressed_pickled)

    @staticmethod
    def get(request_id):
        if request_id:

            compressed_pickled = memcache.get(RequestStats.memcache_key(request_id))

            if compressed_pickled:
                pickled = zlib.decompress(compressed_pickled)
                return pickle.loads(pickled)

        return None

    @staticmethod
    def memcache_key(request_id):
        if not request_id:
            return None
        return "__gae_mini_profiler_request_%s" % request_id


class ThreadFilter(logging.Filter):
    "A logging filter that only allows records from the creating thread."""

    def __init__(self, *args, **kwargs):
        super(ThreadFilter, self).__init__(*args, **kwargs)
        self.currentThreadIdent = threading.current_thread().ident

    def filter(self, _):
        return self.currentThreadIdent == threading.current_thread().ident


class RequestProfiler(object):
    """Profile a single request."""

    def __init__(self, request_id, mode):
        self.request_id = request_id
        self.mode = mode
        self.instrumented_prof = None
        self.sampling_prof = None
        self.linebyline_prof = None
        self.appstats_prof = None
        self.temporary_redirect = False
        self.logs = None
        self.logging_request_id = self.get_logging_request_id()
        self.start = None
        self.end = None

    def profiler_results(self):
        """Return the CPU profiler results for this request, if any.
        
        This will return a dictionary containing results for either the
        sampling profiler, instrumented profiler results, or a simple
        start/stop timer if both profilers are disabled."""

        total_time = util.seconds_fmt(self.end - self.start, 0)
        results = {"total_time": total_time}

        if self.instrumented_prof:
            results.update(self.instrumented_prof.results())
        elif self.sampling_prof:
            results.update(self.sampling_prof.results())
        elif self.linebyline_prof:
            results.update(self.linebyline_prof.results())

        return results

    def appstats_results(self):
        """Return the RPC profiler (appstats) results for this request, if any.

        This will return a dictionary containing results from appstats or an
        empty result set if appstats profiling is disabled."""

        results = {
                "calls": [],
                "total_time": 0,
                }

        if self.appstats_prof:
            results.update(self.appstats_prof.results())

        return results

    def profile_start_response(self, app, environ, start_response):
        """Collect and store statistics for a single request.

        Use this method from middleware in place of the standard
        request-serving pattern. Do:

           profiler = RequestProfiler(...)
           return profiler(app, environ, start_response)

        Instead of:

           return app(environ, start_response)

        Depending on the mode, this method gathers timing information
        and an execution profile and stores them in the datastore for
        later access.
        """

        # Always track simple start/stop time.
        self.start = time.time()

        if self.mode == config.Mode.SIMPLE:

            # Detailed recording is disabled.
            result = app(environ, start_response)
            for value in result:
                yield value

        else:

            # Add logging handler
            handler = RequestProfiler.create_handler()
            logging.getLogger().addHandler(handler)

            if config.Mode.is_rpc_enabled(self.mode):
                # Turn on AppStats monitoring for this request
                # Note that we don't import appstats_profiler at the top of
                # this file so we don't bring in a lot of imports for users who
                # don't have the profiler enabled.
                from . import appstats_profiler
                self.appstats_prof = appstats_profiler.Profile()
                app = self.appstats_prof.wrap(app)

            # By default, we create a placeholder wrapper function that
            # simply calls whatever function it is passed as its first
            # argument.
            result_fxn_wrapper = lambda fxn: fxn()

            # TODO(kamens): both sampling_profiler and instrumented_profiler
            # could subclass the same class. Then they'd both be guaranteed to
            # implement run(), and the following if/else could be simplified.
            if config.Mode.is_sampling_enabled(self.mode):
                # Turn on sampling profiling for this request.
                # Note that we don't import sampling_profiler at the top of
                # this file so we don't bring in a lot of imports for users who
                # don't have the profiler enabled.
                from . import sampling_profiler
                self.sampling_prof = sampling_profiler.Profile()
                result_fxn_wrapper = self.sampling_prof.run

            elif config.Mode.is_instrumented_enabled(self.mode):
                # Turn on cProfile instrumented profiling for this request
                # Note that we don't import instrumented_profiler at the top of
                # this file so we don't bring in a lot of imports for users who
                # don't have the profiler enabled.
                from . import instrumented_profiler
                self.instrumented_prof = instrumented_profiler.Profile()
                result_fxn_wrapper = self.instrumented_prof.run

            elif config.Mode.is_linebyline_enabled(self.mode):
                from . import linebyline_profiler
                self.linebyline_prof = linebyline_profiler.Profile()
                result_fxn_wrapper = self.linebyline_prof.run

            # Get wsgi result
            result = result_fxn_wrapper(lambda: app(environ, start_response))

            # If we're dealing w/ a generator, profile all of the .next calls as well
            if type(result) == GeneratorType:

                while True:
                    try:
                        yield result_fxn_wrapper(result.next)
                    except StopIteration:
                        break

            else:
                for value in result:
                    yield value

            logging.getLogger().removeHandler(handler)
            self.logs = self.get_logs(handler)
            handler.stream.close()

        self.end = time.time()

        # Store stats for later access
        RequestStats(self, environ).store()

    def get_logging_request_id(self):
        """Return the identifier for this request used by GAE's logservice.
        
        This logging_request_id will match the request_id parameter of a
        RequestLog object stored in App Engine's logging API:
        https://developers.google.com/appengine/docs/python/logs/
        """
        return os.environ.get("REQUEST_LOG_ID", None)

    @staticmethod
    def create_handler():
        handler = logging.StreamHandler(StringIO.StringIO())
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter("\t".join([
            '%(levelno)s',
            '%(asctime)s%(msecs)d',
            '%(funcName)s',
            '%(filename)s',
            '%(lineno)d',
            '%(message)s',
        ]), '%M:%S.')
        handler.setFormatter(formatter)
        handler.addFilter(ThreadFilter())
        return handler

    @staticmethod
    def get_logs(handler):
        raw_lines = [l for l in handler.stream.getvalue().split("\n") if l]

        lines = []
        for line in raw_lines:
            if "\t" in line:
                fields = line.split("\t")
                lines.append(fields)
            else: # line is part of a multiline log message (prob a traceback)
                prevline = lines[-1][-1]
                if prevline: # ignore leading blank lines in the message
                    prevline += "\n"
                prevline += line
                lines[-1][-1] = prevline

        return lines

class ProfilerWSGIMiddleware(object):

    def __init__(self, app, keep_appstats=True):
        self.app = app
        self.keep_appstats = keep_appstats

    def __call__(self, environ, start_response):

        CurrentRequestId.set(None)

        # Never profile calls to the profiler itself to avoid endless recursion.
        if (not config.should_profile() or
            environ.get("PATH_INFO", "").startswith("/gae_mini_profiler/")):
            app = self.app
            if self.keep_appstats:
                app = recording.appstats_wsgi_middleware(app)
            result = app(environ, start_response)
            for value in result:
                yield value
        else:
            # Set a random ID for this request so we can look up stats later
            import base64
            CurrentRequestId.set(base64.urlsafe_b64encode(os.urandom(5)))
            profile_mode = config.get_mode(environ, cookies)

            # Send request id in headers so jQuery ajax calls can pick
            # up profiles.
            def profiled_start_response(status, headers, exc_info = None):

                if status.startswith("302 "):
                    # Temporary redirect. Add request identifier to redirect location
                    # so next rendered page can show this request's profile.
                    headers = ProfilerWSGIMiddleware.headers_with_modified_redirect(environ, headers)
                    # Access the profiler in closure scope
                    profiler.temporary_redirect = True

                # Append headers used when displaying profiler results from ajax requests
                headers.append(("X-MiniProfiler-Id", CurrentRequestId.get()))
                headers.append(("X-MiniProfiler-QS", str(environ.get("QUERY_STRING"))))
                headers.append(("Set-Cookie",
                    cookies.set_cookie_value("g-m-p-mode", profile_mode, path="/")))

                return start_response(status, headers, exc_info)

            # As a simple form of rate-limiting, appstats protects all
            # its work with a memcache lock to ensure that only one
            # appstats request ever runs at a time, across all
            # appengine instances.  (GvR confirmed this is the purpose
            # of the lock).  So our attempt to profile will fail if
            # appstats is running on another instance.  Boo-urns!  We
            # just turn off the lock-checking for us, which means we
            # don't rate-limit quite as much with the mini-profiler as
            # we would do without.
            old_memcache_add = memcache.add
            old_memcache_delete = memcache.delete
            memcache.add = (lambda key, *args, **kwargs:
                                (True if key == recording.lock_key() 
                                 else old_memcache_add(key, *args, **kwargs)))
            memcache.delete = (lambda key, *args, **kwargs:
                                   (True if key == recording.lock_key()
                                    else old_memcache_delete(key, *args, **kwargs)))

            try:
                profiler = RequestProfiler(CurrentRequestId.get(), profile_mode)
                result = profiler.profile_start_response(self.app, environ, profiled_start_response)
                for value in result:
                    yield value
            finally:
                CurrentRequestId.set(None)
                memcache.add = old_memcache_add
                memcache.delete = old_memcache_delete

    @staticmethod
    def headers_with_modified_redirect(environ, headers):
        """Return headers with redirects modified to include miniprofiler id.

        If this response is a redirect, we want the URL that's redirected *to*
        to be able to display the profiler results from *this* request that's
        being redirected *from*. We do this by adding a query string param,
        'mp-r-id', to the location that is being redirected to. (mp-r-id stands
        for mini profiler redirect id.) The value of this parameter is a unique
        identifier for the profiler results for the current request that is
        being redirected from.

        The mini profiler then knows how to use this id to display profiler
        results for two requests: the original request that redirected and the
        request that was served as a result of the redirect.

        e.g. if this set of headers is attempting to redirect to
            Location:http://khanacademy.org?login, the modified header will be:
            Location:http://khanacademy.org?login&mp-r-id={current request id}
        """
        headers_modified = []

        for header in headers:
            if header[0] == "Location":
                reg = re.compile("mp-r-id=([^&]+)")

                # Keep any chain of redirects around
                request_id_chain = CurrentRequestId.get()
                match = reg.search(environ.get("QUERY_STRING"))
                if match:
                    request_id_chain = ",".join([match.groups()[0], request_id_chain])

                # Remove any pre-existing miniprofiler redirect id
                url_parts = list(urlparse.urlparse(header[1]))
                query_string = reg.sub("", url_parts[4])

                # Add current request id as miniprofiler redirect id
                if query_string and not query_string.endswith("&"):
                    query_string += "&"
                query_string += "mp-r-id=%s" % request_id_chain
                url_parts[4] = query_string

                # Swap in the modified Location: header.
                location = urlparse.urlunparse(url_parts)
                headers_modified.append((header[0], location))
            else:
                headers_modified.append(header)

        return headers_modified
