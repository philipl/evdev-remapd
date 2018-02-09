#!/usr/bin/env python3
#
# Copyright (c) 2017 Philip Langdale
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import argparse
import asyncio
from concurrent.futures import CancelledError
import functools
from pathlib import Path
import signal
import os
import pwd


import daemon
import evdev
from evdev import InputDevice, UInput, categorize, ecodes
from xdg import BaseDirectory
import yaml


@asyncio.coroutine
def handle_events(input, output, remappings, user, commands, passunmapped):
    while True:
        events = yield from input.async_read()  # noqa
        for event in events:
            mapped = False

            if remappings is not None and \
               event.type == ecodes.EV_KEY and \
               event.code in remappings:
                mapped = True
                remap_event(output, event, remappings)

            if commands is not None and \
               event.type == ecodes.EV_KEY and \
               event.code in commands:
                mapped = True
                if event.value == evdev.events.KeyEvent.key_down or\
                   event.value == evdev.events.KeyEvent.key_hold:
                 execute_command(event, user, commands)

            if passunmapped and not mapped:
                output.write_event(event)
                output.syn()


def remap_event(output, event, remappings):
    for code in remappings[event.code]:
        event.code = code
        output.write_event(event)
    output.syn()


def execute_command(event, user, commands):
    for command in commands[event.code]:
        newpid = os.fork()
        if newpid == 0:
            if user is not None:
                gid = pwd.getpwnam(user).pw_gid
                uid = pwd.getpwnam(user).pw_uid
                #group id needs to be set before the user id,
                #otherwise the permissions are dropped too soon
                os.setgid(gid)
                os.setuid(uid)
            os.execl('/bin/sh', '/bin/sh', '-c', command)


def load_config(config_override):
    conf_path = None
    if config_override is None:
        for dir in BaseDirectory.load_config_paths('evdevremapkeys'):
            conf_path = Path(dir) / 'config.yaml'
            if conf_path.is_file():
                break
        if conf_path is None:
            raise NameError('No config.yaml found')
    else:
        conf_path = Path(config_override)
        if not conf_path.is_file():
            raise NameError('Cannot open %s' % config_override)

    with open(conf_path.as_posix(), 'r') as fd:
        config = yaml.safe_load(fd)
        for device in config['devices']:
            if 'remappings' in device:
                device['remappings'] = resolve_ecodes_remappings(device['remappings'])
            if 'commands' in device:
                device['commands'] = resolve_ecodes_commands(device['commands'])

    return config


def resolve_ecodes_remappings(by_name):
    by_id = {}
    if by_name is not None:
        for key, values in by_name.items():
            by_id[ecodes.ecodes[key]] = [ecodes.ecodes[value] for value in values]
    return by_id


def resolve_ecodes_commands(by_name):
    by_id = {}
    if by_name is not None:
        for key, values in by_name.items():
            by_id[ecodes.ecodes[key]] = values;
    return by_id


def find_input(device):
    name = device.get('input_name', None);
    phys = device.get('input_phys', None);
    fn = device.get('input_fn', None);

    if name is None and phys is None and fn is None:
        raise NameError('Devices must be identified by at least one of "input_name", "input_phys", or "input_fn"');

    devices = [InputDevice(fn) for fn in evdev.list_devices()];
    for input in devices:
        if name != None and input.name != name:
            continue
        if phys != None and input.phys != phys:
            continue
        if fn != None and input.fn != fn:
            continue
        return input
    return None


def register_device(device):
    input = find_input(device)
    if input is None:
        raise NameError("Can't find input device")
    input.grab()

    caps = input.capabilities()
    # EV_SYN is automatically added to uinput devices
    del caps[ecodes.EV_SYN]

    remappings = device.get('remappings', None);
    user = device.get('command_user', None);
    commands = device.get('commands', None);
    passunmapped = device.get('pass_unmapped', True);

    extended = set(caps[ecodes.EV_KEY])
    [extended.update(keys) for keys in remappings.values()]
    caps[ecodes.EV_KEY] = list(extended)

    output = UInput(caps, name=device['output_name'])

    asyncio.ensure_future(handle_events(input, output, remappings, user, commands, passunmapped))


@asyncio.coroutine
def shutdown(loop):
    tasks = [task for task in asyncio.Task.all_tasks() if task is not
             asyncio.tasks.Task.current_task()]
    list(map(lambda task: task.cancel(), tasks))
    results = yield from asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


def run_loop(args):
    config = load_config(args.config_file)
    for device in config['devices']:
        register_device(device)

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM,
                            functools.partial(asyncio.ensure_future,
                                              shutdown(loop)))

    #prevent defunct processes
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.run_until_complete(asyncio.ensure_future(shutdown(loop)))
    finally:
        loop.close()


def list_devices():
    devices = [InputDevice(fn) for fn in evdev.list_devices()];
    for device in reversed(devices):
        print('%s:\t"%s" | "%s"' % (device.fn, device.phys, device.name))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Re-bind keys for input devices')
    parser.add_argument('-d', '--daemon',
                        help='Run as a daemon', action='store_true')
    parser.add_argument('-f', '--config-file',
                        help='Config file that overrides default location')
    parser.add_argument('-l', '--list-devices', action='store_true',
                        help='List input devices by name and physical address')
    args = parser.parse_args()
    if args.list_devices:
        list_devices()
    elif args.daemon:
        with daemon.DaemonContext():
            run_loop(args)
    else:
        run_loop(args)
