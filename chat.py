from __future__ import unicode_literals

from Cookie import SimpleCookie
from functools import partial
import json
import mimetypes
import os.path
import random
import re
import string
from urlparse import parse_qs

import bleach
import redis
from sse import Sse

import logging
log = logging.getLogger(__name__)

pool = redis.ConnectionPool()

# Rate limiting ?

STATUS_OK = '200 OK'
STATUS_NOT_FOUND = '404 Not Found'
STATUS_METHOD_NOT_ALLOWD = '405 Method not allowed'

RANDOM_CHARS = string.letters + string.digits
def random_string(source=RANDOM_CHARS, length=32):
    return ''.join(random.choice(source) for x in xrange(length))

def get_template(name):
    with file(os.path.join('templates/', name), 'rb') as fin:
        return fin.read()

# Nick handling

def make_key(*args):
    return u':'.join(args)

def get_nicks(request):
    keys = request.conn.keys(make_key(request.channel, '*', 'nick'))
    return dict(zip(request.conn.mget(keys), keys)) if keys else {}

def get_nick(request):
    key = make_key(request.channel, request.tag, 'nick')
    n = request.conn.get(key)
    if n is None:
        n = request.tag[:8]
        set_nick(request, n)
    else:
        request.conn.expire(key, 90)
    return n

def set_nick(request, name):
    name = strip_tags(name)
    if name in get_nicks(request):
        raise ValueError('Nick in use!')
    key = make_key(request.channel, request.tag, 'nick')
    request.conn.set(key, name, ex=90)
    return name

# Message handling

def post_message(request, message, mode='message', queue=None, **data):
    if queue is None:
        queue = make_key(request.channel, 'channel')

    data.setdefault('message', message)
    data.setdefault('sender', get_nick(request))

    content = json.dumps(data)
    request.conn.publish(queue, json.dumps([mode, content]))

strip_tags = partial(bleach.clean, tags=[], strip=True)

def linkify_external(attrs, new=False):
    attrs['target'] = '_blank'
    return attrs

# The application!

class Response(object):
    def __init__(self, content='', status=STATUS_OK, content_type='text/html'):
        self.content = content
        self.status = status
        self.headers = {}
        self.headers['Content-Type'] = content_type
        self.cookies = SimpleCookie()


class App(object):
    def __init__(self, patterns):
        self.patterns = patterns

    def __call__(self, environ, start_response):
        self.environ = environ

        self.method = environ['REQUEST_METHOD']
        self.path = environ.get('PATH_INFO', '/')

        self.cookies = self.parse_cookies()
        self.QUERY_DATA = self.parse_query_data()

        tag = self.cookies.get('chatterbox')
        if tag:
            self.tag = random_string()
        else:
            self.tag = tag

        # Dispatch
        response = Response(status=STATUS_NOT_FOUND)
        for pattern in self.patterns:
            m = re.match(pattern[0], self.path)
            if m:
                response = pattern[1](self, **m.groupdict())

        if not tag:
            response.cookies['chatterbox'] = self.tag

        headers = list(response.headers.items()) + [
            ('Set-Cookie', cookie.OutputString())
            for cookie in response.cookies.values()
        ]

        start_response(response.status, headers)
        return bytes(response.content.encode('utf-8'))

    def parse_cookies(self):
        cookies = self.environ.get('HTTP_COOKIE', '')
        if cookies == '':
            return {}
        else:
            c = SimpleCookie()
            c.load(cookies)
            return { key: c.get(key).value for key in c.keys() }

    def parse_query_data(self):
        if self.method == 'GET':
            return parse_qs(self.environ.get('QUERY_STRING', ''))
        elif self.method == 'POST':
            # Should test content type
            size = int(self.environ.get('CONTENT_LENGTH', 0))
            if not size:
                return {}
            return parse_qs(self.environ['wsgi.input'].read(size))

def index(request):
    return Response(get_template('index.html'))

def chat(request, channel=None):
    request.channel = channel

    request.conn = redis.StrictRedis(connection_pool=pool)

    if request.method == 'GET':
        if not 'text/event-stream' in request.environ['HTTP_ACCEPT']:
            return Response(get_template('chat.html'))

        pubsub = request.conn.pubsub()
        pubsub.subscribe([
            make_key(request.channel, 'channel'),
            make_key(request.tag, 'private'),
        ])

        def _iterator():
            sse = Sse()
            for msg in pubsub.listen():
                if msg['type'] == 'message':
                    mode, data = json.loads(msg['data'])
                    sse.add_message(mode, data)
                    for item in sse:
                        yield bytes(item.encode('utf-8'))

        post_message(request, '{} connected.'.format(get_nick(request)), 'join', sender='Notice')

        response = Response(_iterator(), content_type='text/event-stream')
        response.headers['Cache-Control'] = 'no-cache'

    elif request.method == 'POST':

        nick = get_nick(request)

        mode = request.QUERY_DATA.get('mode', ['message'])[0]
        msg = request.QUERY_DATA.get('message', [''])[0]
        msg = bleach.linkify(strip_tags(msg), callbacks=[linkify_external,])

        if mode == 'nick' and msg:
            try:
                new_nick = set_nick(request, msg)
            except ValueError:
                post_message(request, 'Nick in use!', 'alert', sender='Notice')
            else:
                post_message(request, '{} is now known as {}'.format(nick, new_nick),
                    mode='nick',
                    sender='Notice'
                )

        elif mode == 'names':
            post_message(request, get_nicks(request).keys(), 'names')

        elif mode == 'msg':
            target = request.QUERY_DATA['target'][0]
            nicks = get_nicks(request)
            _, target_tag, _ = nicks[target].split(':')
            post_message(request, msg, 'msg', target=target,
                queue=make_key(target_tag, 'private')
            )
            post_message(request, msg, 'msg', target=target,
                queue=make_key(request.tag, 'private')
            )

        elif mode in ['message', 'action']:
            post_message(request, msg, mode)

        else:
            log.warning('Unknown message: %r', mode)

        response = Response()

    else:
        response = Response('', status=STATUS_METHOD_NOT_ALLOWED)

    return response

def static(request, filename):
    try:
        fin = open(os.path.join('static/', filename), 'rb')
        content_type, encoding = mimetypes.guess_type(filename)
        content_type = content_type or 'application/octet-stream'
        return Response(fin.read(), content_type=content_type)
    except:
        return Response(status=STATUS_NOT_FOUND)

urlpatterns = [
    (r'^/$', index, ),
    (r'^/static/(?P<filename>.*)$', static,),
    (r'^/(?P<channel>.+)/$', chat, ),
]

application = App(urlpatterns)
