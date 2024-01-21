NAME		= foundation-gtk+
PKGROOT		= /opt/rocks
VERSION		= 3.22.30
RELEASE		= 0
SUBDIR		= gtk+-$(VERSION)
TARFILE		= $(SUBDIR).tar.xz
RPM.REQUIRES	= foundation-atk,foundation-at-spi2-atk,foundation-glib2
RPM.FILES	= "$(PKGROOT)/bin/*\\n$(PKGROOT)/etc/*\\n$(PKGROOT)/include/*\\n$(PKGROOT)/lib/*\\n$(PKGROOT)/share/*"
