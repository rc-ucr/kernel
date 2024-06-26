#
# kickstart.py: kickstart install support
#
# Copyright (C) 1999-2016
# Red Hat, Inc.  All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import glob
import os
import os.path
import shlex
import sys
import tempfile
import urlgrabber

import blivet.arch
import blivet.fcoe
import blivet.iscsi
import blivet.zfcp
import blivet.nvdimm

import pykickstart.commands as commands

from contextlib import contextmanager

from pyanaconda import iutil
from pyanaconda import keyboard
from pyanaconda import localization
from pyanaconda import network
from pyanaconda import nm
from pyanaconda import ntp
from pyanaconda import screen_access
from pyanaconda import timezone

from pyanaconda.addons import AddonSection, AddonData, AddonRegistry, collect_addon_paths
from pyanaconda.bootloader import GRUB2, get_bootloader
from pyanaconda.constants import ADDON_PATHS, IPMI_ABORTED, THREAD_STORAGE
from pyanaconda.desktop import Desktop
from pyanaconda.errors import ScriptError, errorHandler
from pyanaconda.flags import flags, can_touch_runtime_system
from pyanaconda.i18n import _
from pyanaconda.pwpolicy import F22_PwPolicy, F22_PwPolicyData
from pyanaconda.simpleconfig import SimpleConfigFile
from pyanaconda.storage_utils import device_matches, try_populate_devicetree
from pyanaconda.threads import threadMgr
from pyanaconda.timezone import NTP_PACKAGE, NTP_SERVICE
from pyanaconda.ui.common import collect
from pyanaconda.users import getPassAlgo

from blivet import udev

from blivet.deviceaction import ActionCreateFormat, ActionDestroyFormat, ActionResizeDevice, ActionResizeFormat
from blivet.devicelibs.crypto import MIN_CREATE_ENTROPY
from blivet.autopart import swapSuggestion
from blivet.devicelibs.lvm import getPossiblePhysicalExtents, LVM_PE_SIZE, KNOWN_THPOOL_PROFILES
from blivet.devices import LUKSDevice
from blivet.devices.lvm import LVMCacheRequest, LVMThinLogicalVolumeDevice, LVMThinSnapShotDevice
from blivet.errors import PartitioningError
from blivet.formats.fs import XFS
from blivet.formats import getFormat
from blivet.partitioning import doPartitioning, growLVM
from blivet.platform import platform
from blivet.size import Size

from pykickstart.constants import CLEARPART_TYPE_NONE, CLEARPART_TYPE_ALL, \
                                  FIRSTBOOT_SKIP, FIRSTBOOT_RECONFIG, \
                                  KS_SCRIPT_POST, KS_SCRIPT_PRE, KS_SCRIPT_TRACEBACK, KS_SCRIPT_PREINSTALL, \
                                  SELINUX_DISABLED, SELINUX_ENFORCING, SELINUX_PERMISSIVE, \
                                  SNAPSHOT_WHEN_POST_INSTALL, SNAPSHOT_WHEN_PRE_INSTALL, \
                                  NVDIMM_ACTION_RECONFIGURE, NVDIMM_ACTION_USE
from pykickstart.base import BaseHandler, KickstartCommand
from pykickstart.errors import formatErrorMsg, KickstartError, KickstartValueError
from pykickstart.parser import KickstartParser
from pykickstart.parser import Script as KSScript
from pykickstart.sections import NullSection, PackageSection, PostScriptSection, PreScriptSection, PreInstallScriptSection, \
                                 OnErrorScriptSection, TracebackScriptSection, Section
from pykickstart.version import returnClassForVersion, RHEL7
from pykickstart.options import KSOptionParser

import logging
log = logging.getLogger("anaconda")
stderrLog = logging.getLogger("anaconda.stderr")
storage_log = logging.getLogger("blivet")
stdoutLog = logging.getLogger("anaconda.stdout")
from pyanaconda.anaconda_log import logger, logLevelMap, setHandlersLevel, DEFAULT_LEVEL

@contextmanager
def check_kickstart_error():
    try:
        yield
    except KickstartError as e:
        # We do not have an interface here yet, so we cannot use our error
        # handling callback.
        print(e)
        iutil.ipmi_report(IPMI_ABORTED)
        sys.exit(1)

class AnacondaKSScript(KSScript):
    """ Execute a kickstart script

        This will write the script to a file named /tmp/ks-script- before
        execution.
        Output is logged by the program logger, the path specified by --log
        or to /tmp/ks-script-*.log
    """
    def run(self, chroot):
        """ Run the kickstart script
            @param chroot directory path to chroot into before execution
        """
        if self.inChroot:
            scriptRoot = chroot
        else:
            scriptRoot = "/"

        # Environment variables that cause problems for %post scripts
        env_prune = ["LIBUSER_CONF"]

        (fd, path) = tempfile.mkstemp("", "ks-script-", scriptRoot + "/tmp")

        iutil.eintr_retry_call(os.write, fd, self.script)
        iutil.eintr_retry_call(os.close, fd)
        iutil.eintr_retry_call(os.chmod, path, 0o700)

        # Always log stdout/stderr from scripts.  Using --log just lets you
        # pick where it goes.  The script will also be logged to program.log
        # because of execWithRedirect.
        if self.logfile:
            if self.inChroot:
                messages = "%s/%s" % (scriptRoot, self.logfile)
            else:
                messages = self.logfile

            d = os.path.dirname(messages)
            if not os.path.exists(d):
                os.makedirs(d)
        else:
            # Always log outside the chroot, we copy those logs into the
            # chroot later.
            messages = "/tmp/%s.log" % os.path.basename(path)

        with open(messages, "w") as fp:
            rc = iutil.execWithRedirect(self.interp, ["/tmp/%s" % os.path.basename(path)],
                                        stdout=fp,
                                        root = scriptRoot,
                                        env_prune = env_prune)

        if rc != 0:
            log.error("Error code %s running the kickstart script at line %s", rc, self.lineno)
            if self.errorOnFail:
                err = ""
                with open(messages, "r") as fp:
                    err = "".join(fp.readlines())

                errorHandler.cb(ScriptError(self.lineno, err))
                iutil.ipmi_report(IPMI_ABORTED)
                sys.exit(0)

class AnacondaInternalScript(AnacondaKSScript):
    def __init__(self, *args, **kwargs):
        AnacondaKSScript.__init__(self, *args, **kwargs)
        self._hidden = True

    def __str__(self):
        # Scripts that implement portions of anaconda (copying screenshots and
        # log files, setfilecons, etc.) should not be written to the output
        # kickstart file.
        return ""

def getEscrowCertificate(escrowCerts, url):
    if not url:
        return None

    if url in escrowCerts:
        return escrowCerts[url]

    needs_net = not url.startswith("/") and not url.startswith("file:")
    if needs_net and not nm.nm_is_connected():
        msg = _("Escrow certificate %s requires the network.") % url
        raise KickstartError(msg)

    log.info("escrow: downloading %s", url)

    try:
        f = urlgrabber.urlopen(url)
    except urlgrabber.grabber.URLGrabError as e:
        msg = _("The following error was encountered while downloading the escrow certificate:\n\n%s") % e
        raise KickstartError(msg)

    try:
        escrowCerts[url] = f.read()
    finally:
        f.close()

    return escrowCerts[url]

def lookupAlias(devicetree, alias):
    for dev in devicetree.devices:
        if getattr(dev, "req_name", None) == alias:
            return dev

    return None

# Remove any existing formatting on a device, but do not remove the partition
# itself.  This sets up an existing device to be used in a --onpart option.
def removeExistingFormat(device, storage):
    deps = storage.deviceDeps(device)
    while deps:
        leaves = [d for d in deps if d.isleaf]
        for leaf in leaves:
            storage.destroyDevice(leaf)
            deps.remove(leaf)

    storage.devicetree.registerAction(ActionDestroyFormat(device))

def getAvailableDiskSpace(storage):
    """
    Get overall disk space available on disks we may use.

    :param storage: blivet.Blivet instance
    :return: overall disk space available
    :rtype: :class:`blivet.size.Size`

    """

    free_space = storage.freeSpaceSnapshot
    return sum(disk_free for disk_free, fs_free in free_space.values())

def refreshAutoSwapSize(storage):
    """
    Refresh size of the auto partitioning request for swap device according to
    the current state of the storage configuration.

    :param storage: blivet.Blivet instance

    """

    for request in storage.autoPartitionRequests:
        if request.fstype == "swap":
            disk_space = getAvailableDiskSpace(storage)
            request.size = swapSuggestion(disk_space=disk_space)
            break

###
### SUBCLASSES OF PYKICKSTART COMMAND HANDLERS
###

class Authconfig(commands.authconfig.FC3_Authconfig):
    def __init__(self, *args, **kwargs):
        commands.authconfig.FC3_Authconfig.__init__(self, *args, **kwargs)
        self.packages = []

    def setup(self):
        if self.seen:
            self.packages = ["authconfig"]

    def execute(self, *args):
        cmd = "/usr/sbin/authconfig"
        if not os.path.lexists(iutil.getSysroot()+cmd):
            if self.seen:
                msg = _("%s is missing. Cannot setup authentication.") % cmd
                raise KickstartError(msg)
            else:
                return

        args = ["--update", "--nostart"] + shlex.split(self.authconfig)

        if not flags.automatedInstall and \
           (os.path.exists(iutil.getSysroot() + "/lib64/security/pam_fprintd.so") or \
            os.path.exists(iutil.getSysroot() + "/lib/security/pam_fprintd.so")):
            args += ["--enablefingerprint"]

        try:
            iutil.execInSysroot(cmd, args)
        except RuntimeError as msg:
            log.error("Error running %s %s: %s", cmd, args, msg)

class AutoPart(commands.autopart.RHEL7_AutoPart):
    def parse(self, args):
        retval = commands.autopart.RHEL7_AutoPart.parse(self, args)

        if self.fstype:
            fmt = blivet.formats.getFormat(self.fstype)
            if not fmt or fmt.type is None:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("autopart fstype of %s is invalid.") % self.fstype))

        return retval

    def execute(self, storage, ksdata, instClass):
        from blivet.partitioning import doAutoPartition
        from pyanaconda.storage_utils import storage_checker

        if not self.autopart:
            return

        # Sets up default autopartitioning. Use clearpart separately if you want it.
        # The filesystem type is already set in the storage.
        refreshAutoSwapSize(storage)
        storage.doAutoPart = True

        if self.encrypted:
            storage.encryptedAutoPart = True
            storage.encryptionPassphrase = self.passphrase
            storage.encryptionCipher = self.cipher
            storage.autoPartEscrowCert = getEscrowCertificate(storage.escrowCertificates, self.escrowcert)
            storage.autoPartAddBackupPassphrase = self.backuppassphrase

        if self.type is not None:
            storage.autoPartType = self.type

        doAutoPartition(storage, ksdata, min_luks_entropy=MIN_CREATE_ENTROPY)
        report = storage_checker.check(storage)
        report.log(log)

        if report.failure:
            raise PartitioningError("autopart failed: \n" + "\n".join(report.all_errors))

class Bootloader(commands.bootloader.RHEL7_Bootloader):
    def __init__(self, *args, **kwargs):
        commands.bootloader.RHEL7_Bootloader.__init__(self, *args, **kwargs)
        self.location = "mbr"
        self._useBackup = False
        self._origBootDrive = None

    def parse(self, args):
        commands.bootloader.RHEL7_Bootloader.parse(self, args)
        if self.location == "partition" and isinstance(get_bootloader(), GRUB2):
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("GRUB2 does not support installation to a partition.")))

        if self.isCrypted and isinstance(get_bootloader(), GRUB2):
            if not self.password.startswith("grub.pbkdf2."):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg="GRUB2 encrypted password must be in grub.pbkdf2 format."))

        return self

    def execute(self, storage, ksdata, instClass, dry_run=False):
        """ Resolve and execute the bootloader installation.

            :param storage: object storing storage-related information
                            (disks, partitioning, bootloader, etc.)
            :type storage: blivet.Blivet
            :param payload: object storing packaging-related information
            :type payload: pyanaconda.packaging.Payload
            :param instclass: distribution-specific information
            :type instclass: pyanaconda.installclass.BaseInstallClass
            :param dry_run: flag if this is only dry run before the partitioning
                            will be resolved
            :type dry_run: bool
        """
        if flags.imageInstall and blivet.arch.isS390():
            self.location = "none"

        if dry_run:
            self._origBootDrive = self.bootDrive
            self._useBackup = True
        elif self._useBackup:
            self.bootDrive = self._origBootDrive
            self._useBackup = False

        if self.location == "none":
            location = None
        elif self.location == "partition":
            location = "boot"
        else:
            location = self.location

        if not location:
            storage.bootloader.skip_bootloader = True
            return

        if self.appendLine:
            args = self.appendLine.split()
            storage.bootloader.boot_args.update(args)

        if self.password:
            if self.isCrypted:
                storage.bootloader.encrypted_password = self.password
            else:
                storage.bootloader.password = self.password

        if location:
            storage.bootloader.set_preferred_stage1_type(location)

        if self.timeout is not None:
            storage.bootloader.timeout = self.timeout

        # Throw out drives specified that don't exist or cannot be used (iSCSI
        # device on an s390 machine)
        disk_names = [d.name for d in storage.disks
                      if not d.format.hidden and not d.protected and
                      (not blivet.arch.isS390() or not isinstance(d, blivet.devices.iScsiDiskDevice))]
        diskSet = set(disk_names)

        valid_disks = []
        # Drive specifications can contain | delimited variant specifications,
        # such as for example: "vd*|hd*|sd*"
        # So use the resolved disk identifiers returned by the device_matches() function in place
        # of the original specification but still remove the specifications that don't match anything
        # from the output kickstart to keep existing --driveorder processing behavior.
        for drive in self.driveorder[:]:
            matches = device_matches(drive, devicetree=storage.devicetree, disks_only=True)
            if set(matches).isdisjoint(diskSet):
                log.warning("requested drive %s in boot drive order doesn't exist or cannot be used",
                            drive)
                self.driveorder.remove(drive)
            else:
                valid_disks.extend(matches)

        storage.bootloader.disk_order = valid_disks

        # When bootloader doesn't have --boot-drive parameter then use this logic as fallback:
        # 1) If present first valid disk from driveorder parameter
        # 2) If present and usable, use disk where /boot partition is placed
        # 3) Use first disk from Blivet
        if self.bootDrive:
            matches = set(device_matches(self.bootDrive, devicetree=storage.devicetree, disks_only=True))
            if len(matches) > 1:
                raise KickstartValueError(
                            formatErrorMsg(self.lineno,
                                           msg=(_("More than one match found for given boot drive \"%s\".")
                                                % self.bootDrive)))
            elif matches.isdisjoint(diskSet):
                raise KickstartValueError(
                            formatErrorMsg(self.lineno,
                                           msg=(_("Requested boot drive \"%s\" doesn't exist or cannot be used.")
                                                % self.bootDrive)))
        # Take valid disk from --driveorder
        elif len(valid_disks) >= 1:
            log.debug("Bootloader: use '%s' first disk from driveorder as boot drive, dry run %s",
                      valid_disks[0], dry_run)
            self.bootDrive = valid_disks[0]
        else:
            # Try to find /boot
            #
            # This method is executed two times. Before and after partitioning.
            # In the first run, the result is used for other partitioning but
            # the second will be used.
            try:
                boot_dev = storage.mountpoints["/boot"]
            except KeyError:
                log.debug("Bootloader: /boot partition is not present, dry run %s", dry_run)
            else:
                boot_drive = ""
                # Find disk ancestor
                for ancestor in boot_dev.ancestors:
                    if ancestor.isDisk:
                        boot_drive = ancestor.name
                        break

                if boot_drive and boot_drive in disk_names:
                    self.bootDrive = boot_drive
                    log.debug("Bootloader: use /boot partition's disk '%s' as boot drive, dry run %s",
                              boot_drive, dry_run)

        # Nothing was found use first disk from Blivet
        if not self.bootDrive:
            log.debug("Bootloader: fallback use first disk return from Blivet '%s' as boot drive, dry run %s",
                      disk_names[0], dry_run)
            self.bootDrive = disk_names[0]

        drive = storage.devicetree.resolveDevice(self.bootDrive)
        storage.bootloader.stage1_disk = drive

        if self.leavebootorder:
            flags.leavebootorder = True

        if self.nombr:
            flags.nombr = True

class BTRFS(commands.btrfs.RHEL7_BTRFS):
    def execute(self, storage, ksdata, instClass):
        for b in self.btrfsList:
            b.execute(storage, ksdata, instClass)

class BTRFSData(commands.btrfs.RHEL7_BTRFSData):
    def execute(self, storage, ksdata, instClass):
        devicetree = storage.devicetree

        storage.doAutoPart = False

        members = []

        # Get a list of all the devices that make up this volume.
        for member in self.devices:
            dev = devicetree.resolveDevice(member)
            if not dev:
                # if using --onpart, use original device
                member_name = ksdata.onPart.get(member, member)
                dev = devicetree.resolveDevice(member_name) or lookupAlias(devicetree, member)

            if dev and dev.format.type == "luks":
                try:
                    dev = devicetree.getChildren(dev)[0]
                except IndexError:
                    dev = None

            if dev and dev.format.type != "btrfs":
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Btrfs partition \"%(device)s\" has a format of \"%(format)s\", but should have a format of \"Btrfs\".") %
                             {"device": member, "format": dev.format.type}))

            if not dev:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Tried to use undefined partition \"%s\" in Btrfs volume specification.") % member))

            members.append(dev)

        if self.subvol:
            name = self.name
        elif self.label:
            name = self.label
        else:
            name = None

        if len(members) == 0 and not self.preexist:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("Btrfs volume defined without any member devices.  Either specify member devices or use --useexisting.")))

        # allow creating btrfs vols/subvols without specifying mountpoint
        if self.mountpoint in ("none", "None"):
            self.mountpoint = ""

        # Sanity check mountpoint
        if self.mountpoint != "" and self.mountpoint[0] != '/':
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("The mount point \"%s\" is not valid.  It must start with a /.") % self.mountpoint))

        # If a previous device has claimed this mount point, delete the
        # old one.
        try:
            if self.mountpoint:
                device = storage.mountpoints[self.mountpoint]
                storage.destroyDevice(device)
        except KeyError:
            pass

        if self.preexist:
            device = devicetree.resolveDevice(self.name)
            if not device:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Btrfs volume \"%s\" specified with --useexisting does not exist.") % self.name))

            device.format.mountpoint = self.mountpoint
        else:
            request = storage.newBTRFS(name=name,
                                       subvol=self.subvol,
                                       mountpoint=self.mountpoint,
                                       metaDataLevel=self.metaDataLevel,
                                       dataLevel=self.dataLevel,
                                       parents=members,
                                       createOptions=self.mkfsopts)

            storage.createDevice(request)

class Realm(commands.realm.F19_Realm):
    def __init__(self, *args):
        commands.realm.F19_Realm.__init__(self, *args)
        self.packages = []
        self.discovered = ""

    def setup(self):
        if not self.join_realm:
            return

        try:
            argv = ["discover", "--verbose"] + \
                    self.discover_options + [self.join_realm]
            output = iutil.execWithCapture("realm", argv, filter_stderr=True)
        except OSError:
            # TODO: A lousy way of propagating what will usually be
            # 'no such realm'
            # The error message is logged by iutil
            return

        # Now parse the output for the required software. First line is the
        # realm name, and following lines are information as "name: value"
        self.packages = ["realmd"]
        self.discovered = ""

        lines = output.split("\n")
        if not lines:
            return
        self.discovered = lines.pop(0).strip()
        log.info("Realm discovered: %s", self.discovered)
        for line in lines:
            parts = line.split(":", 1)
            if len(parts) == 2 and parts[0].strip() == "required-package":
                self.packages.append(parts[1].strip())

        log.info("Realm %s needs packages %s",
                 self.discovered, ", ".join(self.packages))

    def execute(self, *args):
        if not self.discovered:
            return
        for arg in self.join_args:
            if arg.startswith("--no-password") or arg.startswith("--one-time-password"):
                pw_args = []
                break
        else:
            # no explicit password arg using implicit --no-password
            pw_args = ["--no-password"]

        argv = ["join", "--install", iutil.getSysroot(), "--verbose"] + \
               pw_args + self.join_args
        rc = -1
        try:
            rc = iutil.execWithRedirect("realm", argv)
        except OSError:
            pass

        if rc == 0:
            log.info("Joined realm %s", self.join_realm)


class ClearPart(commands.clearpart.F21_ClearPart):
    def parse(self, args):
        retval = commands.clearpart.F21_ClearPart.parse(self, args)

        if self.type is None:
            self.type = CLEARPART_TYPE_NONE

        if self.disklabel and self.disklabel not in platform.diskLabelTypes:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("Disklabel \"%s\" given in clearpart command is not "
                          "supported on this platform.") % self.disklabel))

        # Do any glob expansion now, since we need to have the real list of
        # disks available before the execute methods run.
        drives = []
        for spec in self.drives:
            matched = device_matches(spec, disks_only=True)
            if matched:
                drives.extend(matched)
            else:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Disk \"%s\" given in clearpart command does not exist.") % spec))

        self.drives = drives

        # Do any glob expansion now, since we need to have the real list of
        # devices available before the execute methods run.
        devices = []
        for spec in self.devices:
            matched = device_matches(spec)
            if matched:
                devices.extend(matched)
            else:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Device \"%s\" given in clearpart device list does not exist.") % spec))

        self.devices = devices

        return retval

    def execute(self, storage, ksdata, instClass):
        storage.config.clearPartType = self.type
        storage.config.clearPartDisks = self.drives
        storage.config.clearPartDevices = self.devices

        if self.initAll:
            storage.config.initializeDisks = self.initAll

        if self.disklabel:
            if not platform.setDefaultDiskLabelType(self.disklabel):
                log.warn("%s is not a supported disklabel type on this platform. "
                         "Using default disklabel %s instead.", self.disklabel, platform.defaultDiskLabelType)

        storage.clearPartitions()

class Fcoe(commands.fcoe.RHEL7_Fcoe):
    def parse(self, args):
        fc = commands.fcoe.RHEL7_Fcoe.parse(self, args)

        if fc.nic not in nm.nm_devices():
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("NIC \"%s\" given in fcoe command does not exist.") % fc.nic))

        if fc.nic in (info[0] for info in blivet.fcoe.fcoe().nics):
            log.info("Kickstart fcoe device %s already added from EDD, ignoring", fc.nic)
        else:
            msg = blivet.fcoe.fcoe().addSan(nic=fc.nic, dcb=fc.dcb, auto_vlan=True)
            if not msg:
                msg = "Succeeded."
                blivet.fcoe.fcoe().added_nics.append(fc.nic)

            log.info("adding FCoE SAN on %s: %s", fc.nic, msg)

        return fc

class Firewall(commands.firewall.RHEL7_Firewall):
    def __init__(self, *args, **kwargs):
        commands.firewall.RHEL7_Firewall.__init__(self, *args, **kwargs)
        self.packages = []

    def setup(self):
        if self.seen:
            self.packages = ["firewalld"]

    def execute(self, storage, ksdata, instClass):
        args = []

        # If --use-system-defaults was passed then the user wants
        # whatever was provided by the rpms or ostree to be the
        # default, do nothing.
        if self.use_system_defaults:
            log.info("ks file instructs to use system defaults for "
                     "firewall, skipping configuration.")
            return

        # enabled is None if neither --enable or --disable is passed
        # default to enabled if nothing has been set.
        if self.enabled == False:
            args += ["--disabled"]
        else:
            args += ["--enabled"]

        if "ssh" not in self.services and "ssh" not in self.remove_services \
            and "22:tcp" not in self.ports:
            args += ["--service=ssh"]

        for dev in self.trusts:
            args += [ "--trust=%s" % (dev,) ]

        for port in self.ports:
            args += [ "--port=%s" % (port,) ]

        for remove_service in self.remove_services:
            args += [ "--remove-service=%s" % (remove_service,) ]

        for service in self.services:
            args += [ "--service=%s" % (service,) ]

        cmd = "/usr/bin/firewall-offline-cmd"
        if not os.path.exists(iutil.getSysroot()+cmd):
            if self.enabled:
                msg = _("%s is missing. Cannot setup firewall.") % (cmd,)
                raise KickstartError(msg)
        else:
            iutil.execInSysroot(cmd, args)

class Firstboot(commands.firstboot.FC3_Firstboot):
    def setup(self, *args):
        # firstboot should be disabled by default after kickstart installations
        if flags.automatedInstall and not self.seen:
            self.firstboot = FIRSTBOOT_SKIP

    def execute(self, *args):
        action = "enable"
        unit_name = "initial-setup.service"

        # find if the unit file for the Initial Setup service is installed
        unit_exists = os.path.exists(os.path.join(iutil.getSysroot(), "lib/systemd/system/", unit_name))
        if unit_exists and self.firstboot == FIRSTBOOT_RECONFIG:
            # write the reconfig trigger file
            f = open(os.path.join(iutil.getSysroot(), "etc/reconfigSys"), "w+")
            f.close()

        if self.firstboot == FIRSTBOOT_SKIP:
            action = "disable"

            if self.seen:
                # Also tell the screen access manager, so that the fact that post installation tools
                # should be disabled propagates to the user interaction config file,
                # But only to this if explicitely requested through the "firstboot --disable/--disabled"
                # command in kickstart.
                screen_access.sam.post_install_tools_disabled = True

        # enable/disable the Initial Setup service (if its unit is installed)
        if unit_exists:
            iutil.execInSysroot("systemctl", [action, unit_name])

class Group(commands.group.F12_Group):
    def execute(self, storage, ksdata, instClass, users):
        for grp in self.groupList:
            kwargs = grp.__dict__
            kwargs.update({"root": iutil.getSysroot()})
            users.createGroup(grp.name, **kwargs)

class IgnoreDisk(commands.ignoredisk.RHEL6_IgnoreDisk):
    def parse(self, args):
        retval = commands.ignoredisk.RHEL6_IgnoreDisk.parse(self, args)

        # See comment in ClearPart.parse
        drives = []
        for spec in self.ignoredisk:
            matched = device_matches(spec, disks_only=True)
            if matched:
                drives.extend(matched)
            else:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Disk \"%s\" given in ignoredisk command does not exist.") % spec))

        self.ignoredisk = drives

        drives = []
        for spec in self.onlyuse:
            matched = device_matches(spec, disks_only=True)
            if matched:
                drives.extend(matched)
            else:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Disk \"%s\" given in ignoredisk command does not exist.") % spec))

        self.onlyuse = drives

        return retval

class Iscsi(commands.iscsi.F17_Iscsi):
    def parse(self, args):
        tg = commands.iscsi.F17_Iscsi.parse(self, args)

        if tg.iface:
            if not network.wait_for_network_devices([tg.iface]):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Network interface \"%(nic)s\" required by iSCSI \"%(iscsiTarget)s\" target is not up.") %
                             {"nic": tg.iface, "iscsiTarget": tg.target}))

        mode = blivet.iscsi.iscsi().mode
        if mode == "none":
            if tg.iface:
                blivet.iscsi.iscsi().create_interfaces(nm.nm_activated_devices())
        elif ((mode == "bind" and not tg.iface)
              or (mode == "default" and tg.iface)):
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("iscsi --iface must be specified (binding used) either for all targets or for none")))

        try:
            blivet.iscsi.iscsi().addTarget(tg.ipaddr, tg.port, tg.user,
                                            tg.password, tg.user_in,
                                            tg.password_in,
                                            target=tg.target,
                                            iface=tg.iface)
            log.info("added iscsi target %s at %s via %s", tg.target,
                                                           tg.ipaddr,
                                                           tg.iface)
        except (IOError, ValueError) as e:
            raise KickstartValueError(formatErrorMsg(self.lineno, msg=str(e)))

        return tg

class IscsiName(commands.iscsiname.FC6_IscsiName):
    def parse(self, args):
        retval = commands.iscsiname.FC6_IscsiName.parse(self, args)

        blivet.iscsi.iscsi().initiator = self.iscsiname
        return retval

class Lang(commands.lang.F19_Lang):
    def execute(self, *args, **kwargs):
        localization.write_language_configuration(self, iutil.getSysroot())

# no overrides needed here
Eula = commands.eula.F20_Eula

class LogVol(commands.logvol.RHEL7_LogVol):
    def execute(self, storage, ksdata, instClass):
        for l in self.lvList:
            l.execute(storage, ksdata, instClass)

        if self.lvList:
            growLVM(storage)

class LogVolData(commands.logvol.RHEL7_LogVolData):
    def execute(self, storage, ksdata, instClass):
        devicetree = storage.devicetree

        storage.doAutoPart = False

        # FIXME: we should be running sanityCheck on partitioning that is not ks
        # autopart, but that's likely too invasive for #873135 at this moment
        if self.mountpoint == "/boot" and blivet.arch.isS390():
            raise KickstartValueError(formatErrorMsg(self.lineno, msg="/boot can not be of type 'lvmlv' on s390x"))

        # we might have truncated or otherwise changed the specified vg name
        vgname = ksdata.onPart.get(self.vgname, self.vgname)

        size = None

        if self.percent:
            size = Size(0)

        if self.mountpoint == "swap":
            ty = "swap"
            self.mountpoint = ""
            if self.recommended or self.hibernation:
                disk_space = getAvailableDiskSpace(storage)
                size = swapSuggestion(hibernation=self.hibernation, disk_space=disk_space)
                self.grow = False
        else:
            if self.fstype != "":
                ty = self.fstype
            else:
                ty = storage.defaultFSType

        if size is None and not self.preexist:
            if not self.size:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg="Size can not be decided on from kickstart nor obtained from device."))
            try:
                size = Size("%d MiB" % self.size)
            except ValueError:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg="The size \"%s\" is invalid." % self.size))

        if self.thin_pool:
            self.mountpoint = ""
            ty = None

        # Sanity check mountpoint
        if self.mountpoint != "" and self.mountpoint[0] != '/':
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("The mount point \"%s\" is not valid.  It must start with a /.") % self.mountpoint))

        # Check that the VG this LV is a member of has already been specified.
        vg = devicetree.getDeviceByName(vgname)
        if not vg:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("No volume group exists with the name \"%s\".  Specify volume groups before logical volumes.") % self.vgname))

        # If cache PVs specified, check that they belong to the same VG this LV is a member of
        if self.cache_pvs:
            pv_devices = (lookupAlias(devicetree, pv) for pv in self.cache_pvs)
            if not all(pv in vg.pvs for pv in pv_devices):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("Cache PVs must belong to the same VG as the cached LV")))

        pool = None
        if self.thin_volume:
            pool = devicetree.getDeviceByName("%s-%s" % (vg.name, self.pool_name))
            if not pool:
                err = formatErrorMsg(self.lineno,
                                     msg=_("No thin pool exists with the name \"%s\". Specify thin pools before thin volumes.") % self.pool_name)
                raise KickstartValueError(err)

        # If this specifies an existing request that we should not format,
        # quit here after setting up enough information to mount it later.
        if not self.format:
            if not self.name:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("logvol --noformat must also use the --name= option.")))

            dev = devicetree.getDeviceByName("%s-%s" % (vg.name, self.name))
            if not dev:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Logical volume \"%s\" given in logvol command does not exist.") % self.name))

            if self.resize:
                size = dev.raw_device.alignTargetSize(size)
                if size < dev.currentSize:
                    # shrink
                    try:
                        devicetree.registerAction(ActionResizeFormat(dev, size))
                        devicetree.registerAction(ActionResizeDevice(dev, size))
                    except ValueError:
                        raise KickstartValueError(formatErrorMsg(self.lineno,
                                msg=_("Target size \"%(size)s\" for device \"%(device)s\" is invalid.") %
                                     {"size": self.size, "device": dev.name}))
                else:
                    # grow
                    try:
                        devicetree.registerAction(ActionResizeDevice(dev, size))
                        devicetree.registerAction(ActionResizeFormat(dev, size))
                    except ValueError:
                        raise KickstartValueError(formatErrorMsg(self.lineno,
                                msg=_("Target size \"%(size)s\" for device \"%(device)s\" is invalid.") %
                                     {"size": self.size, "device": dev.name}))

            dev.format.mountpoint = self.mountpoint
            dev.format.mountopts = self.fsopts
            if ty == "swap":
                storage.addFstabSwap(dev)
            return

        # Make sure this LV name is not already used in the requested VG.
        if not self.preexist:
            tmp = devicetree.getDeviceByName("%s-%s" % (vg.name, self.name))
            if tmp:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Logical volume name \"%(logvol)s\" is already in use in volume group \"%(volgroup)s\".") %
                             {"logvol": self.name, "volgroup": vg.name}))

            # Size specification checks
            if not self.percent:
                if not size:
                    raise KickstartValueError(formatErrorMsg(self.lineno,
                            msg=_("No size given for logical volume \"%s\".  Use one of --useexisting, --size, or --percent.") % self.name))
                elif not self.grow and size < vg.peSize:
                    raise KickstartValueError(formatErrorMsg(self.lineno,
                            msg=_("Logical volume size \"%(logvolSize)s\" must be larger than the volume group extent size of \"%(extentSize)s\".") %
                                 {"logvolSize": size, "extentSize": vg.peSize}))
            elif self.percent <= 0 or self.percent > 100:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Percentage must be between 0 and 100.")))

        # Now get a format to hold a lot of these extra values.
        fmt = getFormat(ty,
                        mountpoint=self.mountpoint,
                        label=self.label,
                        fsprofile=self.fsprofile,
                        createOptions=self.mkfsopts,
                        mountopts=self.fsopts)
        if not fmt.type and not self.thin_pool:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("The \"%s\" file system type is not supported.") % ty))

        add_fstab_swap = None
        # If we were given a pre-existing LV to create a filesystem on, we need
        # to verify it and its VG exists and then schedule a new format action
        # to take place there.  Also, we only support a subset of all the
        # options on pre-existing LVs.
        if self.preexist:
            device = devicetree.getDeviceByName("%s-%s" % (vg.name, self.name))
            if not device:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Logical volume \"%s\" given in logvol command does not exist.") % self.name))

            removeExistingFormat(device, storage)

            if self.resize:
                size = device.raw_device.alignTargetSize(size)
                try:
                    devicetree.registerAction(ActionResizeDevice(device, size))
                except ValueError:
                    raise KickstartValueError(formatErrorMsg(self.lineno,
                            msg=_("Target size \"%(size)s\" for device \"%(device)s\" is invalid.") %
                                 {"size": self.size, "device": device.name}))

            devicetree.registerAction(ActionCreateFormat(device, fmt))
            if ty == "swap":
                add_fstab_swap = device
        else:
            # If a previous device has claimed this mount point, delete the
            # old one.
            try:
                if self.mountpoint:
                    device = storage.mountpoints[self.mountpoint]
                    storage.destroyDevice(device)
            except KeyError:
                pass

            if self.thin_volume:
                parents = [pool]
            else:
                parents = [vg]

            pool_args = {}
            if self.thin_pool:
                if self.profile:
                    matching = (p for p in KNOWN_THPOOL_PROFILES if p.name == self.profile)
                    profile = next(matching, None)
                    if profile:
                        pool_args["profile"] = profile
                    else:
                        log.warning("No matching profile for %s found in LVM configuration", self.profile)
                if self.metadata_size:
                    pool_args["metadatasize"] = Size("%d MiB" % self.metadata_size)
                if self.chunk_size:
                    pool_args["chunksize"] = Size("%d KiB" % self.chunk_size)

            if self.maxSizeMB:
                try:
                    maxsize = Size("%d MiB" % self.maxSizeMB)
                except ValueError:
                    raise KickstartValueError(formatErrorMsg(self.lineno,
                            msg="The maximum size \"%s\" is invalid." % self.maxSizeMB))
            else:
                maxsize = None

            if self.cache_size and self.cache_pvs:
                pv_devices = (lookupAlias(devicetree, pv) for pv in self.cache_pvs)
                pv_devices_names = [pv.name for pv in pv_devices]
                cache_size = Size("%d MiB" % self.cache_size)
                cache_mode = self.cache_mode or None
                cache_request = LVMCacheRequest(cache_size, pv_devices_names, cache_mode)
            else:
                cache_request = None

            request = storage.newLV(fmt=fmt,
                                    name=self.name,
                                    parents=parents,
                                    size=size,
                                    thin_pool=self.thin_pool,
                                    thin_volume=self.thin_volume,
                                    grow=self.grow,
                                    maxsize=maxsize,
                                    percent=self.percent,
                                    cacheRequest=cache_request,
                                    **pool_args)

            storage.createDevice(request)
            if ty == "swap":
                add_fstab_swap = request

        if self.encrypted:
            if self.passphrase and not storage.encryptionPassphrase:
                storage.encryptionPassphrase = self.passphrase

            cert = getEscrowCertificate(storage.escrowCertificates, self.escrowcert)
            if self.preexist:
                luksformat = fmt
                device.format = getFormat("luks", passphrase=self.passphrase, device=device.path,
                                          cipher=self.cipher,
                                          escrow_cert=cert,
                                          add_backup_passphrase=self.backuppassphrase)
                luksdev = LUKSDevice("luks%d" % storage.nextID,
                                     fmt=luksformat,
                                     parents=device,
                                     min_luks_entropy=MIN_CREATE_ENTROPY)
            else:
                luksformat = request.format
                request.format = getFormat("luks", passphrase=self.passphrase,
                                           cipher=self.cipher,
                                           escrow_cert=cert,
                                           add_backup_passphrase=self.backuppassphrase,
                                           min_luks_entropy=MIN_CREATE_ENTROPY)
                luksdev = LUKSDevice("luks%d" % storage.nextID,
                                     fmt=luksformat,
                                     parents=request)
            if ty == "swap":
                # swap is on the LUKS device not on the LUKS' parent device,
                # override the info here
                add_fstab_swap = luksdev

            storage.createDevice(luksdev)

        if add_fstab_swap:
            storage.addFstabSwap(add_fstab_swap)

class Logging(commands.logging.FC6_Logging):
    def execute(self, *args):
        if logger.loglevel == DEFAULT_LEVEL:
            # not set from the command line
            level = logLevelMap[self.level]
            logger.loglevel = level
            setHandlersLevel(log, level)
            setHandlersLevel(storage_log, level)

        if logger.remote_syslog == None and len(self.host) > 0:
            # not set from the command line, ok to use kickstart
            remote_server = self.host
            if self.port:
                remote_server = "%s:%s" %(self.host, self.port)
            logger.updateRemote(remote_server)

class Mount(commands.mount.RHEL7_Mount):
    def execute(self, storage, ksdata, instClass):
        storage.do_autopart = False

        for md in self.dataList():
            md.execute(storage, ksdata, instClass)

class MountData(commands.mount.RHEL7_MountData):
    def execute(self, storage, ksdata, instClass):
        dev = storage.devicetree.resolveDevice(self.device)
        if dev is None:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                                      msg=_("Unknown or invalid device '%s' specified") % self.device))
        if self.reformat:
            if self.format:
                fmt = getFormat(self.format)
                if not fmt:
                    msg = _("Unknown or invalid format '%(format)s' specified for device '%(device)s'") % \
                            {"format" : self.format, "device" : self.device}
                    raise KickstartValueError(formatErrorMsg(self.lineno, msg))
            else:
                old_fmt = dev.format
                if not old_fmt or old_fmt.type is None:
                    raise KickstartValueError(formatErrorMsg(self.lineno,
                                              msg=_("No format on device '%s'") % self.device))
                fmt = getFormat(old_fmt.type)
            storage.formatDevice(dev, fmt)

        # only set mount points for mountable formats
        if dev.format.mountable and self.mount_point is not None and self.mount_point != "none":
            dev.format.mountpoint = self.mount_point

        dev.format.createOptions = self.mkfs_opts
        dev.format.options = self.mount_opts

        # make sure swaps end up in /etc/fstab
        if fmt.type == "swap":
            storage.addFstabSwap(dev)

class Network(commands.network.RHEL7_Network):
    def parse(self, args):
        nd = commands.network.RHEL7_Network.parse(self, args)
        setting_only_hostname = nd.hostname and len(args) <= 2
        if not setting_only_hostname:
            if not nd.device:
                ksdevice = flags.cmdline.get('ksdevice')
                if ksdevice:
                    log.info('network: setting %s from ksdevice for missing kickstart --device', ksdevice)
                    nd.device = ksdevice
                else:
                    log.info('network: setting "link" for missing --device specification in kickstart')
                    nd.device = "link"
        return nd
    def execute(self, storage, ksdata, instClass):
        network.write_network_config(storage, ksdata, instClass, iutil.getSysroot())

class Nvdimm(commands.nvdimm.RHEL7_Nvdimm):
    def parse(self, args):
        action = commands.nvdimm.RHEL7_Nvdimm.parse(self, args)

        if not blivet.nvdimm.nvdimm().nvdimm_plugin_available:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("nvdimm: nvdimm plugin not available.")))

        namespaces = blivet.nvdimm.nvdimm().namespaces
        if action.action == NVDIMM_ACTION_RECONFIGURE:
            if action.namespace not in namespaces:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("nvdimm: namespace %s not found.") % action.namespace))
            else:
                log.info("nvdimm: reconfiguring %s to %s mode", action.namespace, action.mode)
                blivet.nvdimm.nvdimm().reconfigure_namespace(action.namespace, action.mode,
                                                             sector_size=action.sectorsize)
        elif action.action == NVDIMM_ACTION_USE:
            if action.namespace and action.namespace not in namespaces:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("nvdimm: namespace %s not found.") % action.namespace))

            if action.blockdevs:
                # See comment in ClearPart.parse
                drives = []
                for spec in action.blockdevs:
                    matched = device_matches(spec, disks_only=True)
                    if matched:
                        drives.extend(matched)
                    else:
                        raise KickstartValueError(formatErrorMsg(self.lineno,
                                msg=_("Disk \"%s\" given in nvdimm command does not exist.") % spec))

                action.blockdevs = drives

        return action

class MultiPath(commands.multipath.FC6_MultiPath):
    def parse(self, args):
        raise NotImplementedError(_("The %s kickstart command is not currently supported.") % "multipath")

class DmRaid(commands.dmraid.FC6_DmRaid):
    def parse(self, args):
        raise NotImplementedError(_("The %s kickstart command is not currently supported.") % "dmraid")

class Partition(commands.partition.RHEL7_Partition):
    def execute(self, storage, ksdata, instClass):
        for p in self.partitions:
            p.execute(storage, ksdata, instClass)

        if self.partitions:
            doPartitioning(storage)

class PartitionData(commands.partition.RHEL7_PartData):
    def execute(self, storage, ksdata, instClass):
        devicetree = storage.devicetree
        kwargs = {}

        storage.doAutoPart = False

        if self.onbiosdisk != "":
            for (disk, biosdisk) in storage.eddDict.iteritems():
                if "%x" % biosdisk == self.onbiosdisk:
                    self.disk = disk
                    break

            if not self.disk:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("No disk found for specified BIOS disk \"%s\".") % self.onbiosdisk))

        size = None

        if self.mountpoint == "swap":
            ty = "swap"
            self.mountpoint = ""
            if self.recommended or self.hibernation:
                disk_space = getAvailableDiskSpace(storage)
                size = swapSuggestion(hibernation=self.hibernation, disk_space=disk_space)
                self.grow = False
        # if people want to specify no mountpoint for some reason, let them
        # this is really needed for pSeries boot partitions :(
        elif self.mountpoint == "None":
            self.mountpoint = ""
            if self.fstype:
                ty = self.fstype
            else:
                ty = storage.defaultFSType
        elif self.mountpoint == 'appleboot':
            ty = "appleboot"
            self.mountpoint = ""
        elif self.mountpoint == 'prepboot':
            ty = "prepboot"
            self.mountpoint = ""
        elif self.mountpoint == 'biosboot':
            ty = "biosboot"
            self.mountpoint = ""
        elif self.mountpoint.startswith("raid."):
            ty = "mdmember"
            kwargs["name"] = self.mountpoint
            self.mountpoint = ""

            if devicetree.getDeviceByName(kwargs["name"]):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("RAID partition \"%s\" is defined multiple times.") % kwargs["name"]))

            if self.onPart:
                ksdata.onPart[kwargs["name"]] = self.onPart
        elif self.mountpoint.startswith("pv."):
            ty = "lvmpv"
            kwargs["name"] = self.mountpoint
            self.mountpoint = ""

            if devicetree.getDeviceByName(kwargs["name"]):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("PV partition \"%s\" is defined multiple times.") % kwargs["name"]))

            if self.onPart:
                ksdata.onPart[kwargs["name"]] = self.onPart
        elif self.mountpoint.startswith("btrfs."):
            ty = "btrfs"
            kwargs["name"] = self.mountpoint
            self.mountpoint = ""

            if devicetree.getDeviceByName(kwargs["name"]):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Btrfs partition \"%s\" is defined multiple times.") % kwargs["name"]))

            if self.onPart:
                ksdata.onPart[kwargs["name"]] = self.onPart
        elif self.mountpoint == "/boot/efi":
            ty = "EFI System Partition"
            self.fsopts = "defaults,uid=0,gid=0,umask=0077,shortname=winnt"
        else:
            if self.fstype != "":
                ty = self.fstype
            elif self.mountpoint == "/boot":
                ty = storage.defaultBootFSType
            else:
                ty = storage.defaultFSType

        if not size and self.size:
            try:
                size = Size("%d MiB" % self.size)
            except ValueError:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("The size \"%s\" is invalid.") % self.size))

        # If this specified an existing request that we should not format,
        # quit here after setting up enough information to mount it later.
        if not self.format:
            if not self.onPart:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("part --noformat must also use the --onpart option.")))

            dev = devicetree.resolveDevice(self.onPart)
            if not dev:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Partition \"%s\" given in part command does not exist.") % self.onPart))

            if self.resize:
                size = dev.raw_device.alignTargetSize(size)
                if size < dev.currentSize:
                    # shrink
                    try:
                        devicetree.registerAction(ActionResizeFormat(dev, size))
                        devicetree.registerAction(ActionResizeDevice(dev, size))
                    except ValueError:
                        raise KickstartValueError(formatErrorMsg(self.lineno,
                                msg=_("Target size \"%(size)s\" for device \"%(device)s\" is invalid.") %
                                     {"size": self.size, "device": dev.name}))
                else:
                    # grow
                    try:
                        devicetree.registerAction(ActionResizeDevice(dev, size))
                        devicetree.registerAction(ActionResizeFormat(dev, size))
                    except ValueError:
                        raise KickstartValueError(formatErrorMsg(self.lineno,
                                msg=_("Target size \"%(size)s\" for device \"%(device)s\" is invalid.") %
                                     {"size": self.size, "device": dev.name}))

            dev.format.mountpoint = self.mountpoint
            dev.format.mountopts = self.fsopts
            if ty == "swap":
                storage.addFstabSwap(dev)
            return

        # Now get a format to hold a lot of these extra values.
        kwargs["fmt"] = getFormat(ty,
           mountpoint=self.mountpoint,
           label=self.label,
           fsprofile=self.fsprofile,
           mountopts=self.fsopts,
           createOptions=self.mkfsopts,
           size=size)
        if not kwargs["fmt"].type:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("The \"%s\" file system type is not supported.") % ty))

        # If we were given a specific disk to create the partition on, verify
        # that it exists first.  If it doesn't exist, see if it exists with
        # mapper/ on the front.  If that doesn't exist either, it's an error.
        if self.disk:
            disk = devicetree.resolveDevice(self.disk)
            # if this is a multipath member promote it to the real mpath
            if disk and disk.format.type == "multipath_member":
                mpath_device = storage.devicetree.getChildren(disk)[0]
                storage_log.info("kickstart: part: promoting %s to %s",
                                 disk.name, mpath_device.name)
                disk = mpath_device
            if not disk:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Disk \"%s\" given in part command does not exist.") % self.disk))
            if not disk.partitionable:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Cannot install to unpartitionable device \"%s\".") % self.disk))

            should_clear = storage.shouldClear(disk)
            if disk and (disk.partitioned or should_clear):
                kwargs["parents"] = [disk]
            elif disk:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Disk \"%s\" in part command is not partitioned.") % self.disk))

            if not kwargs["parents"]:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Disk \"%s\" given in part command does not exist.") % self.disk))

        kwargs["grow"] = self.grow
        kwargs["size"] = size
        if self.maxSizeMB:
            try:
                maxsize = Size("%d MiB" % self.maxSizeMB)
            except ValueError:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("The maximum size \"%s\" is invalid.") % self.maxSizeMB))
        else:
            maxsize = None

        kwargs["maxsize"] = maxsize

        kwargs["primary"] = self.primOnly

        add_fstab_swap = None
        # If we were given a pre-existing partition to create a filesystem on,
        # we need to verify it exists and then schedule a new format action to
        # take place there.  Also, we only support a subset of all the options
        # on pre-existing partitions.
        if self.onPart:
            device = devicetree.resolveDevice(self.onPart)
            if not device:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Partition \"%s\" given in part command does not exist.") % self.onPart))

            removeExistingFormat(device, storage)
            if self.resize:
                size = device.raw_device.alignTargetSize(size)
                try:
                    devicetree.registerAction(ActionResizeDevice(device, size))
                except ValueError:
                    raise KickstartValueError(formatErrorMsg(self.lineno,
                            msg=_("Target size \"%(size)s\" for device \"%(device)s\" is invalid.") %
                                 {"size": self.size, "device": device.name}))

            devicetree.registerAction(ActionCreateFormat(device, kwargs["fmt"]))
            if ty == "swap":
                add_fstab_swap = device
        # tmpfs mounts are not disks and don't occupy a disk partition,
        # so handle them here
        elif self.fstype == "tmpfs":
            request = storage.newTmpFS(**kwargs)
            storage.createDevice(request)
        else:
            # If a previous device has claimed this mount point, delete the
            # old one.
            try:
                if self.mountpoint:
                    device = storage.mountpoints[self.mountpoint]
                    storage.destroyDevice(device)
            except KeyError:
                pass

            request = storage.newPartition(**kwargs)
            storage.createDevice(request)
            if ty == "swap":
                add_fstab_swap = request

        if self.encrypted:
            if self.passphrase and not storage.encryptionPassphrase:
                storage.encryptionPassphrase = self.passphrase

            cert = getEscrowCertificate(storage.escrowCertificates, self.escrowcert)
            if self.onPart:
                luksformat = kwargs["fmt"]
                device.format = getFormat("luks", passphrase=self.passphrase, device=device.path,
                                          cipher=self.cipher,
                                          escrow_cert=cert,
                                          add_backup_passphrase=self.backuppassphrase,
                                          min_luks_entropy=MIN_CREATE_ENTROPY)
                luksdev = LUKSDevice("luks%d" % storage.nextID,
                                     fmt=luksformat,
                                     parents=device)
            else:
                luksformat = request.format
                request.format = getFormat("luks", passphrase=self.passphrase,
                                           cipher=self.cipher,
                                           escrow_cert=cert,
                                           add_backup_passphrase=self.backuppassphrase,
                                           min_luks_entropy=MIN_CREATE_ENTROPY)
                luksdev = LUKSDevice("luks%d" % storage.nextID,
                                     fmt=luksformat,
                                     parents=request)

            if ty == "swap":
                # swap is on the LUKS device not on the LUKS' parent device,
                # override the info here
                add_fstab_swap = luksdev

            storage.createDevice(luksdev)

        if add_fstab_swap:
            storage.addFstabSwap(add_fstab_swap)

class Raid(commands.raid.RHEL7_Raid):
    def execute(self, storage, ksdata, instClass):
        for r in self.raidList:
            r.execute(storage, ksdata, instClass)

class RaidData(commands.raid.RHEL7_RaidData):
    def execute(self, storage, ksdata, instClass):
        raidmems = []
        devicetree = storage.devicetree
        devicename = self.device
        if self.preexist:
            device = devicetree.resolveDevice(devicename)
            if device:
                devicename = device.name

        kwargs = {}

        storage.doAutoPart = False

        if self.mountpoint == "swap":
            ty = "swap"
            self.mountpoint = ""
        elif self.mountpoint.startswith("pv."):
            ty = "lvmpv"
            kwargs["name"] = self.mountpoint
            ksdata.onPart[kwargs["name"]] = devicename

            if devicetree.getDeviceByName(kwargs["name"]):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("PV partition \"%s\" is defined multiple times.") % kwargs["name"]))

            self.mountpoint = ""
        elif self.mountpoint.startswith("btrfs."):
            ty = "btrfs"
            kwargs["name"] = self.mountpoint
            ksdata.onPart[kwargs["name"]] = devicename

            if devicetree.getDeviceByName(kwargs["name"]):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Btrfs partition \"%s\" is defined multiple times.") % kwargs["name"]))

            self.mountpoint = ""
        else:
            if self.fstype != "":
                ty = self.fstype
            elif self.mountpoint == "/boot" and \
                 "mdarray" in storage.bootloader.stage2_device_types:
                ty = storage.defaultBootFSType
            else:
                ty = storage.defaultFSType

        # Sanity check mountpoint
        if self.mountpoint != "" and self.mountpoint[0] != '/':
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("The mount point \"%s\" is not valid.  It must start with a /.") % self.mountpoint))

        # If this specifies an existing request that we should not format,
        # quit here after setting up enough information to mount it later.
        if not self.format:
            if not devicename:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("raid --noformat must also use the --device option.")))

            dev = devicetree.getDeviceByName(devicename)
            if not dev:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("RAID device  \"%s\" given in raid command does not exist.") % devicename))

            dev.format.mountpoint = self.mountpoint
            dev.format.mountopts = self.fsopts
            if ty == "swap":
                storage.addFstabSwap(dev)
            return

        # Get a list of all the RAID members.
        for member in self.members:
            dev = devicetree.resolveDevice(member)
            if not dev:
                # if member is using --onpart, use original device
                mem = ksdata.onPart.get(member, member)
                dev = devicetree.resolveDevice(mem) or lookupAlias(devicetree, member)
            if dev and dev.format.type == "luks":
                try:
                    dev = devicetree.getChildren(dev)[0]
                except IndexError:
                    dev = None

            if dev and dev.format.type != "mdmember":
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("RAID device \"%(device)s\" has a format of \"%(format)s\", but should have a format of \"mdmember\".") %
                             {"device": member, "format": dev.format.type}))

            if not dev:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Tried to use undefined partition \"%s\" in RAID specification.") % member))

            raidmems.append(dev)

        # Now get a format to hold a lot of these extra values.
        kwargs["fmt"] = getFormat(ty,
           label=self.label,
           fsprofile=self.fsprofile,
           mountpoint=self.mountpoint,
           mountopts=self.fsopts,
           createOptions=self.mkfsopts)
        if not kwargs["fmt"].type:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("The \"%s\" file system type is not supported.") % ty))

        kwargs["name"] = devicename
        kwargs["level"] = self.level
        kwargs["parents"] = raidmems
        kwargs["memberDevices"] = len(raidmems) - self.spares
        kwargs["totalDevices"] = len(raidmems)

        if self.chunk_size:
            kwargs["chunkSize"] = Size("%d KiB" % self.chunk_size)

        add_fstab_swap = None

        # If we were given a pre-existing RAID to create a filesystem on,
        # we need to verify it exists and then schedule a new format action
        # to take place there.  Also, we only support a subset of all the
        # options on pre-existing RAIDs.
        if self.preexist:
            device = devicetree.getDeviceByName(devicename)
            if not device:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("RAID volume \"%s\" specified with --useexisting does not exist.") % devicename))

            removeExistingFormat(device, storage)
            devicetree.registerAction(ActionCreateFormat(device, kwargs["fmt"]))
            if ty == "swap":
                add_fstab_swap = device
        else:
            if devicename and devicename in (a.name for a in storage.mdarrays):
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("The RAID volume name \"%s\" is already in use.") % devicename))

            # If a previous device has claimed this mount point, delete the
            # old one.
            try:
                if self.mountpoint:
                    device = storage.mountpoints[self.mountpoint]
                    storage.destroyDevice(device)
            except KeyError:
                pass

            try:
                request = storage.newMDArray(**kwargs)
            except ValueError as e:
                raise KickstartValueError(formatErrorMsg(self.lineno, msg=str(e)))

            storage.createDevice(request)
            if ty == "swap":
                add_fstab_swap = request

        if self.encrypted:
            if self.passphrase and not storage.encryptionPassphrase:
                storage.encryptionPassphrase = self.passphrase

            cert = getEscrowCertificate(storage.escrowCertificates, self.escrowcert)
            if self.preexist:
                luksformat = kwargs["fmt"]
                device.format = getFormat("luks", passphrase=self.passphrase, device=device.path,
                                          cipher=self.cipher,
                                          escrow_cert=cert,
                                          add_backup_passphrase=self.backuppassphrase)
                luksdev = LUKSDevice("luks%d" % storage.nextID,
                                     fmt=luksformat,
                                     parents=device)
            else:
                luksformat = request.format
                request.format = getFormat("luks", passphrase=self.passphrase,
                                           cipher=self.cipher,
                                           escrow_cert=cert,
                                           add_backup_passphrase=self.backuppassphrase)
                luksdev = LUKSDevice("luks%d" % storage.nextID,
                                     fmt=luksformat,
                                     parents=request)

            if ty == "swap":
                # swap is on the LUKS device instead of the parent device,
                # override the device here
                add_fstab_swap = luksdev

            storage.createDevice(luksdev)

        if add_fstab_swap:
            storage.addFstabSwap(add_fstab_swap)

class RepoData(commands.repo.RHEL7_RepoData):
    def __init__(self, *args, **kwargs):
        """ Add enabled kwarg

            :param enabled: The repo has been enabled
            :type enabled: bool
        """
        self.enabled = kwargs.pop("enabled", True)

        commands.repo.RHEL7_RepoData.__init__(self, *args, **kwargs)

class ReqPart(commands.reqpart.RHEL7_ReqPart):
    def execute(self, storage, ksdata, instClass):
        from blivet.partitioning import doReqPartition

        if not self.reqpart:
            return

        log.debug("Looking for platform-specific bootloader requirements.")
        reqs = platform.setPlatformBootloaderReqs()

        if self.addBoot:
            log.debug("Looking for platform-specific boot requirements.")
            bootPartitions = platform.setPlatformBootPartition()

            # blivet doesn't know this - anaconda sets up the default boot fstype
            # in various places in this file, as well as in setDefaultPartitioning
            # in the install classes.  We need to duplicate that here.
            for part in bootPartitions:
                if part.mountpoint == "/boot":
                    part.fstype = storage.defaultBootFSType

            reqs += bootPartitions

        if reqs:
            log.debug("Applying requirements:\n%s", "".join(map(str, reqs)))
            doReqPartition(storage, reqs)

class RootPw(commands.rootpw.F18_RootPw):
    def execute(self, storage, ksdata, instClass, users):
        if flags.automatedInstall and not self.password and not self.seen:
            # Lock the root password if during an installation with kickstart
            # the root password is empty & not specififed as empty in the kickstart
            # (seen == False) via the rootpw command.
            # Note that kickstart is actually the only way to specify an empty
            # root password - we don't allow that via the UI.
            self.lock = True
        elif not flags.automatedInstall and not self.password:
            # Also lock the root password if it was not set during interactive installation.
            self.lock = True


        algo = getPassAlgo(ksdata.authconfig.authconfig)
        users.setRootPassword(self.password, self.isCrypted, self.lock, algo)

class SELinux(commands.selinux.FC3_SELinux):
    def execute(self, *args):
        selinux_states = { SELINUX_DISABLED: "disabled",
                           SELINUX_ENFORCING: "enforcing",
                           SELINUX_PERMISSIVE: "permissive" }

        if self.selinux is None:
            # Use the defaults set by the installed (or not) selinux package
            return
        elif self.selinux not in selinux_states:
            log.error("unknown selinux state: %s", self.selinux)
            return

        try:
            selinux_cfg = SimpleConfigFile(iutil.getSysroot()+"/etc/selinux/config")
            selinux_cfg.read()
            selinux_cfg.set(("SELINUX", selinux_states[self.selinux]))
            selinux_cfg.write()
        except IOError as msg:
            log.error ("Error setting selinux mode: %s", msg)

class Services(commands.services.FC6_Services):
    def execute(self, storage, ksdata, instClass):
        for svc in self.disabled:
            if not svc.endswith(".service"):
                svc += ".service"

            iutil.execInSysroot("systemctl", ["disable", svc])

        for svc in self.enabled:
            if not svc.endswith(".service"):
                svc += ".service"

            iutil.execInSysroot("systemctl", ["enable", svc])

class SshKey(commands.sshkey.F22_SshKey):
    def execute(self, storage, ksdata, instClass, users):
        for usr in self.sshUserList:
            users.setUserSshKey(usr.username, usr.key)

class Timezone(commands.timezone.RHEL7_Timezone):
    def __init__(self, *args):
        commands.timezone.RHEL7_Timezone.__init__(self, *args)

        self._added_chrony = False
        self._enabled_chrony = False
        self._disabled_chrony = False

    def setup(self, ksdata):
        # do not install and use NTP package
        if self.nontp or NTP_PACKAGE in ksdata.packages.excludedList:
            if iutil.service_running(NTP_SERVICE) and \
                    can_touch_runtime_system("stop NTP service"):
                ret = iutil.stop_service(NTP_SERVICE)
                if ret != 0:
                    log.error("Failed to stop NTP service")

            if self._added_chrony and NTP_PACKAGE in ksdata.packages.packageList:
                ksdata.packages.packageList.remove(NTP_PACKAGE)
                self._added_chrony = False

            # Both un-enable and disable chrony, because sometimes it's installed
            # off by default (packages) and sometimes not (liveimg).
            if self._enabled_chrony and NTP_SERVICE in ksdata.services.enabled:
                ksdata.services.enabled.remove(NTP_SERVICE)
                self._enabled_chrony = False

            if NTP_SERVICE not in ksdata.services.disabled:
                ksdata.services.disabled.append(NTP_SERVICE)
                self._disabled_chrony = True
        # install and use NTP package
        else:
            if not iutil.service_running(NTP_SERVICE) and \
                    can_touch_runtime_system("start NTP service"):
                ret = iutil.start_service(NTP_SERVICE)
                if ret != 0:
                    log.error("Failed to start NTP service")

            if not NTP_PACKAGE in ksdata.packages.packageList:
                ksdata.packages.packageList.append(NTP_PACKAGE)
                self._added_chrony = True

            if self._disabled_chrony and NTP_SERVICE in ksdata.services.disabled:
                ksdata.services.disabled.remove(NTP_SERVICE)
                self._disabled_chrony = False

            if not NTP_SERVICE in ksdata.services.enabled and \
                    not NTP_SERVICE in ksdata.services.disabled:
                ksdata.services.enabled.append(NTP_SERVICE)
                self._enabled_chrony = True

    def execute(self, *args):
        # write out timezone configuration
        if timezone.is_valid_timezone(self.timezone):
            if not timezone.is_valid_ui_timezone(self.timezone):
                log.warning("Timezone specification %s set in kickstart is "\
                            "not offered by installer GUI.", self.timezone)
        else:
            # this should never happen, but for pity's sake
            log.warning("Timezone %s set in kickstart is not valid, falling "\
                        "back to default (America/New_York).", self.timezone)
            self.timezone = "America/New_York"

        timezone.write_timezone_config(self, iutil.getSysroot())

        # write out NTP configuration (if set)
        if not self.nontp and self.ntpservers:
            chronyd_out_path = os.path.normpath(iutil.getSysroot() + ntp.CHRONY_CONFIG_FILE)
            ntpd_out_path = os.path.normpath(iutil.getSysroot() + ntp.NTP_CONFIG_FILE)
            if os.path.exists(chronyd_out_path):
                log.debug("Modifying installed chrony configuration")
                try:
                    ntp.save_servers_to_config(self.ntpservers, conf_file_path=chronyd_out_path)
                except ntp.NTPconfigError as ntperr:
                    log.warning("Failed to save NTP configuration for chrony: %s", ntperr)
            else:
                log.debug("Creating chrony configuration based on the "
                          "configuration from installation environment")
                try:
                    ntp.save_servers_to_config(self.ntpservers,
                                               conf_file_path=ntp.CHRONY_CONFIG_FILE,
                                               out_file_path=chronyd_out_path)
                except ntp.NTPconfigError as ntperr:
                    log.warning("Failed to save NTP configuration without chrony package: %s", ntperr)

            if os.path.exists(ntpd_out_path):
                log.debug("Modifying installed ntpd configuration")
                try:
                    ntp.save_servers_to_config(self.ntpservers, conf_file_path=ntpd_out_path)
                except ntp.NTPconfigError as ntperr:
                    log.warning("Failed to save NTP configuration for ntpd: %s", ntperr)
            # This is special customer request when the ntp is installed so there is no point
            # in creating conf file from NTP from installation environment.
            else:
                log.debug("ntpd configuration isn't created because the package wasn't installed")

class User(commands.user.F19_User):
    def execute(self, storage, ksdata, instClass, users):
        algo = getPassAlgo(ksdata.authconfig.authconfig)

        for usr in self.userList:
            kwargs = usr.__dict__
            kwargs.update({"algo": algo, "root": iutil.getSysroot()})

            # If the user password came from a kickstart and it is blank we
            # need to make sure the account is locked, not created with an
            # empty password.
            if ksdata.user.seen and kwargs.get("password", "") == "":
                kwargs["password"] = None
            if not users.createUser(usr.name, **kwargs):
                log.error("User %s already exists, not creating.", usr.name)

class VolGroup(commands.volgroup.RHEL7_VolGroup):
    def execute(self, storage, ksdata, instClass):
        for v in self.vgList:
            v.execute(storage, ksdata, instClass)

class VolGroupData(commands.volgroup.RHEL7_VolGroupData):
    def execute(self, storage, ksdata, instClass):
        pvs = []

        devicetree = storage.devicetree

        storage.doAutoPart = False

        # Get a list of all the physical volume devices that make up this VG.
        for pv in self.physvols:
            dev = devicetree.resolveDevice(pv)
            if not dev:
                # if pv is using --onpart, use original device
                pv_name = ksdata.onPart.get(pv, pv)
                dev = devicetree.resolveDevice(pv_name) or lookupAlias(devicetree, pv)
            if dev and dev.format.type == "luks":
                try:
                    dev = devicetree.getChildren(dev)[0]
                except IndexError:
                    dev = None

            if dev and dev.format.type != "lvmpv":
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Physical volume \"%(device)s\" has a format of \"%(format)s\", but should have a format of \"lvmpv\".") %
                             {"device": pv, "format": dev.format.type}))

            if not dev:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Tried to use undefined partition \"%s\" in Volume Group specification") % pv))

            pvs.append(dev)

        if len(pvs) == 0 and not self.preexist:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("Volume group \"%s\" defined without any physical volumes.  Either specify physical volumes or use --useexisting.") % self.vgname))

        if self.pesize == 0:
            # default PE size requested -- we use blivet's default in KiB
            self.pesize = LVM_PE_SIZE.convertTo(spec="KiB")

        pesize = Size("%d KiB" % self.pesize)
        possible_extents = getPossiblePhysicalExtents()
        if pesize not in possible_extents:
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("Volume group given physical extent size of \"%(extentSize)s\", but must be one of:\n%(validExtentSizes)s.") %
                         {"extentSize": pesize, "validExtentSizes": ", ".join(str(e) for e in possible_extents)}))

        # If --noformat or --useexisting was given, there's really nothing to do.
        if not self.format or self.preexist:
            if not self.vgname:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("volgroup --noformat and volgroup --useexisting must also use the --name= option.")))

            dev = devicetree.getDeviceByName(self.vgname)
            if not dev:
                raise KickstartValueError(formatErrorMsg(self.lineno,
                        msg=_("Volume group \"%s\" given in volgroup command does not exist.") % self.vgname))
        elif self.vgname in (vg.name for vg in storage.vgs):
            raise KickstartValueError(formatErrorMsg(self.lineno,
                    msg=_("The volume group name \"%s\" is already in use.") % self.vgname))
        else:
            request = storage.newVG(parents=pvs,
                                    name=self.vgname,
                                    peSize=pesize)

            storage.createDevice(request)
            if self.reserved_space:
                request.reserved_space = Size("%dMiB" % self.reserved_space)
            elif self.reserved_percent:
                request.reserved_percent = self.reserved_percent

            # in case we had to truncate or otherwise adjust the specified name
            ksdata.onPart[self.vgname] = request.name

class XConfig(commands.xconfig.F14_XConfig):
    def execute(self, *args):
        desktop = Desktop()
        if self.startX:
            desktop.runlevel = 5

        if self.defaultdesktop:
            desktop.desktop = self.defaultdesktop

        # now write it out
        desktop.write()

class SkipX(commands.skipx.FC3_SkipX):
    def execute(self, *args):
        if self.skipx:
            desktop = Desktop()
            desktop.runlevel = 3
            desktop.write()

class Snapshot(commands.snapshot.RHEL7_Snapshot):
    def _post_snapshots(self):
        return filter(lambda snap: snap.when == SNAPSHOT_WHEN_POST_INSTALL, self.dataList())

    def _pre_snapshots(self):
        return filter(lambda snap: snap.when == SNAPSHOT_WHEN_PRE_INSTALL, self.dataList())

    def has_snapshot(self, when):
        """ Is snapshot with this `when` parameter contained in the list of snapshots?

            :param when: `when` parameter from pykickstart which should be test for present.
            :type when: One of the constants from `pykickstart.constants.SNAPSHOT_*`
            :returns: True if snapshot with this `when` parameter is present,
                      False otherwise.
        """
        return any(snap.when == when for snap in self.dataList())

    def setup(self, storage, ksdata, instClass):
        """ Prepare post installation snapshots.

            This will also do the checking of snapshot validity.
        """
        for snap_data in self._post_snapshots():
            snap_data.setup(storage, ksdata, instClass)

    def execute(self, storage, ksdata, instClass):
        """ Create ThinLV snapshot after post section stops.

            Blivet must be reset before creation of the snapshot. This is
            required because the storage could be changed in post section.
        """
        post_snapshots = self._post_snapshots()

        if post_snapshots:
            try_populate_devicetree(storage.devicetree)
            for snap_data in post_snapshots:
                log.debug("Snapshot: creating post-install snapshot %s", snap_data.name)
                snap_data.execute(storage, ksdata, instClass)

    def pre_setup(self, storage, ksdata, instClass):
        """ Prepare pre installation snapshots.

            This will also do the checking of snapshot validity.
        """
        pre_snapshots = self._pre_snapshots()

        # wait for the storage to load devices
        if pre_snapshots:
            threadMgr.wait(THREAD_STORAGE)

        for snap_data in pre_snapshots:
            snap_data.setup(storage, ksdata, instClass)

    def pre_execute(self, storage, ksdata, instClass):
        """ Create ThinLV snapshot before installation starts.

            This must be done before user can change anything
        """
        pre_snapshots = self._pre_snapshots()

        if pre_snapshots:
            threadMgr.wait(THREAD_STORAGE)

            if (ksdata.clearpart.devices or ksdata.clearpart.drives or
                ksdata.clearpart.type == CLEARPART_TYPE_ALL):
                log.warning("Snapshot: \"clearpart\" command could erase pre-install snapshots!")
            if ksdata.zerombr.zerombr:
                log.warning("Snapshot: \"zerombr\" command could erase pre-install snapshots!")

            for snap_data in pre_snapshots:
                log.debug("Snapshot: creating pre-install snapshot %s", snap_data.name)
                snap_data.execute(storage, ksdata, instClass)

            try_populate_devicetree(storage.devicetree)

class SnapshotData(commands.snapshot.RHEL7_SnapshotData):
    def __init__(self, *args, **kwargs):
        commands.snapshot.RHEL7_SnapshotData.__init__(self, *args, **kwargs)
        self.thin_snapshot = None

    def setup(self, storage, ksdata, instClass):
        """ Add ThinLV snapshot to Blivet model but do not create it.

            This will plan snapshot creation on the end of the installation. This way
            Blivet will do a validity checking for future snapshot.
        """
        if not self.origin.count('/') == 1:
            raise KickstartError(
                        formatErrorMsg(self.lineno,
                                       msg=_("Incorrectly specified origin of the snapshot."
                                             " Use format \"VolGroup/LV_name\"")))
        # modify origin and snapshot name to the proper DM naming
        snap_name = self.name.replace('-', '--')
        origin = self.origin.replace('-', '--').replace('/', '-')
        origin_dev = storage.devicetree.getDeviceByName(origin)
        log.debug("Snapshot: name %s has origin %s", self.name, origin_dev)

        if not isinstance(origin_dev, LVMThinLogicalVolumeDevice):
            raise KickstartError(
                        formatErrorMsg(self.lineno,
                                       msg=(_("Snapshot: origin \"%(origin)s\" of snapshot \"%(name)s\""
                                              " is not a valid thin LV device.") %
                                            {"origin": self.origin,
                                             "name": self.name})))

        if storage.devicetree.getDeviceByName("%s-%s" % (origin_dev.vg.name, snap_name)):
            raise KickstartError(
                        formatErrorMsg(self.lineno,
                                       msg=(_("Snapshot %s already exists.") % self.name)))
        self.thin_snapshot = None
        try:
            self.thin_snapshot = LVMThinSnapShotDevice(name=self.name,
                                                       parents=[origin_dev.pool],
                                                       segType="thin",
                                                       origin=origin_dev)
        except ValueError as e:
            raise KickstartError(formatErrorMsg(self.lineno, msg=e))

    def execute(self, storage, ksdata, instClass):
        """ Execute an action for snapshot creation. """
        self.thin_snapshot.create()
        if isinstance(self.thin_snapshot.format, XFS):
            log.debug("Generating new UUID for XFS snapshot")
            self.thin_snapshot.format.regenerate_uuid()

class ZFCP(commands.zfcp.F14_ZFCP):
    def parse(self, args):
        fcp = commands.zfcp.F14_ZFCP.parse(self, args)
        try:
            blivet.zfcp.ZFCP().addFCP(fcp.devnum, fcp.wwpn, fcp.fcplun)
        except ValueError as e:
            log.warning(str(e))

        return fcp

class Keyboard(commands.keyboard.F18_Keyboard):
    def execute(self, *args):
        keyboard.write_keyboard_config(self, iutil.getSysroot())

class Upgrade(commands.upgrade.F20_Upgrade):
    # Upgrade is no longer supported. If an upgrade command was included in
    # a kickstart, warn the user and exit.
    def parse(self, *args):
        log.error("The upgrade kickstart command is no longer supported. Upgrade functionality is provided through fedup.")
        sys.stderr.write(_("The upgrade kickstart command is no longer supported. Upgrade functionality is provided through fedup."))
        iutil.ipmi_report(IPMI_ABORTED)
        sys.exit(1)

###
### %anaconda Section
###

class RHEL7_InstallClass(KickstartCommand):
    removedKeywords = KickstartCommand.removedKeywords
    removedAttrs = KickstartCommand.removedAttrs

    def __init__(self, *args, **kwargs):
        KickstartCommand.__init__(self, *args, **kwargs)
        self.op = self._getParser()
        self.name = kwargs.get("name", "")

    def __str__(self):
        retval = KickstartCommand.__str__(self)
        if not self.seen:
            return retval

        retval += "installclass%s\n" % self._getArgsAsStr()
        return retval

    def _getArgsAsStr(self):
        retval = ""
        if self.name:
            retval += ' --name="%s"' % self.name
        return retval

    def _getParser(self):
        op = KSOptionParser()
        op.add_option("--name", dest="name", required=True, type="string")
        return op

    def parse(self, args):
        (opts, _) = self.op.parse_args(args=args, lineno=self.lineno)
        self._setToSelf(self.op, opts)
        return self

class AnacondaSectionHandler(BaseHandler):
    """A handler for only the anaconda ection's commands."""
    commandMap = {
        "installclass": RHEL7_InstallClass,
        "pwpolicy": F22_PwPolicy
    }

    dataMap = {
        "PwPolicyData": F22_PwPolicyData
    }

    def __init__(self):
        BaseHandler.__init__(self, mapping=self.commandMap, dataMapping=self.dataMap)

    def __str__(self):
        """Return the %anaconda section"""
        retval = ""
        lst = sorted(self._writeOrder.keys())
        for prio in lst:
            for obj in self._writeOrder[prio]:
                retval += str(obj)

        if retval:
            retval = "\n%anaconda\n" + retval + "%end\n"
        return retval

class AnacondaSection(Section):
    """A section for anaconda specific commands."""
    sectionOpen = "%anaconda"

    def __init__(self, *args, **kwargs):
        Section.__init__(self, *args, **kwargs)
        self.cmdno = 0

    def handleLine(self, line):
        if not self.handler:
            return

        self.cmdno += 1
        args = shlex.split(line, comments=True)
        self.handler.currentCmd = args[0]
        self.handler.currentLine = self.cmdno
        return self.handler.dispatcher(args, self.cmdno)

    def handleHeader(self, lineno, args):
        """Process the arguments to the %anaconda header."""
        Section.handleHeader(self, lineno, args)

    def finalize(self):
        """Let %anaconda know no additional data will come."""
        Section.finalize(self)

###
### HANDLERS
###

# This is just the latest entry from pykickstart.handlers.control with all the
# classes we're overriding in place of the defaults.
commandMap = {
        "auth": Authconfig,
        "authconfig": Authconfig,
        "autopart": AutoPart,
        "btrfs": BTRFS,
        "bootloader": Bootloader,
        "clearpart": ClearPart,
        "dmraid": DmRaid,
        "eula": Eula,
        "fcoe": Fcoe,
        "firewall": Firewall,
        "firstboot": Firstboot,
        "group": Group,
        "ignoredisk": IgnoreDisk,
        "iscsi": Iscsi,
        "iscsiname": IscsiName,
        "keyboard": Keyboard,
        "lang": Lang,
        "logging": Logging,
        "logvol": LogVol,
        "mount": Mount,
        "multipath": MultiPath,
        "network": Network,
        "nvdimm": Nvdimm,
        "part": Partition,
        "partition": Partition,
        "raid": Raid,
        "realm": Realm,
        "reqpart": ReqPart,
        "rootpw": RootPw,
        "selinux": SELinux,
        "services": Services,
        "sshkey": SshKey,
        "skipx": SkipX,
        "snapshot": Snapshot,
        "timezone": Timezone,
        "upgrade": Upgrade,
        "user": User,
        "volgroup": VolGroup,
        "xconfig": XConfig,
        "zfcp": ZFCP,
}

dataMap = {
        "BTRFSData": BTRFSData,
        "LogVolData": LogVolData,
        "MountData": MountData,
        "PartData": PartitionData,
        "RaidData": RaidData,
        "RepoData": RepoData,
        "SnapshotData": SnapshotData,
        "VolGroupData": VolGroupData,
}

superclass = returnClassForVersion(RHEL7)

class AnacondaKSHandler(superclass):
    AddonClassType = AddonData

    def __init__ (self, addon_paths=None, commandUpdates=None, dataUpdates=None):
        if addon_paths is None:
            addon_paths = []

        if commandUpdates is None:
            commandUpdates = commandMap

        if dataUpdates is None:
            dataUpdates = dataMap

        superclass.__init__(self, commandUpdates=commandUpdates, dataUpdates=dataUpdates)
        self.onPart = {}

        # collect all kickstart addons for anaconda to addons dictionary
        # which maps addon_id to it's own data structure based on BaseData
        # with execute method
        addons = {}

        # collect all AddonData subclasses from
        # for p in addon_paths: <p>/<plugin id>/ks/*.(py|so)
        # and register them under <plugin id> name
        for module_name, path in addon_paths:
            addon_id = os.path.basename(os.path.dirname(os.path.abspath(path)))
            if not os.path.isdir(path):
                continue

            classes = collect(module_name, path, lambda cls: issubclass(cls, self.AddonClassType))
            if classes:
                addons[addon_id] = classes[0](name = addon_id)

        # Prepare the final structures for 3rd party addons
        self.addons = AddonRegistry(addons)

        # The %anaconda section uses its own handler for a limited set of commands
        self.anaconda = AnacondaSectionHandler()

    def __str__(self):
        return superclass.__str__(self) + "\n" + str(self.addons) + str(self.anaconda)

class AnacondaPreParser(KickstartParser):
    # A subclass of KickstartParser that only looks for %pre scripts and
    # sets them up to be run.  All other scripts and commands are ignored.
    def __init__ (self, handler, followIncludes=True, errorsAreFatal=True,
                  missingIncludeIsFatal=True):
        KickstartParser.__init__(self, handler, missingIncludeIsFatal=False)

    def handleCommand (self, lineno, args):
        pass

    def setupSections(self):
        self.registerSection(PreScriptSection(self.handler, dataObj=AnacondaKSScript))
        self.registerSection(NullSection(self.handler, sectionOpen="%pre-install"))
        self.registerSection(NullSection(self.handler, sectionOpen="%post"))
        self.registerSection(NullSection(self.handler, sectionOpen="%onerror"))
        self.registerSection(NullSection(self.handler, sectionOpen="%traceback"))
        self.registerSection(NullSection(self.handler, sectionOpen="%packages"))
        self.registerSection(NullSection(self.handler, sectionOpen="%addon"))
        self.registerSection(NullSection(self.handler.anaconda, sectionOpen="%anaconda"))


class AnacondaKSParser(KickstartParser):
    def __init__ (self, handler, followIncludes=True, errorsAreFatal=True,
                  missingIncludeIsFatal=True, scriptClass=AnacondaKSScript):
        self.scriptClass = scriptClass
        KickstartParser.__init__(self, handler)

    def handleCommand (self, lineno, args):
        if not self.handler:
            return

        return KickstartParser.handleCommand(self, lineno, args)

    def setupSections(self):
        self.registerSection(PreScriptSection(self.handler, dataObj=self.scriptClass))
        self.registerSection(PreInstallScriptSection(self.handler, dataObj=self.scriptClass))
        self.registerSection(PostScriptSection(self.handler, dataObj=self.scriptClass))
        self.registerSection(TracebackScriptSection(self.handler, dataObj=self.scriptClass))
        self.registerSection(OnErrorScriptSection(self.handler, dataObj=self.scriptClass))
        self.registerSection(PackageSection(self.handler))
        self.registerSection(AddonSection(self.handler))
        self.registerSection(AnacondaSection(self.handler.anaconda))

def preScriptPass(f):
    # The first pass through kickstart file processing - look for %pre scripts
    # and run them.  This must come in a separate pass in case a script
    # generates an included file that has commands for later.
    ksparser = AnacondaPreParser(AnacondaKSHandler())

    with check_kickstart_error():
        ksparser.readKickstart(f)

    # run %pre scripts
    runPreScripts(ksparser.handler.scripts)

def parseKickstart(f):
    # preprocessing the kickstart file has already been handled in initramfs.

    addon_paths = collect_addon_paths(ADDON_PATHS)
    handler = AnacondaKSHandler(addon_paths["ks"])
    ksparser = AnacondaKSParser(handler)

    # We need this so all the /dev/disk/* stuff is set up before parsing.
    udev.trigger(subsystem="block", action="change")
    # So that drives onlined by these can be used in the ks file
    blivet.iscsi.iscsi().startup()
    blivet.fcoe.fcoe().startup()
    blivet.zfcp.ZFCP().startup()
    # Note we do NOT call dasd.startup() here, that does not online drives, but
    # only checks if they need formatting, which requires zerombr to be known

    with check_kickstart_error():
        ksparser.readKickstart(f)

    return handler

def appendPostScripts(ksdata):
    scripts = ""

    # Read in all the post script snippets to a single big string.
    for fn in glob.glob("/usr/share/anaconda/post-scripts/*ks"):
        f = open(fn, "r")
        scripts += f.read()
        f.close()

    # Then parse the snippets against the existing ksdata.  We can do this
    # because pykickstart allows multiple parses to save their data into a
    # single data object.  Errors parsing the scripts are a bug in anaconda,
    # so just raise an exception.
    ksparser = AnacondaKSParser(ksdata, scriptClass=AnacondaInternalScript)
    ksparser.readKickstartFromString(scripts, reset=False)

def runPostScripts(scripts):
    postScripts = filter (lambda s: s.type == KS_SCRIPT_POST, scripts)

    if len(postScripts) == 0:
        return

    log.info("Running kickstart %%post script(s)")
    map (lambda s: s.run(iutil.getSysroot()), postScripts)
    log.info("All kickstart %%post script(s) have been run")

def runPreScripts(scripts):
    preScripts = filter (lambda s: s.type == KS_SCRIPT_PRE, scripts)

    if len(preScripts) == 0:
        return

    log.info("Running kickstart %%pre script(s)")
    stdoutLog.info(_("Running pre-installation scripts"))

    map (lambda s: s.run("/"), preScripts)

    log.info("All kickstart %%pre script(s) have been run")

def runPreInstallScripts(scripts):
    preInstallScripts = [s for s in scripts if s.type == KS_SCRIPT_PREINSTALL]

    if len(preInstallScripts) == 0:
        return

    log.info("Running kickstart %%pre-install script(s)")

    for script in preInstallScripts:
        script.run("/")

    log.info("All kickstart %%pre-install script(s) have been run")

def runTracebackScripts(scripts):
    log.info("Running kickstart %%traceback script(s)")
    for script in filter (lambda s: s.type == KS_SCRIPT_TRACEBACK, scripts):
        script.run("/")
    log.info("All kickstart %%traceback script(s) have been run")

def resetCustomStorageData(ksdata):
    cmds = ["partition", "raid", "volgroup", "logvol", "btrfs"]
    map(ksdata.resetCommand, cmds)

    ksdata.clearpart.type = CLEARPART_TYPE_NONE

def doKickstartStorage(storage, ksdata, instClass):
    """ Setup storage state from the kickstart data """
    ksdata.clearpart.execute(storage, ksdata, instClass)
    if not any(d for d in storage.disks
               if not d.format.hidden and not d.protected):
        return

    # snapshot free space now so that we know how much we had available
    storage.createFreeSpaceSnapshot()

    ksdata.bootloader.execute(storage, ksdata, instClass, dry_run=True)
    ksdata.autopart.execute(storage, ksdata, instClass)
    ksdata.reqpart.execute(storage, ksdata, instClass)
    ksdata.partition.execute(storage, ksdata, instClass)
    ksdata.raid.execute(storage, ksdata, instClass)
    ksdata.volgroup.execute(storage, ksdata, instClass)
    ksdata.logvol.execute(storage, ksdata, instClass)
    ksdata.btrfs.execute(storage, ksdata, instClass)
    ksdata.mount.execute(storage, ksdata, instClass)
    # setup snapshot here, that means add it to model and do the tests
    # snapshot will be created on the end of the installation
    ksdata.snapshot.setup(storage, ksdata, instClass)
    # also calls ksdata.bootloader.execute
    storage.setUpBootLoader()

