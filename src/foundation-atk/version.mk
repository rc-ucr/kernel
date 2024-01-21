NAME		= foundation-atk
PKGROOT		= /opt/rocks
VERSION		= 2.28.1
RELEASE		= 0
SUBDIR		= atk-$(VERSION)
TARFILE		= $(SUBDIR).tar.xz
RPM.FILES	= "$(PKGROOT)/include/*\\n$(PKGROOT)/lib/*\\n$(PKGROOT)/share/*"
