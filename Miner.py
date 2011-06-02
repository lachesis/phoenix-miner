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

from time import time
import platform

from twisted.internet import reactor

from minerutil.MMPProtocol import MMPClient
from KernelInterface import KernelInterface

class Miner(object):
    """The main managing class for the miner itself."""
    
    # This gets updated automatically by SVN.
    REVISION = int('$Rev$'[6:-2])
    VERSION = 'r%s' % REVISION
    
    def __init__(self):
        self.logger = None
        self.options = None
        self.connection = None
        self.kernel = None
        self.queue = None
        self.idle = True
        
        self.cores = []
        self.lastMetaRate = 0.0
        self.lastRateUpdate = time()
    
    # Connection callbacks...
    def onFailure(self):
        self.logger.reportConnectionFailed()
    def onConnect(self):
        self.logger.reportConnected(True)
    def onDisconnect(self):
        self.logger.reportConnected(False)
    def onBlock(self, block):
        self.logger.reportBlock(block)
    def onMsg(self, msg):
        self.logger.reportMsg(msg)
    def onWork(self, work):
        self.logger.reportDebug('Server gave new work; passing to WorkQueue')
        self.queue.storeWork(work)
    def onLongpoll(self, lp):
        self.logger.reportType('RPC' + (' (+LP)' if lp else ''))
    def onPush(self, ignored):
        self.logger.log('LP: New work pushed')

    def start(self, options):
        """Configures the Miner via the options specified and begins mining."""
        
        self.options = options
        
        self.logger = self.options.makeLogger(self, self)
        self.connection = self.options.makeConnection(self)
        self.kernel = self.options.makeKernel(KernelInterface(self))
        self.queue = self.options.makeQueue(self)
        
        #log a message to let the user know that phoenix is starting
        self.logger.log("Phoenix %s starting..." % self.VERSION)
        
        #this will need to be changed to add new protocols
        if isinstance(self.connection, MMPClient):
            self.logger.reportType('MMP')
        else:
            self.logger.reportType('RPC')
        
        self.applyMeta()
        
        # Go!
        self.connection.connect()
        self.kernel.start()
        reactor.addSystemEventTrigger('before', 'shutdown', self.kernel.stop)
    
    def applyMeta(self):
        """Applies any static metafields to the connection, such as version,
        kernel, hardware, etc.
        """
        
        # It's important to note here that the name is already put in place by
        # the Options's makeConnection function, since the Options knows the
        # user's desired name for this miner anyway.
        
        self.connection.setVersion(
            'phoenix', 'Phoenix Miner', self.VERSION)
        system = platform.system() + ' ' + platform.version()
        self.connection.setMeta('os', system)
    
    def _addCore(self, core):
        """Temporary function. Practically already deprecated."""
        self.cores.append(core)
    
    #used by WorkQueue to report when the miner is idle
    def reportIdle(self, idle):

        #if idle status has changed force an update
        if self.idle != idle:
            if idle:
                self.logger.reportRate(0, True)
                self.connection.setMeta('rate', 0)
                self.lastMetaRate = time()
                self.idleFixer()
            else:
                self.logger.updateStatus(True)
        
        self.idle = idle

    #since i can't find the actual cause of the ide bug im going to add a simple 
    #workaround that spams work requests every 20 seconds while idle.
    def idleFixer(self):
        if self.idle:
            self.connection.requestWork()
            reactor.callLater(20, self.idleFixer)
    
    def updateAverage(self):
        #Query all mining cores for their Khash/sec rate and sum.
        
        total = 0
        if not self.idle:
            for core in self.cores:
                total += core.getRate()
        
        self.logger.reportRate(total)
        
        # Let's not spam the server with rate messages.
        if self.lastMetaRate+30 < time():
            self.connection.setMeta('rate', total)
            self.lastMetaRate = time()