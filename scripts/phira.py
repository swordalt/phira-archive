"""Minimal client for the Phira chart API (stdlib only, no dependencies)."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://phira.5wyxi.com"
USER_AGENT = "phira-archive/1.0 (+https://github.com/swordalt/phira-archive)"

DIVISIONS = ("regular", "troll", "plain", "visual")
MAX_PAGE_NUM = 30  # the API rejects anything larger with 400 INVALID_INPUT


class ApiError(RuntimeError):
    """The API answered, but not with data we can archive."""


class NotFound(ApiError):
    """404 - the chart is gone or was never public."""


class Client:
    """Polite, retrying HTTP client. One instance per run."""

    def __init__(self, delay=0.3, retries=3, timeout=30):
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self._last_request = 0.0
        self.requests = 0

    def _sleep(self):
        wait = self.delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)

    def _get(self, path, params=None):
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

        for attempt in range(self.retries + 1):
            self._sleep()
            self._last_request = time.monotonic()
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise NotFound("404 {}".format(url)) from exc
                if exc.code < 500:
                    # 400 and friends mean the API contract changed. Stop, do not
                    # write garbage into the archive.
                    body = exc.read().decode("utf-8", "replace")[:200]
                    raise ApiError("HTTP {} {} {}".format(exc.code, url, body)) from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last = exc
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise ApiError("giving up on {} after {} attempts: {}".format(url, self.retries + 1, last))

    def list_charts(self, division, order=None, page=1, page_num=20):
        """One page of a division. Returns (total_count, [chart, ...])."""
        params = {"division": division, "page": page, "pageNum": min(page_num, MAX_PAGE_NUM)}
        if order:
            params["order"] = order
        payload = self._get("/chart", params)
        return payload.get("count", 0), payload.get("results", [])

    def get_chart(self, chart_id):
        """The canonical record for one chart. Raises NotFound if it is gone."""
        return self._get("/chart/{}".format(chart_id))
