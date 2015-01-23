# mini_chat

A raw WSGI app to provide realtime web chat.

Features:
- Under 300 lines of code
- Under 40k page weight [including images and unminified CSS/JS]
- Rate limiting per IP
- Virtually unlimited channels
- No frameworks
- "moment.js" is the ONLY 3rd party JS lib [bundled]
- sanitised user input
- linkified links

## How to Install

### Dependencies

1. Python 3.3+
2. Redis 2.6+
3. Header files as needed for eventlet

A browser that isn't crap [yes, I'm looking at you IE.]

It uses and requires the following browser technologies:

- DME SSE / EventSource
- Flex Box layout

### Installation

    $ pip install gunicorn
    
    $ pip install -r requirements.txt

    $ gunicorn -k eventlet -b 0:8000 chat

## Usage

Point your browser to http://localhost:8000/#name where `name` is anything you like.  Different fragment names will affect different `channels`.

