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
from twisted.web2 import stream
from twisted.internet import defer, reactor, protocol, error
from twisted.python import failure

from ClientBase import ClientBase, AssignedWork

class SimpleHTTPManager(http.EmptyHTTPClientManager):
    def __init__(self):
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
        """Client is idle; time to release the next item in the waitlist."""
        if client in self.busyClients:
            self.busyClients.remove(client)
        waitlist = self.waitDeferreds.get(client, [])
        if waitlist:
            waitlist.pop(0).callback(True)

    def clientBusy(self, client):
        """Remember busy clients so that we can defer."""
        if client not in self.busyClients:
            self.busyClients.append(client)
    
    def clientGone(self, client):
        """Disconnecting clients are cleaned up."""
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
        
        if not client or not yield self.waitForClient(client):
            c = creator.ClientCreator(reactor, http.HTTPClientProtocol, self)
            client = yield c.connectTCP(host, port)
            self.clients[key] = client
        
        request = http.ClientRequest('POST' if data else 'GET', url.path,
                                     headers, stream.MemoryStream(stream))
        
        response = yield client.submitRequest(request, False)
        
        d = defer.Deferred()
        stream.readStream(response.stream, d.callback).chainDeferred(d)
        data = yield d
        
        defer.returnValue((response.inHeaders, data))

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
        if self.askInterval is not None:
            self.askCall = reactor.callDelayed(self.askInterval, self.ask)
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
            if failure.trap(ServerMessage):
                self.root.runCallback('msg', failure.getErrorMessage())
            self.root._failure()
            self._startCall()
        d.addErrback(errback)
        
        def callback((headers, result)):
            self.root.handleWork(result)
            self.root.handleHeaders(headers)
            self._startCall()
        d.addCallback(callback)
    
    @defer.inlineCallbacks
    def call(self, method, params=[]):
        """Call the specified remote function."""
        
        headers, data = yield self.root.manager.request('http://%s:%d%s' %
            (self.root.url.hostname, self.root.url.port, self.root.url.path),
            {
                'Authorization': self.root.auth,
                'User-Agent': self.root.version,
                'Connection': 'Keep-Alive',
                'Content-Type': 'application/json',
            }, data=json.dumps({'method': method, 'params': params, 'id': 1}))
        
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
        
        d = self.root.manager.request('http://%s:%d%s' %
            (self.url.hostname, self.url.port, self.url.path),
            {
                'Authorization': self.root.auth,
                'User-Agent': self.root.version,
                'Connection': 'Keep-Alive'
            }, channel=self)
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
            result = RPCRequester.parse(data)
        except ValueError:
            return
        
        self.root.handleWork(result)