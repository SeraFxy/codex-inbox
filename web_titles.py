"""Read a public HTML page's title without executing JavaScript or sending cookies."""
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 512 * 1024
SKIP_SUFFIX = re.compile(r'\.(?:pdf|zip|gz|tar|png|jpe?g|gif|webp|svg|mp[34]|mov|xlsx?|docx?|pptx?|json|csv)(?:$|/)', re.I)
LOGIN_PATH = re.compile(r'/(?:login|signin|sign-in|logout|auth|oauth|sso|api|open-apis|graphql)(?:/|$)', re.I)
SECRET_PARAM = re.compile(r'token|password|secret|signature|api.?key|credential|^code$|^auth', re.I)


def canonical_web_url(url):
    try:
        p = urlsplit(url)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return ''
        if p.port not in (None, 80, 443) or LOGIN_PATH.search(p.path) or SKIP_SUFFIX.search(p.path):
            return ''
        if any(SECRET_PARAM.search(k) for k, _ in parse_qsl(p.query)):
            return ''
        return url.split('#', 1)[0]
    except (ValueError, TypeError):
        return ''


def public_web_url(url):
    canonical = canonical_web_url(url)
    if not canonical:
        return False
    try:
        p = urlsplit(canonical)
        try:
            if not ipaddress.ip_address(p.hostname).is_global:
                return False
        except ValueError:
            pass
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(x[4][0]).is_global for x in addresses)
    except (OSError, ValueError):
        return False


def clean_title(value):
    value = re.sub(r'\s+', ' ', value or '').strip()
    if not value or re.match(r'^(?:sign in|log in|login|access denied|just a moment|403\b|404\b|登录|登陆|无权访问|访问被拒绝)(?:\b|\s|$)', value, re.I):
        return ''
    return value[:300]


class PageTitleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading, self.title, self.meta = '', '', ''
        self.capture, self.parts, self.skip, self.hidden = '', [], 0, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style', 'template', 'noscript', 'svg'):
            self.skip += 1
        if tag not in ('meta', 'br', 'img', 'input', 'link', 'hr', 'source', 'wbr'):
            self.hidden.append((tag, 'hidden' in attrs or attrs.get('aria-hidden') == 'true' or
                                bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style', ''), re.I))))
        if self.skip or any(x[1] for x in self.hidden):
            return
        if tag == 'meta' and (attrs.get('property') or attrs.get('name') or '').lower() in ('og:title', 'twitter:title'):
            self.meta = self.meta or clean_title(attrs.get('content'))
        if tag in ('h1', 'title') and not self.capture:
            self.capture, self.parts = tag, []
        elif tag == 'br' and self.capture:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag == self.capture:
            text = clean_title(''.join(self.parts))
            if tag == 'h1':
                self.heading = self.heading or text
            else:
                self.title = self.title or text
            self.capture, self.parts = '', []
        if tag in ('script', 'style', 'template', 'noscript', 'svg'):
            self.skip = max(0, self.skip - 1)
        for i in range(len(self.hidden) - 1, -1, -1):
            if self.hidden[i][0] == tag:
                del self.hidden[i:]
                break

    def handle_data(self, data):
        if self.capture and not self.skip and not any(x[1] for x in self.hidden):
            self.parts.append(data)


def parse_page_title(html):
    parser = PageTitleParser()
    parser.feed(html)
    # 网页元信息通常保留副标题和版本号；没有元信息时再用正文的主标题。
    return parser.meta or parser.title or parser.heading


class PublicRedirects(HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 1

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_web_url(newurl):
            raise ValueError('Unsupported title redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_web_title(url):
    if not public_web_url(url):
        return ''
    try:
        req = Request(canonical_web_url(url), headers={'User-Agent': 'codex-inbox title-preview', 'Accept': 'text/html,application/xhtml+xml', 'Accept-Encoding': 'identity'})
        with build_opener(PublicRedirects()).open(req, timeout=4) as response:
            if response.headers.get_content_type() not in ('text/html', 'application/xhtml+xml'):
                return ''
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                return ''
            parts, size, deadline = [], 0, time.monotonic() + 6
            while size < MAX_BYTES and time.monotonic() < deadline:
                chunk = response.read1(min(64 * 1024, MAX_BYTES - size))
                if not chunk:
                    break
                parts.append(chunk)
                size += len(chunk)
            data = b''.join(parts)
            charset = response.headers.get_content_charset()
            if not charset:
                match = re.search(br'charset\s*=\s*["\']?([\w-]+)', data[:4096], re.I)
                charset = match.group(1).decode('ascii') if match else 'utf-8'
            try:
                html = data.decode(charset, 'replace')
            except LookupError:
                html = data.decode('utf-8', 'replace')
            return parse_page_title(html)
    except (OSError, ValueError, UnicodeError):
        return ''
