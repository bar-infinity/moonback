"""One public HTTPS page per invocation; no cookies, proxies, redirects or JS."""
import http.client
import ipaddress
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from content import public_url

USER_AGENT = 'MoonBackArchiveBot/0.3'
LIMIT = 512 * 1024


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def fetch(url):
    public_url(url)
    p = urlsplit(url)
    addresses = sorted({x[4][0] for x in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError('Non-public network destination refused')
    # Connect to the validated IP, with TLS verification against the original host.
    conn = PinnedHTTPS(p.hostname, addresses[0])
    try:
        conn.request('GET', p.path or '/', headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError('HTTP status refused: ' + str(response.status))
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            raise ValueError('Encoded response refused')
        mime = response.headers.get_content_type()
        if mime not in ('text/html', 'text/plain', 'text/markdown'):
            raise ValueError('Unsupported content type')
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError('Response too large')
        return data.decode(response.headers.get_content_charset() or 'utf-8', errors='replace'), mime
    finally:
        conn.close()


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def crawl(url):
    public_url(url)
    p = urlsplit(url)
    robots_url = 'https://' + p.netloc + '/robots.txt'
    rules, _ = fetch(robots_url)  # Fail closed even on 404 or redirect.
    parser = RobotFileParser()
    parser.parse(rules.splitlines())
    if not parser.can_fetch(USER_AGENT, url):
        raise ValueError('robots.txt disallows this URL')
    # Conservative extra check: honor wildcard Disallow in ANY group, too.
    # This intentionally skips some otherwise permitted pages in v0.3.
    for line in rules.splitlines():
        field, sep, value = line.partition(':')
        value = value.split('#', 1)[0].strip()
        if sep and field.strip().lower() == 'disallow' and value:
            pattern = re.escape(value).replace(r'\*', '.*')
            if pattern.endswith(r'\$'):
                pattern = pattern[:-2] + '$'
            if re.match(pattern, p.path or '/'):
                raise ValueError('Conservative robots exclusion')
    delays = [2.0]
    for line in rules.splitlines():
        field, sep, value = line.partition(':')
        if sep and field.strip().lower() in ('crawl-delay', 'request-rate'):
            value = value.split('#', 1)[0].strip()
            try:
                if field.strip().lower() == 'request-rate':
                    count, seconds = value.split('/')
                    delay = float(seconds) / float(count)
                else:
                    delay = float(value)
                if not 0 <= delay <= 30:
                    raise ValueError()
                delays.append(delay)
            except (ValueError, ZeroDivisionError):
                raise ValueError('Unsupported robots rate; skip this site')
    time.sleep(max(delays))
    text, mime = fetch(url)
    if mime == 'text/html':
        extractor = TextExtractor()
        extractor.feed(text)
        text = ' '.join(extractor.parts)
    return {'source_url': url, 'public_text': ' '.join(text.split())[:12000]}
