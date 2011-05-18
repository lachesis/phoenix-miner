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
from zope.interface import implements
from twisted.web.iweb import IBodyProducer
from client3420 import Agent, ResponseDone
from _newclient3420 import ResponseFailed
from twisted.web.http import PotentialDataLoss
from twisted.web.http_headers import Headers
from twisted.internet import defer, reactor, protocol, error
from twisted.internet.protocol import Protocol
from twisted.python import failure

from ClientBase import ClientBase, AssignedWork

class StringBodyProducer(object):
    """Something Twisted itself needs..."""
    implements(IBodyProducer)
    
    def __init__(self, body):
        self.body = body
        self.length = len(self.body)
    def startProducing(self, consumer):
        consumer.write(self.body)
        return defer.succeed(None)
    def pauseProducing(self):
        pass
    def stopProducing(self):
        pass

class BodyLoader(Protocol):
    """Loads an HTTP body and fires it, as a string, through a Deferred."""    
    def __init__(self, d):
        self.d = d
        self.data = ''
    def dataReceived(self, bytes):
        self.data += bytes
    def connectionLost(self, reason):
        if not reason.check(ResponseDone, PotentialDataLoss):
            self.d.errback(failure.Failure(reason))
        else:
            self.d.callback(self.data)

class ServerMessage(Exception): pass
        
class RPCPoller(object):
    """Polls the root's chosen bitcoind or pool RPC server for work."""
    
    def __init__(self, root):
        self.root = root
        self.agent = Agent(reactor, persistent=True)
        self.askInterval = None
        self.askCall = None
        self.currentlyAsking = False
    
    def setInterval(self, interval):
        """Change the interval at which to poll the getwork() function."""
        self.askInterval = interval
        self._startCall()
    
    def _startCall(self):
        self._stopCall()
        #if self.currentlyAsking:
            #return # ask() will _startCall when it finishes
        if self.askInterval:
            self.askCall = reactor.callLater(self.askInterval, self.ask)
        else:
            self.askCall = None
    
    def _stopCall(self):
        if self.askCall:
            try:
                self.askCall.cancel()
            except (error.AlreadyCancelled, error.AlreadyCalled):
                pass
            self.askCall = None
    
    def ask(self):
        """Run a getwork request immediately."""
        
        if self.currentlyAsking:
            return
        self.currentlyAsking = True
        self._stopCall()
        
        d = self.call('getwork')
        
        def errback(failure):
            if not self.currentlyAsking:
                return
            self.currentlyAsking = False
            if failure.check(ServerMessage):
                self.root.runCallback('msg', failure.getErrorMessage())
            self.root._failure()
            self._startCall()
        def errback_delay(x): reactor.callLater(0, errback, x)
        d.addErrback(errback_delay)
        
        def callback(x):
            if not self.currentlyAsking:
                return
            self.currentlyAsking = False
            try:
                (headers, result) = x
            except TypeError:
                return
            self.root.handleWork(result)
            self.root.handleHeaders(headers)
            self._startCall()
        # Minor bug in the #3420 patch; you can't start new requests during
        # callbacks from old ones, so this function has the reactor call it a
        # little bit later (with no artificial delay)
        def callback_delay(x): reactor.callLater(0, callback, x)
        d.addCallback(callback_delay)
    
    @defer.inlineCallbacks
    def call(self, method, params=[]):
        """Call the specified remote function."""
        
        body = json.dumps({'method': method, 'params': params, 'id': 1})
        response = yield self.agent.request('POST',
            self.root.url,
            Headers({
                'Authorization': [self.root.auth],
                'User-Agent': [self.root.version],
                'Content-Type': ['application/json']
            }), StringBodyProducer(body))
        
        d = defer.Deferred()
        response.deliverBody(BodyLoader(d))
        data = yield d
        result = self.parse(data)

        defer.returnValue((response.headers, result))
    
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
        self.agent = Agent(reactor, persistent=True)
        self.polling = False
    
    def start(self):
        """Begin requesting data from the LP server, if we aren't already..."""
        if self.polling:
            return
        self.polling = True
        
        self._request()
        
    def _request(self):
        if self.polling:
            d = self.agent.request('GET', self.url,
                Headers({
                    'Authorization': [self.root.auth],
                    'User-Agent': [self.root.version]
                }))
            d.addBoth(self._requestComplete)
    
    def stop(self):
        """Stop polling. This LongPoller probably shouldn't be reused."""
        self.polling = False
    
    @defer.inlineCallbacks
    def _requestComplete(self, response):
        if not self.polling:
            return
        
        if isinstance(response, failure.Failure):
            self._request()
            return
        
        d = defer.Deferred()
        response.deliverBody(BodyLoader(d))
        try:
            data = yield d
        except ResponseFailed:
            self._request()
            return
        
        try:
            result = RPCPoller.parse(data)
        except ValueError:
            self._request()
            return
        
        self._request()
        self.root.handleWork(result, True)

class RPCClient(ClientBase):
    """The actual root of the whole RPC client system."""
    
    def __init__(self, handler, url):
        self.handler = handler
        self.url = '%s://%s:%d%s' % (url.scheme, url.hostname,
                                     url.port or 80, url.path)
        self.params = {}
        for param in url.params.split('&'):
            s = param.split('=',1)
            if len(s) == 2:
                self.params[s[0]] = s[1]
        self.auth = 'Basic ' + ('%s:%s' % (
            url.username, url.password)).encode('base64').strip()
        self.version = 'RPCClient/0.8'
    
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
            
        #we need to return the result, not the headers
        def callback(x):
            try:
                (headers, accepted) = x
            except TypeError:
                return False
            return accepted
        
        d.addErrback(errback)
        d.addCallback(callback)
        return d
    
    def useAskrate(self, variable):
        defaults = {'askrate': 999, 'retryrate': 15, 'lpaskrate': 0}
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
            urlParsed = urlparse.urlparse(self.url)
            lpURL = '%s://%s:%d%s' % (
                lpParsed.scheme or urlParsed.scheme,
                lpParsed.hostname or urlParsed.hostname,
                (lpParsed.port if lpParsed.hostname else urlParsed.port) or 80,
                lpParsed.path)
            if self.longPoller and self.longPoller.url != lpURL:
                self.longPoller.stop()
                self.longPoller = None
            if not self.longPoller:
                self.longPoller = LongPoller(lpURL, self)
                self.longPoller.start()
                self.useAskrate('lpaskrate')
                self.runCallback('longpoll', True)
        elif self.longPoller:
            self.longPoller.stop()
            self.longPoller = None
            self.useAskrate('askrate')
            self.runCallback('longpoll', False)
        
    def _failure(self):
        if self.saidConnected:
            self.saidConnected = False
            self.runCallback('disconnect')
        else:
            self.runCallback('failure')
        self.useAskrate('retryrate')
        if self.longPoller:
            self.longPoller.stop()
            self.longPoller = None
            self.runCallback('longpoll', False)