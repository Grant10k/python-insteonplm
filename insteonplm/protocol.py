"""Module to maintain PLM state information and network interface."""
import asyncio
import logging
import binascii
import collections

from .ipdb import IPDB
from .plm import Address, PLMProtocol, Message

__all__ = ('PLM', 'ALDB')

PP = PLMProtocol()

class ALDB(object):
    ipdb = IPDB()

    def __init__(self):
        self._devices = {}
        self._cb_new_device = []
        self._cb_status = []
        self.state = 'empty'
        self.log = logging.getLogger(__name__)

    def __len__(self):
        return len(self._devices)

    def __iter__(self):
        for x in self._devices.keys():
            yield x

    def __getitem__(self, address):
        if address in self._devices:
            return self._devices[address]
        raise KeyError

    def __setitem__(self, key, value):
        if not 'cat' in value or value['cat'] == 0:
            self.log.debug('Ignoring device setitem with no cat: %s', value)
            return

        if key in self._devices:
            if 'firmware' in value and value['firmware'] < 255:
                self._devices[key] = value
        else:
            productdata = self.ipdb[value['cat'], value['subcat']]
            value.update(productdata._asdict())
            value['address_hex'] = key
            value['address'] = Address(key).human
            self._devices[key] = value

            self.log.info('New INSTEON Device %r: %s (%02x:%02x)',
                          Address(key), value['description'], value['cat'], value['subcat'])

            for cb, criteria in self._cb_new_device:
                if self._device_matches_criteria(value,criteria):
                    cb(value)

    def add_device_callback(self, callback, criteria):
        self.log.warn('New callback %s with %s (%d items already in list)', callback, criteria, len(self._devices.keys()))
        self._cb_new_device.append([callback, criteria])
        for d in self:
            value = self[d]
            if self._device_matches_criteria(value,criteria):
                self.log.info('retroactive callback for device %s matching %s', value['address'], criteria)
                callback(value)


    def status_update_callback(self, callback, criteria):
        self.log.warn('Status callback %s', callback, criteria)
        self._cb_status.append([callback, criteria])

    def setattr(self, key, attr, value):
        key = Address(key).hex

        self.log.debug('setattr called with %s %s %s', key, attr, value)

        if key in self._devices:
            oldvalue = None
            if attr in self._devices[key]:
                oldvalue = self._devices[key][attr]
            self._devices[key][attr] = value
            if value != oldvalue:
                self.log.info('Device %s.%s changed: %s->%s"',
                              key, attr, oldvalue, value)
                return True
            else:
                self.log.info('Device %s.%s unchanged: %s->%s"',
                              key, attr, oldvalue, value)
                return False
        else:
            raise KeyError

    def _device_matches_criteria(self, device, criteria):
        match = True

        if 'address' in criteria:
            criteria['address'] = Address(criteria['address'])

        for key in criteria.keys():
            if key == 'capability':
                if criteria[key] not in device['capabilities']:
                    self.log.debug('device does not advertise capability %s', key)
                    match = False
                    break
            elif key[0] != '_':
                if key not in device:
                    self.log.debug('key %s from criteria is not in device', key)
                    match = False
                    break
                elif criteria[key] != device[key]:
                    self.log.debug('key %s from criteria does not match: %r/%r', key, criteria[key], device[key])
                    match = False
                    break

        if match is True:
            self.log.debug('I found what I was waiting for')
        else:
            self.log.debug('device did not match criteria')

        return match


# In Python 3.4.4, `async` was renamed to `ensure_future`.
try:
    ensure_future = asyncio.ensure_future
except AttributeError:
    ensure_future = asyncio.async


# pylint: disable=too-many-instance-attributes, too-many-public-methods
class PLM(asyncio.Protocol):
    """The Insteon PLM IP control protocol handler."""

    def __init__(self, loop=None, connection_lost_callback=None):
        """Protocol handler that handles all status and changes on PLM.

        This class is expected to be wrapped inside a Connection class object
        which will maintain the socket and handle auto-reconnects.

            :param update_callback:
                called if any state information changes in device (optional)
            :param connection_lost_callback:
                called when connection is lost to device (optional)
            :param loop:
                asyncio event loop (optional)

            :type update_callback:
                callable
            :type: connection_lost_callback:
                callable
            :type loop:
                asyncio.loop
        """
        self._loop = loop

        self._connection_lost_callback = connection_lost_callback
        self._update_callbacks = []
        self._message_callbacks = []

        self._buffer = bytearray()
        self._last_command = None
        self._wait_for = {}
        self._recv_queue = []
        self._send_queue = []

        self.devices = ALDB()

        self.log = logging.getLogger(__name__)
        self.transport = None

        self.add_message_callback(self._parse_insteon_standard, dict(code=0x50))
        self.add_message_callback(self._parse_insteon_extended, dict(code=0x51))
        self.add_message_callback(self._parse_all_link_completed, dict(code=0x53))
        self.add_message_callback(self._parse_button_event, dict(code=0x54))
        self.add_message_callback(self._parse_all_link_record, dict(code=0x57))
        self.add_message_callback(self._parse_get_plm_info, dict(code=0x60))
        self.add_message_callback(self._parse_get_plm_config, dict(code=0x73))

    #
    # asyncio network functions
    #

    def connection_made(self, transport):
        """Called when asyncio.Protocol establishes the network connection."""
        self.log.info('Connection established to PLM')
        self.transport = transport

        self.transport.set_write_buffer_limits(128)
        limit = self.transport.get_write_buffer_size()
        self.log.debug('Write buffer size is %d', limit)
        self.load_all_link_database()

    def data_received(self, data):
        """Called when asyncio.Protocol detects received data from network."""
        self.log.debug('Received %d bytes from PLM: %s',
                       len(data), binascii.hexlify(data))

        self._buffer.extend(data)
        self._strip_messages_off_front_of_buffer()

        for message in self._recv_queue:
            self._process_message(message)
            self._recv_queue.remove(message)

    def connection_lost(self, exc):
        """Called when asyncio.Protocol loses the network connection."""
        if exc is None:
            self.log.warning('eof from modem?')
        else:
            self.log.warning('Lost connection to modem: %s', exc)

        self.transport = None

        if self._connection_lost_callback:
            self._connection_lost_callback()

    def _rsize(self, message):
        code = message[1]
        ppc = PP.lookup(code, fullmessage=message)

        if hasattr(ppc, 'rsize') and ppc.rsize:
            self.log.debug('Found a code 0x%x message which returns %d bytes',
                           code, ppc.rsize)
            return ppc.rsize
        else:
            self.log.debug('Unable to find an rsize for code 0x%x', code)
            return len(message) + 1

    def _timeout_reached(self):
        self.log.debug('timeout_reached invoked')
        self._clear_wait()
        self._process_queue()

    def _clear_wait(self):
        self.log.debug('clear_wait invoked')
        if '_thandle' in self._wait_for:
            self.log.debug('Cancelling wait_for timeout callback')
            self._wait_for['_thandle'].cancel()
        self._wait_for = {}

    def _schedule_wait(self, keys, timeout=2):
        self.log.debug('setting wait_for to %s timeout %d', keys, timeout)
        if self._wait_for != {}:
            self.log.warn('Overwriting stale wait_for: %s', self._wait_for)
            self._clear_wait()

        if timeout > 0:
            self.log.debug('Set timeout on wait_for at %d seconds', timeout)
            keys['_thandle'] = self._loop.call_later(timeout,
                                                    self._timeout_reached)

        self._wait_for = keys

    def _wait_for_last_command(self):
        sm = self._last_command
        rsize = self._rsize(sm)
        self.log.debug('Wait for ACK/NAK on sent: %s expecting rsize of %d',
                       binascii.hexlify(sm), rsize)
        if self._buffer.find(sm) == 0:
            if len(self._buffer) < rsize:
                self.log.debug('Waiting for all of message to arrive, %d/%d',
                               len(self._buffer), rsize)
                return

            code = self._buffer[1]
            message_length = len(sm)
            response_length = rsize - message_length
            response = self._buffer[message_length:response_length]
            acknak = self._buffer[rsize-1]

            mla = self._buffer[:rsize]
            buffer = self._buffer[rsize:]

            if acknak == 0x06:
                if len(response) > 0:
                    self.log.debug('Sent command %s OK with response %s',
                                   binascii.hexlify(sm), response)
                    self._recv_queue.append(mla)
                else:
                    self.log.debug('Sent command %s OK', binascii.hexlify(sm))
            else:
                if code == 0x6a:
                    self.log.info('ALL-Link database dump is complete')
                    self.devices.state = 'loaded'
                    for da in self.devices:
                        d = self.devices[da]
                        if 'cat' in d and d['cat'] > 0:
                            self.log.debug('I know the category for %s (0x%x)',
                                           da, d['cat'])
                        else:
                            self.product_data_request(da)
                    self.poll_devices()
                else:
                    self.log.warn('Sent command %s UNsuccessful! (acknak 0x%x)',
                                  binascii.hexlify(sm), acknak)
            self._last_command = None
            self._buffer = buffer

    def _wait_for_recognized_message(self):
        code = self._buffer[1]
        self.log.debug('Code is 0x%x', code)

        for c in PP:
            if c == code or c == bytes([code]):
                ppc = PP.lookup(code, fullmessage=self._buffer)

                self.log.debug('Found a code 0x%x message which is %d bytes',
                               code, ppc.size)

                if len(self._buffer) == ppc.size:
                    new_message = self._buffer[0:ppc.size]
                    self.log.debug('new message is: %s',
                                   binascii.hexlify(new_message))
                    self._recv_queue.append(new_message)
                    self._buffer = self._buffer[ppc.size:]
                else:
                    self.log.debug('Need more bytes to process message.')

    def _strip_messages_off_front_of_buffer(self):
        lastlooplen = 0
        worktodo = True

        while worktodo:
            if len(self._buffer) == 0:
                self.log.debug('Clean break!  There is no buffer left')
                worktodo = False
                break

            if len(self._buffer) < 2:
                worktodo = False
                break

            if self._buffer[0] != 2:
                self._buffer = self._buffer[1:]
                self.log.debug('Trimming leading buffer garbage')

            if len(self._buffer) == lastlooplen:
                # Buffer size did not change so we should wait for more data
                worktodo = False
                break

            lastlooplen = len(self._buffer)

            if self._buffer.find(2) < 0:
                self.log.debug('Buffer does not contain a 2, we should bail')
                worktodo = False
                break

            if self._last_command:
                self._wait_for_last_command()
            else:
                self._wait_for_recognized_message()

        self._process_queue()

    def _process_queue(self):
        self.log.debug('processing queue with %d items', len(self._send_queue))
        if self._clear_to_send() is True:
            self.log.debug('Clear to send next command in send_queue')
            command, wait_for = self._send_queue[0]
            self._send_hex(command, wait_for=wait_for)
            self._send_queue.remove([command, wait_for])

    def _clear_to_send(self):
        if len(self._buffer) == 0:
            if len(self._send_queue) > 0:
                if self._last_command is None:
                    if self._wait_for == {}:
                        return True

    def _process_message(self, message):
        self.log.debug('Processing message: %s', binascii.hexlify(message))
        if message[0] != 2 or len(message) < 2:
            self.log.warn('process_message called with a malformed message')
            return

        code = message[1]
        self.log.debug('Code is 0x%x', code)

        callbacked = False
        for cb, criteria in self._message_callbacks:
            if self._message_matches_criteria(message, criteria):
                self.log.debug('message callback %s with criteria %s', cb, criteria)
                self._loop.call_soon(cb, message)
                callbacked = True

        if callbacked is False:
            ppc = PP.lookup(code, fullmessage=message)
            if hasattr(ppc, 'name') and ppc.name:
                self.log.warning('Unhandled event: %s (%s)', ppc.name,
                              binascii.hexlify(message))
            else:
                self.log.warning('Unrecognized event: UNKNOWN (%s)',
                              binascii.hexlify(message))

        if self._message_matches_criteria(message, self._wait_for):
            self.log.debug('clearing wait_for')
            self._clear_wait()

        self._process_queue()



    def _message_matches_criteria(self, rawmessage, criteria):
        match = True

        if 'address' in criteria:
            criteria['address'] = Address(criteria['address'])

        msg = Message(rawmessage)

        for key in criteria.keys():
            if key[0] != '_':
                self.log.debug('_mmc looking for %s', key)
                mattr = getattr(msg, key, None)
                if mattr is None:
                    self.log.debug('key %s from criteria is not in message', key)
                    match = False
                    break
                elif criteria[key] != mattr:
                    self.log.debug('key %s from criteria does not match: %r/%r', key, criteria[key], mattr)
                    match = False
                    break

        if match is True:
            self.log.debug('I found what I was waiting for')
            if '_callback' in criteria:
                criteria['_callback'](rawmessage)
        else:
            self.log.debug('message did not match criteria')

        return match

    def _parse_insteon_standard(self, rawmessage):
        msg = Message(rawmessage)

        self.log.info('INSTEON standard %r->%r: cmd1:%02x cmd2:%02x flags:%02x',
                      msg.address, msg.target,
                      msg.cmd1, msg.cmd2, msg.flagsval)

        if msg.cmd1 == 0x13 or msg.cmd1 == 0x14:
            self.log.debug('Hey You Guys')
            if self.devices.setattr(msg.address, 'onlevel', 0):
                self._do_update_callback(rawmessage)
        elif msg.cmd1 == 0x11 or msg.cmd1 == 0x12:
            self.log.debug('Hey Youse Guys')
            if self.devices.setattr(msg.address, 'onlevel', msg.cmd2):
                self._do_update_callback(rawmessage)

    def _parse_insteon_extended(self, rawmessage):
        msg = Message(rawmessage)

        self.log.info('INSTEON extended %r->%r: cmd1:%02x cmd2:%02x flags:%02x data:%s',
                      msg.address, msg.target, msg.cmd1, msg.cmd2, msg.flagsval,
                      binascii.hexlify(msg.userdata))

        if msg.cmd1 == 0x03 and msg.cmd2 == 0x00:
            self._parse_product_data_response(msg.address, msg.userdata)

    def _parse_status_response(self, rawmessage):
        msg = Message(rawmessage)
        onlevel = msg.cmd2

        self.log.info('INSTEON device status %r is at level %s',
                      msg.address, hex(onlevel))
        self.devices.setattr(msg.address, 'onlevel', onlevel)
        self._do_update_callback(rawmessage)

    def _parse_sensor_response(self, rawmessage):
        msg = Message(rawmessage)
        onlevel = msg.cmd2

        self.log.info('INSTEON device sensor %r is at level %s',
                      msg.address, hex(onlevel))
        self.devices.setattr(msg.address, 'sensorlevel', onlevel)
        self._do_update_callback(rawmessage)

    def _do_update_callback(self, rawmessage):
        for cb, criteria in self._update_callbacks:
            self.log.debug('update callback %s with criteria %s', cb, criteria)
            if self._message_matches_criteria(rawmessage, criteria):
                self._loop.call_soon(cb, rawmessage)

    def _parse_product_data_response(self, address, userdata):
        category = userdata[4]
        subcategory = userdata[5]
        firmware = userdata[6]
        self.log.info('INSTEON Product Data Response from %r: cat:%s, subcat:%s',
                      address, hex(category), hex(subcategory))

        self.devices[address.hex] = dict(cat=category, subcat=subcategory, firmware=firmware)

    def _parse_button_event(self, rawmessage):
        msg = Message(rawmessage)
        self.log.info('PLM button event: %02x (%s)', msg.event, msg.description)

    def _parse_get_plm_info(self, rawmessage):
        msg = Message(rawmessage)
        self.log.info('PLM Info from %r: category:%02x subcat:%02x firmware:%02x',
                      msg.address, msg.category, msg.subcategory, msg.firmware)

    def _parse_get_plm_config(self, rawmessage):
        msg = Message(rawmessage)
        self.log.info('PLM Config: flags:%02x spare:%02x spare:%02x',
                    msg.flagsval, msg.spare1, msg.spare2)

    def _parse_all_link_record(self, rawmessage):
        msg = Message(rawmessage)

        self.log.info('ALL-Link Record for %r: flags:%02x group:%02x data:%02x/%02x/%02x',
                      msg.address, msg.flagsval, msg.group,
                      msg.linkdata1, msg.linkdata2, msg.linkdata3)

        self.devices[msg.address.hex] = dict(cat=msg.linkdata1, subcat=msg.linkdata2, firmware=msg.linkdata3)

        if self.devices.state == 'loading':
            self.get_next_all_link_record()

    def _parse_all_link_completed(self, message):
        msg = Message(rawmessage)
        self.log.info('ALL-Link Completed for %s: group:%s cat:%s subcat:%s firmware:%s linkcode:%s',
                      device_addr.human, hex(group),
                      hex(category), hex(subcategory), hex(firmware),
                      hex(linkcode))

    def _queue_hex(self, message, wait_for={}):
        self.log.debug('Adding command to queue: %s', message)
        self._send_queue.append([message, wait_for])

    def _send_hex(self, message, wait_for={}):
        if self._last_command or self._wait_for:
            self.log.debug('Still waiting on last_command.')
            self._queue_hex(message, wait_for)
        else:
            self._send_raw(binascii.unhexlify(message))
            self._schedule_wait(wait_for)

    def _send_raw(self, message):
        self.log.debug('Sending %d byte message: %s', len(message), binascii.hexlify(message))
        self.transport.write(message)
        self._last_command = message

    def add_message_callback(self, callback, criteria):
        self._message_callbacks.append([callback, criteria])
        self.log.debug('Added message callback to %s on %s', callback, criteria)

    def add_update_callback(self, callback, criteria):
        self._update_callbacks.append([callback, criteria])
        self.log.debug('Added update callback to %s on %s', callback, criteria)

    def add_device_callback(self, callback, criteria):
        self.devices.add_device_callback(callback, criteria)

    def send_insteon_standard(self, device, cmd1, cmd2, wait_for={}):
        """Send an INSTEON Standard message to the PLM."""
        device = Address(device)
        rawstr = '0262'+device.hex+'00'+cmd1+cmd2
        self._send_hex(rawstr, wait_for)

    def send_insteon_extended(self, device, cmd1, cmd2, wait_for={}):
        """Send an INSTEON Extended message to the PLM."""
        device = Address(device)
        rawstr = '0262'+device.hex+'00'+cmd1+cmd2
        self._send_hex(rawstr, wait_for)

    def get_plm_info(self):
        """Request PLM Info."""
        self.log.info('Requesting PLM Info')
        self._send_hex('0260')

    def get_plm_config(self):
        """Request PLM Config."""
        self.log.info('Requesting PLM Config')
        self._send_hex('0273')

    def get_first_all_link_record(self):
        """Request first ALL-Link record."""
        self.log.info('Requesting First ALL-Link Record')
        self._send_hex('0269', wait_for={'code': 0x57})

    def get_next_all_link_record(self):
        """Request next ALL-Link record."""
        self.log.info('Requesting Next ALL-Link Record')
        self._send_hex('026a', wait_for={'code': 0x57})

    def load_all_link_database(self):
        """Load the ALL-Link Database into object."""
        self.devices.state = 'loading'
        self.get_first_all_link_record()

    def product_data_request(self, addr):
        """Request Product Data Record for device."""
        device = Address(addr)
        self.log.info('Requesting product data for %s', device.human)
        self.send_insteon_standard(
            device, '03', '00',
            wait_for={'code': 0x51, 'cmd1': 0x03, 'cmd2': 0x00})

    def text_string_request(self, addr):
        """Request Device Text String."""
        device = Address(addr)
        self.log.info('Requesting text string for %s', device.human)
        self.send_insteon_standard(
            device, '03', '02',
            wait_for={'code': 0x51, 'cmd1': 0x03, 'cmd2': 0x02})

    def status_request(self, addr, type='main'):
        """Request Device Status."""
        device = Address(addr)
        self.log.info('Requesting status for %s', device.human)
        self.send_insteon_standard(
            device, '19', '00',
            wait_for={'code': 0x50, '_callback': self._parse_status_response})

    def sensor_request(self, addr):
        """Request Device Status."""
        device = Address(addr)
        self.log.info('Requesting sensor status for %s', device.human)
        self.send_insteon_standard(
            device, '19', '01',
            wait_for={'code': 0x50, '_callback': self._parse_sensor_response})

    def get_device_attr(self, addr, attr):
        address = Address(addr)
        device = self.devices[address.hex]
        if attr in device:
            self.log.debug('Device attr %s from %r: %r', attr, address, device[attr])
            return device[attr]
        else:
            self.log.warning('Device attr %s from %r: NOTFOUND (%r)', attr, address, device)

    def turn_off(self, addr):
        device = Address(addr)
        self.send_insteon_standard(device,'13','00')

    def turn_on(self, addr, brightness=255):
        device = Address(addr)
        bhex = str.format('{:02X}', int(brightness)).lower()
        self.send_insteon_standard(device,'11',bhex)

    def poll_devices(self):
        for d in self.devices:
            device = self.devices[d]
            self.status_request(d)
            if 'binary_sensor' in device['capabilities']:
                self.log.info('this is a sensor device making supplemental request')
                self.sensor_request(d)

    def list_devices(self):
        for d in self.devices:
            dev = self.devices[d]
            print(d,':',dev)
