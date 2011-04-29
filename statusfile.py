import os,time,json

PATH_STRING = '/tmp/status/{name}.{time:.0f}.{pid}'
MAX_STATUS_AGE = 10

class StatusFile(object):
	''' Class to track some status variables of a currently-running instance in a machine-readable format. '''
	def __init__(self,pid=None,startTime=None,programName='poclbm',keepOnClose=False,path=None):
		# Set variables
		self.pid = pid
		self.startTime = startTime
		self.programName = programName
		self.keepOnClose = keepOnClose

		# Defaults
		if self.pid == None:
			self.pid = os.getpid()
		if self.startTime == None:
			self.startTime = int(time.time())

		# Build file path
		if path:
			self.path = path
		else:
			self.path = PATH_STRING.format(pid=self.pid,time=self.startTime,name=self.programName)
		self.path = os.path.abspath(self.path)

		# Files to write and read
		self.writeFile = None
		self.readFile = None

		# Last save timestamp
		self.lastSave = 0

		# Variable list
		self.variables = {}

		# Just for good measure
		self.update('_StartTime', self.startTime)
		self.update('_PID', self.pid)

	def update(self,key,value,force=False):
		''' Update the proper variable in the variables list. Save if it's been more than MAX_STATUS_AGE seconds. '''
		self.variables[key] = value
		if force or (time.time() - self.lastSave > MAX_STATUS_AGE and len(self) > 1):
			self.save()

	def increment(self,key,amount=1,force=False):
		if not key in self.variables:
			self.variables[key] = 0
		self.variables[key] += amount
		if force or (time.time() - self.lastSave > MAX_STATUS_AGE and len(self) > 1):
			self.save()

	def __len__(self):
		''' How many variables are we tracking? '''
		return len(self.variables)

	def __del__(self):
		''' Cleanup when we close. '''
		weWrote = False
		if self.writeFile:
			weWrote = True
			self.writeFile.close()
		if self.readFile:
			self.readFile.close()

		# Delete the status file if we ever wrote to it
		if not self.keepOnClose and weWrote:
			os.unlink(self.path)

	def save(self):
		# Open file if it isn't
		if not self.writeFile:
			self.writeFile = open(self.path,'w')

		# Update last save time
		self.lastSave = int(time.time())
		self.update('_LastSave',self.lastSave)

		# Write to the file
		self.writeFile.seek(0)
		json.dump(self.variables,self.writeFile)
		self.writeFile.write('\n')
		self.writeFile.flush()

	def load(self):
		# Open file if it isn't
		if not self.readFile:
			self.readFile = open(self.path,'r')

		# Read all values from the file
		self.readFile.seek(0)
		self.variables = json.load(self.readFile)
		return self

	def __str__(self):
		return '\n'.join(["{0}: {1}".format(key,self.variables[key]) for key in self.variables])
