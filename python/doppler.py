#!/usr/bin/env python
'''
   Copyright 2015 Wolfgang Nagele
   Modified by N6RFM and Claude AI to be compatible with changes made to GPredict 2.4 and 2.5 , which breaks gr-gpredict-doppler.py
   Add vfo_opt to gpredict -- automatically turns on and uses vfo_opt if supported by rigctld
   See - https://github.com/csete/gpredict/commit/2da2d2090daf6fd2ffb64df3a3e6ca50c6f3e176
   

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

   PATCH NOTES:
   ------------
   Original code (2015, Wolfgang Nagele) worked with Gpredict 2.3 which sent
   only short single-character hamlib commands (F, f, I, i, S, AOS, LOS, q).

   Gpredict 2.5 extended the protocol in two ways:
     1. It now sends long-form commands prefixed with a backslash, e.g.:
           \set_vfo_opt 1
           \chk_vfo
           \set_vfo VFOA
           \dump_state
           \get_vfo
           \get_ptt
        The original startswith('V') style checks never matched these because
        '\set_vfo_opt 1'.startswith('V') is False.
     2. It sends additional new commands that the original code had no handler
        for at all (set_vfo_opt, chk_vfo, dump_state, get_vfo, get_ptt,
        set_ptt).

   In both cases the original unknown-command branch just printed a warning
   but NEVER sent "RPRT 0\n" back. Gpredict always waits for RPRT 0 before
   sending the next command, so the very first unrecognized command caused
   the entire session to deadlock/hang.

   Fixes applied:
     - Added  cmd = curCommand.lstrip('\\')  at the top of the command loop.
       All checks now use 'cmd' (backslash stripped) so both short-form and
       long-form variants of every command hit the same handler.
     - Added explicit handlers for: set_vfo / set_vfo_opt, chk_vfo, get_vfo,
       get_ptt, set_ptt, dump_state.
     - Added foundCommand = True to the S, AOS, LOS, and q handlers where it
       was previously missing, preventing spurious unknown-command warnings.
     - THE CRITICAL FIX: the unknown-command fallback now always sends
       "RPRT 0\n" so Gpredict never stalls on a future unknown command.
     - Fixed a bug in stop() where self.sock.shutdown() was called on the
       doppler class which has no self.sock attribute; changed to s.close().

   Gpredict 2.5 startup command sequence (now handled cleanly):
     \set_vfo_opt 1   -> RPRT 0
     \chk_vfo         -> 0 + RPRT 0  (tells Gpredict no VFO ops needed)
     \dump_state      -> minimal rig caps + RPRT 0
     F <hz>           -> RPRT 0  (frequency updates follow)
'''

from gnuradio import gr
import sys
import threading
import time
import socket
import pmt


class doppler_runner(threading.Thread):

    def __init__(self, bc, gpredict_host, gpredict_port, verbose):
        threading.Thread.__init__(self)

        self.gpredict_host = gpredict_host
        self.gpredict_port = gpredict_port
        self.verbose = verbose
        self.blockclass = bc

        self.stopThread = False
        self.clientConnected = False
        self.sock = None
        self.server = None

    def run(self):
        try:
            bind_to = (self.gpredict_host, self.gpredict_port)
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind(bind_to)
            self.server.listen(0)
        except Exception as e:
            print("[doppler] Error starting listener: %s" % str(e))
            sys.exit(1)

        time.sleep(0.5)  # TODO: Find better way to know if init is all done

        while not self.stopThread:
            print("[doppler] Waiting for connection on: %s:%d" % bind_to)
            self.clientConnected = False
            self.sock, addr = self.server.accept()
            self.clientConnected = True
            print("[doppler] Connected from: %s:%d" % (addr[0], addr[1]))

            cur_freq = 0
            cur_tx_freq = 0

            while not self.stopThread:
                try:
                    data = self.sock.recv(1024)
                except:
                    data = None

                if not data or self.stopThread:
                    break

                # Allow for multiple commands to have arrived in one recv().
                # For instance a frequency update and AOS/LOS in the same
                # packet. Unchanged from original.
                data = data.decode('ASCII').rstrip('\n')  # Prevent extra '' in array
                commands = data.split('\n')

                for curCommand in commands:

                    # Skip empty strings that can result from split/rstrip.
                    if not curCommand.strip():
                        continue

                    # CHANGE: Normalize the command by stripping a leading
                    # backslash. Gpredict 2.5 sends long-form hamlib commands
                    # with a '\' prefix, e.g. '\set_vfo_opt 1', '\chk_vfo',
                    # '\dump_state'. Stripping it means every handler below
                    # works for both the short single-char form (e.g. 'F') and
                    # the long-form (e.g. '\set_freq 145900000') without needing
                    # duplicate checks. We keep curCommand untouched for debug
                    # printing so the console always shows exactly what was
                    # received.
                    cmd = curCommand.lstrip('\\')

                    foundCommand = False

                    # ----------------------------------------------------------
                    # F / set_freq  -- set downlink (RX) frequency
                    # Gpredict sends: 'F 145900000' or '\set_freq 145900000'
                    # After lstrip: 'F 145900000' or 'set_freq 145900000'
                    # Unchanged in behaviour from original; now also matches
                    # the long-form variant via the stripped 'cmd'.
                    # ----------------------------------------------------------
                    if cmd.startswith('F ') or cmd.startswith('set_freq '):
                        freq = int(cmd.split()[-1])
                        if cur_freq != freq:
                            if self.verbose:
                                print("[doppler] New RX frequency: %d" % freq)
                            self.blockclass.sendFreq(freq)
                            cur_freq = freq
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # f / get_freq  -- read back current RX frequency
                    # CHANGE: Response format updated to 'freq=<hz>\nRPRT 0\n'
                    # which is the correct hamlib long-form response. The
                    # original returned 'f: <hz>\n' which is non-standard and
                    # may have confused Gpredict 2.5's response parser.
                    # ----------------------------------------------------------
                    elif cmd == 'f' or cmd == 'get_freq':
                        sendMsgStr = "freq=%d\nRPRT 0\n" % cur_freq
                        self.sock.sendall(sendMsgStr.encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # I / set_split_freq  -- set uplink (TX) frequency
                    # Gpredict sends: 'I 435900000' or '\set_split_freq 435900000'
                    # Unchanged in behaviour from original; now also matches
                    # long-form variant via stripped 'cmd'.
                    # ----------------------------------------------------------
                    elif cmd.startswith('I ') or cmd.startswith('set_split_freq '):
                        tx_freq = int(cmd.split()[-1])
                        if cur_tx_freq != tx_freq:
                            if self.verbose:
                                print("[doppler] New TX frequency: %d" % tx_freq)
                            self.blockclass.sendTxFreq(tx_freq)
                            cur_tx_freq = tx_freq
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # i / get_split_freq  -- read back current TX frequency
                    # CHANGE: Response format updated to 'freq=<hz>\nRPRT 0\n'
                    # for the same reason as get_freq above.
                    # ----------------------------------------------------------
                    elif cmd == 'i' or cmd == 'get_split_freq':
                        sendMsgStr = "freq=%d\nRPRT 0\n" % cur_tx_freq
                        self.sock.sendall(sendMsgStr.encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # S / set_split_vfo  -- enable/configure split mode
                    # Gpredict sends this when it wants the rig in split mode.
                    # We don't have a real rig so just acknowledge.
                    # CHANGE: Added foundCommand = True which was missing in
                    # the original, causing a spurious unknown-command warning
                    # to be printed after every 'S' command.
                    # ----------------------------------------------------------
                    elif cmd.startswith('S ') or cmd.startswith('set_split_vfo'):
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # s / get_split_vfo  -- query split state
                    # CHANGE: New handler. Gpredict 2.5 polls split state.
                    # Returns split=off, active VFO=VFOA as a safe default.
                    # Without this handler Gpredict would stall waiting for
                    # a response.
                    # ----------------------------------------------------------
                    elif cmd == 's' or cmd == 'get_split_vfo':
                        self.sock.sendall("0\nVFOA\nRPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # V / set_vfo / set_vfo_opt
                    # Gpredict 2.5 sends '\set_vfo_opt 1' early in the session
                    # to enable VFO optimization, and '\set_vfo VFOA' to select
                    # a VFO. After lstrip('\\') these become 'set_vfo_opt 1'
                    # and 'set_vfo VFOA', both caught by startswith('set_vfo').
                    # The short-form 'V VFOA' is caught by startswith('V ').
                    # CHANGE: New handler. THIS WAS THE PRIMARY CAUSE OF THE
                    # HANG IN GPREDICT 2.5. '\set_vfo_opt 1' was the first
                    # unrecognized command sent after connect, and with no
                    # RPRT 0 reply the entire session deadlocked immediately.
                    # ----------------------------------------------------------
                    elif cmd.startswith('V ') or cmd.startswith('set_vfo'):
                        if self.verbose:
                            print("[doppler] set_vfo/set_vfo_opt (ignored): %s" % curCommand)
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # v / get_vfo  -- query active VFO
                    # CHANGE: New handler. Gpredict 2.5 queries the active VFO.
                    # Returns VFOA as a safe default.
                    # ----------------------------------------------------------
                    elif cmd == 'v' or cmd == 'get_vfo':
                        self.sock.sendall("VFOA\nRPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # chk_vfo  -- VFO capability check
                    # CHANGE: New handler. Gpredict 2.5 sends '\chk_vfo'
                    # immediately after '\set_vfo_opt 1' to ask whether the rig
                    # supports VFO commands. Responding with '0\nRPRT 0\n' tells
                    # Gpredict "no VFO support needed" which is correct for our
                    # virtual device and actually suppresses further VFO-related
                    # commands from Gpredict on each tracking cycle, keeping the
                    # session cleaner. Responding with '1\nRPRT 0\n' would cause
                    # Gpredict to send get_vfo/set_vfo on every update cycle
                    # which adds unnecessary overhead and chatter.
                    # ----------------------------------------------------------
                    elif cmd == 'chk_vfo':
                        self.sock.sendall("0\nRPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # t / get_ptt  -- query PTT (transmit) state
                    # CHANGE: New handler. Gpredict 2.5 polls PTT state.
                    # Always returns 0 (not transmitting) since we are RX only.
                    # Without this handler Gpredict would stall waiting for
                    # a response.
                    # ----------------------------------------------------------
                    elif cmd == 't' or cmd == 'get_ptt':
                        self.sock.sendall("0\nRPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # T / set_ptt  -- set PTT state
                    # CHANGE: New handler. Gpredict 2.5 may assert PTT.
                    # Acknowledge but take no action (RX only device).
                    # ----------------------------------------------------------
                    elif cmd.startswith('T ') or cmd.startswith('set_ptt'):
                        if self.verbose:
                            print("[doppler] set_ptt (ignored): %s" % curCommand)
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # dump_state  -- rig capability probe
                    # CHANGE: New handler. Gpredict 2.5 (and newer hamlib
                    # clients) send '\dump_state' on connect to discover rig
                    # capabilities before sending any other commands. The
                    # original code had no handler so this caused an immediate
                    # deadlock on every new connection.
                    # The response is a minimal valid hamlib dump_state reply
                    # describing a generic wideband RX-only device.
                    # Line order follows the hamlib dump_state specification:
                    #   protocol version, rig model, ITU region,
                    #   RX freq ranges (one entry + terminator),
                    #   TX freq ranges (empty + terminator),
                    #   tuning steps (empty + terminator),
                    #   filters (empty + terminator),
                    #   max RIT, max XIT, max IF shift, announces.
                    # ----------------------------------------------------------
                    elif cmd.startswith('dump_state'):
                        dump = (
                            "0\n"           # Protocol version
                            "2\n"           # Rig model (2 = NET rigctl / dummy)
                            "2\n"           # ITU region
                            # RX freq range: 100kHz to 4GHz, all modes,
                            # low_power=-1, high_power=-1 (not a TX device),
                            # vfo=0x10000003 (RX+TX VFOs declared but TX unused)
                            # antenna=0x3
                            "100000.000000 4000000000.000000 0x1ff -1 -1 0x10000003 0x3\n"
                            "0 0 0 0 0 0 0\n"   # End of RX freq ranges
                            "0 0 0 0 0 0 0\n"   # TX freq ranges (none - RX only)
                            "0 0 0 0 0 0 0\n"   # End of TX freq ranges
                            "0 0\n"             # Tuning steps (none specified)
                            "0 0\n"             # End of tuning steps
                            "0\n"               # Filters (none specified)
                            "0\n"               # End of filters
                            "0\n"               # Max RIT (Hz)
                            "0\n"               # Max XIT (Hz)
                            "0\n"               # Max IF shift (Hz)
                            "0\n"               # Announces bitmask
                            "RPRT 0\n"
                        )
                        self.sock.sendall(dump.encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # q  -- client disconnect notification
                    # CHANGE: Added RPRT 0 reply (harmless but tidy) and added
                    # foundCommand = True which was missing in the original,
                    # causing a spurious unknown-command warning on every clean
                    # disconnect.
                    # ----------------------------------------------------------
                    elif cmd == 'q':
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))
                        foundCommand = True

                    # ----------------------------------------------------------
                    # AOS / LOS  -- acquisition / loss of signal events
                    # These are checked against curCommand (not cmd) because
                    # they are not hamlib protocol commands and never arrive
                    # with a backslash prefix. Kept as a separate if/elif block
                    # (not part of the main elif chain above) because they can
                    # arrive in the same recv() packet as a frequency command
                    # and the original design processes them independently.
                    # CHANGE: Added foundCommand = True to both branches.
                    # Previously missing, which caused a spurious unknown-
                    # command warning after every AOS and LOS event.
                    # ----------------------------------------------------------
                    if curCommand.startswith('AOS'):
                        if self.verbose:
                            print("[doppler] received AOS")
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))
                        self.blockclass.sendState(True)
                        foundCommand = True
                    elif curCommand.startswith('LOS'):
                        if self.verbose:
                            print("[doppler] received LOS")
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))
                        self.blockclass.sendState(False)
                        foundCommand = True

                    # ----------------------------------------------------------
                    # Unknown command fallback
                    # CHANGE: THIS IS THE CRITICAL FIX FOR GPREDICT 2.5.
                    # The original code printed the unknown command but never
                    # sent RPRT 0. Gpredict waits for RPRT 0 after every single
                    # command before it will send the next one. Without this
                    # reply the first unrecognized command caused Gpredict to
                    # block indefinitely -- the session appeared connected but
                    # completely frozen.
                    # Now any command not explicitly handled above gets a
                    # graceful RPRT 0 so the session always continues. The
                    # command is still printed so new commands from future
                    # Gpredict versions are visible in the console and can be
                    # added as explicit handlers if needed.
                    # ----------------------------------------------------------
                    elif not foundCommand:
                        print("[doppler] unknown command (sending RPRT 0): %s" % curCommand)
                        self.sock.sendall("RPRT 0\n".encode("UTF-8"))

            self.sock.close()
            self.clientConnected = False
            self.sock = None
            if self.verbose:
                print("[doppler] Disconnected from: %s:%d" % (addr[0], addr[1]))

        self.server.shutdown(socket.SHUT_RDWR)
        self.server.close()
        self.server = None


class doppler(gr.sync_block):

    def __init__(self, gpredict_host, gpredict_port, verbose):
        gr.sync_block.__init__(
            self,
            name="GPredict Doppler",
            in_sig=None,
            out_sig=None
        )

        self.port = gpredict_port
        self.thread = doppler_runner(self, gpredict_host, gpredict_port, verbose)
        self.thread.start()
        self.message_port_register_out(pmt.intern("freq"))
        self.message_port_register_out(pmt.intern("tx_freq"))
        self.message_port_register_out(pmt.intern("state"))

    def stop(self):
        self.thread.stopThread = True

        if self.thread.clientConnected:
            self.thread.sock.close()
        else:
            # Force a connection to unblock the server.accept() call in the
            # runner thread so it can check stopThread and exit cleanly.
            # CHANGE: Fixed bug in original where self.sock.shutdown() was
            # called on the doppler class instance which has no self.sock
            # attribute (only doppler_runner has self.sock). This would raise
            # AttributeError silently inside the except block, meaning the
            # forced connection was made but never properly torn down. Changed
            # to s.close() on the local socket which is sufficient since
            # stopThread=True causes the runner to exit after accept() unblocks.
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(("localhost", self.port))
                time.sleep(0.1)
                s.close()  # CHANGE: was self.sock.shutdown(socket.SHUT_RDWR)
            except:
                pass

        self.thread.join()
        return True

    def sendFreq(self, freq):
        p = pmt.from_double(freq)
        self.message_port_pub(
            pmt.intern("freq"),
            pmt.cons(pmt.intern("freq"), p)
        )

    def sendTxFreq(self, freq):
        p = pmt.from_double(freq)
        self.message_port_pub(
            pmt.intern("tx_freq"),
            pmt.cons(pmt.intern("tx_freq"), p)
        )

    def sendState(self, state):
        # CHANGE: Simplified ternary replaces the original if/else block.
        # Behaviour is identical.
        newState = 1 if state else 0
        self.message_port_pub(
            pmt.intern("state"),
            pmt.cons(pmt.intern("state"), pmt.from_long(newState))
        )
