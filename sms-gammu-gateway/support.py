"""
SMS Gammu Gateway - Support functions
Gammu integration functions for SMS operations and state machine management

Based on: https://github.com/pajikos/sms-gammu-gateway
Licensed under Apache License 2.0
"""

import sys
import os
import stat
import time
import logging
import gammu

# Modem initialization retry policy (bounded - hardware errors must not be hidden)
INIT_ATTEMPTS = 3
INIT_RETRY_DELAY = 5  # seconds between attempts

# Errors that may be transient right after boot / power-up of the modem.
# Everything else (e.g. device open errors) fails immediately.
_TRANSIENT_ERRORS = (gammu.ERR_TIMEOUT, gammu.ERR_DEVICEREADERROR)


def _describe_device_permissions(device_path):
    """Return a human friendly description of device permissions/owner."""
    try:
        info = os.stat(device_path)
    except FileNotFoundError:
        return f"⚠️ Device {device_path} not found"
    except Exception as e:
        return f"⚠️ Could not stat {device_path}: {e}"

    mode = info.st_mode
    parts = [f"Permissions: {stat.filemode(mode)}"]
    try:
        import pwd
        import grp
        try:
            owner = pwd.getpwuid(info.st_uid).pw_name
        except KeyError:
            owner = str(info.st_uid)
        try:
            group = grp.getgrgid(info.st_gid).gr_name
        except KeyError:
            group = str(info.st_gid)
        parts.append(f"Owner: {owner}:{group}")
    except ImportError:
        parts.append(f"Owner: {info.st_uid}:{info.st_gid}")

    if stat.S_ISCHR(mode) and hasattr(os, "major"):
        parts.append(f"Major:Minor = {os.major(info.st_rdev)}:{os.minor(info.st_rdev)}")
    else:
        parts.append("⚠️ not a character device")
    return " | ".join(parts)


def log_device_diagnostics(device_path):
    """Log diagnostics for the configured device path.

    Returns one of: 'ok', 'missing', 'permission', 'error'
    (result of a passive read-only open test - no data is sent to the modem).
    """
    logging.info("🔎 Collecting modem diagnostics...")
    logging.info(f"Configured device: {device_path}")

    if os.path.islink(device_path):
        logging.info(f"Symlink target: {os.path.realpath(device_path)}")

    logging.info(_describe_device_permissions(device_path))

    result = 'ok'
    try:
        with open(device_path, 'rb', buffering=0) as dev:
            dev.readable()  # Trigger open/permission check
        logging.info("✅ Able to open device file (read-only test)")
    except FileNotFoundError:
        logging.error("❌ Device file does not exist")
        result = 'missing'
    except PermissionError:
        logging.error("❌ Permission denied when opening device file")
        result = 'permission'
    except Exception as e:
        logging.warning(f"⚠️ Unexpected error opening device: {e}")
        result = 'error'
    return result


def log_gammu_versions():
    """Log libGammu and python-gammu versions."""
    try:
        versions = gammu.Version()
        logging.info(f"Gammu versions (libGammu / python-gammu): {versions}")
    except Exception as e:
        logging.warning(f"Could not determine gammu version: {e}")


def probe_serial_at(device_path, baud_rates=(115200, 9600, 57600, 38400, 19200), timeout=1.5):
    """Send a plain 'AT' to the modem at several baud rates and report who answers.

    Only call this when NO other component (gammu, URC proxy) holds the port,
    i.e. after a failed initialization. Sends nothing except 'AT'.
    Returns (baud, usable): the first baud rate that answers 'OK' (or None) and
    whether the port could be used at all.
    """
    try:
        import serial
    except ImportError:
        logging.warning("pyserial not available - skipping raw AT probe")
        return None, False

    logging.info(f"🔬 Raw AT probe on {device_path} (bauds: {list(baud_rates)})")
    for baud in baud_rates:
        try:
            with serial.Serial(device_path, baud, timeout=0.3) as ser:
                answer = b""
                # Two attempts: the very first AT after autobauding may be lost
                for _ in range(2):
                    ser.reset_input_buffer()
                    ser.write(b"AT\r")
                    ser.flush()
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        answer += ser.read(64)
                        if b"OK" in answer or b"ERROR" in answer:
                            break
                    if b"OK" in answer or b"ERROR" in answer:
                        break
            shown = answer.decode('ascii', errors='replace').strip().replace("\r", " ").replace("\n", " ")
            if b"OK" in answer:
                logging.info(f"   {baud}: ✅ modem answered: '{shown}'")
                return baud, True
            logging.info(f"   {baud}: no valid answer" + (f" (received: '{shown}')" if shown else " (silence)"))
        except Exception as e:
            logging.error(f"   {baud}: could not open/use port: {e}")
            return None, False
    return None, True


def diagnose_init_failure(error, real_device, configured_baud):
    """Explain a failed gammu initialization in a way that separates the causes.

    real_device must be the physical port (not a proxy pty) and must be free.
    """
    logging.error("=" * 60)
    logging.error(f"❌ Modem initialization failed: {type(error).__name__}: {error}")

    if not os.path.exists(real_device):
        logging.error(f"CAUSE 1 - device does not exist: {real_device}")
        logging.error("   → UART not enabled / wrong path. On Raspberry Pi add the matching "
                      "dtoverlay (e.g. uart2-pi5) to config.txt and reboot; check the device list above.")
        logging.error("=" * 60)
        return

    state = log_device_diagnostics(real_device)
    if state in ('permission', 'error'):
        logging.error(f"CAUSE 2 - device exists but cannot be opened ({state})")
        logging.error("   → check add-on device mapping / permissions (Owner:Group above).")
    elif isinstance(error, gammu.ERR_TIMEOUT):
        working, usable = probe_serial_at(real_device)
        if not usable:
            logging.error("CAUSE 3 - raw AT probe could not use the port (see above); "
                          "another process may hold it or pyserial is missing.")
        elif working:
            logging.error(f"CAUSE 3 - device opens and modem answers AT at {working} baud, "
                          f"but gammu timed out at configured '{configured_baud}'.")
            logging.error(f"   → set add-on option modem_baud_rate to '{working}'.")
        else:
            logging.error("CAUSE 3 - device opens, but the modem does not answer AT at any tested baud rate.")
            logging.error("   → check: TX/RX not swapped (TX→RXD, RX→TXD), common GND, modem power "
                          "(SIM800L needs 3.4-4.4 V and ~2 A peaks), modem powered on/registered, "
                          "correct UART port, no other process/console using the UART.")
    else:
        logging.error("CAUSE 4 - device is fine, gammu init failed for another reason: "
                      f"{type(error).__name__}: {error}")
    logging.error("=" * 60)


def _connection_name(baud_rate):
    """Gammu connection string: 'at' = autodetect, 'at<baud>' = fixed speed."""
    if baud_rate and str(baud_rate) != 'auto':
        return f"at{baud_rate}"
    return "at"


def _release(sm):
    """Best-effort release of a StateMachine after a failed Init."""
    try:
        sm.Terminate()
    except Exception:
        pass


def init_state_machine(pin, device_path='/dev/ttyUSB0', baud_rate='auto'):
    """Initialize gammu state machine with HA add-on config.

    baud_rate: 'auto' leaves gammu speed autodetection on (connection = at),
    a concrete value (e.g. '115200') fixes the speed (connection = at115200).
    Autodetection hangs some modules (SIM800C) → default is a fixed speed.

    Init is retried a bounded number of times (INIT_ATTEMPTS) for transient
    errors only; the StateMachine is released after every failed attempt.
    """
    connection = _connection_name(baud_rate)

    # Create gammu config dynamically
    config_content = f"""[gammu]
device = {device_path}
connection = {connection}
commtimeout = 40
"""

    # Write config to temporary file
    config_file = '/tmp/gammu.config'
    with open(config_file, 'w') as f:
        f.write(config_content)

    log_gammu_versions()
    logging.info(f"📄 Gammu config ({config_file}): device={device_path}, "
                 f"connection={connection}, commtimeout=40")

    for attempt in range(1, INIT_ATTEMPTS + 1):
        sm = gammu.StateMachine()
        sm.ReadConfig(Filename=config_file)
        try:
            sm.Init()
            logging.info(f"Successfully initialized gammu with device: {device_path} "
                         f"(attempt {attempt}/{INIT_ATTEMPTS})")
            break
        except gammu.ERR_NOSIM:
            logging.warning("SIM card not accessible, but device is connected")
            break
        except Exception as e:
            _release(sm)
            logging.error(f"Error initializing device (attempt {attempt}/{INIT_ATTEMPTS}): "
                          f"{type(e).__name__}: {e}")
            if isinstance(e, _TRANSIENT_ERRORS) and attempt < INIT_ATTEMPTS:
                logging.info(f"Retrying in {INIT_RETRY_DELAY}s (modem may not be ready yet)...")
                time.sleep(INIT_RETRY_DELAY)
                continue
            raise

    # Try to check security status
    try:
        security_status = sm.GetSecurityStatus()
        logging.info(f"SIM security status: {security_status}")

        if security_status == 'PIN':
            if pin is None or pin == '':
                logging.error("PIN is required but not provided.")
                sys.exit(1)
            else:
                sm.EnterSecurityCode('PIN', pin)
                logging.info("PIN entered successfully")

    except Exception as e:
        logging.warning(f"Could not check SIM security status: {e}")

    return sm


def retrieveAllSms(machine):
    """Retrieve all SMS messages from SIM/device memory"""
    try:
        status = machine.GetSMSStatus()
        allMultiPartSmsCount = status['SIMUsed'] + status['PhoneUsed'] + status['TemplatesUsed']

        allMultiPartSms = []
        start = True

        while len(allMultiPartSms) < allMultiPartSmsCount:
            if start:
                currentMultiPartSms = machine.GetNextSMS(Start=True, Folder=0)
                start = False
            else:
                currentMultiPartSms = machine.GetNextSMS(Location=currentMultiPartSms[0]['Location'], Folder=0)
            allMultiPartSms.append(currentMultiPartSms)

        allSms = gammu.LinkSMS(allMultiPartSms)

        results = []
        for sms in allSms:
            smsPart = sms[0]

            # Multipart completeness check: u dlouhých (concatenated) SMS nese
            # každá část v UDH údaj "AllParts" = kolik částí zpráva celkem má.
            # Pokud ještě nedorazily všechny části, nesmíme zprávu publikovat ani
            # mazat - jinak uživatel dostane useknutý text a smazáním první části
            # znemožníme pozdější složení zbytku.
            all_parts = 0
            for part in sms:
                udh = part.get('UDH') or {}
                part_total = udh.get('AllParts', 0) or 0
                if part_total > all_parts:
                    all_parts = part_total
            complete = (all_parts <= 1) or (len(sms) >= all_parts)

            result = {
                "Date": str(smsPart['DateTime']),
                "Number": smsPart['Number'],
                "State": smsPart['State'],
                "Locations": [smsPart['Location'] for smsPart in sms],
                "Complete": complete,
                "PartsReceived": len(sms),
                "PartsExpected": all_parts if all_parts > 1 else 1,
            }

            # Try to decode SMS - this may fail for MMS notifications or corrupted messages
            try:
                decodedSms = gammu.DecodeSMS(sms)
                if decodedSms == None:
                    # DecodeSMS returned None - use raw text from SMS part
                    result["Text"] = smsPart.get('Text', '')
                else:
                    # Successfully decoded - concatenate all text entries
                    text = ""
                    for entry in decodedSms['Entries']:
                        if entry.get('Buffer') is not None:
                            text += entry['Buffer']
                    result["Text"] = text if text else smsPart.get('Text', '')

            except UnicodeDecodeError as e:
                # MMS notification or binary message that can't be decoded as UTF-8
                logging.warning(f"Cannot decode SMS as UTF-8 (probably MMS notification): {e}")
                # Try to get raw text, but handle potential binary data safely
                try:
                    raw_text = smsPart.get('Text', '')
                    # If Text is bytes, try to decode with error handling
                    if isinstance(raw_text, bytes):
                        result["Text"] = raw_text.decode('utf-8', errors='replace')
                    else:
                        result["Text"] = str(raw_text) if raw_text else '[MMS or binary message]'
                except Exception:
                    result["Text"] = '[MMS or binary message - cannot display]'

            except Exception as e:
                # Any other decoding error (corrupted SMS, unknown format, etc.)
                logging.warning(f"Error decoding SMS: {e}")
                # Fallback to raw text with safe handling
                try:
                    raw_text = smsPart.get('Text', '')
                    if isinstance(raw_text, bytes):
                        result["Text"] = raw_text.decode('utf-8', errors='replace')
                    else:
                        result["Text"] = str(raw_text) if raw_text else '[Decoding error]'
                except Exception:
                    result["Text"] = '[Message decoding failed]'

            results.append(result)

        return results

    except Exception as e:
        logging.error(f"Error retrieving SMS: {e}")
        raise  # Re-raise exception so track_gammu_operation can detect failure


def deleteSms(machine, sms):
    """Delete SMS by location"""
    try:
        list(map(lambda location: machine.DeleteSMS(Folder=0, Location=location), sms["Locations"]))
    except Exception as e:
        logging.error(f"Error deleting SMS: {e}")


def encodeSms(smsinfo):
    """Encode SMS for sending"""
    return gammu.EncodeSMS(smsinfo)


def setupCallbacks(machine, unified_callback):
    """
    Nastaví callback pro příchozí hovory a SMS.
    Využívá Gammu SetIncomingCall, SetIncomingSMS a jeden společný SetIncomingCallback.

    Args:
        machine: Gammu state machine
        unified_callback: Callback funkce pro všechny události (sm, event_type, data)
                         event_type může být 'Call' nebo 'SMS'

    Returns: {'calls': bool, 'sms': bool} - co se podařilo nastavit
    """
    result = {'calls': False, 'sms': False}

    # Nastav společný callback pro všechny události
    try:
        machine.SetIncomingCallback(unified_callback)
        logging.info("📱 Unified callback: SetIncomingCallback registered")
    except Exception as e:
        logging.error(f"📱 SetIncomingCallback failed: {type(e).__name__}: {e}")
        return result

    # Povol Call notifikace (bez parametru podle dokumentace)
    try:
        machine.SetIncomingCall()
        result['calls'] = True
        logging.info("📞 Call notifications: ENABLED")
    except gammu.ERR_NOTSUPPORTED:
        logging.warning("📞 SetIncomingCall: Not supported by this modem")
    except Exception as e:
        logging.error(f"📞 SetIncomingCall failed: {type(e).__name__}: {e}")

    # Povol SMS notifikace (bez parametru podle dokumentace)
    try:
        machine.SetIncomingSMS()
        result['sms'] = True
        logging.info("📨 SMS notifications: ENABLED")
    except gammu.ERR_NOTSUPPORTED:
        logging.warning("📨 SetIncomingSMS: Not supported by this modem")
    except Exception as e:
        logging.error(f"📨 SetIncomingSMS failed: {type(e).__name__}: {e}")

    return result