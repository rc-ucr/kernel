#
# installclass.py:  This is the prototypical class for workstation, server, and
# kickstart installs.  The interface to BaseInstallClass is *public* --
# ISVs/OEMs can customize the install by creating a new derived type of this
# class.
#
# Copyright (C) 1999, 2000, 2001, 2002, 2003, 2004, 2005, 2006, 2007
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

from distutils.sysconfig import get_python_lib
import os
import sys

from blivet.partspec import PartSpec
from blivet.autopart import swapSuggestion
from blivet.platform import platform
from blivet.size import Size

from pyanaconda.kickstart import getAvailableDiskSpace
from pyanaconda.constants import DEFAULT_AUTOPART_TYPE
from pyanaconda.ui.common import collect

import logging
log = logging.getLogger("anaconda")


class BaseInstallClass(object):
    # default to not being hidden
    sortPriority = 0
    hidden = False
    name = "base"
    bootloaderTimeoutDefault = None
    bootloaderExtraArgs = []

    # Anaconda flags several packages to be installed based on the configuration
    # of the system -- things like fs utilities, bootloader, &c. This is a list
    # of packages that we should not try to install using the aforementioned
    # mechanism.
    ignoredPackages = []

    # This flag controls whether or not Anaconda should provide an option to
    # install the latest updates during installation source selection.
    installUpdates = True

    _l10n_domain = None

    # The default filesystem type to use.  If None, we will use whatever
    # Blivet uses by default.
    defaultFS = None

    # The default partitioning scheme to use for autopartitioning.
    # It has to be always set.
    default_autopart_type = DEFAULT_AUTOPART_TYPE

    # help
    help_folder = "/usr/share/anaconda/help/rhel"
    help_main_page = "Installation_Guide.xml"
    help_placeholder = None
    help_placeholder_with_links = None

    # geolocation
    use_geolocation_with_kickstart = False

    @property
    def l10n_domain(self):
        if self._l10n_domain is None:
            raise RuntimeError("Localization domain for '%s' not set." %
                               self.name)
        return self._l10n_domain

    def setPackageSelection(self, anaconda):
        pass

    def getBackend(self):
        # The default is to return None here, which means anaconda should
        # use live or yum (whichever can be detected).  This method is
        # provided as a way for other products to specify their own.
        return None

    def setDefaultPartitioning(self, storage):
        autorequests = [PartSpec(mountpoint="/", fstype=storage.defaultFSType,
                                 size=Size("1GiB"),
                                 maxSize=Size("50GiB"),
                                 grow=True,
                                 btr=True, lv=True, thin=True, encrypted=True),
                        PartSpec(mountpoint="/home",
                                 fstype=storage.defaultFSType,
                                 size=Size("500MiB"), grow=True,
                                 requiredSpace=Size("50GiB"),
                                 btr=True, lv=True, thin=True, encrypted=True)]

        bootreqs = platform.setDefaultPartitioning()
        if bootreqs:
            autorequests.extend(bootreqs)


        disk_space = getAvailableDiskSpace(storage)
        swp = swapSuggestion(disk_space=disk_space)
        autorequests.append(PartSpec(fstype="swap", size=swp, grow=False,
                                     lv=True, encrypted=True))

        for autoreq in autorequests:
            if autoreq.fstype is None:
                if autoreq.mountpoint == "/boot":
                    autoreq.fstype = storage.defaultBootFSType
                else:
                    autoreq.fstype = storage.defaultFSType

        storage.autoPartitionRequests = autorequests

    def customizeDefaultPartitioning(self, storage, data):
        # Customize the default partitioning with kickstart data.
        skipped_mountpoints = set()

        # Create a set of mountpoints to remove from autorequests.
        # Add /home to the set if --nohome is selected.
        if data.autopart.autopart and data.autopart.nohome:
            skipped_mountpoints.add("/home")

        # Skip mountpoints we want to remove.
        storage.autoPartitionRequests = [req for req in storage.autoPartitionRequests
                                         if req.mountpoint not in skipped_mountpoints]

    def configure(self, anaconda):
        anaconda.bootloader.timeout = self.bootloaderTimeoutDefault
        anaconda.bootloader.boot_args.update(self.bootloaderExtraArgs)

        # The default partitioning should be always set.
        self.setDefaultPartitioning(anaconda.storage)

        # Customize the default partitioning with kickstart data.
        self.customizeDefaultPartitioning(anaconda.storage, anaconda.ksdata)

    def setStorageChecker(self, storage_checker):
        # Update constraints and add or remove some checks in
        # the storage checker to customize the storage sanity
        # checking.
        pass

    # sets default ONBOOT values and updates ksdata accordingly
    def setNetworkOnbootDefault(self, ksdata):
        pass

    def filterSupportedLangs(self, ksdata, langs):
        return langs

    def filterSupportedLocales(self, ksdata, lang, locales):
        return locales

    def __init__(self):
        pass


class InstallClassFactory(object):
    """Class used to get an install class instance."""

    def __init__(self):
        self._classes = []
        self._visible_classes = []
        self._paths = []

    def get_install_class_by_name(self, name):
        """Return an instance of an install class with a requested name."""
        for install_class in self.classes:
            if install_class.name == name:
                log.info("Using the requested install class %s.",
                         self._get_class_description(install_class))

                return install_class()

        raise RuntimeError("Unable to find the install class %s.", name)

    def get_best_install_class(self):
        """Return the instance of the best found install class.

        We choose an install class with the highest priority. The priority
        of an install class is based on its visibility (we prefer visible),
        its sort priority (we prefer higher number) and its name (in reverse
        alphabetical order, so Fedora Workstation is preferred before Fedora).
        """
        if self.visible_classes:
            install_class = self.visible_classes[0]
            log.info("Using a visible install class %s.",
                     self._get_class_description(install_class))

        elif self.classes:
            install_class = self.classes[0]
            log.info("Using a hidden install class %s.",
                     self._get_class_description(install_class))

        else:
            raise RuntimeError("Unable to find an install class to use.")

        return install_class()

    @property
    def paths(self):
        """Return paths where to look for install classes."""
        if not self._paths:
            self._paths = self._get_install_class_paths()

        return self._paths

    @property
    def classes(self):
        """Return all available install classes."""
        if not self._classes:
            self._classes = self._get_available_classes(self.paths)

        return self._classes

    @property
    def visible_classes(self):
        """Return only install classes that are not hidden."""
        if not self._visible_classes:
            self._visible_classes = list(filter(self._is_visible_class, self.classes))

        return self._visible_classes

    @staticmethod
    def _is_visible_class(obj):
        """Is the class visible?"""
        return not obj.hidden

    @staticmethod
    def _is_install_class(obj):
        """Is the class the install class?"""
        return issubclass(obj, BaseInstallClass) and obj != BaseInstallClass

    @staticmethod
    def _get_install_class_key(obj):
        """Return the install class key for sorting."""
        return obj.sortPriority, obj.name

    @staticmethod
    def _get_class_description(install_class):
        """Return the description of the install class."""
        return "%s (%s)" % (install_class.name, install_class.__name__)

    def _get_install_class_paths(self):
        """Return a list of paths to directories with install classes."""
        path = []

        if "ANACONDA_INSTALL_CLASSES" in os.environ:
            path += os.environ["ANACONDA_INSTALL_CLASSES"].split(":")

        path += [
            "installclasses",
            "/tmp/updates/pyanaconda/installclasses",
            "/tmp/product/pyanaconda/installclasses",
            "%s/pyanaconda/installclasses" % get_python_lib(plat_specific=1)
        ]

        return list(filter(lambda d: os.access(d, os.R_OK), path))

    def _get_available_classes(self, paths):
        """Return a list of available install classes."""
        # Append the location of install classes to the python path
        # so install classes can import and inherit correct classes.
        sys.path = paths + sys.path

        classes = set()
        for path in paths:
            log.debug("Searching %s.", path)

            for install_class in collect("%s", path, self._is_install_class):
                log.debug("Found %s.", self._get_class_description(install_class))
                classes.add(install_class)

        # Classes are sorted by their priority and name in the reversed order,
        # so classes with the highest priority and longer name are preferred.
        # For example, Fedora Workstation doesn't have higher priority.
        return sorted(classes, key=self._get_install_class_key, reverse=True)

factory = InstallClassFactory()
