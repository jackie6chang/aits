# BS2 GAE Reverse Proxy
# Russell <yufeiwu@gmail.com>
# Please use wisely

import wsgiref.handlers, logging, zlib, re, traceback, logging, sys
import webapp2
import jinja2
import httpagentparser
import os
from google.appengine.api import urlfetch
from google.appengine.api import urlfetch_errors
from google.appengine.api import memcache
from bs2grpconfig import BS2GRPConfig
from bs2grpfile import *
from bs2grpadmin import *
from lib.basehandler import BaseHandler
version = '1.0'

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)+"/templates"),
    extensions=['jinja2.ext.autoescape'])

class BS2GRProxyHandler(webapp2.RequestHandler):
    IgnoreHeaders= ['connection', 'keep-alive', 'proxy-authenticate',
               'proxy-authorization', 'te', 'trailers',
               'transfer-encoding', 'upgrade', 'content-length', 'host']

    def permanent_redirect(self, url):
        logging.info("Redirecting to: " + url)
        self.response.set_status(301)
        self.response.headers['Location'] = str(url)
        return

    def process(self, allow_cache = True):
        ## JCC: no cache for testing
        allow_cache = False
        try:
            config = BS2GRPConfig.get_config()

            path_qs = self.request.path_qs
            method = self.request.method
            new_path = config.target_host
            block_path = '.0.0.7.3.0.9.3.1.1.1.13.1.1.11.1.5.1.1.35'
            block_condition = 'columnID='+'5'

            # Check method
            if method != 'GET' and method != 'HEAD' and method != 'POST':
                raise Exception('Method not allowed.')
            if method == 'GET':
                method = urlfetch.GET
            elif method == 'HEAD':
                method = urlfetch.HEAD
            elif method == 'POST':
                method = urlfetch.POST

            # Check path
            scm = self.request.scheme
            if (scm.lower() != 'http' and scm.lower() != 'https'):
                raise Exception('Unsupported Scheme.')
            if block_path in path_qs and block_condition in path_qs :
                self.redirect('http://aits-test.appspot.com/NeedPermissionPage/')
                return
            else :
                new_path = scm + '://' + new_path

            # Check Referer
            referrer = self.request.headers.get('Referer')
            if referrer and config.referrer_re and re.search(config.referrer_re, referrer, re.IGNORECASE):
                if config.referrer_redirect:
                    self.permanent_redirect(config.referrer_redirect)
                else:
                    self.permanent_redirect(BS2GRPConfig.REFERRER_REDIRECT)
                return

            # Redirect filters
            if (config.filter1_re and config.filter1_redirect and re.search(config.filter1_re, path_qs, re.IGNORECASE)):
                new_path = scm + '://' + config.filter1_redirect + path_qs
                self.permanent_redirect(new_path)
                return

            if (config.filter2_re and config.filter2_redirect and re.search(config.filter2_re, path_qs, re.IGNORECASE)):
                new_path = scm + '://' + config.filter2_redirect + path_qs
                self.permanent_redirect(new_path)
                return

            if (config.filter3_re and config.filter3_redirect and re.search(config.filter3_re, path_qs, re.IGNORECASE)):
                new_path = scm + '://' + config.filter3_redirect + path_qs
                self.permanent_redirect(new_path)
                return

            # Process headers
            newHeaders = dict(self.request.headers)
            newHeaders['Connection'] = 'close'

            # Try cache
            cache = None
            need_cache = False
            fetched = False
            not_processed = True

            # If client is expecting a 304 response, remember it
            after_date = string_to_datetime(newHeaders.get('If-Modified-Since', None))

            if not_processed and allow_cache and config.cache_static:
                # Cachable file
                if config.cachable_re and re.search(config.cachable_re, path_qs, re.IGNORECASE):
                    need_cache = True

                cache = BS2GRPFile.get_file(path_qs)
                if cache:
                    logging.info("Cache result:" + str(cache.status_code) + str(cache.get_mdate()))

                # If cache exists, we can make the fetch more efficient by adding If-Modified-Since header
                if cache and cache.mdatetime:
                    newHeaders['If-Modified-Since'] = cache.get_mdate()

                # Do a fetch to update the cache or if the cache doesn't exist. Otherwise simply return the cache
                if cache:
                    not_processed = cache.need_check(config.cache_check)
                    if not_processed:
                        logging.info("Cache update:" + cache.path)

                if after_date and not cache:
                    logging.info("No Cache 304 expectation:" + path_qs)

            # Do a fetch if needed
            resp = None
            if not_processed:
                # Reverse convert
                new_path_qs = path_qs
                if config.abs_url_filter:
                    new_path_qs = path_qs.replace(self.request.host, config.target_host)
                    logging.info("ABS URL filter Reverse: %s filtered" % path_qs)

                new_path = new_path + new_path_qs
                logging.info("Requesting target: " + new_path)
                for _ in range(config.retry):
                    try:
                        resp = urlfetch.fetch(new_path, self.request.body, method, newHeaders, False, False)
                        fetched = True
                        break
                    except urlfetch_errors.ResponseTooLargeError:
                        raise Exception('Response too large.')
                    except Exception:
                        continue
                else:
                    raise Exception('Requested host is not available. Please try again later.')

                if config.abs_url_filter:
                    count = 0
                    if resp.headers.get('Content-Type', '').find('html') >= 0:
                        resp.content, tmp = config.host_exp.subn(self.request.host, resp.content)
                        count += tmp
                    if resp.headers.has_key('Location'):
                        resp.headers['Location'], tmp = config.host_exp.subn(self.request.host, resp.headers['Location'])
                        count += tmp
                    logging.info("ABS URL filter: %d filtered" % count)

            # We did urlfetch and we got cache. So refresh the cache's last check time stamp
            if cache and fetched:
                cache.last_check = datetime.datetime.now()
                cache.put()

            # Process cache
            if (not fetched) or (resp.status_code == 304 and cache):
                if after_date and cache.mdatetime and after_date == cache.mdatetime:
                    # Don't return cached content since client already has a cached copy
                    # So just return a 304 response
                    logging.info('Cache hit and 304:' + cache.path)
                    self.response.set_status(304)
                    return
                elif (not after_date) or (not cache.mdatetime) or (after_date < cache.mdatetime):
                    # Cached file is newer
                    # Return cache
                    logging.info('Cache hit %d:' % cache.status_code + cache.path)
                    self.response.set_status(cache.status_code)
                    cache.to_headers(self.response.headers)
                    #logging.info("HEADERS:\n" + str(self.response.headers))
                    cache.to_string_io(self.response.out)
                    return
            elif need_cache and (not 300 <= resp.status_code < 400):
                # Cache response except 3xx
                logging.info('Caching %d:' % resp.status_code + path_qs)
                if cache:
                    # Refresh cache
                    cache.clear_content()
                    cache.clear_headers()
                else:
                    cache = BS2GRPFile(key_name=path_qs, path=path_qs, last_check = datetime.datetime.now())

                cache.status_code = resp.status_code
                cache.from_headers(resp.headers)
                cache.from_string(resp.content)
                cache.refresh_content_length()
                cache.put()

            # Forward response if no cache is provided
            self._resp_to_response(resp)

        except Exception, e:
            self.response.out.write('BS2Proxy Error: %s.' % str(e))
            t1, t2, tb = sys.exc_info()
            logging.error('Exception:' + str(e))
            logging.error(traceback.format_tb(tb, 5))
            return

    def _resp_to_response(self, resp):
        self.response.set_status(resp.status_code)
        textContent = True
        for header in resp.headers:
            if header.strip().lower() in self.IgnoreHeaders:
                logging.info("Ignoring header '%s' of %s" % (header, resp.headers[header]))
                continue
            ### JCC: keep session cookie
            if header.strip().lower() == 'set-cookie':
                logging.info("got cookie header '%s' of %s" % (header, resp.headers[header]))
                ### JCC: non-weekday-comma means concatenation of 'Set-Cookie' headers
                cookies = re.split(r'(?<!Sun|Mon|Tue|Wed|Thu|Fri|Sat), ', resp.headers[header])
                for cookie in cookies:
                    self.response.headers.add_header('Set-Cookie', cookie)
                    logging.info("set cookie '%s' to be : %s" % (header, cookie))
                continue

            self.response.headers[header] = resp.headers[header]

        ### JCC: replace TARGET_HOST with my hostname (from user request)
        import bs2grpconfig
        self.response.out.write(resp.content.replace(bs2grpconfig.TARGET_HOST, self.request.url.split('//')[1].split('/')[0]))


    def post(self):
        return self.process(False)

    def get(self,):
        return self.process(True)

    def head(self, path = None):
        return self.process(True)



