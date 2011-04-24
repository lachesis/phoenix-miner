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

import hashlib
import struct
from twisted.internet import defer

# I'm using this as a sentinel value to indicate that an option has no default;
# it must be specified.
REQUIRED = object()

class KernelOption(object):
    """This works like a property, and is used in defining easy option tables
    for kernels.
    """
    
    def __init__(self, name, type, help=None, default=REQUIRED,
        advanced=False):
        
        self.localValues = {}
        
        self.name = name
        self.type = type
        self.help = help
        self.default = default
        self.advanced = advanced
    
    def __get__(self, instance, owner):
        if instance in self.localValues:
            return self.localValues[instance]
        else:
            return instance.interface._getOption(
                self.name, self.type, self.default)
    
    def __set__(self, instance, value):
        self.localValues[instance] = value

class KernelInterface(object):
    """This is an object passed to kernels as an API back to the Phoenix
    framework.
    """
    
    def __init__(self, miner):
        self.miner = miner
        
        self.workFactor = 1
        
    def _getOption(self, name, type, default):
        """KernelOption uses this to read the actual value of the option."""
        if not name in self.miner.options.kernelOptions:
            if default == REQUIRED:
                self.fatal('Required option %s not provided!' % name)
            else:
                return default
        
        givenOption = self.miner.options.kernelOptions[name]
        if type == bool:
            # The following are considered true
            return givenOption is None or \
                givenOption.lower() in ('t', 'true', 'on', '1', 'y', 'yes')
        
        try:
            return type(givenOption)
        except (TypeError, ValueError):
            self.fatal('Option %s expects a value of type %s!' % (name, type))
    
    def getRevision(self):
        """Return the Phoenix core revision, so that kernels can require a
        minimum revision before operating (such as if they rely on a certain
        feature added in a certain revision)
        """
        
        return self.miner.REVISION
    
    def setWorkFactor(self, workFactor):
        """Specify the multiple by which all ranges retrieved through
        fetchRange must be divisible.
        """
        
        self.workFactor = workFactor
    
    def setMeta(self, var, value):
        """Set metadata for this kernel."""
        
        self.miner.connection.setMeta(var, value)
    
    def fetchRange(self, size=None):
        """Fetch a range from the WorkQueue, optionally specifying a size
        (in nonces) to include in the range.
        """
        
        if size is None:
            return self.miner.queue.fetchRange(workFactor=self.workFactor)
        else:
            return self.miner.queue.fetchRange(size, self.workFactor)
    
    def updateRate(self, rate):
        """Used by kernels to declare their hashrate."""
        
        self.miner.updateAverage(rate)
    
    def checkTarget(self, hash, target):
        """Utility function that the kernel can use to see if a nonce meets a
        target before sending it back to the core.
        
        Since the target is checked before submission anyway, this is mostly
        intended to be used in hardware sanity-checks.
        """
        
        for t,h in zip(target[::-1], hash[::-1]):
            if ord(t) > ord(h):
                return True
            elif ord(t) < ord(h):
                return False
        return True
 
    def calculateHash(self, range, nonce):
        staticDataUnpacked = struct.unpack('<' + 'I'*19, range.unit.data[:76])
        staticData = struct.pack('>' + 'I'*19, *staticDataUnpacked)
        hashInput = struct.pack('>76sI', staticData, nonce)
        return hashlib.sha256(hashlib.sha256(hashInput).digest()).digest()
    
    def foundNonce(self, range, nonce):
        """Called by kernels when they may have found a nonce."""
        
        hash = self.calculateHash(range, nonce)
        
        if self.checkTarget(hash, range.unit.target):
            formattedResult = struct.pack('<76sI', range.unit.data[:76], nonce)
            d = self.miner.connection.sendResult(formattedResult)
            def callback(accepted):
                self.miner.logger.reportFound(hash, accepted)
            d.addCallback(callback)
            return True
        else:
            return False
    
    def debug(self, msg):
        """Log information as debug so that it can be viewed only when -v is
        enabled.
        """
        self.miner.logger.reportDebug(msg)
    
    def log(self, msg, withTimestamp=True, withIdentifier=True):
        """Log some general kernel information to the console."""
        self.miner.logger.say(msg, True, not withTimestamp)
    
    def error(self, msg=None):
        """The kernel has an issue that requires user attention."""
        if msg is not None:
            self.miner.logger.log('Kernel error: ' + msg)
    
    def fatal(self, msg=None):
        """The kernel has an issue that is preventing it from continuing to
        operate.
        """
        if msg is not None:
            self.miner.logger.log('FATAL kernel error: ' + msg)
        raise SystemExit()
    