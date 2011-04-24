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

import time
import struct
import hashlib
import platform

from minerutil.MMPProtocol import MMPClient

from KernelInterface import KernelInterface

class Miner(object):
    """The main managing class for the miner itself."""
    
    # This gets updated automatically by SVN.
    REVISION = int('$Rev$'[6:-2])
    VERSION = 'r%s' % self.REVISION
    
    def __init__(self):
        self.logger = None
        self.options = None
        self.connection = None
        self.kernel = None
        self.queue = None
        
        self.lastMetaRate = 0.0
        self.lastRateUpdate = time.time()
    
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
        
        self.averageSamples = [0]*self.options.getAvgSamples()
        self.averageSamples = max(1, self.averageSamples)
        
        self.logger = self.options.makeLogger(self)
        self.connection = self.options.makeConnection(self)
        self.kernel = self.options.makeKernel(KernelInterface(self))
        self.queue = self.options.makeQueue(self)
        
        if isinstance(self.connection, MMPClient):
            self.logger.reportType('MMP')
        else:
            self.logger.reportType('RPC')
        
        self.applyMeta()
        
        # Go!
        self.connection.connect()
        self.kernel.start()
    
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
        
        # The kernel knows the rest.
        self.kernel.applyMeta(self.connection)
    
    def updateAverage(self, rate):
        """Average 'khash' more Khashes into our current sliding window
        average and report to the rest of the system.
        """
        
        self.averageSamples.append(rate)
        self.averageSamples.pop(0)
        average = sum(self.averageSamples)/len(self.averageSamples)
        
        self.logger.reportRate(average)
        
        # Let's not spam the server with rate messages.
        if self.lastMetaRate+30 < time.time():
            self.connection.setMeta('rate', average)
            self.lastMetaRate = time.time()