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

import pyopencl as cl
import numpy as np
import sys
import os

from hashlib import md5
from struct import pack, unpack
from twisted.internet import reactor

from minerutil.Midstate import calculateMidstate
from QueueReader import QueueReader
from KernelInterface import *

class KernelData(object):
    """This class is a container for all the data required for a single kernel 
    execution.
    """
    
    def __init__(self, nonceRange, vectors, iterations):
        # Prepare some raw data, converting it into the form that the OpenCL
        # function expects.
        target = np.array(
            unpack('IIIIIIII', nonceRange.unit.target), dtype=np.uint32)
        data   = np.array(
            unpack('IIII', nonceRange.unit.data[64:]), dtype=np.uint32)
        
        # Vectors do twice the work per execution, so calculate accordingly...
        rateDivisor = 2 if vectors else 1
        
        self.size = (nonceRange.size / rateDivisor) / iterations
        self.iterations = iterations
        
        self.base = [None] * iterations
        for i in range(iterations):
            self.base[i] = pack('I',
                (nonceRange.base/rateDivisor) + (i * self.size))
        
        self.state  = np.array(
            unpack('IIIIIIII', nonceRange.unit.midstate), dtype=np.uint32)
        self.state2 = np.array(unpack('IIIIIIII',
            calculateMidstate(nonceRange.unit.data[64:80] +
                '\x00\x00\x00\x80' + '\x00'*40 + '\x80\x02\x00\x00',
                nonceRange.unit.midstate, 3)), dtype=np.uint32)
        self.state2 = np.array(
            list(self.state2)[3:] + list(self.state2)[:3], dtype=np.uint32)
        self.nr = nonceRange
        
        self.f = np.zeros(8, np.uint32)
        self.calculateF(data)
    
    def calculateF(self, data):
        rotr = lambda x,y: x>>y | x<<(32-y)
        self.f[0] = np.uint32(data[0] + (rotr(data[1], 7) ^ rotr(data[1], 18) ^
            (data[1] >> 3)))
        self.f[1] = np.uint32(data[1] + (rotr(data[2], 7) ^ rotr(data[2], 18) ^
            (data[2] >> 3)) + 0x01100000)
        self.f[2] = np.uint32(data[2] + (rotr(self.f[0], 17) ^
            rotr(self.f[0], 19) ^ (self.f[0] >> 10)))
        self.f[3] = np.uint32(0x11002000 + (rotr(self.f[1], 17) ^
            rotr(self.f[1], 19) ^ (self.f[1] >> 10)))
        self.f[4] = np.uint32(0x00000280 + (rotr(self.f[0], 7) ^
            rotr(self.f[0], 18) ^ (self.f[0] >> 3)))
        self.f[5] = np.uint32(self.f[0] + (rotr(self.f[1], 7) ^
            rotr(self.f[1], 18) ^ (self.f[1] >> 3)))
        self.f[6] = np.uint32(self.state[4] + (rotr(self.state2[1], 6) ^
            rotr(self.state2[1], 11) ^ rotr(self.state2[1], 25)) +
            (self.state2[3] ^ (self.state2[1] & (self.state2[2] ^
            self.state2[3]))) + 0xe9b5dba5)
        self.f[7] = np.uint32((rotr(self.state2[5], 2) ^
            rotr(self.state2[5], 13) ^ rotr(self.state2[5], 22)) +
            ((self.state2[5] & self.state2[6]) | (self.state2[7] &
            (self.state2[5] | self.state2[6]))))
    
    def getHashCount(self):
        return self.nr.size
        
        
class MiningKernel(object):
    """A Phoenix Miner-compatible kernel that uses the poclbm OpenCL kernel."""
    
    PLATFORM = KernelOption(
        'PLATFORM', int, default=None,
        help='The ID of the OpenCL platform to use')
    DEVICE = KernelOption(
        'DEVICE', int, default=None,
        help='The ID of the OpenCL device to use')
    VECTORS = KernelOption(
        'VECTORS', bool, default=False, advanced=True,
        help='Enable vector support in the kernel?')
    FASTLOOP = KernelOption(
        'FASTLOOP', bool, default=False, advanced=True,
        help='Run iterative mining thread?')
    AGGRESSION = KernelOption(
        'AGGRESSION', int, default=1, advanced=True,
        help='Exponential factor indicating how much work to run '
        'per OpenCL execution')
    WORKSIZE = KernelOption(
        'WORKSIZE', int, default=None, advanced=True,
        help='The worksize to use when executing CL kernels.')
    OUTPUT_SIZE = KernelOption(
        'OUTPUTSIZE', int, default=0x20, advanced=True,
        help='Size of the nonce buffer')
    
    # This gets updated automatically by SVN.
    REVISION = int('$Rev$'[6:-2])
    
    def __init__(self, interface):
        platforms = cl.get_platforms()
        
        # Initialize object attributes and retrieve command-line options...)
        self.device = None
        self.kernel = None
        self.interface = interface
        self.defines = ''
        
        # FASTLOOP lowers the size of a single execution on the device, which
        # reduces the hashrate drop for low aggression.
        if self.FASTLOOP:
            # mineRange() will be doing its mining in substeps (2^3 = 8)
            loopExponent = 3
        else:
            # Do all work in a single iteration.
            loopExponent = 0  
            
        # Number of iterations per mineRange()
        self.iterations = 1 << loopExponent
            
        # This is the number of nonces to run per kernel;
        # 2^(16 + sqrt(iterations) + aggression)
        self.AGGRESSION += 16 + loopExponent
        self.AGGRESSION = min(32, self.AGGRESSION)
        self.AGGRESSION = max(16, self.AGGRESSION)
        self.size = 1 << self.AGGRESSION
        
        # We need a QueueReader to efficiently provide our dedicated thread
        # with work.
        self.qr = QueueReader(self.interface, lambda nr: KernelData(nr,
            self.VECTORS, self.iterations), lambda x,y: self.size)
        
        # The platform selection must be valid to mine.
        if self.PLATFORM >= len(platforms) or \
            (self.PLATFORM is None and len(platforms) > 1):
            self.interface.log(
                'Wrong platform or more than one OpenCL platform found, '
                'use PLATFORM=ID to select one of the following\n',
                False, True)
            
            for i,p in enumerate(platforms):
                self.interface.log('    [%d]\t%s' % (i, p.name), False, False)
            
            # Since the platform is invalid, we can't mine.
            self.interface.fatal()
        elif self.PLATFORM is None:
            self.PLATFORM = 0
            
        devices = platforms[self.PLATFORM].get_devices()
        
        # The device selection must be valid to mine.
        if self.DEVICE >= len(devices) or \
            (self.DEVICE is None and len(devices) > 1):
            self.interface.log(
                'No device specified or device not found, '
                'use DEVICE=ID to specify one of the following\n',
                False, True)
            
            for i,d in enumerate(devices):
                self.interface.log('    [%d]\t%s' % (i, d.name), False, False)
        
            # Since the device selection is invalid, we can't mine.
            self.interface.fatal()
        elif self.DEVICE is None:
            self.DEVICE = 0
        
        self.device = devices[self.DEVICE]
        
        # We need the appropriate kernel for this device...
        try:
            self.loadKernel(self.device)
        except KeyboardInterrupt:
            sys.exit()
        except Exception:
            self.interface.fatal("Failed to load OpenCL kernel!")
        
        # Initialize a command queue to send commands to the device, and a
        # buffer to collect results in...
        self.commandQueue = cl.CommandQueue(self.context)
        self.output = np.zeros(self.OUTPUT_SIZE+1, np.uint32)
        self.output_buf = cl.Buffer(
            self.context, cl.mem_flags.WRITE_ONLY | cl.mem_flags.USE_HOST_PTR,
            hostbuf=self.output)
        
        self.applyMeta()
    
    def applyMeta(self):
        """Apply any kernel-specific metadata."""
        self.interface.setMeta('kernel', 'poclbm r%s' % self.REVISION)
        self.interface.setMeta('device', self.device.name.replace('\x00',''))
        self.interface.setMeta('cores', self.device.max_compute_units)
    
    def loadKernel(self, device):
        """Load the kernel and initialize the device."""
        self.context = cl.Context([device], None, None)
        
        # These definitions are required for the kernel to function.
        self.defines += (' -DOUTPUT_SIZE=' + str(self.OUTPUT_SIZE))
        self.defines += (' -DOUTPUT_MASK=' + str(self.OUTPUT_SIZE - 1))
        
        # If the user wants to mine with vectors, enable the appropriate code
        # in the kernel source.
        if self.VECTORS:
            self.defines += ' -DVECTORS'
        
        # Some AMD devices support a special "bitalign" instruction that makes
        # bitwise rotation (required for SHA-256) much faster.
        if (device.extensions.find('cl_amd_media_ops') != -1):
            self.defines += ' -DBITALIGN'
        
        # Locate and read the OpenCL source code in the kernel's directory.
        kernelFileDir, pyfile = os.path.split(__file__)
        kernelFilePath = os.path.join(kernelFileDir, 'kernel.cl')
        kernelFile = open(kernelFilePath, 'r')
        kernel = kernelFile.read()
        kernelFile.close()
        
        # For fast startup, we cache the compiled OpenCL code. The name of the
        # cache is determined as the hash of a few important,
        # compilation-specific pieces of information.
        m = md5()
        m.update(device.platform.name)
        m.update(device.platform.version)
        m.update(device.name)
        m.update(self.defines)
        m.update(kernel)
        cacheName = '%s.elf' % m.hexdigest()
        
        fileName = os.path.join(kernelFileDir, cacheName)
        
        # Finally, the actual work of loading the kernel...
        try:
            binary = open(fileName, 'rb')
        except IOError: 
            binary = None
        
        try:
            if binary is None:
                self.kernel = cl.Program(
                    self.context, kernel).build(self.defines)
                binaryW = open(fileName, 'wb')
                binaryW.write(self.kernel.binaries[0])
                binaryW.close()
            else:
                binarydata = binary.read()
                self.kernel = cl.Program(
                    self.context, [device], [binarydata]).build(self.defines)
                if self.kernel.binaries[0] == binarydata:
                    self.interface.debug("Loaded cached kernel")
                else:
                    self.interface.debug("Failed to load cached kernel")
                    
        except cl.LogicError:
            self.interface.fatal("Failed to compile OpenCL kernel!")
        finally:
            if binary: binary.close()
        
        # If the user didn't specify their own worksize, use the maxium
        # supported by the device.
        maxSize = self.kernel.search.get_work_group_info(
                  cl.kernel_work_group_info.WORK_GROUP_SIZE, self.device)
        
        if self.WORKSIZE is None:
            self.WORKSIZE = maxSize
        else:
            self.WORKSIZE = min(self.WORKSIZE, maxSize)
            self.WORKSIZE = max(self.WORKSIZE, 1)
            #if the worksize is not a power of 2, round down to the nearest one
            if not ((self.WORKSIZE & (self.WORKSIZE - 1)) == 0):   
                self.WORKSIZE = 1 << int(math.floor(math.log(X)/math.log(2)))
            
        self.interface.setWorkFactor(self.WORKSIZE)
    
    def start(self):
        """Mines out a nonce range, returning a Deferred which will be fired
        with a Python list of Python ints, indicating which nonces meet the
        target specified in the range's WorkUnit.
        """
        
        self.qr.start()
        reactor.callInThread(self.mineThread)
    
    def postprocess(self, output, nr):
        """Scans over a single buffer produced as a result of running the
        OpenCL kernel on the device. This is done outside of the mining thread
        for efficiency reasons.
        """
        # Iterate over only the first OUTPUT_SIZE items. Exclude the last item
        # which is a duplicate of the most recently-found nonce.
        for i in xrange(self.OUTPUT_SIZE):
            if output[i]:   
                if not self.interface.foundNonce(nr, int(output[i])):
                    hash = self.interface.calculateHash(nr, int(output[i]))
                    if not hash.endswith('\x00\x00\x00\x00'):
                        self.interface.error('Unusual behavior from OpenCL. '
                            'Hardware problem?')
    
    def mineThread(self):
        for data in self.qr:
            self.mineRange(data)
    
    def mineRange(self, data):
        for i in range(data.iterations):
            self.kernel.search(
                self.commandQueue, (data.size, ), (self.WORKSIZE, ),
                data.state[0], data.state[1], data.state[2], data.state[3],
                data.state[4], data.state[5], data.state[6], data.state[7],
                data.state2[1], data.state2[2], data.state2[3],
                data.state2[5], data.state2[6], data.state2[7],
                data.base[i],
                data.f[0], data.f[1], data.f[2], data.f[3],
                data.f[4], data.f[5], data.f[6], data.f[7],
                self.output_buf)
            cl.enqueue_read_buffer(
                self.commandQueue, self.output_buf, self.output)
            self.commandQueue.finish()
                
            # The OpenCL code will flag the last item in the output buffer when
            # it finds a valid nonce. If that's the case, send it to the main
            # thread for postprocessing and clean the buffer for the next pass.
            if self.output[self.OUTPUT_SIZE]:
                reactor.callFromThread(self.postprocess, self.output.copy(),
                data.nr)
            
                self.output.fill(0)
                cl.enqueue_write_buffer(
                    self.commandQueue, self.output_buf, self.output)
 