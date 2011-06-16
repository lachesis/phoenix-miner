#!/usr/bin/python

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

import imp
from sys import exit
from twisted.internet import reactor
from optparse import OptionParser

import minerutil
from ConsoleLogger import ConsoleLogger
from WorkQueue import WorkQueue
from Miner import Miner

class CommandLineOptions(object):
    """Implements the Options interface for user-specified command-line
    arguments.
    """
    
    def __init__(self):
        self.parsedSettings = None
        self.url = None
        
        self.logger = None
        self.connection = None
        self.kernel = None
        self.queue = None
        
        self.kernelOptions = {}
        
        self._parse()
    
    def _parse(self):
        parser = OptionParser(usage="%prog -u URL [-k kernel] [kernel params]")
        parser.add_option("-v", "--verbose", action="store_true",
            dest="verbose", default=False, help="show debug messages")
        parser.add_option("--statusfile", dest="statusfile", help="write to a status file")
        parser.add_option('--blkfound', dest='blkfound', help='command to run when a block is found')
        parser.add_option("-k", "--kernel", dest="kernel", default="poclbm",
            help="the name of the kernel to use")
        parser.add_option("-u", "--url", dest="url", default=None,
            help="the URL of the mining server to work for [REQUIRED]")
        parser.add_option("-q", "--queuesize", dest="queuesize", type="int",
            default=1, help="how many work units to keep queued at all times")
        parser.add_option("-a", "--avgsamples", dest="avgsamples", type="int",
            default=10,
            help="how many samples to use for hashrate average")
        
        self.parsedSettings, args = parser.parse_args()
        
        if self.parsedSettings.url is None:
            parser.print_usage()
            exit()
        else:
            self.url = self.parsedSettings.url
        
        for arg in args:
            self._kernelOption(arg)
    
    def getQueueSize(self):
        return self.parsedSettings.queuesize
    def getAvgSamples(self):
        return self.parsedSettings.avgsamples
    
    def _kernelOption(self, arg):
        pair = arg.split('=',1)
        if len(pair) < 2:
            pair.append(None)
        var, value = tuple(pair)
        self.kernelOptions[var.upper()] = value
    
    def makeLogger(self, requester, miner):
        if not self.logger:
            self.logger = ConsoleLogger(miner, self.parsedSettings.verbose,statusfile=self.parsedSettings.statusfile,blkfound=self.parsedSettings.blkfound)
        return self.logger
    
    def makeConnection(self, requester):
        if not self.connection:
            try:
                self.connection = minerutil.openURL(self.url, requester)
            except ValueError, e:
                print(e)
                exit()
        return self.connection
    
    def makeKernel(self, requester):
        if not self.kernel:
            module = self.parsedSettings.kernel
            try:
                file, filename, smt = imp.find_module(module, ['kernels'])
            except ImportError:
                print("Could not locate the specified kernel!")
                exit()
            kernelModule = imp.load_module(module, file, filename, smt)
            self.kernel = kernelModule.MiningKernel(requester)
        return self.kernel
    
    def makeQueue(self, requester):
        if not self.queue:
            self.queue = WorkQueue(requester, self)
        return self.queue

if __name__ == '__main__':
    options = CommandLineOptions()
    miner = Miner()
    miner.start(options)
    
    reactor.run()
