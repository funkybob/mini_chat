import asyncio
from functools import partial
from http.cookies import SimpleCookie
import json
import mimetypes
import os.path
import random
import re
import string
import time
from urllib.parse import parse_qs

import asyncio_redis
import bleach

import logging
#logging.basicConfig(level=logging.DEBUG)


RATE_LIMIT_DURATION = 60
RATE_LIMIT = 100

STATUS_OK = '200 OK'
STATUS_NOT_FOUND = '404 Not Found'
STATUS_METHOD_NOT_ALLOWED = '405 Method not allowed'
STATUS_RATE_LIMITED = '429 Too Many Requests'


def get_template(name):
    return open(os.path.join('templates/', name), 'rb')

make_key = lambda *args: ':'.join(args)


# Nick handling
def get_nicks(request):
    keys = yield from request.conn.keys(make_key(request.channel, '*', 'nick'))
    if keys:
        values = yield from request.conn.mget(keys)
        yield dict(zip(values, keys))
    yield {}


def get_nick(request):
    key = make_key(request.channel, request.tag, 'nick')
    nick = yield from request.conn.get(key)
    if nick is None:
        nick = yield from set_nick(request, request.tag[:8])
    else:
        nick = nick
        _ = yield from request.conn.expire(key, 90)
    yield nick


def set_nick(request, name):
    name = strip_tags(name)
    nicks = yield from get_nicks(request)
    if name in nicks:
        raise ValueError('Nick in use!')
    key = make_key(request.channel, request.tag, 'nick')
    _ = yield from request.conn.set(key, name, ex=90)
    yield name


# Message handling
def post_message(request, message, mode='message', queue=None, **data):
    if queue is None:
        queue = make_key(request.channel, 'channel')

    data.setdefault('message', message)
    data.setdefault('sender', get_nick(request))

    content = json.dumps(data)
    next(request.conn.publish(queue, json.dumps([mode, content])))

strip_tags = partial(bleach.clean, tags=[], strip=True)


def linkify_external(attrs, new=False):
    attrs['target'] = '_blank'
    return attrs


# The application!
class Request(object):
    def __init__(self, environ, conn):
        self.environ = environ

        self.method = environ['REQUEST_METHOD']
        self.path = environ.get('PATH_INFO', '/')

        self.cookies = self.parse_cookies()
        self.query_data = self.parse_query_data()

        self.conn = conn

    def parse_cookies(self):
        cookie_data = self.environ.get('HTTP_COOKIE', '')
        cookies = SimpleCookie()
        if cookie_data:
            cookies.load(cookie_data)
        return {key: cookies.get(key).value for key in cookies.keys()}

    def parse_query_data(self):
        if self.method == 'POST':
            size = int(self.environ.get('CONTENT_LENGTH', 0))
            if not size:
                return {}
            src = self.environ['wsgi.input'].read(size)
        else:
            src = self.environ.get('QUERY_STRING', '')
        return {
            k.decode('utf-8'): [x.decode('utf-8') for x in v]
            for k, v in parse_qs(src).items()
        }


class Response(object):
    def __init__(self, content=None, status=STATUS_OK, content_type=None):
        self.content = '' if content is None else content
        self.status = status
        self.headers = {'Content-Type': content_type or 'text/html'}
        self.cookies = SimpleCookie()


def application(environ, start_response):
    conn = yield from asyncio_redis.Pool.create(host='localhost', port=6379, poolsize=10)
    request = Request(environ, conn)
    # Session cookie
    tag = request.cookies.get('chatterbox')
    if not tag:
        request.tag = ''.join(
            random.choice(string.ascii_letters + string.digits)
            for x in range(16))
    else:
        request.tag = tag
    # Rate limiting
    key = make_key(request.tag, 'rated')

    now = int(time.time())
    next(conn.zadd(key, {str(now): float(now)}))
    next(conn.expireat(key, now + RATE_LIMIT_DURATION))
    next(conn.zremrangebyscore(key, asyncio_redis.ZScoreBoundary.MAX_VALUE, asyncio_redis.ZScoreBoundary(float(now - RATE_LIMIT_DURATION))))
    size = yield from conn.zcard(key)
    if size > RATE_LIMIT:
        response = Response(status=STATUS_RATE_LIMITED)
    else:
        # Dispatch
        response = Response(status=STATUS_NOT_FOUND)
        for pattern in URLPATTERNS:
            match = re.match(pattern[0], request.path)
            if match:
                response = pattern[1](request, **match.groupdict())

    if not tag:
        response.cookies['chatterbox'] = request.tag
        response.cookies['chatterbox']['path'] = b'/'

    headers = list(response.headers.items()) + [
        ('Set-Cookie', cookie.OutputString())
        for cookie in response.cookies.values()
    ]

    start_response(response.status, headers)
    return response.content


def index(request):
    return Response(get_template('chat.html'))


def chat(request, channel=None):
    request.channel = channel

    if request.method == 'GET':
        if 'text/event-stream' not in request.environ['HTTP_ACCEPT']:
            return Response(get_template('chat.html'))

        def _iterator():
            nick = yield from get_nick(request)
            _ = yield from post_message(request, '{} connected.'.format(nick), 'join',
                         sender='Notice')

            pubsub = yield from request.conn.start_subscribe()
            _ = yield from pubsub.subscribe([
                make_key(request.channel, 'channel'),
                make_key(request.tag, 'private'),
            ])

            while True:
                msg = yield from pubsub.next_published()
                print(msg)
                if msg['type'] == 'message':
                    mode, data = json.loads(msg['data'].decode('utf-8'))
                    yield 'event: {}\n'.format(mode).encode('utf-8')
                    for line in data.splitlines():
                        yield 'data: {}\n'.format(line).encode('utf-8')
                    yield '\n'.encode('utf-8')

        response = Response(_iterator(), content_type='text/event-stream')
        response.headers['Cache-Control'] = 'no-cache'

    elif request.method == 'POST':

        def _generator():
            nick = yield from get_nick(request) # Triggers timeout update

            mode = request.query_data.get('mode', ['message'])[0]
            msg = request.query_data.get('message', [''])[0]
            msg = bleach.linkify(strip_tags(msg), callbacks=[linkify_external])

            if mode == 'nick' and msg:
                try:
                    new_nick = yield from set_nick(request, msg)
                except ValueError:
                    _ = yield from post_message(request, 'Nick in use!', 'alert', sender='Notice')
                else:
                    _ = yield from post_message(request,
                                 '{} is now known as {}'.format(nick, new_nick),
                                 mode='nick',
                                 sender='Notice')

            elif mode == 'names':
                nicks = yield from get_nicks(request)
                _ = yield from post_message(request, list(nicks.keys()), 'names')

            elif mode == 'msg':
                target = request.query_data['target'][0]
                nicks = yield from get_nicks(request)
                _, target_tag, _ = nicks[target].split(':')
                _ = yield from post_message(request, msg, 'msg', target=target,
                             queue=make_key(target_tag, 'private'))
                _ = yield from post_message(request, msg, 'msg', target=target,
                             queue=make_key(request.tag, 'private'))

            elif mode in ['message', 'action']:
                _ = yield from post_message(request, msg, mode)

            else:
                logging.warning('Unknown message: %r', mode)

            yield b''

        response = Response(_generator())

    else:
        response = Response(status=STATUS_METHOD_NOT_ALLOWED)

    return response


def static(request, filename):
    root = os.path.dirname(__file__)
    try:
        fin = open(os.path.join(root, 'static/', filename), 'rb')
    except:
        return Response(status=STATUS_NOT_FOUND)
    else:
        content_type, encoding = mimetypes.guess_type(filename)
        content_type = content_type or 'application/octet-stream'
        def _reader(f):
            yield f.read()
            f.close()
        return Response(_reader(fin), content_type=content_type)

URLPATTERNS = [
    (r'^/$', index, ),
    (r'^/static/(?P<filename>.*)$', static,),
    (r'^/(?P<channel>.+)/$', chat, ),
]
