# mini_chat

A raw WSGI app to provide realtime web chat.

Features:
- Under 265 lines of code
- Under 14k page weight [including images and unminified CSS/JS]
- Rate limiting per IP
- Virtually unlimited channels
- No frameworks
- sanitised user input
- linkified links

## How to Install

### Dependencies

1. Python 3.3+
2. Redis 2.6+

A browser that isn't crap [yes, I'm looking at you IE.]

It uses and requires the following browser technologies:

- DOM SSE / EventSource
- Flex Box layout

### Installation

    $ pip install -r requirements.txt

### Launch

    $ gunicorn -k eventlet -b 0:8000 chat

Add ``-D`` to daemonise.

## Usage

Point your browser to http://localhost:8000/#name where `name` is anything you like.  Different fragment names will affect different `channels`.
