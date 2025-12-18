"""
SMS Gammu Gateway - Support functions
Gammu integration functions for SMS operations and state machine management

Based on: https://github.com/pajikos/sms-gammu-gateway
Licensed under Apache License 2.0
"""


import sys
import os
import stat
import glob
import pwd
import grp
import gammu

def _describe_device_permissions(device_path):
    """Return a human friendly description of device permissions/owner."""
    try:
        info = os.stat(device_path)
    except FileNotFoundError:
        return f"⚠️ Device {device_path} not found"
    except Exception as e:
        return f"⚠️ Could not stat {device_path}: {e}"

    mode = info.st_mode
    perms = stat.filemode(mode)
    try:
        owner = pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        owner = str(info.st_uid)
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        group = str(info.st_gid)

    major = os.major(info.st_rdev) if stat.S_ISCHR(mode) else None
    minor = os.minor(info.st_rdev) if stat.S_ISCHR(mode) else None

    parts = [f"Permissions: {perms}", f"Owner: {owner}:{group}"]
    if major is not None and minor is not None:
        parts.append(f"Major:Minor = {major}:{minor}")
    return " | ".join(parts)


def log_device_diagnostics(device_path):
    """Print detailed diagnostics for the configured device path."""
    print("🔎 Collecting modem diagnostics...")
    print(f"Configured device: {device_path}")

    if os.path.islink(device_path):
        print(f"Symlink target: {os.path.realpath(device_path)}")

    description = _describe_device_permissions(device_path)
    print(description)

    try:
        with open(device_path, 'rb', buffering=0) as dev:
            dev.readable()  # Trigger open/permission check
            print("✅ Able to open device file (read-only test)")
    except FileNotFoundError:
        print("❌ Device file does not exist")
    except PermissionError:
        print("❌ Permission denied when opening device file")
    except Exception as e:
        print(f"⚠️ Unexpected error opening device: {e}")

    try:
        siblings = sorted(glob.glob('/dev/tty*'))
        nearby = [dev for dev in siblings if any(tag in dev for tag in ['USB', 'ACM', 'AMA'])]
        print("Discovered TTY devices:")
        for dev in nearby:
            print(f"  {dev}")
    except Exception as e:
        print(f"⚠️ Unable to list /dev/tty* devices: {e}")



def init_state_machine(pin, device_path='/dev/ttyUSB0'):
    """Initialize gammu state machine with HA add-on config"""
    sm = gammu.StateMachine()

    # Create gammu config dynamically
    config_content = f"""[gammu]
device = {device_path}
connection = at
commtimeout = 40
"""

    # Write config to temporary file
    config_file = '/tmp/gammu.config'
    with open(config_file, 'w') as f:
        f.write(config_content)
        
    print("📄 Generated gammu config:")
    print(config_content.strip())
    print(f"Config file saved to: {config_file}")

    log_device_diagnostics(device_path)

    sm.ReadConfig(Filename=config_file)
    
    try:
        sm.Init()
        print(f"Successfully initialized gammu with device: {device_path}")
        
        # Try to check security status
        try:
            security_status = sm.GetSecurityStatus()
            print(f"SIM security status: {security_status}")
            
            if security_status == 'PIN':
                if pin is None or pin == '':
                    print("PIN is required but not provided.")
                    sys.exit(1)
                else:
                    sm.EnterSecurityCode('PIN', pin)
                    print("PIN entered successfully")
                    
        except Exception as e:
            print(f"Warning: Could not check SIM security status: {e}")
            
    except gammu.ERR_NOSIM:
        print("Warning: SIM card not accessible, but device is connected")
    except Exception as e:
        print(f"Error initializing device: {e}")
        print("Available devices:")
        import os
        try:
            devices = [d for d in os.listdir('/dev/') if d.startswith('tty')]
            for device in sorted(devices):
                print(f"  /dev/{device}")
        except:
            pass
        except Exception as inner_e:
            print(f"⚠️ Unable to list /dev devices: {inner_e}")            
        raise
        
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

            result = {
                "Date": str(smsPart['DateTime']),
                "Number": smsPart['Number'],
                "State": smsPart['State'],
                "Locations": [smsPart['Location'] for smsPart in sms],
            }

            decodedSms = gammu.DecodeSMS(sms)
            if decodedSms == None:
                result["Text"] = smsPart['Text']
            else:
                text = ""
                for entry in decodedSms['Entries']:
                    if entry['Buffer'] != None:
                        text += entry['Buffer']

                result["Text"] = text

            results.append(result)

        return results

    except Exception as e:
        print(f"Error retrieving SMS: {e}")
        raise  # Re-raise exception so track_gammu_operation can detect failure


def deleteSms(machine, sms):
    """Delete SMS by location"""
    try:
        list(map(lambda location: machine.DeleteSMS(Folder=0, Location=location), sms["Locations"]))
    except Exception as e:
        print(f"Error deleting SMS: {e}")


def encodeSms(smsinfo):
    """Encode SMS for sending"""
    return gammu.EncodeSMS(smsinfo)