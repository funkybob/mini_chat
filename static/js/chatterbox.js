// Standard django CSRF handling
function getCookie(name) {
    var i, cookie, cookies, cookieValue = null;
    if (document.cookie && document.cookie != '') {
        cookies = document.cookie.split(';');
        for (i = 0; i < cookies.length; i++) {
            cookie = cookies[i].trim();
            // Does this cookie string begin with the name we want?
            if (cookie.substring(0, name.length + 1) == (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}
var csrftoken = getCookie('csrftoken');

// Simple template renderer
String.prototype.render = function (data) {
    return this.replace(/\{(\w+)\}/g, function (match, key) { return data[key]; });
};

// EventSource malarky
var ChatterBox = (function () {
    var modemap = {}, input, messages, nicks, source, url;

    var template = {
        message : '<div class="message {mode}"><time>{when}</time><span>{sender}</span><p>{message}</p></div>',
        action  : '<div class="message action"><time>{when}</time><p><i>{sender}</i> {message}</p></div>',
        join    : '<div class="message join"><time>{when}</time><p><i>{message}</i></p></div>',
        nick    : '<div class="message nick"><time>{when}</time><p><i>{message}</i></p></div>',
        msg     : '<div class="message msg"><time>{when}</time><span><i>{sender}</i> &rArr; <i>{target}</i></span><p><em>{message}</em></p></div>'
    };

    // Send a message to server
    function send(message, mode, extra) {
        var xhr = new window.XMLHttpRequest();

        extra = extra || {};
        extra.message = message;
        extra.mode = mode;

        // Convert 'extra' to x-www-form-urlencoded
        data = Object.keys(extra).map(function (key) {
            return encodeURIComponent(key) + '=' + encodeURIComponent(extra[key]);
        });

        xhr.open('POST', url);
        xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
        xhr.setRequestHeader("X-CSRFToken", csrftoken);
        xhr.send(data.join('&'));
    };

    // Print message to screen
    function append_message(data, tmpl) {
        data.mode = tmpl;
        data.when = data.when || moment().format('H:mm:ss');
        tmpl = template[tmpl] || template['message'];
        messages.innerHTML += tmpl.render(data);
        messages.scrollTop = 9999999;
        Array.prototype.slice.call(
            messages.querySelectorAll('.message'), 0, -1000
        ).map(function (el) { messages.removeChild(el); });
    };

    // Parse event data and render message
    function parse_message(event, tmpl) {
        var data = JSON.parse(event.data);
        append_message(data, tmpl || 'message');
    }

    function setStatus(state) {
        input.classList.remove(input.classList[0]);
        input.classList.add(state);
    };

    modemap['open'] = function (event) {
        setStatus('ready');
        send('', 'names');
        send('', 'topic');
    };

    modemap['error'] = function (event) {
        if(event.readyState == EventSource.CLOSED) {
            setStatus('disconnected');
            connect();
        } else {
            setStatus('error');
        }
    };

    modemap['action'] = function (event) { parse_message(event, 'action'); };

    modemap['message'] = function (event) { parse_message(event, 'message'); };

    modemap['note'] = function (event) { parse_message(event, 'note'); };

    modemap['join'] = function (event) { send('', 'names'); parse_message(event, 'join'); };

    modemap['nick'] = function (event) { send('', 'names'); parse_message(event, 'nick'); };

    modemap['msg'] = function (event) { parse_message(event, 'msg'); };

    modemap['topic'] = function (event) {
        var data = JSON.parse(event.data);
        document.querySelector('h1 span').innerHTML = data.message;
    };

    modemap['names'] = function (event) {
        var data = JSON.parse(event.data);
        var content = [];
        for(var i=0, j=data.message.length; i < j ; i++ ) {
            content.push('<li>' + data.message[i] + '</li>');
        }
        nicks.innerHTML = content.join('\n');
    };

    function connect () {
        setStatus('connecting');
        source = new EventSource(url);
        Object.keys(modemap).forEach(function (key) {
            source.addEventListener(key, modemap[key], false);
        });
    };

    function keypress(e) {
        var extra = {};
        if( e.keyCode == 9 ) {
            // Nick complete
            // parse back for the last space to now.
            var match = /(\w+)$/i.exec(input.value);
            if(match) {
                var pfx = match[1], pattern = RegExp('^' + match[1], 'i');
                // Now find if it matches a known nick
                var nl = nicks.querySelectorAll('li');
                for(var i=0; i < nl.length; i++) {
                    if(pattern.test(nl[i].innerHTML)) {
                        input.value = input.value.slice(0, -pfx.length) + nl[i].innerHTML;
                        input.value += (input.value.length == nl[i].innerHTML.length) ? ': ' : ' ';
                        break;
                    }
                }
            }
            e.preventDefault();
            return false;
        }
        if( e.keyCode != 13 ) return;
        var msg = input.value;
        if(msg.length == 0) return;
        var mode = 'message'
        var match = /^\/(\w+)\s?(.+)/g.exec(msg);
        if(match) {
            switch(match[1]) {
            case 'nick':
                var match = /^(\w+)/.exec(match[2]);
                mode = 'nick';
                msg = match[1];
                break;
            case 'me':
                mode = 'action';
                msg = match[2];
                break;
            case 'names':
                mode = 'names';
                msg = '';
                break;
            case 'msg':
                var match = /([-\w]+)\s+(.+)/.exec(match[2]);
                mode = 'msg';
                extra.target = match[1];
                msg = match[2];
                break;
            case 'topic':
                mode = 'topic';
                msg = match[2] || '';
                break;
            default:
                break;
            }
        }
        send(msg, mode, extra);
        clear();
    };

    function clear () {
        input.value = '';
        input.focus();
    };

    function init (root_url) {
        url = root_url;
        input = document.querySelector('#input input');
        messages = document.querySelector('#messages');
        nicks = document.querySelector('#nicks ul');
        // Attach input handling, and connect
        input.addEventListener('keydown', keypress);
        clear();
        connect();
        window.setInterval(ChatterBox.send, 30000, '', 'names');
    };

    return {
        init: init,
        send: send,
    };
})();

document.addEventListener('DOMContentLoaded', function () { ChatterBox.init(document.location.hash.replace('#', '') + '/'); }, false);
