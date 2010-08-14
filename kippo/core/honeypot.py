# Copyright (c) 2009 Upi Tamminen <desaster@gmail.com>
# See the COPYRIGHT file for more information

from twisted.cred import portal, checkers, credentials, error
from twisted.conch import avatar, recvline, interfaces as conchinterfaces
from twisted.conch.ssh import factory, userauth, connection, keys, session, common, transport
from twisted.conch.insults import insults
from twisted.application import service, internet
from twisted.protocols.policies import TrafficLoggingFactory
from twisted.internet import reactor, protocol, defer
from twisted.python import failure, log
from zope.interface import implements
from copy import deepcopy, copy
import sys, os, random, pickle, time, stat, shlex, anydbm

from kippo.core import ttylog, fs, utils
from kippo.core.config import config
import commands

class HoneyPotCommand(object):
    def __init__(self, honeypot, *args):
        self.honeypot = honeypot
        self.args = args
        self.writeln = self.honeypot.writeln
        self.write = self.honeypot.terminal.write
        self.nextLine = self.honeypot.terminal.nextLine
        self.fs = self.honeypot.fs

    def start(self):
        self.call()
        self.exit()

    def call(self):
        self.honeypot.writeln('Hello World! [%s]' % repr(self.args))

    def exit(self):
        self.honeypot.cmdstack.pop()
        self.honeypot.cmdstack[-1].resume()

    def ctrl_c(self):
        print 'Received CTRL-C, exiting..'
        self.writeln('^C')
        self.exit()

    def lineReceived(self, line):
        print 'INPUT: %s' % line

    def resume(self):
        pass

class HoneyPotShell(object):
    def __init__(self, honeypot):
        self.honeypot = honeypot
        self.showPrompt()
        self.cmdpending = []
        self.envvars = {
            'PATH':     '/bin:/usr/bin:/sbin:/usr/sbin',
            }

    def lineReceived(self, line):
        print 'CMD: %s' % line
        for i in [x.strip() for x in line.strip().split(';')]:
            if not len(i):
                continue
            self.cmdpending.append(i)
        if len(self.cmdpending):
            self.runCommand()
        else:
            self.showPrompt()

    def runCommand(self):
        if not len(self.cmdpending):
            self.showPrompt()
            return
        line = self.cmdpending.pop(0)
        try:
            cmdAndArgs = shlex.split(line)
        except:
            self.honeypot.writeln(
                '-bash: syntax error: unexpected end of file')
            # could run runCommand here, but i'll just clear the list instead
            self.cmdpending = []
            self.showPrompt()
            return

        # probably no reason to be this comprehensive for just PATH...
        envvars = copy(self.envvars)
        while len(cmdAndArgs):
            cmd = cmdAndArgs.pop(0)
            if cmd.count('='):
                key, value = cmd.split('=', 1)
                envvars[key] = value
                continue
            break
        args = cmdAndArgs

        rargs = []
        for arg in args:
            matches = self.honeypot.fs.resolve_path_wc(arg, self.honeypot.cwd)
            if matches:
                rargs.extend(matches)
            else:
                rargs.append(arg)
        cmdclass = self.honeypot.getCommand(cmd, envvars['PATH'].split(':'))
        if cmdclass:
            print 'Command found: %s' % (line,)
            self.honeypot.call_command(cmdclass, *rargs)
        else:
            print 'Command not found: %s' % (line,)
            if len(line):
                self.honeypot.writeln('bash: %s: command not found' % cmd)
                if len(self.cmdpending):
                    self.runCommand()
                else:
                    self.showPrompt()

    def resume(self):
        self.honeypot.setInsertMode()
        self.runCommand()

    def showPrompt(self):
        prompt = '%s:%%(path)s# ' % self.honeypot.hostname
        path = self.honeypot.cwd
        if path == '/root':
            path = '~'
        attrs = {'path': path}
        self.honeypot.terminal.write(prompt % attrs)

    def ctrl_c(self):
        self.honeypot.lineBuffer = []
        self.honeypot.lineBufferIndex = 0
        self.honeypot.terminal.nextLine()
        self.showPrompt()

class HoneyPotProtocol(recvline.HistoricRecvLine):
    def __init__(self, user, env):
        self.user = user
        self.env = env
        self.cwd = '/root'
        self.hostname = self.env.cfg.get('honeypot', 'hostname')
        self.fs = fs.HoneyPotFilesystem(deepcopy(self.env.fs))
        # commands is also a copy so we can add stuff on the fly
        self.commands = copy(self.env.commands)
        self.password_input = False
        self.cmdstack = []

    def connectionMade(self):
        recvline.HistoricRecvLine.connectionMade(self)
        self.terminal.write('\x1b[21t', noLog = True) # terminal title
        self.cmdstack = [HoneyPotShell(self)]

        # You are in a maze of twisty little passages, all alike
        p = self.terminal.transport.session.conn.transport.transport.getPeer()

        self.clientIP = p.host
        self.logintime = time.time()

        self.keyHandlers.update({
            '\x04':     self.handle_CTRL_D,
            '\x15':     self.handle_CTRL_U,
            '\x03':     self.handle_CTRL_C,
            })

    def lastlogExit(self):
        starttime = time.strftime('%a %b %d %H:%M',
            time.localtime(self.logintime))
        endtime = time.strftime('%H:%M',
            time.localtime(time.time()))
        duration = utils.durationHuman(time.time() - self.logintime)
        utils.addToLastlog('root\tpts/0\t%s\t%s - %s (%s)' % \
            (self.clientIP, starttime, endtime, duration))

    def connectionLost(self, reason):
        recvline.HistoricRecvLine.connectionLost(self, reason)
        self.lastlogExit()

        # not sure why i need to do this:
        del self.fs
        del self.commands

    # Overriding to prevent terminal.reset()
    def initializeScreen(self):
        self.setInsertMode()

    def txtcmd(self, txt):
        class command_txtcmd(HoneyPotCommand):
            def call(self):
                print 'Reading txtcmd from "%s"' % txt
                f = file(txt, 'r')
                self.write(f.read())
                f.close()
        return command_txtcmd

    def getCommand(self, cmd, paths):
        if not len(cmd.strip()):
            return None
        path = None
        if cmd in self.commands:
            return self.commands[cmd]
        if cmd[0] in ('.', '/'):
            path = self.fs.resolve_path(cmd, self.cwd)
            if not self.fs.exists(path):
                return None
        else:
            for i in ['%s/%s' % (self.fs.resolve_path(x, self.cwd), cmd) \
                    for x in paths]:
                if self.fs.exists(i):
                    path = i
                    break
        txt = os.path.abspath('%s/%s' % \
            (self.env.cfg.get('honeypot', 'txtcmds_path'), path))
        if os.path.exists(txt):
            return self.txtcmd(txt)
        if path in self.commands:
            return self.commands[path]
        return None

    def lineReceived(self, line):
        if len(self.cmdstack):
            self.cmdstack[-1].lineReceived(line)

    def keystrokeReceived(self, keyID, modifier):
        if type(keyID) == type(''):
            ttylog.ttylog_write(self.terminal.ttylog_file, len(keyID),
                ttylog.DIR_READ, time.time(), keyID)
        recvline.HistoricRecvLine.keystrokeReceived(self, keyID, modifier)

    # Easier way to implement password input?
    def characterReceived(self, ch, moreCharactersComing):
        if self.mode == 'insert':
            self.lineBuffer.insert(self.lineBufferIndex, ch)
        else:
            self.lineBuffer[self.lineBufferIndex:self.lineBufferIndex+1] = [ch]
        self.lineBufferIndex += 1
        if not self.password_input: 
            self.terminal.write(ch)

    def writeln(self, data):
        self.terminal.write(data)
        self.terminal.nextLine()

    def call_command(self, cmd, *args):
        obj = cmd(self, *args)
        self.cmdstack.append(obj)
        self.setTypeoverMode()
        obj.start()

    def handle_RETURN(self):
        if len(self.cmdstack) == 1:
            if self.lineBuffer:
                self.historyLines.append(''.join(self.lineBuffer))
            self.historyPosition = len(self.historyLines)
        return recvline.RecvLine.handle_RETURN(self)

    def handle_CTRL_C(self):
        self.cmdstack[-1].ctrl_c()

    def handle_CTRL_U(self):
        for i in range(self.lineBufferIndex):
            self.terminal.cursorBackward()
            self.terminal.deleteCharacter()
        self.lineBuffer = self.lineBuffer[self.lineBufferIndex:]
        self.lineBufferIndex = 0

    def handle_CTRL_D(self):
        self.call_command(self.commands['exit'])

class LoggingServerProtocol(insults.ServerProtocol):
    def connectionMade(self):
        self.ttylog_file = '%s/tty/%s-%s.log' % \
            (config().get('honeypot', 'log_path'),
            time.strftime('%Y%m%d-%H%M%S'),
            int(random.random() * 10000))
        print 'Opening TTY log: %s' % self.ttylog_file
        ttylog.ttylog_open(self.ttylog_file, time.time())
        self.ttylog_open = True
        self.terminal_title = None
        insults.ServerProtocol.connectionMade(self)

    def write(self, bytes, noLog = False):
        if self.ttylog_open and not noLog:
            ttylog.ttylog_write(self.ttylog_file, len(bytes),
                ttylog.DIR_WRITE, time.time(), bytes)
        insults.ServerProtocol.write(self, bytes)

    def connectionLost(self, reason):
        if self.ttylog_open:
            ttylog.ttylog_close(self.ttylog_file, time.time())
            self.ttylog_open = False
        insults.ServerProtocol.connectionLost(self, reason)

    # extended from the standard to read \x1b]lXXXX\x1b\\
    def dataReceived(self, data):
        for ch in data:
            if self.state == 'data':
                if ch == '\x1b':
                    self.state = 'escaped'
                else:
                    self.terminalProtocol.keystrokeReceived(ch, None)
            elif self.state == 'escaped':
                if ch == '[':
                    self.state = 'bracket-escaped'
                    self.escBuf = []
                elif ch == 'O':
                    self.state = 'low-function-escaped'
                elif ch == ']':
                    self.state = 'reverse-bracket-escaped'
                else:
                    self.state = 'data'
                    self._handleShortControlSequence(ch)
            elif self.state == 'bracket-escaped':
                if ch == 'O':
                    self.state = 'low-function-escaped'
                elif ch.isalpha() or ch == '~':
                    self._handleControlSequence(''.join(self.escBuf) + ch)
                    del self.escBuf
                    self.state = 'data'
                else:
                    self.escBuf.append(ch)
            elif self.state == 'low-function-escaped':
                self._handleLowFunctionControlSequence(ch)
                self.state = 'data'
            elif self.state == 'reverse-bracket-escaped':
                if ch == 'l':
                    self.titleBuf = []
                    self.state = 'title-escaped'
                    self.title_escaped = False
            elif self.state == 'title-escaped':
                if ch == '\x1b':
                    self.title_escaped = True
                elif self.title_escaped and ch == '\\':
                    self.terminal_title = ''.join(self.titleBuf)
                    print 'Terminal title: %s' % (self.terminal_title,)
                    self.state = 'data'
                    del self.titleBuf
                else:
                    self.titleBuf.append(ch)
            else:
                raise ValueError("Illegal state")
        # insults.ServerProtocol.dataReceived(self, data)

class HoneyPotAvatar(avatar.ConchUser):
    implements(conchinterfaces.ISession)

    def __init__(self, username, env):
        avatar.ConchUser.__init__(self)
        self.username = username
        self.env = env
        self.channelLookup.update({'session':session.SSHSession})

    def openShell(self, protocol):
        serverProtocol = LoggingServerProtocol(HoneyPotProtocol, self, self.env)
        serverProtocol.makeConnection(protocol)
        protocol.makeConnection(session.wrapProtocol(serverProtocol))

    def getPty(self, terminal, windowSize, attrs):
        print 'Terminal size: %s %s' % windowSize[0:2]
        self.windowSize = windowSize
        return None

    def execCommand(self, protocol, cmd):
        raise NotImplementedError

    def closed(self):
        pass

    def eofReceived(self):
        pass

    def windowChanged(self, windowSize):
        self.windowSize = windowSize

class HoneyPotEnvironment(object):
    def __init__(self):
        self.cfg = config()
        self.commands = {}
        import kippo.commands
        for c in kippo.commands.__all__:
            module = __import__('kippo.commands.%s' % c,
                globals(), locals(), ['commands'])
            self.commands.update(module.commands)
        self.fs = pickle.load(file(
            self.cfg.get('honeypot', 'filesystem_file'), 'rb'))

class HoneyPotRealm:
    implements(portal.IRealm)

    def __init__(self):
        # I don't know if i'm supposed to keep static stuff here
        self.env = HoneyPotEnvironment()

    def requestAvatar(self, avatarId, mind, *interfaces):
        if conchinterfaces.IConchUser in interfaces:
            return interfaces[0], \
                HoneyPotAvatar(avatarId, self.env), lambda: None
        else:
            raise Exception, "No supported interfaces found."

class HoneyPotTransport(transport.SSHServerTransport):

    def connectionMade(self):
        print 'New connection: %s:%s (%s:%s) [session: %d]' % \
            (self.transport.getPeer().host, self.transport.getPeer().port,
            self.transport.getHost().host, self.transport.getHost().port,
            self.transport.sessionno)
        transport.SSHServerTransport.connectionMade(self)

    def ssh_KEXINIT(self, packet):
        print 'Remote SSH version: %s' % (self.otherVersionString,)
        return transport.SSHServerTransport.ssh_KEXINIT(self, packet)

# As implemented by Kojoney
class HoneyPotSSHFactory(factory.SSHFactory):
    services = {
        'ssh-userauth': userauth.SSHUserAuthServer,
        'ssh-connection': connection.SSHConnection,
        }

    def __init__(self):
        cfg = config()
        if cfg.has_option('database', 'engine'):
            engine = cfg.get('database', 'engine')
            self.dblogger = __import__(
                'kippo.dblog.%s' % (engine,),
                globals(), locals(), ['dblog']).DBLogger(cfg)
            log.startLoggingWithObserver(self.dblogger.emit, setStdout=False)

    def buildProtocol(self, addr):
        # FIXME: try to mimic something real 100%
        t = HoneyPotTransport()

        t.ourVersionString = 'SSH-2.0-OpenSSH_5.1p1 Debian-5'
        t.supportedPublicKeys = self.privateKeys.keys()
        if not self.primes:
            ske = t.supportedKeyExchanges[:]
            ske.remove('diffie-hellman-group-exchange-sha1')
            t.supportedKeyExchanges = ske
        t.factory = self
        return t

class HoneypotPasswordChecker:
    implements(checkers.ICredentialsChecker)

    credentialInterfaces = (credentials.IUsernamePassword,
        credentials.IPluggableAuthenticationModules)

    def requestAvatarId(self, credentials):
        if hasattr(credentials, 'password'):
            if self.checkUserPass(credentials.username, credentials.password):
                return defer.succeed(credentials.username)
            else:
                return defer.fail(error.UnauthorizedLogin())
        elif hasattr(credentials, 'pamConversion'):
            return self.checkPamUser(credentials.username,
                credentials.pamConversion)
        return defer.fail(error.UnhandledCredentials())

    def checkPamUser(self, username, pamConversion):
        r = pamConversion((('Password:', 1),))
        return r.addCallback(self.cbCheckPamUser, username)

    def cbCheckPamUser(self, responses, username):
        for response, zero in responses:
            if self.checkUserPass(username, response):
                return defer.succeed(username)
        return defer.fail(error.UnauthorizedLogin())

    def checkUserPass(self, username, password):
        cfg = config()
        data_path = cfg.get('honeypot', 'data_path')
        passdb = anydbm.open('%s/pass.db' % (data_path,), 'c')
        success = False
        if username == 'root' and password == cfg.get('honeypot', 'password'):
            success = True
        elif username == 'root' and password in passdb:
            success = True
        passdb.close()
        if success:
            print 'login attempt [%s/%s] succeeded' % (username, password)
        else:
            print 'login attempt [%s/%s] failed' % (username, password)
        return success

def getRSAKeys():
    cfg = config()
    public_key = cfg.get('honeypot', 'public_key')
    private_key = cfg.get('honeypot', 'private_key')
    if not (os.path.exists(public_key) and os.path.exists(private_key)):
        # generate a RSA keypair
        print "Generating RSA keypair..."
        from Crypto.PublicKey import RSA
        from twisted.python import randbytes
        KEY_LENGTH = 1024
        rsaKey = RSA.generate(KEY_LENGTH, randbytes.secureRandom)
        publicKeyString = keys.Key(rsaKey).public().toString('openssh')
        privateKeyString = keys.Key(rsaKey).toString('openssh')
        # save keys for next time
        file(public_key, 'w+b').write(publicKeyString)
        file(private_key, 'w+b').write(privateKeyString)
        print "done."
    else:
        publicKeyString = file(public_key).read()
        privateKeyString = file(private_key).read()
    return publicKeyString, privateKeyString

# vim: set sw=4 et: