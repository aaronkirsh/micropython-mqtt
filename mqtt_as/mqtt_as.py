# mqtt_as.py Asynchronous version of umqtt.robust
# (C) Copyright Peter Hinch 2017-2020.
# Released under the MIT licence.

# Pyboard D support added
# Various improvements contributed by Kevin Köck.

import gc
import usocket as socket
import ustruct as struct

gc.collect()
from ubinascii import hexlify
import uasyncio as asyncio
from condition import Condition
gc.collect()
from utime import ticks_ms, ticks_diff, time
from uerrno import EINPROGRESS, ETIMEDOUT

gc.collect()
from micropython import const
from machine import unique_id
import network

gc.collect()
from sys import platform

import logging
log = logging.getLogger(__name__)
gc.collect()

VERSION = (0, 6, 0)

# Default short delay for good SynCom throughput (avoid sleep(0) with SynCom).
_DEFAULT_MS = const(20)
_SOCKET_POLL_DELAY = const(5)  # 100ms added greatly to publish latency

# Legitimate errors while waiting on a socket. See uasyncio __init__.py open_connection().
if platform == 'esp32' or platform == 'esp32_LoBo':
    # https://forum.micropython.org/viewtopic.php?f=16&t=3608&p=20942#p20942
    BUSY_ERRORS = [EINPROGRESS, ETIMEDOUT, 118, 119]  # Add in weird ESP32 errors
else:
    BUSY_ERRORS = [EINPROGRESS, ETIMEDOUT]

ESP8266 = platform == 'esp8266'
ESP32 = platform == 'esp32'
PYBOARD = platform == 'pyboard'
LOBO = platform == 'esp32_LoBo'

# Message state
mqtt_ms_invalid = 0
mqtt_ms_publish = 1
mqtt_ms_wait_for_puback = 2
mqtt_ms_wait_for_pubrec = 3
mqtt_ms_resend_pubrel = 4
mqtt_ms_wait_for_pubrel = 5
mqtt_ms_resend_pubcomp = 6
mqtt_ms_wait_for_pubcomp = 7
mqtt_ms_send_pubrec = 8
mqtt_ms_queued = 9
# Error values
MQTT_ERR_AGAIN = -1
MQTT_ERR_SUCCESS = 0
MQTT_ERR_NOMEM = 1
MQTT_ERR_PROTOCOL = 2
MQTT_ERR_INVAL = 3
MQTT_ERR_NO_CONN = 4
MQTT_ERR_CONN_REFUSED = 5
MQTT_ERR_NOT_FOUND = 6
MQTT_ERR_CONN_LOST = 7
MQTT_ERR_TLS = 8
MQTT_ERR_PAYLOAD_SIZE = 9
MQTT_ERR_NOT_SUPPORTED = 10
MQTT_ERR_AUTH = 11
MQTT_ERR_ACL_DENIED = 12
MQTT_ERR_UNKNOWN = 13
MQTT_ERR_ERRNO = 14
MQTT_ERR_QUEUE_SIZE = 15

MQTT_CLIENT = 0
MQTT_BRIDGE = 1

time_func = time



# Default "do little" coro for optional user replacement
async def eliza(*_):  # e.g. via set_wifi_handler(coro): see test program
    await asyncio.sleep_ms(_DEFAULT_MS)


config = {
    'client_id':     hexlify(unique_id()),
    'server':        None,
    'port':          0,
    'user':          '',
    'password':      '',
    'keepalive':     60,
    'ping_interval': 0,
    'ssl':           False,
    'ssl_params':    {},
    'response_time': 10,
    'clean_init':    True,
    'clean':         True,
    'max_repubs':    4,
    'will':          None,
    'subs_cb':       lambda *_: None,
    'wifi_coro':     eliza,
    'connect_coro':  eliza,
    'on_publish':    eliza,
    'ssid':          None,
    'wifi_pw':       None,
}


class MQTTException(Exception):
    pass


def mid_gen():
    mid = 0
    while True:
        mid = mid + 1 if mid < 65535 else 1
        yield mid


def qos_check(qos):
    if not (qos == 0 or qos == 1):
        raise ValueError('Only qos 0 and 1 are supported.')


# MQTT_base class. Handles MQTT protocol on the basis of a good connection.
# Exceptions from connectivity failures are handled by MQTTClient subclass.
class MQTT_base:
    REPUB_COUNT = 0  # TEST
    DEBUG = False

    def __init__(self, config):
        # MQTT config
        self._client_id = config['client_id']
        self._user = config['user']
        self._pswd = config['password']
        self._keepalive = config['keepalive']
        if self._keepalive >= 65536:
            raise ValueError('invalid keepalive time')
        self._response_time = config['response_time'] * 1000  # Repub if no PUBACK received (ms).
        self._max_repubs = config['max_repubs']
        self._clean_init = config['clean_init']  # clean_session state on first connection
        self._clean = config['clean']  # clean_session state on reconnect
        will = config['will']
        if will is None:
            self._lw_topic = False
        else:
            self._set_last_will(*will)
        # WiFi config
        self._ssid = config['ssid']  # Required for ESP32 / Pyboard D. Optional ESP8266
        self._wifi_pw = config['wifi_pw']
        self._ssl = config['ssl']
        self._ssl_params = config['ssl_params']
        # Callbacks and coros
        self._on_message = config['subs_cb']
        self._wifi_handler = config['wifi_coro']
        self._on_connect = config['connect_coro']
        self._on_publish = config['on_publish']
        # Network
        self.port = config['port']
        if self.port == 0:
            self.port = 8883 if self._ssl else 1883
        self.server = config['server']
        if self.server is None:
            raise ValueError('no server specified.')
        self._sock = None
        self._sta_if = network.WLAN(network.STA_IF)
        self._sta_if.active(True)

        self.newmid = mid_gen()
        self.rcv_mids = set()  # PUBACK and SUBACK mids awaiting ACK response
        self.last_rx = ticks_ms()  # Time of last communication from broker
        self.lock = asyncio.Lock()

    def _set_last_will(self, topic, msg, retain=False, qos=0):
        qos_check(qos)
        if not topic:
            raise ValueError('Empty topic.')
        self._lw_topic = topic
        self._lw_msg = msg
        self._lw_qos = qos
        self._lw_retain = retain

    def _timeout(self, t):
        return ticks_diff(ticks_ms(), t) > self._response_time

    async def _as_read(self, n, sock=None):  # OSError caught by superclass
        if sock is None:
            sock = self._sock
        data = b''
        t = ticks_ms()
        while len(data) < n:
            if self._timeout(t) or not self.isconnected():
                raise OSError(-1)
            try:
                msg = sock.read(n - len(data))
            except OSError as e:  # ESP32 issues weird 119 errors here
                msg = None
                if e.args[0] not in BUSY_ERRORS:
                    raise
            if msg == b'':  # Connection closed by host
                raise OSError(-1)
            if msg is not None:  # data received
                data = b''.join((data, msg))
                t = ticks_ms()
                self.last_rx = ticks_ms()
            await asyncio.sleep_ms(_SOCKET_POLL_DELAY)
        return data

    async def _as_write(self, bytes_wr, length=0, sock=None):
        if sock is None:
            sock = self._sock
        if length:
            bytes_wr = bytes_wr[:length]
        t = ticks_ms()
        while bytes_wr:
            if self._timeout(t) or not self.isconnected():
                raise OSError(-1)
            try:
                n = sock.write(bytes_wr)
            except OSError as e:  # ESP32 issues weird 119 errors here
                n = 0
                if e.args[0] not in BUSY_ERRORS:
                    raise
            if n:
                t = ticks_ms()
                bytes_wr = bytes_wr[n:]
            await asyncio.sleep_ms(_SOCKET_POLL_DELAY)

    async def _send_str(self, s):
        await self._as_write(struct.pack("!H", len(s)))
        await self._as_write(s)

    async def _recv_len(self):
        n = 0
        sh = 0
        while 1:
            res = await self._as_read(1)
            b = res[0]
            n |= (b & 0x7f) << sh
            if not b & 0x80:
                return n
            sh += 7

    async def _connect(self, clean):
        self._sock = socket.socket()
        self._sock.setblocking(False)
        try:
            self._sock.connect(self._addr)
        except OSError as e:
            if e.args[0] not in BUSY_ERRORS:
                raise
        await asyncio.sleep_ms(_DEFAULT_MS)
        log.info('Connecting to broker.')
        if self._ssl:
            import ussl
            self._sock = ussl.wrap_socket(self._sock, **self._ssl_params)
        premsg = bytearray(b"\x10\0\0\0\0\0")
        msg = bytearray(b"\x04MQTT\x04\0\0\0")  # Protocol 3.1.1

        sz = 10 + 2 + len(self._client_id)
        msg[6] = clean << 1
        if self._user:
            sz += 2 + len(self._user) + 2 + len(self._pswd)
            msg[6] |= 0xC0
        if self._keepalive:
            msg[7] |= self._keepalive >> 8
            msg[8] |= self._keepalive & 0x00FF
        if self._lw_topic:
            sz += 2 + len(self._lw_topic) + 2 + len(self._lw_msg)
            msg[6] |= 0x4 | (self._lw_qos & 0x1) << 3 | (self._lw_qos & 0x2) << 3
            msg[6] |= self._lw_retain << 5

        i = 1
        while sz > 0x7f:
            premsg[i] = (sz & 0x7f) | 0x80
            sz >>= 7
            i += 1
        premsg[i] = sz
        await self._as_write(premsg, i + 2)
        await self._as_write(msg)
        await self._send_str(self._client_id)
        if self._lw_topic:
            await self._send_str(self._lw_topic)
            await self._send_str(self._lw_msg)
        if self._user:
            await self._send_str(self._user)
            await self._send_str(self._pswd)
        # Await CONNACK
        # read causes ECONNABORTED if broker is out; triggers a reconnect.
        resp = await self._as_read(4)
        log.info('Connected to broker.')  # Got CONNACK
        if resp[3] != 0 or resp[0] != 0x20 or resp[1] != 0x02:
            raise OSError(-1)  # Bad CONNACK e.g. authentication fail.

    async def _ping(self):
        async with self.lock:
            await self._as_write(b"\xc0\0")

    # Check internet connectivity by sending DNS lookup to Google's 8.8.8.8
    async def wan_ok(self,
                     packet=b'$\x1a\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03www\x06google\x03com\x00\x00\x01\x00\x01'):
        if not self.isconnected():  # WiFi is down
            return False
        length = 32  # DNS query and response packet size
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        s.connect(('8.8.8.8', 53))
        await asyncio.sleep(1)
        try:
            await self._as_write(packet, sock=s)
            await asyncio.sleep(2)
            res = await self._as_read(length, s)
            if len(res) == length:
                return True  # DNS response size OK
        except OSError:  # Timeout on read: no connectivity.
            return False
        finally:
            s.close()
        return False

    async def broker_up(self):  # Test broker connectivity
        if not self.isconnected():
            return False
        tlast = self.last_rx
        if ticks_diff(ticks_ms(), tlast) < 1000:
            return True
        try:
            await self._ping()
        except OSError:
            return False
        t = ticks_ms()
        while not self._timeout(t):
            await asyncio.sleep_ms(100)
            if ticks_diff(self.last_rx, tlast) > 0:  # Response received
                return True
        return False

    async def disconnect(self):
        try:
            async with self.lock:
                self._sock.write(b"\xe0\0")
        except OSError:
            pass
        self._has_connected = False
        self.close()

    def close(self):
        if self._sock is not None:
            self._sock.close()

    async def _await_mid(self, mid):
        t = ticks_ms()
        while mid in self.rcv_mids:  # local copy
            if self._timeout(t) or not self.isconnected():
                break  # Must repub or bail out
            await asyncio.sleep_ms(100)
        else:
            return True  # mid received. All done.
        return False

    # qos == 1: coro blocks until wait_msg gets correct mid.
    # If WiFi fails completely subclass re-publishes with new mid.
    async def publish(self, topic, payload=None, retain=False, qos=0):
        log.info("publish: \n topic: {}\n payload: {}\n retain: {}\n qos: {}".format(topic,payload,retain,qos))

        if topic is None or len(topic) == 0:
            raise ValueError('Invalid topic.')
        if qos < 0 or qos > 2: # qos = 0,1,2
            raise ValueError('Invalid QoS level.')

        if isinstance(payload,dict):
            try: local_payload = dumps(payload)
            except: from json import dumps; local_payload = dumps(payload)
        elif isinstance(payload, (bytes, bytearray)):
            local_payload = payload
        # elif isinstance(payload, unicode):
        #     local_payload = payload.encode('utf-8')
        elif isinstance(payload, (int, float)):
            local_payload = str(payload).encode('ascii')
        elif isinstance(payload,str):
            local_payload = payload
        elif payload is None:
            local_payload = b''
        else:
            log.info('Payload type: {}'.format(type(payload)))
            raise TypeError(
                'payload must be a string, bytearray, int, float or None.')
        if len(local_payload) > 268435455:
            raise ValueError('Payload too large.')

        local_mid = next(self.newmid)
        info = MQTTMessageInfo(local_mid)
        if qos:
            self.rcv_mids.add(local_mid)

        # Do publish
        async with self.lock:
            info.rc = await self._publish(topic, local_payload, retain, qos, 0, local_mid)
        # Do on publish callback if we arren't ensuring delivery
        if qos == 0:
            await self._on_publish(topic, payload, info)
            return info
        #else:
        #    message = MQTTMessage(local_mid)

        count = 0
        while 1:  # Await PUBACK, republish on timeout
            # If we recieve a mid that we are waiting for, then we are done
            if await self._await_mid(local_mid):
                return info
            # If we have tried more than our max tries or if we are disconnected
            if count >= self._max_repubs or not self.isconnected():
                # throw error, subclass will republish with new PID???
                raise OSError(-1)  
            # Try to publish again
            async with self.lock:
                info.rc = await self._publish(topic, local_payload, retain, qos, dup=1, mid=local_mid)  # Add mid

            count += 1
            self.REPUB_COUNT += 1

    async def _publish(self, topic, msg, retain, qos, dup, mid):
        try:
            pkt = bytearray(b"\x30\0\0\0")
            pkt[0] |= qos << 1 | retain | dup << 3
            sz = 2 + len(topic) + len(msg)
            if qos > 0:
                sz += 2
            if sz >= 2097152:
                return 9 # string to long, return rc 9 TB_ERR_PAYLOAD_SIZE
            i = 1
            while sz > 0x7f:
                pkt[i] = (sz & 0x7f) | 0x80
                sz >>= 7
                i += 1
            pkt[i] = sz
            await self._as_write(pkt, i + 1)
            await self._send_str(topic)
            if qos > 0:
                struct.pack_into("!H", pkt, 0, mid)
                await self._as_write(pkt, 2)
            await self._as_write(msg)
            return 0 # success
        except:
            return 13  # unknown error

    # Can raise OSError if WiFi fails. Subclass traps
    async def subscribe(self, topic, qos):
        pkt = bytearray(b"\x82\0\0\0")
        mid = next(self.newmid)
        self.rcv_mids.add(mid)
        struct.pack_into("!BH", pkt, 1, 2 + 2 + len(topic) + 1, mid)
        async with self.lock:
            await self._as_write(pkt)
            await self._send_str(topic)
            await self._as_write(qos.to_bytes(1, "little"))

        if not await self._await_mid(mid):
            raise OSError(-1)

    # Wait for a single incoming MQTT message and process it.
    # Subscribed messages are delivered to a callback previously
    # set by .setup() method. Other (internal) MQTT
    # messages processed internally.
    # Immediate return if no data available. Called from ._handle_msg().
    async def wait_msg(self):
        res = self._sock.read(1)  # Throws OSError on WiFi fail
        if res is None:
            return
        if res == b'':
            raise OSError(-1)

        if res == b"\xd0":  # PINGRESP
            await self._as_read(1)  # Update .last_rx time
            return
        op = res[0]

        if op == 0x40:  # PUBACK: save mid
            sz = await self._as_read(1)
            if sz != b"\x02":
                raise OSError(-1)
            rcv_mid = await self._as_read(2)
            mid = rcv_mid[0] << 8 | rcv_mid[1]
            if mid in self.rcv_mids:
                self.rcv_mids.discard(mid)
            else:
                raise OSError(-1)

        if op == 0x90:  # SUBACK
            resp = await self._as_read(4)
            if resp[3] == 0x80:
                raise OSError(-1)
            mid = resp[2] | (resp[1] << 8)
            if mid in self.rcv_mids:
                self.rcv_mids.discard(mid)
            else:
                raise OSError(-1)

        if op & 0xf0 != 0x30:
            return
        sz = await self._recv_len()
        topic_len = await self._as_read(2)
        topic_len = (topic_len[0] << 8) | topic_len[1]
        topic = await self._as_read(topic_len)
        sz -= topic_len + 2
        if op & 6:
            mid = await self._as_read(2)
            mid = mid[0] << 8 | mid[1]
            sz -= 2
        msg = await self._as_read(sz)
        retained = op & 0x01
        try: await self._on_message(topic, msg, bool(retained))
        except: self._on_message(topic, msg, bool(retained))
        if op & 6 == 2:  # qos 1
            pkt = bytearray(b"\x40\x02\0\0")  # Send PUBACK
            struct.pack_into("!H", pkt, 2, mid)
            await self._as_write(pkt)
        elif op & 6 == 4:  # qos 2 not supported
            raise OSError(-1)


# MQTTClient class. Handles issues relating to connectivity.
class MQTTClient(MQTT_base):
    def __init__(self, config):
        super().__init__(config)
        self._isconnected = False  # Current connection state
        keepalive = 1000 * self._keepalive  # ms
        self._ping_interval = keepalive // 4 if keepalive else 20000
        p_i = config['ping_interval'] * 1000  # Can specify shorter e.g. for subscribe-only
        if p_i and p_i < self._ping_interval:
            self._ping_interval = p_i
        self._in_connect = False
        self._has_connected = False  # Define 'Clean Session' value to use.
        if ESP8266:
            import esp
            esp.sleep_type(0)  # Improve connection integrity at cost of power consumption.

    async def wifi_connect(self):
        s = self._sta_if
        if ESP8266:
            if s.isconnected():  # 1st attempt, already connected.
                return
            s.active(True)
            s.connect()  # ESP8266 remembers connection.
            for _ in range(60):
                if s.status() != network.STAT_CONNECTING:  # Break out on fail or success. Check once per sec.
                    break
                await asyncio.sleep(1)
            if s.status() == network.STAT_CONNECTING:  # might hang forever awaiting dhcp lease renewal or something else
                s.disconnect()
                await asyncio.sleep(1)
            if not s.isconnected() and self._ssid is not None and self._wifi_pw is not None:
                s.connect(self._ssid, self._wifi_pw)
                while s.status() == network.STAT_CONNECTING:  # Break out on fail or success. Check once per sec.
                    await asyncio.sleep(1)
        elif ESP32:
            from wifimgr import get_connection
            try:
                s = get_connection()                
            except Exception as e:
                log.info('using ssid: %s' %self._ssid)
                log.info('using password: %s' %self._wifi_pw)
                log.info('Error connecting to wifi..: {}'.format(e))
        else:
            s.active(True)
            try:
                s.connect(str(self._ssid), str(self._wifi_pw))
            except Exception as e:
                log.info('using ssid: %s' %self._ssid)
                log.info('using password: %s' %self._wifi_pw)
                log.info('Error connecting to wifi..: {}'.format(e))
                
            if PYBOARD:  # Doesn't yet have STAT_CONNECTING constant
                while s.status() in (1, 2):
                    await asyncio.sleep(1)
            elif LOBO:
                i = 0
                while not s.isconnected():
                    await asyncio.sleep(1)
                    i += 1
                    if i >= 10:
                        break
            else:
                while s.status() == network.STAT_CONNECTING:  # Break out on fail or success. Check once per sec.
                    await asyncio.sleep(1)

        if not s.isconnected():
            raise OSError
        # Ensure connection stays up for a few secs.
        log.info('Checking WiFi integrity.')
        for _ in range(5):
            if not s.isconnected():
                raise OSError  # in 1st 5 secs
            await asyncio.sleep(1)
        log.info('Got reliable connection')

    async def connect(self):
        if not self._has_connected:
            await self.wifi_connect()  # On 1st call, caller handles error
            # Note this blocks if DNS lookup occurs. Do it once to prevent
            # blocking during later internet outage:
            self._addr = socket.getaddrinfo(self.server, self.port)[0][-1]
        self._in_connect = True  # Disable low level ._isconnected check
        clean = self._clean if self._has_connected else self._clean_init
        try:
            await self._connect(clean)
        except Exception:
            self.close()
            log.info('FATAL ERROR: Error connecting to broker! Are your credentials correct?')
            raise
        self.rcv_mids.clear()
        # If we get here without error broker/LAN must be up.
        self._isconnected = True
        self._in_connect = False  # Low level code can now check connectivity.
        loop = asyncio.get_event_loop()
        loop.create_task(self._wifi_handler(True))  # User handler.
        if not self._has_connected:
            self._has_connected = True  # Use normal clean flag on reconnect.
            loop.create_task(
                self._keep_connected())  # Runs forever unless user issues .disconnect()

        loop.create_task(self._handle_msg())  # Tasks quit on connection fail.
        loop.create_task(self._keep_alive())
        if self.DEBUG:
            loop.create_task(self._memory())
        loop.create_task(self._on_connect(self))  # User handler.

    # Launched by .connect(). Runs until connectivity fails. Checks for and
    # handles incoming messages.
    async def _handle_msg(self):
        try:
            while self.isconnected():
                async with self.lock:
                    await self.wait_msg()  # Immediate return if no message
                await asyncio.sleep_ms(_DEFAULT_MS)  # Let other tasks get lock

        except OSError:
            pass
        self._reconnect()  # Broker or WiFi fail.

    # Keep broker alive MQTT spec 3.1.2.10 Keep Alive.
    # Runs until ping failure or no response in keepalive period.
    async def _keep_alive(self):
        while self.isconnected():
            pings_due = ticks_diff(ticks_ms(), self.last_rx) // self._ping_interval
            if pings_due >= 4:
                log.info('Reconnect: broker fail.')
                break
            elif pings_due >= 1:
                try:
                    await self._ping()
                except OSError:
                    break
            await asyncio.sleep(1)
        self._reconnect()  # Broker or WiFi fail.

    # DEBUG: show RAM messages.
    async def _memory(self):
        count = 0
        while self.isconnected():  # Ensure just one instance.
            await asyncio.sleep(1)  # Quick response to outage.
            count += 1
            count %= 20
            if not count:
                gc.collect()
                log.info('RAM free {} alloc {}'.format(gc.mem_free(), gc.mem_alloc()))

    def isconnected(self):
        if self._in_connect:  # Disable low-level check during .connect()
            return True
        if self._isconnected and not self._sta_if.isconnected():  # It's going down.
            self._reconnect()
        return self._isconnected

    def _reconnect(self):  # Schedule a reconnection if not underway.
        if self._isconnected:
            self._isconnected = False
            self.close()
            loop = asyncio.get_event_loop()
            loop.create_task(self._wifi_handler(False))  # User handler.

    # Await broker connection.
    async def _connection(self):
        while not self._isconnected:
            await asyncio.sleep(1)

    # Scheduled on 1st successful connection. Runs forever maintaining wifi and
    # broker connection. Must handle conditions at edge of WiFi range.
    async def _keep_connected(self):
        while self._has_connected:
            if self.isconnected():  # Pause for 1 second
                await asyncio.sleep(1)
                gc.collect()
            else:
                self._sta_if.disconnect()
                await asyncio.sleep(1)
                try:
                    await self.wifi_connect()
                except OSError:
                    continue
                if not self._has_connected:  # User has issued the terminal .disconnect()
                    log.info('Disconnected, exiting _keep_connected')
                    break
                try:
                    await self.connect()
                    # Now has set ._isconnected and scheduled _on_connect().
                    log.info('Reconnect OK!')
                except OSError as e:
                    log.info('Error in reconnect: {}'.format(e))
                    # Can get ECONNABORTED or -1. The latter signifies no or bad CONNACK received.
                    self.close()  # Disconnect and try again.
                    self._in_connect = False
                    self._isconnected = False
        log.info('Disconnected, exited _keep_connected')

    async def subscribe(self, topic, qos=0):
        qos_check(qos)
        while 1:
            await self._connection()
            try:
                return await super().subscribe(topic, qos)
            except OSError:
                pass
            self._reconnect()  # Broker or WiFi fail.

    async def publish(self, topic, msg, retain=False, qos=0):
        qos_check(qos)
        while 1:
            await self._connection()
            try:
                return await super().publish(topic, msg, retain, qos)
            except OSError:
                pass
            self._reconnect()  # Broker or WiFi fail.







##############################


class MQTTMessageInfo(object):
    """This is a class returned from _publish() and can be used to find
    out the mid of the message that was published, and to determine whether the
    message has been published, and/or wait until it is published.
    """

    __slots__ = 'mid', '_published', '_condition', 'rc', '_iterpos'

    def __init__(self, mid):
        self.mid = mid
        self._published = False
        self._condition = Condition()
        self.rc = 0
        self._iterpos = 0

    def __str__(self):
        return str((self.rc, self.mid))

    def __iter__(self):
        self._iterpos = 0
        return self

    def __next__(self):
        return self.next()

    def next(self):
        if self._iterpos == 0:
            self._iterpos = 1
            return self.rc
        elif self._iterpos == 1:
            self._iterpos = 2
            return self.mid
        else:
            raise StopIteration

    def __getitem__(self, index):
        if index == 0:
            return self.rc
        elif index == 1:
            return self.mid
        else:
            raise IndexError("index out of range")

    async def _set_as_published(self):
        with self._condition:
            self._published = True
            self._condition.notify()

    async def wait_for_publish(self):
        """Block until the message associated with this object is published."""
        if self.rc == MQTT_ERR_QUEUE_SIZE:
            raise ValueError('Message is not queued due to ERR_QUEUE_SIZE')
        with self._condition:
            while not self._published:
                self._condition.wait()

    async def is_published(self):
        """Returns True if the message associated with this object has been
        published, else returns False."""
        if self.rc == MQTT_ERR_QUEUE_SIZE:
            raise ValueError('Message is not queued due to ERR_QUEUE_SIZE')
        with self._condition:
            return self._published


##############################
class MQTTMessage(object):
    """ This is a class that describes an incoming or outgoing message. It is
    passed to the _on_message callback as the message parameter.
    Members:
    topic : String/bytes. topic that the message was published on.
    payload : String/bytes the message payload.
    qos : Integer. The message Quality of Service 0, 1 or 2.
    retain : Boolean. If true, the message is a retained message and not fresh.
    mid : Integer. The message id.
    On Python 3, topic must be bytes.
    """

    __slots__ = 'timestamp', 'state', 'dup', 'mid', '_topic', 'payload', 'qos', 'retain', 'info'

    def __init__(self, mid=0, topic=b""):
        self.timestamp = time_func()
        self.state = mqtt_ms_invalid
        self.dup = False
        self.mid = mid
        self._topic = topic
        self.payload = b""
        self.qos = 0
        self.retain = False
        self.info = MQTTMessageInfo(mid)

    def __eq__(self, other):
        """Override the default Equals behavior"""
        if isinstance(other, self.__class__):
            return self.mid == other.mid
        return False

    def __ne__(self, other):
        """Define a non-equality test"""
        return not self.__eq__(other)

    @property
    def topic(self):
        return self._topic.decode('utf-8')

    @topic.setter
    def topic(self, value):
        self._topic = value


#####################
