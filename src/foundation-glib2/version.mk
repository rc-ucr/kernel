NAME		= foundation-glib2
PKGROOT		= /opt/rocks
VERSION		= 2.56.4
RELEASE		= 0
SUBDIR		= glib-$(VERSION)
TARFILE		= $(SUBDIR).tar.xz
RPM.REQUIRES	= 
RPM.FILES	= "$(PKGROOT)/bin/*\\n$(PKGROOT)/include/*\\n$(PKGROOT)/lib/*\\n$(PKGROOT)/share/*"
