
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

pool = redis.ConnectionPool()

# Rate limiting ?

RANDOM_CHARS = string.letters + string.digits
def random_string(source=RANDOM_CHARS, length=32):
    return ''.join([
        random.choice(source)
        for x in xrange(length)
    ])

def get_template(name):
    fn = os.path.join('templates/', name)
    with file(fn, 'rb') as fin:
        return fin.read()

##

def make_key(*args):
    return u':'.join(args)

##
## Nick handling
##

def get_nicks(request):
    keys = request.conn.keys(make_key(request.channel, '*', 'nick'))
    if not keys:
        return {}
    return {
        key: value
        for key, value in zip(keys, request.conn.mget(keys))
    }

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
    names = get_nicks(request).values()
    if name in names:
        raise ValueError('Nick in use!')
    key = make_key(request.channel, request.tag, 'nick')
    request.conn.set(key, name, ex=90)
    return name

##
## Message handling
##

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

def clean_message(msg):
    '''Clean up and process messages before sending'''
    return bleach.linkify(strip_tags(msg), callbacks=[linkify_external,])

##
## The application!
##

class Request(object):
    def __init__(self, environ):
        self.environ = environ
        self.method = environ['REQUEST_METHOD']

        cookies = environ.get('HTTP_COOKIE', '')
        if cookies == '':
            cookies = {}
        else:
            c = SimpleCookie()
            c.load(cookies)
            cookies = {
                key: c.get(key).value
                for key in c.keys()
            }
        self.cookies = cookies
        if self.method == 'POST':
            # Should test content type
            size = int(environ.get('CONTENT_LENGTH', 0))
            if size:
                self.QUERY_DATA = parse_qs(environ['wsgi.input'].read(size))
            else:
                self.QUERY_DATA = {}
        elif self.method == 'GET':
            self.QUERY_DATA = parse_qs(environ.get('QUERY_STRING', ''))

class Response(object):
    def __init__(self, content='', status=200, content_type='text/html'):
        self.content = content
        self.status = status
        self.headers = {}
        self.headers['Content-Type'] = content_type
        self.cookies = SimpleCookie()

    def add_cookie(self, key, value, **kwargs):
        self.cookies[key] = value

class Response404(Response):
    def __init__(self, content=''):
        super(Response404, self).__init__(content, 404)

STATUS = {
    200: '200 OK',
    404: '404 Not Found',
}

def application(environ, start_response):

    request = Request(environ)

    chatterbox_tag = request.cookies.get('chatterbox')
    if not chatterbox_tag:
        request.tag = random_string()
    else:
        request.tag = chatterbox_tag

    path = environ.get('PATH_INFO', '/')

    # Dispatch
    response = Response404()
    for pattern in urlpatterns:
        m = re.match(pattern[0], path)
        if m:
            response = pattern[1](request, **m.groupdict())

    if not chatterbox_tag:
        response.cookies['chatterbox'] = request.tag

    headers = list(response.headers.items()) + [
        ('Set-Cookie', cookie.OutputString())
        for cookie in response.cookies.values()
    ]

    start_response(STATUS[response.status], response.headers.items())
    return response.content

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
            post_message(request, get_nicks(request).values(), 'names')

        elif mode == 'msg':
            target = request.QUERY_DATA['target'][0]
            nicks = get_nicks(request)
            nick_map = { v: k for k, v in nicks.items() }
            msg = clean_message(msg)
            _, target_tag, _ = nick_map[target].split(':')
            post_message(request, msg, 'msg', target=target,
                queue=make_key(target_tag, 'private')
            )
            post_message(request, msg, 'msg', target=target,
                queue=make_key(request.tag, 'private')
            )

        elif mode in ['message', 'action']:
            post_message(request, clean_message(msg), mode)

        else:
            print('Unknown message: %r', mode)

        response = Response()

    else:
        response = Response('', status=405)

    return response

def static(request, filename):
    try:
        fin = open(os.path.join('static/', filename), 'rb')
        content_type, encoding = mimetypes.guess_type(filename)
        content_type = content_type or 'application/octet-stream'

        return Response(fin.read(), content_type=content_type)
    except:
        return Response404()

urlpatterns = [
    (r'^/$', index, ),
    (r'^/static/(?P<filename>.*)$', static,),
    (r'^/(?P<channel>.+)/$', chat, ),
]

