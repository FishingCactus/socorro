
import psycopg2
import psycopg2.extensions
import datetime
import time
import threading
import functools as ft

db_module = psycopg2

import socorro.lib.util as util

#=================================================================================================================
class SQLDidNotReturnSingleValue (Exception):
  pass

#=================================================================================================================
class SQLDidNotReturnSingleRow (Exception):
  pass

#=================================================================================================================
class CannotConnectToDatabase(Exception):
  pass

#-----------------------------------------------------------------------------------------------------------------

# this are the exceptions that can be raised during database transactions
# that mean trouble in the database server or that the server is offline.
exceptions_eligible_for_retry = (psycopg2.OperationalError,
                                 psycopg2.InterfaceError,
                                 CannotConnectToDatabase)

#-----------------------------------------------------------------------------------------------------------------
def db_transaction_retry_wrapper(fn):
  """a decorator to add backing off retry symantics to any a method that does
  a database transaction.  When using this decorator, it is vital that any
  exception within the 'exceptions_eligible_for_retry' tuple above must not
  be handled within the wrapped function.  This means that exception handlers
  that handle 'Exception' must also have a clause for
  'exceptions_eligible_for_retry' that re-raises the exception."""
  @ft.wraps(fn)
  def f(self, *args, **kwargs):
    backoffGenerator = util.backoffSecondsGenerator()
    try:
      while True:
        try:
          result = fn(self, *args, **kwargs)
          return result
        except exceptions_eligible_for_retry:
          waitInSeconds = backoffGenerator.next()
          try:
            self.logger.critical('server failure in db transaction - '
                                 'retry in %s seconds',
                                 waitInSeconds)
          except AttributeError:
            pass
          try:
            self.responsiveSleep(waitInSeconds,
                                 10,
                                 "waiting for retry after failure in db "
                                 "transaction")
          except AttributeError:
            time.sleep(waitInSeconds)
    except KeyboardInterrupt:
      return
  return f

#-----------------------------------------------------------------------------------------------------------------
def singleValueSql (aCursor, sql, parameters=None):
  aCursor.execute(sql, parameters)
  result = aCursor.fetchall()
  try:
    return result[0][0]
  except Exception, x:
    raise SQLDidNotReturnSingleValue("%s: %s" % (str(x), sql))

#-----------------------------------------------------------------------------------------------------------------
def singleRowSql (aCursor, sql, parameters=None):
  aCursor.execute(sql, parameters)
  result = aCursor.fetchall()
  try:
    return result[0]
  except Exception, x:
    raise SQLDidNotReturnSingleRow("%s: %s" % (str(x), sql))

#-----------------------------------------------------------------------------------------------------------------
def execute (aCursor, sql, parameters=None):
  aCursor.execute(sql, parameters)
  while True:
    aRow = aCursor.fetchone()
    if aRow is not None:
      yield aRow
    else:
      break

#-----------------------------------------------------------------------------------------------------------------
@db_transaction_retry_wrapper
def transaction_execute_with_retry (database_connection_pool, sql, parameters=None):
  connection = database_connection_pool.connection()
  cursor = connection.cursor()
  try:
    cursor.execute(sql, parameters)
    try:
      result = cursor.fetchall()
    except db_module.ProgrammingError:
      result = []
    connection.commit()
  except exceptions_eligible_for_retry:
    raise
  except db_module.Error:
    connection.rollback()
    raise
  return result



#=================================================================================================================
class LoggingCursor(psycopg2.extensions.cursor):
  """Use as cursor_factory when getting cursor from connection:
  ...
  cursor = connection.cursor(cursor_factory = socorro.lib.pyscopghelper.LoggingCursor)
  cursor.setLogger(someLogger)
  ...
  """
  #-----------------------------------------------------------------------------------------------------------------
  def setLogger(self, logger):
    self.logger = logger
    self.logger.info("Now logging cursor")
  #-----------------------------------------------------------------------------------------------------------------
  def execute(self, sql, args=None):
    try:
      self.logger.info(self.mogrify(sql,args))
    except AttributeError:
      pass
    super(LoggingCursor, self).execute(sql,args)
  #-----------------------------------------------------------------------------------------------------------------
  def executemany(self,sql,args=None):
    try:
      try:
        self.logger.info("%s ..." % (self.mogrify(sql,args[0])))
      except TypeError:
        self.logger.info("%s ..." % (sql))
    except AttributeError:
      pass
    super(LoggingCursor,self).executemany(sql,args)


#=================================================================================================================
class Database(object):
  """a simple factory for creating connections for a database.  It doesn't track what it gives out"""
  #-----------------------------------------------------------------------------------------------------------------
  def __init__(self, config, logger=None):
    super(Database, self).__init__()
    if 'databasePort' not in config:
      config['databasePort'] = 5432
    self.dsn = "host=%(databaseHost)s port=%(databasePort)s dbname=%(databaseName)s user=%(databaseUserName)s password=%(databasePassword)s" % config
    self.logger = config.setdefault('logger', None)
    if logger:
      self.logger = logger
    if not self.logger:
      self.logger = util.FakeLogger()

  #-----------------------------------------------------------------------------------------------------------------
  def connection (self, databaseModule=psycopg2):
    threadName = threading.currentThread().getName()
    #self.logger.debug("%s - connecting to database", threadName)
    #self.logger.info("%s - %s", threadName, self.dsn)
    try:
      return databaseModule.connect(self.dsn)
    except Exception, x:
      self.logger.critical("%s - cannot connect to the database", threadName)
      raise CannotConnectToDatabase(x)

#=================================================================================================================
class DatabaseConnectionPool(dict):
  #-----------------------------------------------------------------------------------------------------------------
  def __init__(self, parameters, logger=None):
    super(DatabaseConnectionPool, self).__init__()
    self.database = Database(parameters, logger)
    self.logger = self.database.logger

  #-----------------------------------------------------------------------------------------------------------------
  #def connectionWithoutTest(self, name=None):
  def connection(self, name=None):
    """Try to re-use this named connection, else create one and use that"""
    if not name:
      name = threading.currentThread().getName()
    return self.setdefault(name, self.database.connection())

  #-----------------------------------------------------------------------------------------------------------------
  #def connection(self, name=None):
    #"""Like connecionCursorPairNoTest, but test that the specified connection actually works"""
    #connection = self.connectionWithoutTest(name)
    #try:
      #cursor = connection.cursor()
      #cursor.execute("select 1")
      #cursor.fetchall()
      #return connection
    #except psycopg2.Error:
      ## did the connection time out?
      #self.logger.info("%s - trying to re-establish a database connection", threading.currentThread().getName())
      #try:
        #del self[name]
      #except KeyError:
        #pass
      #try:
        #connection = self.connectionWithoutTest(name)
        #cursor.execute("select 1")
        #cursor.fetchall()
        #return connection
      #except Exception, x:
        #self.logger.critical("%s - something's gone horribly wrong with the database connection", threading.currentThread().getName())
        #raise CannotConnectToDatabase(x)

  #-----------------------------------------------------------------------------------------------------------------
  def connectionCursorPair(self, name=None):
    """Just like connection, but returns a tuple with a connection and a cursor"""
    connection = self.connection(name)
    return (connection, connection.cursor())

  #-----------------------------------------------------------------------------------------------------------------
  def cleanup (self):
    self.logger.debug("%s - killing database connections", threading.currentThread().getName())
    for name, aConnection in self.iteritems():
      try:
        aConnection.close()
        self.logger.debug("%s - connection %s closed", threading.currentThread().getName(), name)
      except psycopg2.InterfaceError:
        self.logger.debug("%s - connection %s already closed", threading.currentThread().getName(), name)
      except:
        util.reportExceptionAndContinue(self.logger)

#===============================================================================
# any routine that uses this module and the ConfigurationManager should have
# these options defined:

import socorro.lib.config_manager as cm
rc = cm.Namespace()

rc.databaseHost = cm.Option(name='databaseHost',
                            doc='the hostname of the database servers',
                            default='localhost')
rc.databasePort = cm.Option(name='databasePort',
                            doc='the port of the database on the host',
                            default=5432)
rc.databaseName =  cm.Option(name='databaseName',
                             doc='the name of the database within the server',
                             default='')
rc.databaseUserName =  cm.Option(name='databaseUserName',
                                 doc='the user name for the database servers',
                                 default='')
rc.databasePassword =  cm.Option(name='databasePassword',
                                 doc='the password for the database user',
                                 default='')

#-------------------------------------------------------------------------------
def get_required_config():
    return rc