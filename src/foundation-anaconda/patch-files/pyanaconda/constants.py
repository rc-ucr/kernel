#
# constants.py: anaconda constants
#
# Copyright (C) 2001  Red Hat, Inc.  All rights reserved.
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
# Author(s): Erik Troan <ewt@redhat.com>
#

import string
from pyanaconda.i18n import N_

# Use -1 to indicate that the selinux configuration is unset
SELINUX_DEFAULT = -1

# where to look for 3rd party addons
ADDON_PATHS = ["/opt/rocks/share/anaconda/addons"]

from pykickstart.constants import AUTOPART_TYPE_LVM

# common string needs to be easy to change
from pyanaconda import product
productName = product.productName
productVersion = product.productVersion
productArch = product.productArch
bugzillaUrl = product.bugUrl
isFinal = product.isFinal
eulaLocation = "/usr/share/rocky-release/EULA"

# for use in device names, eg: "fedora", "rhel"
shortProductName = productName.lower()
if productName.count(" "):
    shortProductName = ''.join(s[0] for s in shortProductName.split())

# DriverDisc Paths
DD_ALL = "/tmp/DD"
DD_FIRMWARE = "/tmp/DD/lib/firmware"
DD_RPMS = "/tmp/DD-*"

TRANSLATIONS_UPDATE_DIR="/tmp/updates/po"

ANACONDA_CLEANUP = "anaconda-cleanup"
MOUNT_DIR = "/mnt/install"
DRACUT_REPODIR = "/run/install/repo"
DRACUT_ISODIR = "/run/install/source"
ISO_DIR = MOUNT_DIR + "/isodir"
IMAGE_DIR = MOUNT_DIR + "/image"
INSTALL_TREE = MOUNT_DIR + "/source"
BASE_REPO_NAME = "anaconda"

# NOTE: this should be LANG_TERRITORY.CODESET, e.g. en_US.UTF-8
DEFAULT_LANG = "en_US.UTF-8"

DEFAULT_VC_FONT = "latarcyrheb-sun16"

DEFAULT_KEYBOARD = "us"

DRACUT_SHUTDOWN_EJECT = "/run/initramfs/usr/lib/dracut/hooks/shutdown/99anaconda-eject.sh"

# VNC questions
USEVNC = N_("Start VNC")
USETEXT = N_("Use text mode")

# Runlevel files
RUNLEVELS = {3: 'multi-user.target', 5: 'graphical.target'}

# Network
NETWORK_CONNECTION_TIMEOUT = 45  # in seconds
NETWORK_CONNECTED_CHECK_INTERVAL = 0.1  # in seconds

# DBus
DEFAULT_DBUS_TIMEOUT = -1       # use default

# Thread names
THREAD_EXECUTE_STORAGE = "AnaExecuteStorageThread"
THREAD_STORAGE = "AnaStorageThread"
THREAD_STORAGE_WATCHER = "AnaStorageWatcher"
THREAD_CHECK_STORAGE = "AnaCheckStorageThread"
THREAD_CUSTOM_STORAGE_INIT = "AnaCustomStorageInit"
THREAD_WAIT_FOR_CONNECTING_NM = "AnaWaitForConnectingNMThread"
THREAD_PAYLOAD = "AnaPayloadThread"
THREAD_PAYLOAD_RESTART = "AnaPayloadRestartThread"
THREAD_INPUT_BASENAME = "AnaInputThread"
THREAD_SYNC_TIME_BASENAME = "AnaSyncTime"
THREAD_EXCEPTION_HANDLING_TEST = "AnaExceptionHandlingTest"
THREAD_LIVE_PROGRESS = "AnaLiveProgressThread"
THREAD_SOFTWARE_WATCHER = "AnaSoftwareWatcher"
THREAD_CHECK_SOFTWARE = "AnaCheckSoftwareThread"
THREAD_SOURCE_WATCHER = "AnaSourceWatcher"
THREAD_INSTALL = "AnaInstallThread"
THREAD_CONFIGURATION = "AnaConfigurationThread"
THREAD_FCOE = "AnaFCOEThread"
THREAD_ISCSI_DISCOVER = "AnaIscsiDiscoverThread"
THREAD_ISCSI_LOGIN = "AnaIscsiLoginThread"
THREAD_GEOLOCATION_REFRESH = "AnaGeolocationRefreshThread"
THREAD_DATE_TIME = "AnaDateTimeThread"
THREAD_TIME_INIT = "AnaTimeInitThread"
THREAD_DASDFMT = "AnaDasdfmtThread"
THREAD_KEYBOARD_INIT = "AnaKeyboardThread"
THREAD_ADD_LAYOUTS_INIT = "AnaAddLayoutsInitThread"
THREAD_NTP_SERVER_CHECK = "AnaNTPserver"
THREAD_NVDIMM_RECONFIGURE = "AnaNVDIMMReconfigureThread"
THREAD_NVDIMM_REPOPULATE = "AnaNVDIMMRepopulateThread"

# Geolocation constants

# geolocation providers
# - values are used by the geoloc CLI/boot option
GEOLOC_PROVIDER_FEDORA_GEOIP = "provider_fedora_geoip"
GEOLOC_PROVIDER_HOSTIP = "provider_hostip"
GEOLOC_PROVIDER_GOOGLE_WIFI = "provider_google_wifi"
# geocoding provider
GEOLOC_GEOCODER_NOMINATIM = "geocoder_nominatim"
# default providers
GEOLOC_DEFAULT_PROVIDER = GEOLOC_PROVIDER_FEDORA_GEOIP
GEOLOC_DEFAULT_GEOCODER = GEOLOC_GEOCODER_NOMINATIM
# timeout (in seconds)
GEOLOC_TIMEOUT = 3


ANACONDA_ENVIRON = "anaconda"
FIRSTBOOT_ENVIRON = "firstboot"

# Tainted hardware
UNSUPPORTED_HW = 1 << 28

# SMT warnings
WARNING_SMT_ENABLED_GUI = N_(
    "Simultaneous Multithreading (SMT) technology can provide performance improvements for "
    "certain workloads, but introduces several publicly disclosed security issues. You have "
    "the option of disabling SMT, which may impact performance. If you choose to leave SMT "
    "enabled, please read https://red.ht/rhel-smt to understand your potential risks and learn "
    "about other ways to mitigate these risks."
)

# This message has to be shorter for serial console.
WARNING_SMT_ENABLED_TUI = N_(
    "Simultaneous Multithreading (SMT) may improve performance for certain workloads, but "
    "introduces several publicly disclosed security issues. You can disable SMT, which may "
    "impact performance. Please read https://red.ht/rhel-smt to understand potential risks "
    "and learn about ways to mitigate these risks."
)

# Password validation
PASSWORD_MIN_LEN = 6
PASSWORD_EMPTY_ERROR = N_("The %(password)s is empty.")  # singular
PASSWORD_CONFIRM_ERROR_GUI = N_("The %(passwords)s do not match.")  # plural
PASSWORD_CONFIRM_ERROR_TUI = N_("The %(passwords)s you entered were different.  Please try again.")  # plural
# The password-too-short constant is used to replace a libpwquality error message,
# which is why it does not end with a ".", like all the other do.
PASSWORD_TOO_SHORT = N_("The %(password)s is too short")  # singular
PASSWORD_WEAK = N_("The %(password)s you have provided is weak.")  # singular
PASSWORD_WEAK_WITH_ERROR = N_("The %(password)s you have provided is weak: %(error_message)s.")  # singular
PASSWORD_FINAL_CONFIRM = N_("Press Done again to use anyway.")
PASSWORD_ASCII = N_("The %(password)s you have provided contains non-ASCII characters. You may not be able to switch between keyboard layouts when typing it.")
# ^ singular
PASSWORD_DONE_TWICE = N_("You will have to press Done twice to confirm it.")
PASSWORD_DONE_TO_CONTINUE = N_("Press Done to continue.")

# password status
PASSWORD_STATUS_EMPTY = N_("Empty")
PASSWORD_STATUS_TOO_SHORT = N_("Too short")
PASSWORD_STATUS_WEAK = N_("Weak")
PASSWORD_STATUS_FAIR = N_("Fair")
PASSWORD_STATUS_GOOD = N_("Good")
PASSWORD_STATUS_STRONG = N_("Strong")

# how should passwords be called in combined strings
NAME_OF_PASSWORD = N_("password")
NAME_OF_PASSWORD_PLURAL = N_("passwords")

# how should passphrases be called in combined strings
NAME_OF_PASSPHRASE = N_("passphrase")
NAME_OF_PASSPHRASE_PLURAL = N_("passphrases")

# the number of seconds we consider a noticeable freeze of the UI
NOTICEABLE_FREEZE = 0.1

# all ASCII characters
PW_ASCII_CHARS = string.digits + string.ascii_letters + string.punctuation + " "
SALT_CHARS = string.ascii_letters + string.digits + './'

# Recognizing a tarfile
TAR_SUFFIX = (".tar", ".tbz", ".tgz", ".txz", ".tar.bz2", "tar.gz", "tar.xz")

# screenshots
SCREENSHOTS_DIRECTORY = "/tmp/anaconda-screenshots"
SCREENSHOTS_TARGET_DIRECTORY = "/root/anaconda-screenshots"

# cmdline arguments that append instead of overwrite
CMDLINE_APPEND = ["modprobe.blacklist", "ifname", "ip"]

DEFAULT_AUTOPART_TYPE = AUTOPART_TYPE_LVM

import logging
LOGLVL_LOCK = logging.DEBUG-1

# for how long (in seconds) we try to wait for enough entropy for LUKS
# keep this a multiple of 60 (minutes)
MAX_ENTROPY_WAIT = 10 * 60

# Constants for reporting status to IPMI.  These are from the IPMI spec v2 rev1.1, page 512.
IPMI_STARTED  = 0x7         # installation started
IPMI_FINISHED = 0x8         # installation finished successfully
IPMI_ABORTED  = 0x9         # installation finished unsuccessfully, due to some non-exn error
IPMI_FAILED   = 0xA         # installation hit an exception

# X display number to use
X_DISPLAY_NUMBER = 1

# NTP server checking
NTP_SERVER_OK = 0
NTP_SERVER_NOK = 1
NTP_SERVER_QUERY = 2

# Timeout for starting X
X_TIMEOUT = 60
