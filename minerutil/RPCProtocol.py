# Copyright (C) 2011 by jedi95 <jedi95@gmail.com> and 
#                       CFSworks <CFSworks@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import urlparse
import json
from twisted.web2.client import http
from twisted.web2 import stream, http_headers
from twisted.internet import defer, reactor, protocol, error
from twisted.python import failure

from ClientBase import ClientBase, AssignedWork

class SimpleHTTPManager(http.EmptyHTTPClientManager):
    def __init__(self, keepalive=3600):
        self.keepalive = keepalive
        self.timeoutTimers = {}
        self.busyClients = []
        self.clients = {}
        self.waitDeferreds = {}
    
    def waitForClient(self, client):
        """Returns a deferred that waits for the client to become available."""
        if client not in self.busyClients:
            return defer.succeed(True)
        d = defer.Deferred()
        waitlist = self.waitDeferreds.setdefault(client, [])
        waitlist.append(d)
    
    def clientIdle(self, client):
        self._startTimeoutTimer(client)
        """Client is idle; time to release the next item in the waitlist."""
        if client in self.busyClients:
            self.busyClients.remove(client)
        waitlist = self.waitDeferreds.get(client, [])
        if waitlist:
            waitlist.pop(0).callback(True)

    def clientBusy(self, client):
        """Remember busy clients so that we can defer."""
        self._stopTimeoutTimer(client)
        if client not in self.busyClients:
            self.busyClients.append(client)
    
    def clientGone(self, client):
        """Disconnecting clients are cleaned up."""
        self._stopTimeoutTimer(client)
        
        keys = []
        for k,v in self.clients.items():
            if v == client:
                keys.append(k)
        for k in keys:
            del self.clients[k]
        
        # All waiting deferreds need to be told that the client is no longer
        # available.
        if client in self.waitDeferreds:
            for deferred in self.waitDeferreds[client]:
                deferred.callback(False)
            del self.waitDeferreds[client]
    
    def _startTimeoutTimer(self, client):
        self._stopTimeoutTimer(client)
        self.timeoutTimers[client] = reactor.callLater(self.keepalive,
            client.transport.loseConnection)
    
    def _stopTimeoutTimer(self, client):
        if client in self.timeoutTimers:
            try:
                self.timeoutTimers[client].cancel()
            except (error.AlreadyCancelled, error.AlreadyCalled):
                pass
            del self.timeoutTimers[client]
        
    
    @defer.inlineCallbacks
    def request(self, url, headers={}, data=None, channel=None):
        """Request URL with headers. If present data is POSTed
        (GETed otherwise) and the unique connection is identified with channel.
        """
        
        url = urlparse.urlparse(url)
        
        host = url.hostname
        port = url.port or 80
        
        key = (host, port, channel)
        client = None
        if key in self.clients:
            client = self.clients[key]
        
        if not client or not (yield self.waitForClient(client)):
            c = protocol.ClientCreator(reactor, http.HTTPClientProtocol, self)
            client = yield c.connectTCP(host, port)
            self.clients[key] = client
        
        outStream = stream.MemoryStream(data) if data else None
        request = http.ClientRequest('POST' if data else 'GET', url.path,
                                     headers, outStream)
        
        response = yield client.submitRequest(request, False)
        
        d = defer.Deferred()
        yield stream.readStream(response.stream, d.callback)
        data = yield d
        
        defer.returnValue((response.headers, data))

class ServerMessage(Exception): pass
        
class RPCPoller(object):
    """Polls the root's chosen bitcoind or pool RPC server for work."""
    
    def __init__(self, root):
        self.root = root
        self.askInterval = None
        self.askCall = None
    
    def setInterval(self, interval):
        """Change the interval at which to poll the getwork() function."""
        self.askInterval = interval
        
        if self.askCall:
            try:
                self.askCall.cancel()
            except (error.AlreadyCancelled, error.AlreadyCalled):
                return
        
        self._startCall()
    
    def _startCall(self):
        if self.askInterval:
            self.askCall = reactor.callLater(self.askInterval, self.ask)
        else:
            self.askCall = None
    
    def ask(self):
        """Run a getwork request immediately."""
        
        if self.askCall:
            try:
                self.askCall.cancel()
            except error.AlreadyCancelled:
                return
            except error.AlreadyCalled:
                pass
        
        d = self.call('getwork')
        
        def errback(failure):
            if failure.check(ServerMessage):
                self.root.runCallback('msg', failure.getErrorMessage())
            self.root._failure()
            self._startCall()
        d.addErrback(errback)
        
        def callback(x):
            try:
                (headers, result) = x
            except TypeError:
                return
            self.root.handleWork(result)
            self.root.handleHeaders(headers)
            self._startCall()
        d.addCallback(callback)
    
    @defer.inlineCallbacks
    def call(self, method, params=[]):
        """Call the specified remote function."""
        
        headers, data = yield self.root.manager.request('http://%s:%d%s' %
            (self.root.url.hostname, self.root.url.port, self.root.url.path),
            http.Headers({
                'Host': '%s:%d' % (self.root.url.hostname, self.root.url.port),
                'Authorization': [self.root.auth],
                'User-Agent': self.root.version,
                'Content-Type': http_headers.MimeType('application', 'json')
            }), data=json.dumps({'method': method, 'params': params, 'id': 1}))
        
        result = self.parse(data)
        
        defer.returnValue((headers, result))
    
    @classmethod
    def parse(cls, data):
        """Attempt to load JSON-RPC data."""
        
        response = json.loads(data)
        try:
            message = response['error']['message']
        except (KeyError, TypeError):
            pass
        else:
            raise ServerMessage(message)
        
        return response.get('result')
    
class LongPoller(object):
    """Polls a long poll URL, reporting any parsed work results to the
    callback function.
    """
    
    def __init__(self, url, root):
        self.url = url
        self.root = root
        self.polling = False
    
    def start(self):
        """Begin requesting data from the LP server, if we aren't already..."""
        if self.polling:
            return
        self.polling = True
        
        d = self.root.manager.request(self.url.geturl(),
            http.Headers({
                'Host': '%s:%d' % (self.root.url.hostname, self.root.url.port),
                'Authorization': [self.root.auth],
                'User-Agent': self.root.version
            }), channel=self)
        d.addBoth(self._requestComplete)
    
    def stop(self):
        """Stop polling. This LongPoller probably shouldn't be reused."""
        self.polling = False
    
    def _requestComplete(self, response):
        if not self.polling:
            return
        # Begin anew...
        self.polling = False
        self.start()
        
        if isinstance(response, failure.Failure):
            return
        
        headers, data = response
        try:
            result = RPCPoller.parse(data)
        except ValueError:
            return
        
        self.root.handleWork(result, True)

class RPCClient(ClientBase):
    """The actual root of the whole RPC client system."""
    
    def __init__(self, handler, url):
        self.handler = handler
        self.url = url
        self.params = {}
        for param in self.url.params.split('&'):
            s = param.split('=',1)
            if len(s) == 2:
                self.params[s[0]] = s[1]
        self.auth = 'Basic ' + ('%s:%s' % (
            self.url.username, self.url.password)).encode('base64').strip()
        self.version = 'RPCClient/0.8'
    
        try:
            keepalive = int(self.params['keepalive'])
        except (KeyError, ValueError):
            keepalive = 60
    
        self.manager = SimpleHTTPManager(keepalive)
        self.poller = RPCPoller(self)
        self.longPoller = None # Gets created later...
        
        self.saidConnected = False
        self.block = None
    
    def connect(self):
        """Begin communicating with the server..."""
        
        self.poller.ask()
    
    def disconnect(self):
        """Cease server communications immediately. The client might be
        reusable, but it's probably best not to try.
        """
        
        self._deactivateCallbacks()
        
        self.poller.setInterval(None)
        if self.longPoller:
            self.longPoller.stop()
            self.longPoller = None
    
    def setMeta(self, var, value):
        """RPC clients do not support meta. Ignore."""

    def setVersion(self, shortname, longname=None, version=None, author=None):
        if version is not None:
            self.version = '%s/%s' % (shortname, version)
        else:
            self.version = shortname
    
    def requestWork(self):
        """Application needs work right now. Ask immediately."""
        self.poller.ask()
    
    def sendResult(self, result):
        """Sends a result to the server, returning a Deferred that fires with
        a bool to indicate whether or not the work was accepted.
        """
        
        # Must be a 128-byte response, but the last 48 are typically ignored.
        result += '\x00'*48
        
        d = self.poller.call('getwork', [result.encode('hex')])
        
        def errback(*ignored):
            return False # ANY error while turning in work is a Bad Thing(TM).
        d.addErrback(errback)
        
        return d
    
    def useAskrate(self, variable):
        defaults = {'askrate': 10, 'retryrate': 15, 'lpaskrate': 0}
        try:
            askrate = int(self.params[variable])
        except (KeyError, ValueError):
            askrate = defaults.get(variable, 10)
        self.poller.setInterval(askrate)
    
    def handleWork(self, work, pushed=False):
        if not self.saidConnected:
            self.saidConnected = True
            self.runCallback('connect')
            self.useAskrate('askrate')
        
        if 'block' in work:
            try:
                block = int(work['block'])
            except (TypeError, ValueError):
                pass
            else:
                if self.block != block:
                    self.block = block
                    self.runCallback('block', block)
        
        aw = AssignedWork()
        aw.data = work['data'].decode('hex')[:80]
        aw.target = work['target'].decode('hex')
        aw.mask = work.get('mask', 32)
        if pushed:
            self.runCallback('push', aw)
        self.runCallback('work', aw)
    
    def handleHeaders(self, headers):
        blocknum = headers.getRawHeaders('X-Blocknum') or ['']
        try:
            block = int(blocknum[0])
        except ValueError:
            pass
        else:
            if self.block != block:
                self.block = block
                self.runCallback('block', block)
        
        longpoll = headers.getRawHeaders('X-Long-Polling')
        if longpoll:
            lpParsed = urlparse.urlparse(longpoll[0])
            lpURL = urlparse.ParseResult(self.url.scheme,
                lpParsed.netloc or self.url.netloc, lpParsed.path, '', '', '')
            if self.longPoller and self.longPoller.url != lpURL:
                self.longPoller.stop()
                self.longPoller = None
            else:
                self.runCallback('longpoll', True)
            if not self.longPoller:
                self.longPoller = LongPoller(lpURL, self)
                self.longPoller.start()
                self.useAskrate('lpaskrate')
        elif self.longPoller:
            self.runCallback('longpoll', False)
            self.longPoller.stop()
            self.longPoller = None
            self.useAskrate('askrate')
        
    def _failure(self):
        if self.saidConnected:
            self.saidConnected = False
            self.runCallback('disconnect')
        else:
            self.runCallback('failure')
        self.useAskrate('retryrate')
        if self.longPoller:
            self.runCallback('longpoll', False)
            self.longPoller.stop()
            self.longPoller = None